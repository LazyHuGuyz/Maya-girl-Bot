"""
Entry point for MayaBot.

Run with:  python bot.py
"""

from __future__ import annotations

import logging
import sys

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from ai import OpenRouterClient
from config import ConfigError, Settings, load_settings
from database import Database
from handlers import (
    Services,
    error_handler,
    help_command,
    lang_command,
    message_handler,
    nickname_command,
    ping_command,
    reset_command,
    start_command,
    stats_command,
)
from memory import MemoryManager
from utils import RateLimiter, setup_logging

logger = logging.getLogger("mayabot")


async def _on_startup(app: Application) -> None:
    """Open the database once the bot is initialized."""
    services: Services = app.bot_data["services"]
    await services.db.connect()
    logger.info("MayaBot is online as @%s", app.bot.username)


async def _on_shutdown(app: Application) -> None:
    """Close the HTTP client and database cleanly."""
    services: Services = app.bot_data["services"]
    await services.ai.close()
    await services.db.close()
    logger.info("MayaBot shut down cleanly")


def build_application(settings: Settings) -> Application:
    """Wire together services, handlers, and lifecycle hooks."""
    db = Database(settings.db_path)
    services = Services(
        settings=settings,
        db=db,
        memory=MemoryManager(db, settings.max_history_messages),
        ai=OpenRouterClient(settings),
        limiter=RateLimiter(settings.rate_limit_per_minute),
    )

    app = (
        Application.builder()
        .token(settings.telegram_token)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )
    app.bot_data["services"] = services

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("nickname", nickname_command))
    app.add_handler(CommandHandler("lang", lang_command))

    # Plain text messages (private chats, groups, supergroups)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    app.add_error_handler(error_handler)
    return app


def main() -> None:
    """Load config, set up logging, and start polling."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(settings.log_level)
    app = build_application(settings)

    # drop_pending_updates avoids replaying a backlog after downtime
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()