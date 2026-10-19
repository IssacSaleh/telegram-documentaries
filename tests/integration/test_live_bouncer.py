"""Live Bouncer integration tests — real Gemini vision calls (ROADMAP Phase 2).

Acceptance (SPECS/ROADMAP.md §2): "a portrait passes through to the
Interviewer; a non-human image gets a rejection reply and state resets;
rejects are logged."

These tests are SKIPPED by default so `./scripts/test` stays offline-green
and deterministic. Enable them explicitly:

    RUN_LIVE_TESTS=1 ./scripts/test

Fixtures in `tests/fixtures/` were content-verified against the same model:
`person.jpg` -> "HUMAN=yes", `landscape.jpg` -> "HUMAN=no".
"""

from __future__ import annotations

import os
import pathlib

import pytest

from app.bouncer import REJECTION_REPLY, AdkHumanPresenceClassifier, bounce
from app.models import TelegramUpdate
from app.state import get_store

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="live Gemini calls; set RUN_LIVE_TESTS=1 to enable",
)

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"

#: One chat that should survive admission and be purged on rejection.
CHAT_ID = 777001


class _FixturePhotoFetcher:
    """Stands in for `TelegramPhotoFetcher`: serves local fixture bytes.

    Keeps these tests dependent on Gemini only — no Telegram network I/O.
    """

    def __init__(self, fixture_name: str) -> None:
        self._image_bytes = (FIXTURES / fixture_name).read_bytes()

    def fetch(self, file_id: str) -> tuple[bytes, str]:
        assert file_id, "fetcher should be handed the largest photo's file_id"
        return self._image_bytes, "image/jpeg"


@pytest.fixture(scope="module")
def classifier(live_credentials: None) -> AdkHumanPresenceClassifier:
    """One classifier per run: the ADK agent is expensive to construct.

    `live_credentials` (see `conftest.py`) loads `.env` into the process
    environment so the ADK client finds `GEMINI_API_KEY`, and skips the whole
    module cleanly when credentials are absent.
    """
    return AdkHumanPresenceClassifier()


def _photo_update() -> TelegramUpdate:
    """A Telegram update carrying a photo and nothing else we care about."""
    return TelegramUpdate.model_validate(
        {
            "update_id": 424242,
            "message": {
                "message_id": 7,
                "date": 1759750000,
                "chat": {"id": CHAT_ID, "type": "private", "first_name": "Issac"},
                "photo": [
                    {"file_id": "small", "file_unique_id": "us", "width": 90, "height": 90},
                    {"file_id": "largest", "file_unique_id": "ul", "width": 900, "height": 1200},
                ],
            },
        }
    )


def test_portrait_with_a_human_is_admitted(
    classifier: AdkHumanPresenceClassifier,
) -> None:
    """Positive path: a portrait passes through, with no reply of its own."""
    get_store().get_or_create(CHAT_ID)

    decision = bounce(
        _photo_update(),
        fetcher=_FixturePhotoFetcher("person.jpg"),
        classifier=classifier,
    )

    assert decision.error is False
    assert decision.admitted is True
    assert decision.reply is None, "admission must stay silent for the Interviewer"

    assert decision.verdict is not None
    assert decision.verdict.human_present is True
    assert 0.0 <= decision.verdict.confidence <= 1.0
    assert decision.verdict.reason.strip(), "Gemini must name what it saw"

    # Admitting must NOT reset the session the Interviewer is about to use.
    assert get_store().get(CHAT_ID) is not None


def test_landscape_is_rejected_and_state_resets(
    classifier: AdkHumanPresenceClassifier,
) -> None:
    """Negative path: no human -> cheeky reply + that chat's state purged."""
    session = get_store().get_or_create(CHAT_ID)
    assert session.phase is not None

    decision = bounce(
        _photo_update(),
        fetcher=_FixturePhotoFetcher("landscape.jpg"),
        classifier=classifier,
    )

    assert decision.error is False
    assert decision.admitted is False

    assert decision.verdict is not None
    assert decision.verdict.human_present is False
    assert decision.verdict.reason.strip()

    assert decision.reply is not None
    assert decision.reply.chat_id == CHAT_ID
    assert decision.reply.text == REJECTION_REPLY

    # Rejection purges ephemeral state for THIS chat only.
    assert get_store().get(CHAT_ID) is None


def test_rejection_does_not_touch_other_chats(
    classifier: AdkHumanPresenceClassifier,
) -> None:
    """Session isolation: a reject resets only the offending chat."""
    other_chat = CHAT_ID + 1
    get_store().get_or_create(other_chat)
    get_store().get_or_create(CHAT_ID)

    decision = bounce(
        _photo_update(),
        fetcher=_FixturePhotoFetcher("landscape.jpg"),
        classifier=classifier,
    )

    assert decision.admitted is False
    assert get_store().get(CHAT_ID) is None
    assert get_store().get(other_chat) is not None, "other chats must be untouched"
