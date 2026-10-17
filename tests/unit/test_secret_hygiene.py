"""MISSION non-negotiable: never leak TELEGRAM_BOT_TOKEN.

Every Telegram request URL carries the bot token
(`https://api.telegram.org/bot<token>/...`). `httpx.HTTPStatusError` (from
`raise_for_status()`) embeds the full URL in `str(exc)`, and those strings
reach `logger.exception(...)` — leaking the secret into the logs.

SPECS/MISSION.md: "Never hardcode or commit secrets ... read from the
environment only." These tests lock in that failures are reported as
status + Telegram's `description`, with the token absent from every emitted
log record and every raised exception message.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.bouncer import TelegramPhotoFetcher, bounce
from app.gateway import TelegramSender, poll_once
from app.models import OutboundMessage
from tests.conftest import FakeClassifier, RecordingSender, human_verdict


def _client_returning(status: int, payload: dict | str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, dict):
            return httpx.Response(status, json=payload, request=request)
        return httpx.Response(status, text=payload, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _photo_update_dict() -> dict:
    return {
        "update_id": 42,
        "message": {
            "message_id": 7,
            "date": 1759750000,
            "chat": {"id": 6682234839, "type": "private"},
            "photo": [
                {"file_id": "small", "file_unique_id": "u1", "width": 90, "height": 90},
                {"file_id": "largest", "file_unique_id": "u4",
                 "width": 900, "height": 900},
            ],
        },
    }


def _file_download_client(status: int, payload: dict | str) -> httpx.Client:
    """A client where `getFile` SUCCEEDS and the actual file download fails.

    The download URL (`/bot<token>/file/<path>`) is the one that used to call
    `raise_for_status()` — a token-bearing URL.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/file_abc.jpg"}},
                request=request,
            )
        if isinstance(payload, dict):
            return httpx.Response(status, json=payload, request=request)
        return httpx.Response(status, text=payload, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestGetUpdatesFailureNeverLeaksTheToken:
    def test_http_error_logs_status_and_description_without_raising(
        self, bot_token, caplog
    ) -> None:
        client = _client_returning(
            401, {"ok": False, "description": "Unauthorized"}
        )

        with caplog.at_level(logging.DEBUG, logger="telegram_documentaries"):
            next_offset = poll_once(bot_token, client, offset=7, sender=RecordingSender())

        assert next_offset == 7, "a failed fetch processes nothing; offset stands"
        assert any(
            "event=getUpdates_failed" in r.message and "Unauthorized" in r.message
            for r in caplog.records
        ), "status + Telegram's description must be logged"
        assert bot_token not in caplog.text, "the bot token must never be logged"

    def test_http_error_with_a_non_json_body_does_not_leak(
        self, bot_token, caplog
    ) -> None:
        client = _client_returning(502, "<html>Bad Gateway</html>")

        with caplog.at_level(logging.DEBUG, logger="telegram_documentaries"):
            next_offset = poll_once(bot_token, client, offset=7, sender=RecordingSender())

        assert next_offset == 7
        assert any("event=getUpdates_failed" in r.message for r in caplog.records)
        assert bot_token not in caplog.text


class TestFileDownloadFailureNeverLeaksTheToken:
    def test_fetcher_raises_a_token_free_error(self, bot_token) -> None:
        client = _file_download_client(403, {"ok": False, "description": "Not Found"})
        fetcher = TelegramPhotoFetcher(token=bot_token, client=client)

        with pytest.raises(RuntimeError) as exc_info:
            fetcher.fetch("file-abc")

        assert bot_token not in str(exc_info.value), (
            "the exception message must not carry the token-bearing URL"
        )
        assert "403" in str(exc_info.value), "the status must be reported"

    def test_fetcher_raises_on_a_non_json_error_body(self, bot_token) -> None:
        """An HTML error page from a proxy must degrade to a sane error too."""
        client = _file_download_client(502, "<html>Bad Gateway</html>")
        fetcher = TelegramPhotoFetcher(token=bot_token, client=client)

        with pytest.raises(RuntimeError) as exc_info:
            fetcher.fetch("file-abc")

        assert bot_token not in str(exc_info.value)
        assert "502" in str(exc_info.value)

    def test_bounce_logs_the_failure_without_the_token(self, bot_token, caplog) -> None:
        """The full path: bounce() catches the fetch failure and logs it with
        logger.exception — the traceback must not contain the token."""
        from app.models import TelegramUpdate

        client = _file_download_client(403, {"ok": False, "description": "Not Found"})
        fetcher = TelegramPhotoFetcher(token=bot_token, client=client)

        with caplog.at_level(logging.DEBUG, logger="telegram_documentaries"):
            decision = bounce(
                TelegramUpdate.model_validate(_photo_update_dict()),
                fetcher=fetcher,
                classifier=FakeClassifier(human_verdict()),
            )

        assert decision.error is True, "a failed download is an error, not a reject"
        assert decision.reply is None, "a failed download must not reject the user"
        assert any("event=photo_fetch_failed" in r.message for r in caplog.records)
        assert bot_token not in caplog.text, "the bot token must never be logged"


class TestSendMessageFailureNeverLeaksTheToken:
    def test_ok_false_failure_is_logged_without_the_token(self, bot_token, caplog) -> None:
        client = _client_returning(
            401, {"ok": False, "description": "Unauthorized"}
        )
        sender = TelegramSender(token=bot_token, client=client)

        with caplog.at_level(logging.DEBUG, logger="telegram_documentaries"), \
                pytest.raises(RuntimeError) as exc_info:
            sender.send(OutboundMessage(chat_id=555, text="hey mate!"))

        assert bot_token not in str(exc_info.value)
        assert bot_token not in caplog.text, "the bot token must never be logged"

    def test_non_json_failure_is_logged_without_the_token(self, bot_token, caplog) -> None:
        client = _client_returning(502, "<html>Bad Gateway</html>")
        sender = TelegramSender(token=bot_token, client=client)

        with caplog.at_level(logging.DEBUG, logger="telegram_documentaries"), \
                pytest.raises(RuntimeError) as exc_info:
            sender.send(OutboundMessage(chat_id=555, text="hey mate!"))

        assert bot_token not in str(exc_info.value)
        assert bot_token not in caplog.text
