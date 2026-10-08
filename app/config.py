"""Configuration loaded from the environment / `.env`.

SPECS/MISSION.md non-negotiable: "Never hardcode or commit secrets."
Values are read at runtime and never logged.

`TELEGRAM_BOT_TOKEN` drives the transport; `GEMINI_API_KEY` authorises the
Bouncer's Gemini vision calls (Phase 2). Both are required by the loader.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv

from app.logging_utils import get_logger

logger = get_logger("config")

#: Telegram Bot API base URL. Shared by the gateway and the Bouncer — one
#: source of truth so the two can never drift apart (they both build
#: token-bearing URLs against it).
TELEGRAM_API = "https://api.telegram.org"


def telegram_payload(response: httpx.Response) -> dict:
    """Parsed JSON body of a Telegram response; `{}` when it is not an object.

    Telegram answers errors with a JSON `{"ok": false, ...}` body *and*, via
    proxies and load balancers, with plain HTML error pages. Callers must
    never `raise_for_status()` on these responses: the request URL carries
    the bot token and `httpx` embeds the full URL in the exception message,
    which would leak the secret into the logs (SPECS/MISSION.md).
    """
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


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
