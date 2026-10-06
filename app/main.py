"""Entrypoint for The Telegram Documentaries gateway.

Run with:

    python -m app.main

Loads `.env`, configures structured logging, then long-polls Telegram and
replies to every message with the Phase 1 hardcoded reply.

SPECS/ROADMAP.md Phase 1: "project skeleton, `.env` loading, and a
long-polling loop that replies to a hardcoded message."
"""

from __future__ import annotations

import sys

from app.config import ConfigError, load_settings
from app.gateway import run
from app.logging_utils import configure_logging, get_logger

logger = get_logger("main")


def main() -> int:
    configure_logging()

    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error("event=config_error error=%r", exc)
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    logger.info("event=starting entrypoint=app.main")

    try:
        run(token=settings.telegram_bot_token)
    except KeyboardInterrupt:
        logger.info("event=shutdown reason=keyboard_interrupt")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
