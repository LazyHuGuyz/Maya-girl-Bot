"""
Helper utilities: input sanitizing, language detection, message splitting,
human-like timing, rate limiting, prompt-injection heuristics, and logging.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections import defaultdict, deque

# Telegram's hard limit is 4096 chars; stay a bit under for safety
TELEGRAM_MESSAGE_LIMIT = 4000

# Control characters and invisible Unicode tricks we strip from user input
_CONTROL_CHARS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\ufeff]"
)

# Devanagari Unicode block
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")

# Common romanized Hindi words used to spot Hinglish
_HINGLISH_WORDS = {
    "kya", "hai", "hain", "nahi", "nahin", "kaise", "kaisi", "tum", "main",
    "mujhe", "mera", "meri", "tera", "teri", "kal", "accha", "achha", "bolo",
    "kyun", "kyu", "thik", "theek", "yaar", "hoon", "bhai", "abhi", "kab",
    "kahan", "kaha", "bahut", "bohot", "matlab", "chalo", "tha", "thi",
    "kuch", "koi", "bata", "batao", "karo", "karna", "wala", "wali", "bas",
}

# Phrases that try to override the bot's rules (heuristic, not a security boundary)
_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts?|rules)",
        r"(reveal|show|print|repeat|leak)\s+(your|the)\s+(system\s+)?(prompt|instructions)",
        r"you\s+are\s+now\s+(dan|an?\s+unrestricted|an?\s+ai\s+without)",
        r"pretend\s+(you\s+are|to\s+be)\s+(not\s+an?\s+ai|chatgpt|gpt|an?\s+unfiltered)",
        r"developer\s+mode",
        r"\bjailbreak\b",
        r"forget\s+(your|all)\s+(rules|instructions|persona)",
    )
]


def sanitize_user_text(text: str, max_chars: int) -> str:
    """
    Clean user input before it reaches the database or the AI.

    - removes control and invisible characters
    - collapses excessive blank lines
    - trims whitespace
    - truncates to max_chars to prevent oversized prompts
    """
    text = _CONTROL_CHARS.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:max_chars]


def looks_like_injection(text: str) -> bool:
    """Return True if the text resembles an attempt to override the bot's rules."""
    return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)


def detect_language(text: str) -> str:
    """
    Guess the language of a message.

    Returns one of: "hi" (Devanagari Hindi), "hinglish" (romanized Hindi
    mixed with English), or "en" (English).
    """
    letters = [c for c in text if c.isalpha()]
    if letters:
        devanagari_ratio = sum(1 for c in letters if _DEVANAGARI.match(c)) / len(letters)
        if devanagari_ratio > 0.3:
            return "hi"

    words = set(re.findall(r"[a-z]+", text.lower()))
    if words & _HINGLISH_WORDS:
        return "hinglish"
    return "en"


def clean_reply(text: str) -> str:
    """Tidy the model's reply: strip whitespace and any leaked 'Maya:' label."""
    text = text.strip()
    text = re.sub(r"^(maya)\s*[:：-]\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """
    Split a long reply into Telegram-sized chunks.

    Splits by paragraph first, then by sentence, and only hard-cuts at a
    space as a last resort so words are rarely broken.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current = ""
    for paragraph in re.split(r"\n{2,}", text):
        for piece in _split_long_paragraph(paragraph, limit):
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                parts.append(current)
                current = piece
    if current:
        parts.append(current)
    return [p for p in parts if p.strip()]


def _split_long_paragraph(paragraph: str, limit: int) -> list[str]:
    """Split a single paragraph that is itself longer than the limit."""
    if len(paragraph) <= limit:
        return [paragraph]

    chunks: list[str] = []
    buffer = ""
    # Split after sentence-ending punctuation (including Hindi danda)
    for sentence in re.split(r"(?<=[.!?।])\s+", paragraph):
        # Hard-cut sentences that are still too long
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()

        candidate = f"{buffer} {sentence}".strip() if buffer else sentence
        if len(candidate) <= limit:
            buffer = candidate
        else:
            chunks.append(buffer)
            buffer = sentence
    if buffer:
        chunks.append(buffer)
    return [c for c in chunks if c]


def human_delay(reply_chars: int, min_seconds: float, max_seconds: float) -> float:
    """
    Compute a realistic 'typing' delay before sending a reply.

    A random base delay plus a small amount per character, capped so
    replies never feel stuck.
    """
    base = random.uniform(min_seconds, max_seconds)
    typing_time = min(reply_chars * 0.02, 3.0)
    return min(base + typing_time, 6.0)


def is_group_chat(chat_type: str) -> bool:
    """True for group and supergroup chats."""
    return chat_type in ("group", "supergroup")


class RateLimiter:
    """Simple in-memory sliding-window rate limiter keyed by user ID."""

    def __init__(self, max_calls: int, window_seconds: float = 60.0) -> None:
        self._max_calls = max_calls
        self._window = window_seconds
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, key: int) -> bool:
        """Record a call for key and return True if it is within the limit."""
        now = time.monotonic()
        hits = self._hits[key]
        # Drop timestamps outside the window
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= self._max_calls:
            return False
        hits.append(now)
        return True


def setup_logging(level: str) -> None:
    """Configure application logging and silence noisy libraries."""
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    # httpx logs full URLs at INFO, which include the Telegram bot token.
    # Keep it at WARNING so secrets never reach the logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)