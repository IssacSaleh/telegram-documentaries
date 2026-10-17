"""Shared fixtures and test doubles for the Telegram Documentaries suite."""

from __future__ import annotations

import os
from collections.abc import Iterable

import pytest

from app.models import BouncerVerdict, InterviewTurn, OutboundMessage, OutboundPhoto

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


@pytest.fixture
def bot_token() -> str:
    """A realistic-looking Telegram bot token for secret-leak assertions.

    Prefers the real `TELEGRAM_BOT_TOKEN` when the environment exports one;
    otherwise a well-formed stand-in in Telegram's `<digits>:<token>` format.

    This value is only ever asserted ABSENT from output — never print it.
    """
    return os.environ.get("TELEGRAM_BOT_TOKEN") or "765432109:AAExampleTokenForTestsOnlyNotARealSecret"


# ---------------------------------------------------------------------------
# Test doubles — shared by gateway, bouncer, and secret-hygiene tests.
# ---------------------------------------------------------------------------


class RecordingSender:
    """`MessageSender` double: records every outbound message and photo.

    Chats listed in `fail_on` raise BEFORE a successful send is recorded, so
    a test can model one chat's send failing (e.g. a 403-blocked chat) while
    observing exactly which replies actually went out.
    """

    def __init__(self, fail_on: Iterable[int] = ()) -> None:
        self.attempts: list[OutboundMessage] = []
        self.sent: list[OutboundMessage] = []
        self.photo_attempts: list[OutboundPhoto] = []
        self.photos: list[OutboundPhoto] = []
        self._fail_on = set(fail_on)

    def send(self, message: OutboundMessage) -> None:
        self.attempts.append(message)
        if message.chat_id in self._fail_on:
            raise RuntimeError(f"send failed for chat {message.chat_id}")
        self.sent.append(message)

    def send_photo(self, message: OutboundPhoto) -> None:
        self.photo_attempts.append(message)
        if message.chat_id in self._fail_on:
            raise RuntimeError(f"send_photo failed for chat {message.chat_id}")
        self.photos.append(message)


class FakePhotoFetcher:
    """PhotoFetcher double: returns canned bytes; no Telegram I/O."""

    def __init__(self, data: bytes = b"\xff\xd8fakejpeg", mime: str = "image/jpeg") -> None:
        self.data = data
        self.mime = mime
        self.requested: list[str] = []

    def fetch(self, file_id: str) -> tuple[bytes, str]:
        self.requested.append(file_id)
        return self.data, self.mime


class ExplodingPhotoFetcher:
    """PhotoFetcher double that models a Telegram file download failure."""

    def fetch(self, file_id: str) -> tuple[bytes, str]:
        raise RuntimeError("telegram getFile exploded")


class FlakyPhotoFetcher:
    """PhotoFetcher double: succeeds for the Bouncer, fails on the later store.

    The gateway fetches an admitted photo twice — once for classification in
    `bounce()`, once to stash it for the Converter. This models the second
    fetch failing (a transient Telegram hiccup) without breaking admission.
    """

    def __init__(self, data: bytes = b"\xff\xd8fakejpeg", mime: str = "image/jpeg") -> None:
        self.data = data
        self.mime = mime
        self.calls = 0

    def fetch(self, file_id: str) -> tuple[bytes, str]:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("telegram getFile exploded on the second call")
        return self.data, self.mime


class FakeClassifier:
    """HumanPresenceClassifier double: returns a canned verdict."""

    def __init__(self, verdict: BouncerVerdict) -> None:
        self.verdict = verdict
        self.received: list[tuple[bytes, str]] = []

    def classify(self, image_bytes: bytes, mime_type: str) -> BouncerVerdict:
        self.received.append((image_bytes, mime_type))
        return self.verdict


class ExplodingClassifier:
    """HumanPresenceClassifier double that models a Gemini failure."""

    def classify(self, image_bytes: bytes, mime_type: str) -> BouncerVerdict:
        raise RuntimeError("gemini 500")


def human_verdict() -> BouncerVerdict:
    return BouncerVerdict(human_present=True, confidence=0.98, reason="clear portrait")


def no_human_verdict() -> BouncerVerdict:
    return BouncerVerdict(human_present=False, confidence=0.97, reason="a mountain range")


class FakeInterviewer:
    """`Interviewer` double: scripted turns, no network, fully deterministic.

    `turns` is a script for the next `next_turn` calls, popped in order. Once
    the script runs out, the double behaves like a sensible adaptive
    interviewer: it asks a generic question until `completion_after`
    questions have been answered, then it completes with `animal`. Set
    `completion_after` high to observe the code's minimum-question guard, or
    pass an explicit completion turn to observe premature-completion handling.
    """

    def __init__(
        self,
        turns: Iterable[InterviewTurn] | None = None,
        *,
        animal: str = "Red Fox",
        completion_after: int = 5,
    ) -> None:
        self._turns = list(turns) if turns is not None else []
        self._animal = animal
        self._completion_after = completion_after
        self.next_turn_calls: list[int] = []
        self.final_turn_calls: list[int] = []

    def next_turn(self, dossier) -> InterviewTurn:
        self.next_turn_calls.append(dossier.asked)
        if self._turns:
            return self._turns.pop(0)
        if dossier.asked >= self._completion_after:
            return self._complete()
        return InterviewTurn(next_question=f"Question {dossier.asked + 1}?")

    def final_turn(self, dossier) -> InterviewTurn:
        self.final_turn_calls.append(dossier.asked)
        return self._complete()

    def _complete(self) -> InterviewTurn:
        return InterviewTurn(
            is_complete=True,
            suggested_animal=self._animal,
            animal_rationale="scripted",
        )


class ExplodingInterviewer:
    """`Interviewer` double that models a Gemini/ADK failure mid-interview."""

    def next_turn(self, dossier) -> InterviewTurn:
        raise RuntimeError("gemini 500")

    def final_turn(self, dossier) -> InterviewTurn:
        raise RuntimeError("gemini 500")


class FakeConverter:
    """`Converter` double: records inputs, returns canned image bytes.

    Proves the gateway passes *all three* Converter inputs together — the
    original photo bytes/mime, the dossier text, and the suggested animal —
    without any network call.
    """

    def __init__(
        self,
        image_bytes: bytes = b"\x89PNGfakeportrait",
        mime_type: str = "image/png",
    ) -> None:
        self.image_bytes = image_bytes
        self.mime_type = mime_type
        self.calls: list[dict] = []

    def generate(
        self,
        *,
        original_photo_bytes: bytes,
        original_photo_mime: str,
        dossier_text: str,
        suggested_animal: str,
    ):
        from app.converter import ConverterResult

        self.calls.append(
            {
                "original_photo_bytes": original_photo_bytes,
                "original_photo_mime": original_photo_mime,
                "dossier_text": dossier_text,
                "suggested_animal": suggested_animal,
            }
        )
        return ConverterResult(
            suggested_animal=suggested_animal,
            image_bytes=self.image_bytes,
            image_mime_type=self.mime_type,
        )


class ExplodingConverter:
    """`Converter` double that models a Gemini image-model failure."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("gemini image 500")
