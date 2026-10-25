"""
Telegram handlers: commands, message flow, typing indicator, and reply sending.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from telegram import Bot, Message, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ai import AIError, OpenRouterClient
from config import Settings
from database import Database
from memory import (
    MemoryManager,
    detect_language_request,
    detect_nickname,
    is_valid_nickname,
)
from utils import (
    TELEGRAM_MESSAGE_LIMIT,
    RateLimiter,
    clean_reply,
    human_delay,
    is_group_chat,
    sanitize_user_text,
    split_message,
)

logger = logging.getLogger(__name__)

FALLBACK_REPLY = "Oops, mera dimaag thoda hang ho gaya 😵‍💫 Ek second baad phir try karo?"
VALID_LANGUAGES = {"auto", "hi", "hinglish", "en"}


@dataclass
class Services:
    """Shared objects passed to every handler through bot_data."""

    settings: Settings
    db: Database
    memory: MemoryManager
    ai: OpenRouterClient
    limiter: RateLimiter
    # One lock per chat so replies in the same chat never interleave
    chat_locks: dict[int, asyncio.Lock] = field(default_factory=dict)

    def lock_for(self, chat_id: int) -> asyncio.Lock:
        """Return the lock for a chat, creating it on first use."""
        return self.chat_locks.setdefault(chat_id, asyncio.Lock())


def _svc(context: ContextTypes.DEFAULT_TYPE) -> Services:
    """Fetch the shared services object."""
    return context.application.bot_data["services"]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
@asynccontextmanager
async def typing_indicator(bot: Bot, chat_id: int) -> AsyncIterator[None]:
    """
    Keep Telegram's 'typing...' status visible while the block runs.

    Telegram clears the status after about 5 seconds, so we refresh it
    every 4 seconds until the block finishes.
    """

    async def _loop() -> None:
        try:
            while True:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except TelegramError as exc:
            logger.debug("Typing action failed: %s", exc)

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def send_in_parts(message: Message, bot: Bot, chat_id: int, text: str) -> None:
    """Send a reply, split into several messages if it is too long."""
    parts = split_message(text, TELEGRAM_MESSAGE_LIMIT)
    for index, part in enumerate(parts):
        try:
            if index:
                # Show typing again between chunks to feel natural
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(random.uniform(0.6, 1.4))
            await message.reply_text(part)
        except TelegramError as exc:
            logger.error("Failed to send reply part %d: %s", index, exc)
            break


def _bot_addressed(message: Message, bot: Bot, text: str) -> tuple[bool, str]:
    """
    Decide whether Maya should answer a group message.

    She answers when she is:
      - mentioned as @username
      - replied to
      - called by name at the start of the message ("Maya ...")

    Returns (addressed, text_with_the_call_removed).
    """
    addressed = False
    username = bot.username or ""

    if username:
        mention = re.compile(rf"@{re.escape(username)}\b", re.IGNORECASE)
        if mention.search(text):
            addressed = True
            text = mention.sub("", text)

    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == bot.id:
        addressed = True

    if re.match(r"^\s*maya\b", text, re.IGNORECASE):
        addressed = True
        text = re.sub(r"^\s*maya\b[,:!]?\s*", "", text, count=1, flags=re.IGNORECASE)

    return addressed, text.strip()


async def _learn_from_message(svc: Services, user_id: int, text: str) -> None:
    """Save nickname or language preferences the user states in chat."""
    nickname = detect_nickname(text)
    if nickname:
        await svc.db.set_preference(user_id, "nickname", nickname)
        logger.info("Stored nickname for user %s", user_id)

    language = detect_language_request(text)
    if language:
        await svc.db.set_preference(user_id, "language", language)
        logger.info("Stored language preference for user %s: %s", user_id, language)


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start: greet the user and explain what Maya is."""
    svc = _svc(context)
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return

    await svc.db.upsert_user(user.id, user.username, user.first_name, count=0)
    name = user.first_name or "yaar"
    await message.reply_text(
        f"Hiii {name}! 👋 Main Maya hoon, ek fictional virtual character.\n"
        "Masti, gossip, thodi baatein, aur kabhi kabhi life advice bhi 😌\n\n"
        "Note: main real insaan nahi hoon. Bas ek virtual dost jo entertainment ke liye bana hai.\n\n"
        "/help se commands dekho, ya bas kuch bhi likh do!"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help: list available commands."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "Mere commands:\n"
        "/start - intro\n"
        "/help - ye list\n"
        "/reset - is chat ki memory clear karo\n"
        "/stats - tumhare stats (aur admin ko global stats)\n"
        "/ping - latency check\n"
        "/nickname <naam> - mujhe kya bulaoge?\n"
        "/lang <hi|hinglish|en|auto> - reply ki language set karo\n\n"
        "Group mein mujhe @mention karo, mere message pe reply karo, ya 'Maya' se shuru karo."
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reset: wipe conversation memory for the current chat."""
    svc = _svc(context)
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return
    removed = await svc.memory.forget_chat(chat.id)
    logger.info("Reset chat %s (%d messages removed)", chat.id, removed)
    await message.reply_text("Done! Memory saaf. Naye sire se shuru karte hain 🧹")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats: show personal stats, plus global stats for admins."""
    svc = _svc(context)
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if user is None or chat is None or message is None:
        return

    my_messages = await svc.db.user_message_count(user.id)
    chat_rows = await svc.db.chat_history_count(chat.id)
    prefs = await svc.db.get_preferences(user.id) or {}

    lines = [
        "📊 Tumhare stats",
        f"Mujhe bheje messages: {my_messages}",
        f"Is chat ki memory: {chat_rows}/{svc.settings.max_history_messages}",
        f"Nickname: {prefs.get('nickname') or 'not set'}",
        f"Language: {prefs.get('language') or 'auto'}",
    ]

    if user.id in svc.settings.admin_ids:
        g = await svc.db.global_stats()
        lines += [
            "",
            "🔐 Global (admin)",
            f"Users: {g['users']}",
            f"Chats: {g['chats']}",
            f"Stored messages: {g['stored_messages']}",
            f"Profiles: {g['profiles']}",
        ]

    await message.reply_text("\n".join(lines))


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ping: measure round-trip time to Telegram."""
    message = update.effective_message
    if message is None:
        return
    start = time.perf_counter()
    await message.reply_text("🏓 Pong!")
    elapsed_ms = (time.perf_counter() - start) * 1000
    await message.reply_text(f"Latency: {elapsed_ms:.0f} ms")


async def nickname_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/nickname <name>: set what Maya calls you."""
    svc = _svc(context)
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return

    name = " ".join(context.args or []).strip()
    if not is_valid_nickname(name):
        await message.reply_text("Naam sirf letters mein, 2 se 20 characters tak. Example: /nickname Raj")
        return

    await svc.db.set_preference(user.id, "nickname", name.capitalize())
    await message.reply_text(f"Done! Ab se main tumhe {name.capitalize()} bulaungi 💖")


async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/lang <hi|hinglish|en|auto>: set preferred reply language."""
    svc = _svc(context)
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return

    choice = (context.args[0].lower() if context.args else "")
    if choice not in VALID_LANGUAGES:
        await message.reply_text("Use: /lang hi | hinglish | en | auto")
        return

    await svc.db.set_preference(user.id, "language", choice)
    await message.reply_text(f"Language set to {choice} ✅")


# ----------------------------------------------------------------------
# Main message handler
# ----------------------------------------------------------------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle every plain text message."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None or user.is_bot or not message.text:
        return

    svc = _svc(context)
    text = sanitize_user_text(message.text, svc.settings.max_input_chars)
    if not text:
        return

    group = is_group_chat(chat.type)
    addressed = True
    if group:
        addressed, text = _bot_addressed(message, context.bot, text)

    display_name = user.first_name or user.username or "friend"
    await svc.db.upsert_user(user.id, user.username, user.first_name, count=1)

    # In groups, store "Name: text" so Maya knows who said what
    stored = f"{display_name}: {text}" if group else text

    # Group chatter she was not called into: remember it, do not answer
    if not addressed:
        if text:
            await svc.memory.record(chat.id, user.id, "user", stored)
        return

    if not text:
        await message.reply_text("Haan? 👀 Bolo, kya chal raha hai?")
        return

    if not svc.limiter.allow(user.id):
        await message.reply_text("Arre, thoda slow! 😅 Ek minute baad phir baat karte hain.")
        return

    await _learn_from_message(svc, user.id, text)
    prefs = await svc.db.get_preferences(user.id) or {}
    await svc.memory.record(chat.id, user.id, "user", stored)

    # Lock the chat so two replies never overlap
    async with svc.lock_for(chat.id):
        async with typing_indicator(context.bot, chat.id):
            try:
                messages = await svc.memory.build_prompt(
                    chat.id,
                    nickname=prefs.get("nickname"),
                    language_pref=prefs.get("language") or "auto",
                    text=text,
                    is_group=group,
                )
                raw_reply = await svc.ai.chat(messages)
            except AIError as exc:
                logger.error("AI failure in chat %s: %s", chat.id, exc)
                raw_reply = ""

            reply = clean_reply(raw_reply)
            if not reply:
                await message.reply_text(FALLBACK_REPLY)
                return

            # Realistic pause while "typing", then send
            await asyncio.sleep(
                human_delay(len(reply), svc.settings.typing_min_delay, svc.settings.typing_max_delay)
            )

        await send_in_parts(message, context.bot, chat.id, reply)
        await svc.memory.record(chat.id, None, "assistant", reply)


# ----------------------------------------------------------------------
# Error handler
# ----------------------------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log every unhandled exception and tell the user something went wrong."""
    logger.error("Unhandled exception while handling an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        with contextlib.suppress(TelegramError):
            await update.effective_message.reply_text(
                "Kuch gadbad ho gayi 😵 Thodi der baad try karo."
            )