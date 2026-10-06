"""The gateway's reply behaviour.

User requirement: "When I send a message to the Telegram bot on my phone the
bot should reply with a hardcoded message such as 'hey mate!' (no API calls
just yet). Every time we send a message, the same hardcoded message should be
sent back."
"""

from __future__ import annotations

import pytest

from app.gateway import ReplyPolicy, handle_update
from app.models import TelegramUpdate


def _update(text: str | None = "hello", chat_id: int = 555) -> TelegramUpdate:
    message: dict = {
        "message_id": 10,
        "date": 1759750000,
        "chat": {"id": chat_id, "type": "private"},
    }
    if text is not None:
        message["text"] = text
    return TelegramUpdate.model_validate({"update_id": 1, "message": message})


class TestHardcodedReply:
    def test_replies_with_the_hardcoded_message(self) -> None:
        outbound = handle_update(_update("hello there"))

        assert outbound is not None
        assert outbound.text == "hey mate!"
        assert outbound.chat_id == 555

    def test_reply_is_identical_every_time(self) -> None:
        """'Every time we send a message, the same hardcoded message back.'"""
        texts = [handle_update(_update(f"msg {i}")).text for i in range(5)]  # type: ignore[union-attr]

        assert texts == ["hey mate!"] * 5

    def test_reply_does_not_echo_the_users_input(self) -> None:
        outbound = handle_update(_update("a completely different sentence"))

        assert outbound is not None
        assert outbound.text == "hey mate!"
        assert "different" not in outbound.text

    @pytest.mark.parametrize(
        "text",
        ["", " ", "hey mate!", "úñîçödé 🦁", "line\nbreak", "x" * 4096],
    )
    def test_reply_is_the_same_for_any_user_text(self, text: str) -> None:
        outbound = handle_update(_update(text))

        assert outbound is not None
        assert outbound.text == "hey mate!"

    def test_reply_targets_the_chats_own_id(self) -> None:
        outbound = handle_update(_update(chat_id=999888777))

        assert outbound is not None
        assert outbound.chat_id == 999888777


class TestWrongPayloadAtWrongStage:
    def test_non_text_message_gets_no_reply(self) -> None:
        """A photo at the AWAITING_PHOTO stage is the Bouncer's job (Phase 2),
        not the gateway's. Phase 1 must not crash on it."""
        update = TelegramUpdate.model_validate(
            {
                "update_id": 2,
                "message": {
                    "message_id": 11,
                    "date": 1,
                    "chat": {"id": 555, "type": "private"},
                    "photo": [{"file_id": "a", "file_unique_id": "b", "width": 1, "height": 1}],
                },
            }
        )

        assert handle_update(update) is None

    def test_update_without_a_message_gets_no_reply(self) -> None:
        update = TelegramUpdate.model_validate({"update_id": 7})
        assert handle_update(update) is None

    def test_never_raises_on_a_message_with_no_text_field(self) -> None:
        handle_update(_update(text=None))  # must not raise


class TestSessionRegistration:
    def test_incoming_chat_is_registered_with_the_shared_state_driver(self) -> None:
        from app.state import SessionPhase, get_store

        get_store().reset(555)
        handle_update(_update(chat_id=555))

        session = get_store().get_or_create(555)
        assert session.chat_id == 555
        assert session.phase is SessionPhase.AWAITING_PHOTO

    def test_registration_is_per_chat(self) -> None:
        from app.state import get_store

        get_store().reset(101)
        get_store().reset(202)

        handle_update(_update(chat_id=101))
        handle_update(_update(chat_id=202))

        assert {101, 202}.issubset(get_store().chat_ids)


class TestRestartCommand:
    @pytest.mark.parametrize("command", ["/start", "/restart"])
    def test_restart_commands_purge_state_and_still_reply(self, command: str) -> None:
        """MISSION: /start and /restart purge state without restarting."""
        from app.state import get_store

        get_store().reset(555)
        get_store().get_or_create(555)

        outbound = handle_update(_update(command, chat_id=555))

        assert outbound is not None
        assert outbound.text == "hey mate!"
        assert 555 not in get_store().chat_ids


class TestReplyPolicy:
    def test_policy_exposes_the_hardcoded_text(self) -> None:
        assert ReplyPolicy.HARDCODED_REPLY == "hey mate!"


class TestReplyIsAuditable:
    def test_a_sent_reply_is_logged_at_info(self, caplog) -> None:
        """A reply is a user-visible side effect; operators must be able to
        audit what was sent without enabling DEBUG (SPECS/TECH.md:
        comprehensive structured logging)."""
        import logging

        with caplog.at_level(logging.INFO, logger="telegram_documentaries.gateway"):
            handle_update(_update("anything"))

        assert any(
            "event=replied" in r.message and r.levelno >= logging.INFO
            for r in caplog.records
        ), "reply must produce an INFO-or-higher log record"

    def test_non_text_skip_is_visible_too(self, caplog) -> None:
        """Payloads that are neither text nor photo (e.g. a sticker) are
        logged rather than silently dropped."""
        import logging

        update = TelegramUpdate.model_validate(
            {
                "update_id": 2,
                "message": {
                    "message_id": 11,
                    "date": 1,
                    "chat": {"id": 555, "type": "private"},
                    "sticker": {"file_id": "s", "file_unique_id": "u",
                                "width": 512, "height": 512, "type": "regular"},
                },
            }
        )

        with caplog.at_level(logging.INFO, logger="telegram_documentaries.gateway"):
            handle_update(update)

        assert any("reason=non_text" in r.message for r in caplog.records)

    def test_photo_without_a_wired_bouncer_is_logged_not_crashed(self, caplog) -> None:
        """If the Bouncer failed to initialise, photos must degrade to a
        logged skip instead of raising into the user's flow."""
        import logging

        update = TelegramUpdate.model_validate(
            {
                "update_id": 3,
                "message": {
                    "message_id": 12,
                    "date": 1,
                    "chat": {"id": 555, "type": "private"},
                    "photo": [{"file_id": "a", "file_unique_id": "b",
                               "width": 90, "height": 90}],
                },
            }
        )

        with caplog.at_level(logging.WARNING, logger="telegram_documentaries.gateway"):
            reply = handle_update(update)  # no fetcher/classifier passed

        assert reply is None
        assert any("reason=bouncer_not_wired" in r.message for r in caplog.records)
