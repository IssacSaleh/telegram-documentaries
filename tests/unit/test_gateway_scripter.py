"""Gateway ↔ Scripter integration (ROADMAP §5, SPECS/2026-10-18-voiceover D2).

The ``Scripter`` class itself is unit-tested in ``test_scripter.py``; this
module drives the real gateway state machine with the offline fakes and asserts
the contract the gateway owes the Scripter:

- the narration is generated from the dossier and delivered in the *same* turn
  the portrait is sent — no extra message, portrait before text — and the raw
  text is captured on ``session.script`` for the Narrator, with an ops-visible
  ``script_captured`` line;
- a raising, empty, or unwired Scripter never leaves the user in silence, never
  raises into the caller, and lands the session in ``DONE``.
"""

from __future__ import annotations

import logging

from app.gateway import ScripterFallbackPolicy, handle_update
from app.models import TelegramUpdate
from app.state import SessionPhase, get_store
from tests.conftest import (
    ExplodingScripter,
    FakeClassifier,
    FakeConverter,
    FakeInterviewer,
    FakePhotoFetcher,
    FakeScripter,
    RecordingSender,
    human_verdict,
)


def _update(text: str = "hello", chat_id: int = 555) -> TelegramUpdate:
    return TelegramUpdate.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 1759750000,
                "chat": {"id": chat_id, "type": "private"},
                "text": text,
            },
        }
    )


def _photo_update(chat_id: int = 555, update_id: int = 2) -> TelegramUpdate:
    """An update whose message carries a photo (the Bouncer's input)."""
    return TelegramUpdate.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": 11,
                "date": 1759750000,
                "chat": {"id": chat_id, "type": "private"},
                "photo": [
                    {"file_id": "small", "file_unique_id": "u1", "width": 90, "height": 90},
                    {"file_id": "largest", "file_unique_id": "u2",
                     "width": 900, "height": 900},
                ],
            },
        }
    )


def _finish_interview(
    sender: RecordingSender, chat_id: int, animal: str = "Red Fox"
) -> FakeInterviewer:
    """Drive a chat through admission to ``DONE``, storing a photo along the way."""
    interviewer = FakeInterviewer([], animal=animal)
    handle_update(
        _photo_update(chat_id),
        sender,
        fetcher=FakePhotoFetcher(data=b"\xff\xd8ORIGINAL", mime="image/jpeg"),
        classifier=FakeClassifier(human_verdict()),
        interviewer=interviewer,
    )
    handle_update(_update("yes", chat_id=chat_id), sender, interviewer=interviewer)
    for _ in range(5):
        handle_update(
            _update("an answer", chat_id=chat_id), sender, interviewer=interviewer
        )
    assert get_store().get(chat_id).phase is SessionPhase.DONE
    return interviewer


class TestScriptGeneration:
    """The narration follows the portrait automatically and is captured."""

    def test_final_answer_auto_triggers_the_narration_script(self) -> None:
        """The Scripter's narration follows the portrait automatically."""
        sender = RecordingSender()
        converter = FakeConverter(image_bytes=b"PORTRAIT")
        scripter = FakeScripter(script="In the quiet hours, a fox-like soul stirs.")
        interviewer = FakeInterviewer([], animal="Red Fox")
        chat_id = 711
        handle_update(
            _photo_update(chat_id),
            sender,
            fetcher=FakePhotoFetcher(data=b"\xff\xd8ORIGINAL", mime="image/jpeg"),
            classifier=FakeClassifier(human_verdict()),
            interviewer=interviewer,
        )
        handle_update(_update("yes", chat_id=chat_id), sender, interviewer=interviewer)
        for _ in range(4):
            handle_update(
                _update("an answer", chat_id=chat_id),
                sender,
                interviewer=interviewer,
                converter=converter,
            )
        sender.sent.clear()
        sender.photos.clear()

        handle_update(
            _update("an answer", chat_id=chat_id),
            sender,
            interviewer=interviewer,
            converter=converter,
            scripter=scripter,
        )

        assert len(scripter.calls) == 1, "script must be generated from the dossier"
        assert len(sender.photos) == 1
        assert sender.sent[-1].text == "In the quiet hours, a fox-like soul stirs."
        # Portrait (photo) is delivered before the narration (text).
        assert sender.photos and sender.sent

        # The raw script is cleanly captured on the session, ready for
        # audio synthesis (Phase 6 Narrator).
        session = get_store().get(chat_id)
        assert session.script == "In the quiet hours, a fox-like soul stirs."
        assert session.script.strip() == session.script, "no stray whitespace"
        assert "\n" not in session.script, "single paragraph only"
        assert len(session.script.split()) <= 90, "≤90 words for TTS"

    def test_script_capture_is_visible_in_the_console_log(self, caplog) -> None:
        """The ops-visible line: chars, words, and that state stored it."""
        sender = RecordingSender()
        converter = FakeConverter(image_bytes=b"PORTRAIT")
        scripter = FakeScripter(script="A quiet narration of the wild couch.")
        interviewer = FakeInterviewer([], animal="Red Fox")
        chat_id = 713
        handle_update(
            _photo_update(chat_id),
            sender,
            fetcher=FakePhotoFetcher(data=b"\xff\xd8ORIGINAL", mime="image/jpeg"),
            classifier=FakeClassifier(human_verdict()),
            interviewer=interviewer,
        )
        handle_update(_update("yes", chat_id=chat_id), sender, interviewer=interviewer)
        for _ in range(4):
            handle_update(
                _update("an answer", chat_id=chat_id),
                sender,
                interviewer=interviewer,
                converter=converter,
            )

        with caplog.at_level(logging.INFO, logger="telegram_documentaries"):
            handle_update(
                _update("an answer", chat_id=chat_id),
                sender,
                interviewer=interviewer,
                converter=converter,
                scripter=scripter,
            )

        captured_lines = [
            r.message
            for r in caplog.records
            if "event=script_captured" in r.message
        ]
        assert captured_lines, "script_captured must appear in the console log"
        line = captured_lines[0]
        assert f"chat_id={chat_id}" in line
        assert "words=" in line
        assert "session_script_stored=True" in line


class TestScripterFailure:
    """A raising or empty Scripter degrades with a friendly reply, lands the
    session in DONE, logs the failure, and never raises into the caller."""

    def test_scripter_failure_notifies_the_user_and_lands_done(self, caplog) -> None:
        store = get_store()
        sender = RecordingSender()
        chat_id = 720
        _finish_interview(sender, chat_id)
        sender.sent.clear()
        sender.photos.clear()
        sender.events.clear()

        with caplog.at_level(logging.INFO, logger="telegram_documentaries"):
            reply = handle_update(
                _update("go", chat_id=chat_id),
                sender,
                converter=FakeConverter(image_bytes=b"PORTRAIT"),
                scripter=ExplodingScripter(),
            )

        assert reply is not None, "a failed narration must never leave silence"
        assert reply.text == ScripterFallbackPolicy.ERROR_REPLY
        replies = [m for m in sender.sent if m.text == ScripterFallbackPolicy.ERROR_REPLY]
        assert len(replies) == 1, "the fallback is sent exactly once"
        assert len(sender.photos) == 1, "the portrait is still delivered first"
        assert sender.events == ["photo", "send"], "portrait then the fallback"
        session = store.get(chat_id)
        assert session.phase is SessionPhase.DONE
        assert session.script is None, "no narration was captured"
        assert any("event=scripter_error" in r.message for r in caplog.records)

    def test_empty_script_also_degrades_with_the_same_reply(self, caplog) -> None:
        store = get_store()
        sender = RecordingSender()
        chat_id = 721
        _finish_interview(sender, chat_id)
        sender.sent.clear()
        sender.photos.clear()
        sender.events.clear()

        with caplog.at_level(logging.INFO, logger="telegram_documentaries"):
            reply = handle_update(
                _update("go", chat_id=chat_id),
                sender,
                converter=FakeConverter(image_bytes=b"PORTRAIT"),
                scripter=FakeScripter(script=""),
            )

        assert reply is not None
        assert reply.text == ScripterFallbackPolicy.ERROR_REPLY
        replies = [m for m in sender.sent if m.text == ScripterFallbackPolicy.ERROR_REPLY]
        assert len(replies) == 1, "the fallback is sent exactly once"
        assert len(sender.photos) == 1, "the portrait is still delivered first"
        assert sender.events == ["photo", "send"], "portrait then the fallback"
        session = store.get(chat_id)
        assert session.phase is SessionPhase.DONE
        assert session.script is None, "no narration was captured"
        assert any("event=scripter_empty" in r.message for r in caplog.records)

    def test_scripter_not_wired_stays_silent_and_logs(self, caplog) -> None:
        """An unwired stage only logs (like the Narrator); no new reply."""
        sender = RecordingSender()
        chat_id = 722
        _finish_interview(sender, chat_id)
        sender.sent.clear()

        with caplog.at_level(logging.INFO, logger="telegram_documentaries"):
            reply = handle_update(
                _update("go", chat_id=chat_id),
                sender,
                converter=FakeConverter(image_bytes=b"PORTRAIT"),
                # scripter deliberately omitted -> None
            )

        assert reply is None, "an unwired Scripter adds no message"
        assert sender.sent == []
        assert len(sender.photos) == 1
        assert any(
            "event=scripter_not_wired" in r.message for r in caplog.records
        ), "an un-logged fallback is prohibited (TECH.md)"
