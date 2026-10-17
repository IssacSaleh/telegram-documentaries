"""Structured logging helpers.

SPECS/TECH.md: "Prefer decorators over threading logging through business
logic." Business code stays clean; the decorator emits the structured record.

Prohibited by the same section: bare ``except: pass``, swallowed exceptions,
un-logged fallbacks.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger for this project."""
    return logging.getLogger(f"telegram_documentaries.{name}")


@contextmanager
def log_context(logger: logging.Logger, **fields: Any) -> Iterator[None]:
    """Run a block, logging entry/exit as structured key=value fields."""
    rendered = " ".join(f"{k}={v!r}" for k, v in fields.items())
    logger.debug("enter %s", rendered)
    try:
        yield
    except Exception:
        logger.exception("failed %s", rendered)
        raise
    else:
        logger.debug("exit %s", rendered)


def logged(logger: logging.Logger, event: str) -> Callable[[F], F]:
    """Decorator: log a structured record around a function call.

    Failures are logged loudly and re-raised — never swallowed.
    """

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            logger.debug("event=%s fn=%s status=start", event, fn.__name__)
            try:
                result = fn(*args, **kwargs)
            except Exception:
                logger.exception("event=%s fn=%s status=error", event, fn.__name__)
                raise
            logger.debug("event=%s fn=%s status=ok", event, fn.__name__)
            return result

        return wrapper  # type: ignore[return-value]

    return decorate


def configure_logging(level: int = logging.INFO) -> None:
    """Install a single structured handler for the whole process."""
    root = logging.getLogger("telegram_documentaries")
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
