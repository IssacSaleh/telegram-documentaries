"""Gateway integration for the Narrator (Phase 6).

Auto-trigger: the Scripter's narration is handed straight to the Narrator
and the voice note follows the script text — portrait -> script text ->
voice note, all to the same chat, with no extra user message (decision D2).
Phase machine per SPECS/TECH.md: SCRIPTING after the portrait, NARRATING
after the script, DONE after the voice note (decision D6) — and every
failure branch (TTS raise, voice-send failure, narrator not wired) lands
DONE with the session still usable, never raising into the poll loop.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.gateway import handle_update, poll_once, run
from app.models import TelegramUpdate
from app.state import SessionPhase, get_store
from tests.conftest import (
    ExplodingNarrator,
    FakeClassifier,
    FakeConverter,
    FakeInterviewer,
    FakeNarrator,
    FakePhotoFetcher,
    FakeScripter,
    RecordingSender,
    human_verdict,
)

SCRIPT = "In the quiet hours, a fox-like soul stirs."
TOKEN = "123456789:AAExampleTokenForTestsOnlyNotARealSecret"


def _update(text: str, chat_id: int, update_id: int = 2) -> TelegramUpdate:
    return TelegramUpdate.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 1759750000,
                "chat": {"id": chat_id, "type": "private"},
                "text": text,
            },
        }
    )


def _photo_update(chat_id: int, update_id: int = 1) -> TelegramUpdate:
    return TelegramUpdate.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 1759750000,
                "chat": {"id": chat_id, "type": "private"},
                "photo": [
                    {"file_id": "small", "file_unique_id": "u1",
                     "width": 90, "height": 90},
                    {"file_id": "largest", "file_unique_id": "u2",
                     "width": 900, "height": 900},
                ],
            },
        }
    )


def _text_raw(update_id: int, chat_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "date": 1759750000,
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


def _batch_client(payload: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _only_session():
    store = get_store()
    ids = store.chat_ids
    assert len(ids) == 1, "these tests drive a single chat"
    session = store.get(next(iter(ids)))
    assert session is not None
    return session


class PhaseProbeScripter:
    """Scripter double that also records the phase it was called in."""

    def __init__(self, script: str = SCRIPT) -> None:
        self.script = script
        self.phase_when_called: SessionPhase | None = None

    def generate_script(self, dossier) -> str:
        self.phase_when_called = _only_session().phase
        return self.script


class PhaseProbeNarrator:
    """Narrator double that also records the phase it was called in."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.phase_when_called: SessionPhase | None = None

    def synthesise(self, script: str) -> bytes:
        self.calls.append(script)
        self.phase_when_called = _only_session().phase
        return b"OggSprobenarrator"


class VoiceFailingSender(RecordingSender):
    """RecordingSender whose voice-note upload always fails (models a
    Telegram sendVoice rejection while every other send still works)."""

    def send_voice(self, message) -> None:
        self.events.append("voice")
        self.voice_attempts.append(message)
        raise RuntimeError("telegram sendVoice failed: 'Bad Request: VOICE_INVALID'")


def _drive_to_four_answers(sender, chat_id: int, interviewer) -> None:
    """Admission -> YES -> four answers; the interview closes on the fifth."""
    handle_update(
        _photo_update(chat_id),
        sender,
        fetcher=FakePhotoFetcher(data=b"\xff\xd8ORIGINAL", mime="image/jpeg"),
        classifier=FakeClassifier(human_verdict()),
        interviewer=interviewer,
    )
    handle_update(_update("yes", chat_id, update_id=10), sender, interviewer=interviewer)
    for index in range(4):
        handle_update(
            _update("an answer", chat_id, update_id=20 + index),
            sender,
            interviewer=interviewer,
        )
    assert get_store().get(chat_id).phase is SessionPhase.INTERVIEWING


def _close_interview(sender, chat_id: int, interviewer, **stages) -> None:
    """The fifth answer completes the interview and auto-triggers the chain."""
    handle_update(
        _update("an answer", chat_id, update_id=30),
        sender,
        interviewer=interviewer,
        **stages,
    )


def _run_full_chain(sender, chat_id: int, **stages) -> None:
    """Admission -> YES -> four answers, then clear the recorder and let the
    fifth answer auto-trigger the portrait -> script -> voice chain."""
    interviewer = FakeInterviewer([], animal="Red Fox")
    _drive_to_four_answers(sender, chat_id, interviewer)
    sender.events.clear()
    sender.sent.clear()
    sender.photos.clear()
    _close_interview(sender, chat_id, interviewer, **stages)


class TestNarratorAutoTrigger:
    """D2: portrait -> script text -> voice note, same chat, no extras."""

    def test_portrait_script_and_voice_arrive_in_order_to_the_same_chat(self) -> None:
        sender = RecordingSender()
        chat_id = 880
        scripter = FakeScripter(script=SCRIPT)
        narrator = FakeNarrator(audio_bytes=b"OggSvoice")

        _run_full_chain(
            sender,
            chat_id,
            converter=FakeConverter(image_bytes=b"PORTRAIT"),
            scripter=scripter,
            narrator=narrator,
        )

        assert sender.events == ["send", "photo", "send", "voice"], (
            "exactly: interview reply, portrait, script text, voice note — "
            "in that order, with nothing extra (D2 keeps the text message)"
        )
        assert sender.sent[-1].text == SCRIPT, "the script text message is kept"
        assert sender.voices[0].audio_bytes == b"OggSvoice"
        assert sender.photos[0].chat_id == chat_id
        assert sender.sent[-1].chat_id == chat_id
        assert sender.voices[0].chat_id == chat_id, "voice goes to the same chat"

    def test_narrator_receives_exactly_the_stored_script(self) -> None:
        sender = RecordingSender()
        chat_id = 881
        narrator = FakeNarrator(audio_bytes=b"OggSvoice")

        _run_full_chain(
            sender,
            chat_id,
            converter=FakeConverter(),
            scripter=FakeScripter(script=SCRIPT),
            narrator=narrator,
        )

        assert narrator.calls == [SCRIPT], "invoked with exactly that script"
        assert get_store().get(chat_id).script == SCRIPT


class TestPhaseTransitions:
    """D6: GENERATING_IMAGE -> SCRIPTING -> NARRATING -> DONE."""

    def test_phase_is_scripting_when_the_scripter_runs(self) -> None:
        sender = RecordingSender()
        chat_id = 884
        interviewer = FakeInterviewer([], animal="Red Fox")
        scripter = PhaseProbeScripter()
        _drive_to_four_answers(sender, chat_id, interviewer)

        _close_interview(
            sender,
            chat_id,
            interviewer,
            converter=FakeConverter(),
            scripter=scripter,
            narrator=FakeNarrator(),
        )

        assert scripter.phase_when_called is SessionPhase.SCRIPTING, (
            "SCRIPTING is entered when the Converter returns the portrait"
        )

    def test_phase_is_narrating_when_the_narrator_runs(self) -> None:
        sender = RecordingSender()
        chat_id = 885
        interviewer = FakeInterviewer([], animal="Red Fox")
        narrator = PhaseProbeNarrator()
        _drive_to_four_answers(sender, chat_id, interviewer)

        _close_interview(
            sender,
            chat_id,
            interviewer,
            converter=FakeConverter(),
            scripter=FakeScripter(script=SCRIPT),
            narrator=narrator,
        )

        assert narrator.phase_when_called is SessionPhase.NARRATING, (
            "NARRATING is entered when the Scripter returns the script"
        )

    def test_phase_is_done_after_the_voice_note_and_session_survives(self) -> None:
        sender = RecordingSender()
        chat_id = 886

        _run_full_chain(
            sender,
            chat_id,
            converter=FakeConverter(),
            scripter=FakeScripter(script=SCRIPT),
            narrator=FakeNarrator(),
        )

        session = get_store().get(chat_id)
        assert session is not None, "the session must stay usable"
        assert session.phase is SessionPhase.DONE
        assert session.script == SCRIPT


class TestNarratorFailureDegradesGracefully:
    def _run_with_exploding_narrator(self, sender, chat_id: int, **stages):
        interviewer = FakeInterviewer([], animal="Red Fox")
        _drive_to_four_answers(sender, chat_id, interviewer)
        _close_interview(sender, chat_id, interviewer, **stages)
        return get_store().get(chat_id)

    def test_tts_failure_sends_a_friendly_reply_lands_done_and_logs(
        self, caplog
    ) -> None:
        from app.gateway import NarratorFallbackPolicy

        sender = RecordingSender()
        chat_id = 887

        with caplog.at_level(logging.INFO, logger="telegram_documentaries"):
            session = self._run_with_exploding_narrator(
                sender,
                chat_id,
                converter=FakeConverter(),
                scripter=FakeScripter(script=SCRIPT),
                narrator=ExplodingNarrator(),
            )

        replies = [m for m in sender.sent if m.text == NarratorFallbackPolicy.ERROR_REPLY]
        assert replies, "a failed voice note must never leave the user in silence"
        assert replies[0].chat_id == chat_id
        assert session is not None and session.phase is SessionPhase.DONE
        assert session.script == SCRIPT, "the script text was already delivered"
        assert any("event=narrator_error" in r.message for r in caplog.records)
        assert sender.voices == []

    def test_voice_send_failure_sends_a_friendly_reply_lands_done_and_logs(
        self, caplog
    ) -> None:
        from app.gateway import NarratorFallbackPolicy

        sender = VoiceFailingSender()
        chat_id = 888

        with caplog.at_level(logging.INFO, logger="telegram_documentaries"):
            session = self._run_with_exploding_narrator(
                sender,
                chat_id,
                converter=FakeConverter(),
                scripter=FakeScripter(script=SCRIPT),
                narrator=FakeNarrator(audio_bytes=b"OggSvoice"),
            )

        replies = [m for m in sender.sent if m.text == NarratorFallbackPolicy.ERROR_REPLY]
        assert replies, "a sendVoice failure must degrade to a friendly reply"
        assert session is not None and session.phase is SessionPhase.DONE
        assert any("event=voice_send_failed" in r.message for r in caplog.records)
        assert sender.voices == [], "the failed voice is not recorded as delivered"

    def test_narrator_not_wired_still_delivers_the_script_and_logs(
        self, caplog
    ) -> None:
        sender = RecordingSender()
        chat_id = 889
        interviewer = FakeInterviewer([], animal="Red Fox")
        _drive_to_four_answers(sender, chat_id, interviewer)

        with caplog.at_level(logging.INFO, logger="telegram_documentaries"):
            _close_interview(
                sender,
                chat_id,
                interviewer,
                converter=FakeConverter(),
                scripter=FakeScripter(script=SCRIPT),
                # narrator deliberately omitted -> None
            )

        assert sender.sent[-1].text == SCRIPT, "the script text is still delivered"
        assert sender.voices == []
        assert any("event=narrator_not_wired" in r.message for r in caplog.records)
        session = get_store().get(chat_id)
        assert session is not None and session.phase is SessionPhase.DONE


class TestNarratorThroughPollOnce:
    def test_poll_once_forwards_the_narrator_end_to_end(self) -> None:
        sender = RecordingSender()
        chat_id = 890
        interviewer = FakeInterviewer([], animal="Red Fox")
        narrator = FakeNarrator(audio_bytes=b"OggSvoice")
        _drive_to_four_answers(sender, chat_id, interviewer)

        client = _batch_client(
            {"ok": True, "result": [_text_raw(60, chat_id, "an answer")]}
        )
        next_offset = poll_once(
            TOKEN,
            client,
            offset=60,
            sender=sender,
            interviewer=interviewer,
            converter=FakeConverter(image_bytes=b"PORTRAIT"),
            scripter=FakeScripter(script=SCRIPT),
            narrator=narrator,
        )

        assert next_offset == 61
        assert narrator.calls == [SCRIPT]
        assert len(sender.voices) == 1
        assert sender.voices[0].chat_id == chat_id
        assert get_store().get(chat_id).phase is SessionPhase.DONE

    def test_tts_failure_never_escapes_the_poll_loop(self, caplog) -> None:
        """The poll loop must never see the TTS exception: the offset still
        advances, the friendly reply goes out, and the failure is logged."""
        sender = RecordingSender()
        chat_id = 891
        interviewer = FakeInterviewer([], animal="Red Fox")
        _drive_to_four_answers(sender, chat_id, interviewer)

        client = _batch_client(
            {"ok": True, "result": [_text_raw(61, chat_id, "an answer")]}
        )
        with caplog.at_level(logging.INFO, logger="telegram_documentaries"):
            next_offset = poll_once(
                TOKEN,
                client,
                offset=61,
                sender=sender,
                interviewer=interviewer,
                converter=FakeConverter(),
                scripter=FakeScripter(script=SCRIPT),
                narrator=ExplodingNarrator(),
            )

        assert next_offset == 62, "the poll loop survived and advanced"
        assert any("event=narrator_error" in r.message for r in caplog.records)
        assert get_store().get(chat_id).phase is SessionPhase.DONE


def _run_until_keyboard_interrupt(monkeypatch, caplog, bot_token) -> None:
    """Drive `run()` through one failed poll, then stop it deterministically."""
    attempts = {"n": 0}

    def flaky_poll_once(**kwargs) -> int:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("connection refused")
        raise KeyboardInterrupt

    monkeypatch.setattr("app.gateway.poll_once", flaky_poll_once)
    with (
        caplog.at_level(logging.INFO, logger="telegram_documentaries"),
        pytest.raises(KeyboardInterrupt),
    ):
        run(bot_token)


class TestRunNarratorWiring:
    def test_gateway_started_reports_the_narrator_model_and_wiring(
        self, monkeypatch, caplog, bot_token
    ) -> None:
        class _DummyNarrator:
            def synthesise(self, script: str) -> bytes:
                return b"OggS"

        # Injected so the unit test never builds a real genai client (offline).
        monkeypatch.setattr(
            "app.gateway.GeminiTtsNarrator", lambda: _DummyNarrator()
        )

        _run_until_keyboard_interrupt(monkeypatch, caplog, bot_token)

        lines = [
            r.message
            for r in caplog.records
            if "event=gateway_started" in r.message
        ]
        assert lines, "startup must log the wiring line"
        assert "narrator_model=gemini-3.1-flash-tts-preview" in lines[0]
        assert "narrator_wired=True" in lines[0]
        assert bot_token not in caplog.text, "the bot token must never be logged"

    def test_run_forwards_the_narrator_into_poll_once(
        self, monkeypatch, bot_token
    ) -> None:
        """The Narrator constructed at startup must actually reach the poll
        loop — a wiring that dies at the `run()`/`poll_once` boundary would
        silently disable the voice stage in production while every other
        test stays green."""

        class _DummyNarrator:
            def synthesise(self, script: str) -> bytes:
                return b"OggS"

        monkeypatch.setattr(
            "app.gateway.GeminiTtsNarrator", lambda: _DummyNarrator()
        )
        captured: dict = {}

        def capturing_poll_once(**kwargs) -> int:
            captured.update(kwargs)
            raise KeyboardInterrupt

        monkeypatch.setattr("app.gateway.poll_once", capturing_poll_once)
        with pytest.raises(KeyboardInterrupt):
            run(bot_token)

        assert captured.get("narrator") is not None, (
            "run() must pass the constructed Narrator into poll_once"
        )
        assert hasattr(captured["narrator"], "synthesise")

    def test_narrator_init_failure_reports_not_wired_and_keeps_polling(
        self, monkeypatch, caplog, bot_token
    ) -> None:
        def _boom() -> object:
            raise RuntimeError("genai client unavailable")

        monkeypatch.setattr("app.gateway.GeminiTtsNarrator", _boom)

        _run_until_keyboard_interrupt(monkeypatch, caplog, bot_token)

        assert any(
            "event=narrator_init_failed" in r.message for r in caplog.records
        )
        lines = [
            r.message
            for r in caplog.records
            if "event=gateway_started" in r.message
        ]
        assert lines and "narrator_wired=False" in lines[0]
        assert bot_token not in caplog.text
