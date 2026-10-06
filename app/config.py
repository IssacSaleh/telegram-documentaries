"""Configuration loaded from the environment / `.env`.

SPECS/MISSION.md non-negotiable: "Never hardcode or commit secrets."
Values are read at runtime and never logged.

Phase 1 uses `TELEGRAM_BOT_TOKEN` only. `GEMINI_API_KEY` is loaded here so
the config surface matches `.env.example`, but it is deliberately unused
until later phases (no model calls yet).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.logging_utils import get_logger

logger = get_logger("config")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    gemini_api_key: str

    def __repr__(self) -> str:
        # Never let a secret reach a log line or a traceback.
        return (
            "Settings(telegram_bot_token=<redacted>, "
            "gemini_api_key=<redacted>)"
        )

    __str__ = __repr__


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from `.env` and the process environment.

    Raises `ConfigError` if a required value is absent or still the
    placeholder from `.env.example`.
    """
    if env_file is None:
        load_dotenv()
    else:
        load_dotenv(env_file)

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    gemini = (os.environ.get("GEMINI_API_KEY") or "").strip()

    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", token),
            ("GEMINI_API_KEY", gemini),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill in real values."
        )

    if token.startswith("your_") or gemini.startswith("your_"):
        raise ConfigError(
            "Environment still holds .env.example placeholders. "
            "Replace them with real credentials."
        )

    logger.debug("event=config_loaded keys=TELEGRAM_BOT_TOKEN,GEMINI_API_KEY")
    return Settings(telegram_bot_token=token, gemini_api_key=gemini)
