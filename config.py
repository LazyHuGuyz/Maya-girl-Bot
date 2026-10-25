"""
Application configuration.

All settings come from environment variables, optionally loaded from a
.env file that sits next to this file. Secrets are never hard-coded and
are never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Project root (the folder containing this file)
BASE_DIR = Path(__file__).resolve().parent

# Load .env from the project root, if it exists
load_dotenv(BASE_DIR / ".env")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _get_str(name: str, default: str) -> str:
    """Read a string env var, falling back to a default when empty."""
    return (os.getenv(name) or default).strip() or default


def _get_int(name: str, default: int) -> int:
    """Read an integer env var with validation."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    """Read a float env var with validation."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings."""

    telegram_token: str
    openrouter_api_key: str
    openrouter_base_url: str
    openrouter_model: str
    openrouter_temperature: float
    openrouter_max_tokens: int
    openrouter_site_url: str
    openrouter_app_name: str
    db_path: Path
    max_history_messages: int
    max_input_chars: int
    request_timeout: float
    max_retries: int
    typing_min_delay: float
    typing_max_delay: float
    rate_limit_per_minute: int
    admin_ids: frozenset[int]
    log_level: str


def load_settings() -> Settings:
    """Validate and return the application settings."""
    telegram_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    openrouter_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()

    if not telegram_token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is missing. Add it to your .env file.")
    if not openrouter_key:
        raise ConfigError("OPENROUTER_API_KEY is missing. Add it to your .env file.")

    # Parse comma-separated admin IDs, ignoring blanks and junk
    admin_raw = os.getenv("ADMIN_IDS") or ""
    admin_ids = frozenset(int(x) for x in admin_raw.split(",") if x.strip().isdigit())

    # Resolve DB path relative to the project folder
    db_path = Path(_get_str("DB_PATH", "data/mayabot.db"))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    min_delay = _get_float("TYPING_MIN_DELAY", 0.8)
    max_delay = _get_float("TYPING_MAX_DELAY", 2.2)
    if min_delay < 0 or min_delay > max_delay:
        raise ConfigError("TYPING_MIN_DELAY must be >= 0 and <= TYPING_MAX_DELAY")

    return Settings(
        telegram_token=telegram_token,
        openrouter_api_key=openrouter_key,
        openrouter_base_url=_get_str("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        openrouter_model=_get_str("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
        openrouter_temperature=_get_float("OPENROUTER_TEMPERATURE", 0.9),
        openrouter_max_tokens=_get_int("OPENROUTER_MAX_TOKENS", 500),
        openrouter_site_url=_get_str("OPENROUTER_SITE_URL", "https://example.com"),
        openrouter_app_name=_get_str("OPENROUTER_APP_NAME", "MayaBot"),
        db_path=db_path,
        max_history_messages=_get_int("MAX_HISTORY_MESSAGES", 40),
        max_input_chars=_get_int("MAX_INPUT_CHARS", 1500),
        request_timeout=_get_float("REQUEST_TIMEOUT", 45.0),
        max_retries=_get_int("MAX_RETRIES", 3),
        typing_min_delay=min_delay,
        typing_max_delay=max_delay,
        rate_limit_per_minute=_get_int("RATE_LIMIT_PER_MINUTE", 12),
        admin_ids=admin_ids,
        log_level=_get_str("LOG_LEVEL", "INFO"),
    )