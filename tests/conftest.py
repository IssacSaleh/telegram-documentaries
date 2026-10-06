"""Shared fixtures for the Telegram Documentaries test suite."""

from __future__ import annotations

import pytest

# A realistic Telegram `getUpdates` payload. Deliberately stuffed with extra
# fields Telegram actually sends but our models do not care about, to prove
# the boundary tolerates arbitrary untrusted input (SPECS/TECH.md).
SAMPLE_UPDATE: dict = {
    "update_id": 987654321,
    "message": {
        "message_id": 42,
        "date": 1759750000,
        "chat": {
            "id": 1122334455,
            "type": "private",
            "first_name": "Issac",
            "username": "issac",
        },
        "text": "hello bot",
        "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        "from": {
            "id": 1122334455,
            "is_bot": False,
            "first_name": "Issac",
            "language_code": "en",
        },
    },
}


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """Reset the shared in-memory state driver between tests.

    The store is a process-wide singleton (SPECS/TECH.md: "one shared state
    driver"). Without this, one test's registered chats leak into the next
    and assertions about isolation become order-dependent.
    """
    from app.state import get_store

    get_store().clear()
    yield
    get_store().clear()


@pytest.fixture
def sample_update_dict() -> dict:
    """A minimal, well-formed update with a plain text message."""
    return {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "date": 1759750000,
            "chat": {"id": 555, "type": "private"},
            "text": "hi",
        },
    }


@pytest.fixture
def photo_update_dict() -> dict:
    """An update whose message carries a photo, not text."""
    return {
        "update_id": 2,
        "message": {
            "message_id": 11,
            "date": 1759750001,
            "chat": {"id": 555, "type": "private"},
            "photo": [{"file_id": "abc", "file_unique_id": "u1", "width": 90, "height": 90}],
        },
    }
