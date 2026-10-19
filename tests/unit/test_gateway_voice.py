"""`TelegramSender.send_voice` — the voice-note transport (Phase 6).

Mirrors the ``send_photo`` tests: multipart ``sendVoice`` with a
``voice.ogg`` part of type ``audio/ogg`` (TECH.md: OGG/Opus voice notes,
never ``sendAudio``/MP3), ``ok:false``/non-JSON bodies reported as status +
Telegram's ``description`` — and, above all, the bot token never reaching a
log line or exception message (MISSION non-negotiable).
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.gateway import TelegramSender
from app.narrator import OutboundVoice

#: A well-formed token stand-in in Telegram's `<digits>:<token>` format.
TEST_TOKEN = "123456789:AAExampleTokenForTestsOnlyNotARealSecret"


def _client_returning(payload: dict | str, status: int = 200) -> httpx.Client:
    """An httpx client whose every request answers with `payload`.

    A `dict` is sent as JSON (what Telegram really returns); a `str` is sent
    as a raw body, for simulating HTML error pages.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, dict):
            return httpx.Response(status, json=payload, request=request)
        return httpx.Response(status, text=payload, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _recording_client(recorder: dict) -> httpx.Client:
    """A client that captures the request and answers `ok:true`."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder["path"] = request.url.path
        recorder["content_type"] = request.headers.get("content-type", "")
        recorder["body"] = request.read()
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1}}, request=request
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestTelegramSenderSendVoice:
    def test_ok_true_returns_without_raising(self) -> None:
        client = _client_returning({"ok": True, "result": {"message_id": 1}})
        sender = TelegramSender(token="123:TOKEN", client=client)

        sender.send_voice(OutboundVoice(chat_id=555, audio_bytes=b"OggS"))  # no raise

    def test_posts_multipart_to_send_voice(self) -> None:
        recorder: dict = {}
        sender = TelegramSender(token="123:TOKEN", client=_recording_client(recorder))

        sender.send_voice(
            OutboundVoice(chat_id=555, audio_bytes=b"OggSdata")
        )

        assert recorder["path"].endswith("/sendVoice"), recorder["path"]
        assert "multipart/form-data" in recorder["content_type"]
        assert b"OggSdata" in recorder["body"]
        assert b"555" in recorder["body"], "chat_id must travel with the request"

    def test_voice_part_is_named_voice_dot_ogg_with_audio_ogg(self) -> None:
        recorder: dict = {}
        sender = TelegramSender(token="123:TOKEN", client=_recording_client(recorder))

        sender.send_voice(OutboundVoice(chat_id=555, audio_bytes=b"OggSdata"))

        body = recorder["body"]
        assert b'name="voice"' in body, "the multipart field must be `voice`"
        assert b'filename="voice.ogg"' in body
        assert b"audio/ogg" in body, "the part must declare audio/ogg"
        assert b"sendAudio" not in recorder["path"].encode()

    def test_ok_false_raises_with_telegrams_description(self) -> None:
        client = _client_returning(
            {"ok": False, "description": "Bad Request: wrong file format"}
        )
        sender = TelegramSender(token="123:TOKEN", client=client)

        with pytest.raises(RuntimeError, match="sendVoice failed") as exc_info:
            sender.send_voice(OutboundVoice(chat_id=555, audio_bytes=b"OggS"))

        assert "wrong file format" in str(exc_info.value)

    def test_non_json_body_raises_runtime_error_and_is_logged(self, caplog) -> None:
        client = _client_returning("<html>502 Bad Gateway</html>", status=502)
        sender = TelegramSender(token="123:TOKEN", client=client)

        with (
            caplog.at_level(logging.ERROR, logger="telegram_documentaries"),
            pytest.raises(RuntimeError, match="sendVoice failed"),
        ):
            sender.send_voice(OutboundVoice(chat_id=555, audio_bytes=b"OggS"))

        assert any(
            "event=send_failed" in r.message and "502" in r.message
            for r in caplog.records
        )

    def test_send_voice_failure_never_leaks_the_token(
        self, bot_token, caplog
    ) -> None:
        """MISSION: the request URL carries the token — it must appear in
        neither the log records nor the exception message."""
        client = _client_returning(
            {"ok": False, "description": "Unauthorized"}, status=401
        )
        sender = TelegramSender(token=bot_token, client=client)

        with (
            caplog.at_level(logging.DEBUG, logger="telegram_documentaries"),
            pytest.raises(RuntimeError) as exc_info,
        ):
            sender.send_voice(OutboundVoice(chat_id=555, audio_bytes=b"OggS"))

        assert "sendVoice failed" in str(exc_info.value)
        assert bot_token not in str(exc_info.value)
        assert bot_token not in caplog.text, "the bot token must never be logged"
