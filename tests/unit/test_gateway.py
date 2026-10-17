"""The gateway's phase-aware reply behaviour.

Phase 1's hardcoded reply has been superseded by the pipeline's real
conversation (SPECS/2026-10-16-interviewer, decision D4): text with no
session now prompts for a photo, and the reply is driven by the chat's
phase. Phase 1's routing guarantees (session registration, `/start` purge,
batch isolation, quiet failures) still hold and are asserted below.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.bouncer import REJECTION_REPLY
from app.gateway import (
    TelegramSender,
    handle_update,
    poll_once,
    run,
)
from app.interviewer import (
    CONFIRMATION_PROMPT,
    PHOTO_PROMPT,
    PORTRAIT_PENDING_REPLY,
    WRONG_PAYLOAD_PROMPT,
)
from app.models import OutboundMessage, OutboundPhoto, TelegramUpdate
from app.state import SessionPhase, get_store
from tests.conftest import (
    ExplodingClassifier,
    ExplodingConverter,
    ExplodingPhotoFetcher,
    FakeClassifier,
    FakeConverter,
    FakeInterviewer,
    FakePhotoFetcher,
    FlakyPhotoFetcher,
    RecordingSender,
    human_verdict,
    no_human_verdict,
)


def _update(text: str | None = "hello", chat_id: int = 555) -> TelegramUpdate:
    message: dict = {
        "message_id": 10,
        "date": 1759750000,
        "chat": {"id": chat_id, "type": "private"},
    }
    if text is not None:
        message["text"] = text
    return TelegramUpdate.model_validate({"update_id": 1, "message": message})


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


class TestTextWithoutSession:
    """D4: text from a chat with no interview in progress prompts for a photo."""

    def test_prompts_for_a_photo(self) -> None:
        outbound = handle_update(_update("hello there"))

        assert outbound is not None
        assert outbound.text == PHOTO_PROMPT
        assert outbound.chat_id == 555

    def test_prompt_is_identical_every_time(self) -> None:
        texts = [handle_update(_update(f"msg {i}")).text for i in range(5)]  # type: ignore[union-attr]

        assert texts == [PHOTO_PROMPT] * 5

    def test_prompt_does_not_echo_the_users_input(self) -> None:
        outbound = handle_update(_update("a completely different sentence"))

        assert outbound is not None
        assert outbound.text == PHOTO_PROMPT
        assert "different" not in outbound.text

    @pytest.mark.parametrize(
        "text",
        ["", " ", "hey mate!", "úñîçödé 🦁", "line\nbreak", "x" * 4096],
    )
    def test_prompt_is_the_same_for_any_user_text(self, text: str) -> None:
        outbound = handle_update(_update(text))

        assert outbound is not None
        assert outbound.text == PHOTO_PROMPT

    def test_prompt_targets_the_chats_own_id(self) -> None:
        outbound = handle_update(_update(chat_id=999888777))

        assert outbound is not None
        assert outbound.chat_id == 999888777

    def test_no_session_is_created_by_bare_text(self) -> None:
        """A chat that has not sent a photo is not "in" anything; registering
        it would make the store lie about who is waiting."""
        handle_update(_update(chat_id=34234))

        assert 34234 not in get_store().chat_ids


class TestWrongPayloadAtWrongStage:
    def test_non_text_message_gets_no_reply(self) -> None:
        """A photo at the AWAITING_PHOTO stage is the Bouncer's job (Phase 2),
        not the gateway's. Phase 1 must not crash on it — and, per the TECH
        error policy, must never leave the user in silence either."""
        from app.gateway import BouncerFallbackPolicy

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

        reply = handle_update(update)  # must not raise

        assert reply is not None
        assert reply.text == BouncerFallbackPolicy.ERROR_REPLY
        assert reply.chat_id == 555

    def test_update_without_a_message_gets_no_reply(self) -> None:
        update = TelegramUpdate.model_validate({"update_id": 7})
        assert handle_update(update) is None

    def test_never_raises_on_a_message_with_no_text_field(self) -> None:
        handle_update(_update(text=None))  # must not raise


class TestSessionRegistration:
    def test_an_admitted_photo_registers_the_chat(self) -> None:
        """A chat is only "in" the pipeline once the Bouncer admits its photo."""
        store = get_store()

        handle_update(
            _photo_update(555),
            RecordingSender(),
            fetcher=FakePhotoFetcher(),
            classifier=FakeClassifier(human_verdict()),
            interviewer=FakeInterviewer(),
        )

        session = store.get(555)
        assert session is not None
        assert session.chat_id == 555
        assert session.phase is SessionPhase.AWAITING_CONFIRMATION

    def test_registration_is_per_chat(self) -> None:
        store = get_store()

        for chat_id in (101, 202):
            handle_update(
                _photo_update(chat_id),
                RecordingSender(),
                fetcher=FakePhotoFetcher(),
                classifier=FakeClassifier(human_verdict()),
                interviewer=FakeInterviewer(),
            )

        assert {101, 202}.issubset(store.chat_ids)


class TestRestartCommand:
    @pytest.mark.parametrize("command", ["/start", "/restart"])
    def test_restart_commands_purge_state_and_still_reply(self, command: str) -> None:
        """MISSION: /start and /restart purge state without restarting."""
        store = get_store()
        store.get_or_create(555)

        outbound = handle_update(_update(command, chat_id=555))

        assert outbound is not None
        assert outbound.text == PHOTO_PROMPT
        assert 555 not in store.chat_ids

    @pytest.mark.parametrize("command", ["/start", "/restart"])
    def test_purges_a_mid_interview_dossier(self, command: str) -> None:
        """A reset mid-interview must wipe the dossier, not merely move on."""
        store = get_store()
        interviewer = FakeInterviewer()
        handle_update(
            _photo_update(555),
            RecordingSender(),
            fetcher=FakePhotoFetcher(),
            classifier=FakeClassifier(human_verdict()),
            interviewer=interviewer,
        )
        handle_update(_update("yes", chat_id=555), interviewer=interviewer)
        assert store.get(555) is not None and store.get(555).dossier is not None

        handle_update(_update(command, chat_id=555))

        assert store.get(555) is None


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
        logged, friendly reply instead of raising into the user's flow —
        and never leave the user in silence (SPECS/TECH.md error policy)."""
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

        from app.gateway import BouncerFallbackPolicy

        assert reply is not None, "a photo must never be met with silence"
        assert reply.chat_id == 555
        assert reply.text == BouncerFallbackPolicy.ERROR_REPLY
        assert any("reason=bouncer_not_wired" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# /start@BotName and "/start args" must reset like "/start" (NB2)
# ---------------------------------------------------------------------------


class TestResetCommandMatching:
    @pytest.mark.parametrize(
        "text", ["/start@MyBot", "/start@MyBot hello there", "/restart@MyBot"]
    )
    def test_command_with_a_bot_mention_purges_state(self, text: str) -> None:
        """Telegram clients send `/start@MyBot` in group chats; matching on
        the bare `text.strip()` misses it."""
        from app.state import get_store

        get_store().get_or_create(555)

        outbound = handle_update(_update(text, chat_id=555))

        assert outbound is not None
        assert outbound.text == PHOTO_PROMPT
        assert 555 not in get_store().chat_ids, f"{text!r} must reset the session"

    @pytest.mark.parametrize("text", ["/start some args", "/restart arg1 arg2"])
    def test_command_with_arguments_purges_state(self, text: str) -> None:
        from app.state import get_store

        get_store().get_or_create(555)

        outbound = handle_update(_update(text, chat_id=555))

        assert outbound is not None
        assert 555 not in get_store().chat_ids, f"{text!r} must reset the session"

    @pytest.mark.parametrize("text", ["please /start now", "restart", "/startup"])
    def test_text_that_only_mentions_a_command_does_not_purge(self, text: str) -> None:
        from app.state import get_store

        get_store().get_or_create(555)

        handle_update(_update(text, chat_id=555))

        assert 555 in get_store().chat_ids, f"{text!r} is not a reset command"

    def test_reset_command_is_matched_on_the_first_token_only(self) -> None:
        """/start@MyBot extra words — only the first token decides."""
        from app.state import get_store

        get_store().get_or_create(555)
        handle_update(_update("/start@MyBot with arguments", chat_id=555))
        assert 555 not in get_store().chat_ids


# ---------------------------------------------------------------------------
# B2: a Bouncer failure must never leave the user in silence
# ---------------------------------------------------------------------------


class TestPhotoBouncerDegradesGracefully:
    def test_bouncer_failure_still_replies_to_the_user(self) -> None:
        """TECH error policy: on a validated user's conversation path, catch,
        log loudly, and degrade gracefully with a friendly message."""
        sender = RecordingSender()

        reply = handle_update(
            _photo_update(777),
            sender,
            fetcher=ExplodingPhotoFetcher(),
            classifier=FakeClassifier(human_verdict()),
        )

        from app.gateway import BouncerFallbackPolicy

        assert reply is not None, "a failed Bouncer must not be silent"
        assert reply.chat_id == 777
        assert reply.text == BouncerFallbackPolicy.ERROR_REPLY
        assert sender.sent == [reply], "the friendly reply must actually be sent"

    def test_bouncer_failure_leaves_session_state_untouched(self) -> None:
        from app.state import SessionPhase, get_store

        store = get_store()
        store.get_or_create(777).phase = SessionPhase.INTERVIEWING

        handle_update(
            _photo_update(777),
            RecordingSender(),
            fetcher=ExplodingPhotoFetcher(),
            classifier=FakeClassifier(human_verdict()),
        )

        session = store.get(777)
        assert session is not None, "a Bouncer error must not purge the session"
        assert session.phase is SessionPhase.INTERVIEWING

    def test_bouncer_classification_failure_still_replies(self) -> None:
        """The classifier blowing up (Gemini 500) is the same degrade path."""
        sender = RecordingSender()

        reply = handle_update(
            _photo_update(778),
            sender,
            fetcher=FakePhotoFetcher(),
            classifier=ExplodingClassifier(),
        )

        from app.gateway import BouncerFallbackPolicy

        assert reply is not None
        assert reply.text == BouncerFallbackPolicy.ERROR_REPLY
        assert sender.sent == [reply]

    def test_not_wired_bouncer_still_replies_to_the_user(self) -> None:
        """`run()` degrades to classifier=None when ADK fails to initialise;
        photos must get a friendly reply, not nothing."""
        sender = RecordingSender()

        reply = handle_update(_photo_update(779), sender)

        from app.gateway import BouncerFallbackPolicy

        assert reply is not None, "a photo with no Bouncer must not be silent"
        assert reply.chat_id == 779
        assert reply.text == BouncerFallbackPolicy.ERROR_REPLY
        assert sender.sent == [reply]

    def test_not_wired_bouncer_creates_no_session_state(self) -> None:
        from app.state import SessionPhase, get_store

        store = get_store()
        store.get_or_create(780).phase = SessionPhase.INTERVIEWING

        handle_update(_photo_update(780))

        session = store.get(780)
        assert session is not None, "the degrade path must not touch state"
        assert session.phase is SessionPhase.INTERVIEWING


# ---------------------------------------------------------------------------
# NB6: the headline path end-to-end — photo in, reply out, via a sender
# ---------------------------------------------------------------------------


class TestWiredBouncerEndToEnd:
    def test_admitted_photo_gets_the_confirmation_reply_sent(self) -> None:
        from app.state import get_store

        store = get_store()
        sender = RecordingSender()

        reply = handle_update(
            _photo_update(900),
            sender,
            fetcher=FakePhotoFetcher(),
            classifier=FakeClassifier(human_verdict()),
            interviewer=FakeInterviewer(),
        )

        assert reply is not None
        assert reply.text == CONFIRMATION_PROMPT
        assert reply.chat_id == 900
        assert sender.sent == [reply], "exactly one reply goes out"
        session = store.get(900)
        assert session is not None, "admission continues the flow"
        assert session.phase is SessionPhase.AWAITING_CONFIRMATION
        assert session.dossier is not None, "a fresh dossier is started"

    def test_admitted_photo_without_an_interviewer_degrades_friendly(self) -> None:
        """A failed Interviewer init must not leave the admitted user in
        silence (TECH error policy)."""
        from app.gateway import BouncerFallbackPolicy

        sender = RecordingSender()

        reply = handle_update(
            _photo_update(902),
            sender,
            fetcher=FakePhotoFetcher(),
            classifier=FakeClassifier(human_verdict()),
            interviewer=None,
        )

        assert reply is not None
        assert reply.text == BouncerFallbackPolicy.ERROR_REPLY
        assert sender.sent == [reply]

    def test_rejected_photo_gets_the_rejection_reply_sent_and_state_reset(self) -> None:
        from app.state import get_store

        store = get_store()
        store.get_or_create(901)
        sender = RecordingSender()

        reply = handle_update(
            _photo_update(901),
            sender,
            fetcher=FakePhotoFetcher(),
            classifier=FakeClassifier(no_human_verdict()),
            interviewer=FakeInterviewer(),
        )

        assert reply is not None
        assert reply.text == REJECTION_REPLY
        assert reply.chat_id == 901
        assert sender.sent == [reply], "exactly one rejection goes out"
        assert store.get(901) is None, "rejection purges that chat's state"


# ---------------------------------------------------------------------------
# Phase 3 routing: confirmation, answering, the DONE phase, wrong payloads
# ---------------------------------------------------------------------------


class TestInterviewRouting:
    def _admit(self, sender, chat_id: int, interviewer: FakeInterviewer) -> None:
        handle_update(
            _photo_update(chat_id),
            sender,
            fetcher=FakePhotoFetcher(),
            classifier=FakeClassifier(human_verdict()),
            interviewer=interviewer,
        )

    def test_yes_starts_the_interview_and_sends_the_first_question(self) -> None:
        store = get_store()
        sender = RecordingSender()
        interviewer = FakeInterviewer()
        self._admit(sender, 600, interviewer)
        sender.sent.clear()

        reply = handle_update(
            _update("yes", chat_id=600), sender, interviewer=interviewer
        )

        assert reply is not None
        assert reply.text == "Question 1?"
        assert sender.sent == [reply]
        assert store.get(600).phase is SessionPhase.INTERVIEWING
        assert store.get(600).pending_question == "Question 1?"

    @pytest.mark.parametrize("text", ["maybe", "nope", "Y E S", "", "   "])
    def test_anything_but_yes_re_asks_the_confirmation(self, text: str) -> None:
        store = get_store()
        sender = RecordingSender()
        interviewer = FakeInterviewer()
        self._admit(sender, 601, interviewer)
        sender.sent.clear()

        reply = handle_update(_update(text, chat_id=601), sender, interviewer=interviewer)

        assert reply is not None
        assert reply.text == CONFIRMATION_PROMPT
        assert store.get(601).phase is SessionPhase.AWAITING_CONFIRMATION
        assert interviewer.next_turn_calls == [], "no interview before YES"

    @pytest.mark.parametrize("text", ["yes", "YES", "Yes", " y ", "Y", "y"])
    def test_confirmation_is_case_insensitive_and_trimmed(self, text: str) -> None:
        sender = RecordingSender()
        interviewer = FakeInterviewer()
        chat_id = 602 + len(text)  # unique chat per case
        self._admit(sender, chat_id, interviewer)

        reply = handle_update(_update(text, chat_id=chat_id), sender, interviewer=interviewer)

        assert reply is not None
        assert get_store().get(chat_id).phase is SessionPhase.INTERVIEWING

    def test_an_answer_is_recorded_and_the_next_question_asked(self) -> None:
        store = get_store()
        sender = RecordingSender()
        interviewer = FakeInterviewer()
        self._admit(sender, 603, interviewer)
        handle_update(_update("yes", chat_id=603), sender, interviewer=interviewer)

        reply = handle_update(
            _update("I like long walks", chat_id=603), sender, interviewer=interviewer
        )

        assert reply is not None
        assert reply.text == "Question 2?"
        dossier = store.get(603).dossier
        assert dossier is not None
        assert dossier.asked == 1
        assert dossier.answers[0].answer == "I like long walks"
        assert store.get(603).pending_question == "Question 2?"

    def test_done_phase_points_ahead_to_the_portrait(self) -> None:
        store = get_store()
        sender = RecordingSender()
        interviewer = FakeInterviewer([], animal="Red Fox")
        self._admit(sender, 604, interviewer)
        handle_update(_update("yes", chat_id=604), sender, interviewer=interviewer)
        # Five answers is the minimum interview length.
        for _ in range(5):
            handle_update(_update("an answer", chat_id=604), sender, interviewer=interviewer)
        assert store.get(604).phase is SessionPhase.DONE

        reply = handle_update(_update("what now?", chat_id=604), sender, interviewer=interviewer)

        assert reply is not None
        assert reply.text == PORTRAIT_PENDING_REPLY

    def test_a_stray_photo_mid_interview_nudges_instead_of_silence(self) -> None:
        store = get_store()
        sender = RecordingSender()
        interviewer = FakeInterviewer()
        self._admit(sender, 605, interviewer)
        handle_update(_update("yes", chat_id=605), sender, interviewer=interviewer)
        sender.sent.clear()

        reply = handle_update(_photo_update(605, update_id=99), sender, interviewer=interviewer)

        assert reply is not None
        assert reply.text == WRONG_PAYLOAD_PROMPT
        assert sender.sent == [reply]
        assert store.get(605).pending_question == "Question 1?", (
            "the open question must survive a wrong payload"
        )

    def test_a_stray_sticker_mid_confirmation_nudges(self) -> None:
        sender = RecordingSender()
        interviewer = FakeInterviewer()
        self._admit(sender, 606, interviewer)

        sticker = TelegramUpdate.model_validate(
            {
                "update_id": 50,
                "message": {
                    "message_id": 50,
                    "date": 1,
                    "chat": {"id": 606, "type": "private"},
                    "sticker": {"file_id": "s", "file_unique_id": "u",
                                "width": 512, "height": 512, "type": "regular"},
                },
            }
        )

        reply = handle_update(sticker, sender, interviewer=interviewer)

        assert reply is not None
        assert reply.text == WRONG_PAYLOAD_PROMPT

    def test_text_mid_interview_without_a_wired_interviewer_degrades(self) -> None:
        from app.gateway import BouncerFallbackPolicy

        store = get_store()
        sender = RecordingSender()
        self._admit(sender, 607, FakeInterviewer())
        store.get(607).phase = SessionPhase.INTERVIEWING

        reply = handle_update(_update("an answer", chat_id=607), sender, interviewer=None)

        assert reply is not None, "never leave a user mid-interview in silence"
        assert reply.text == BouncerFallbackPolicy.ERROR_REPLY


# ---------------------------------------------------------------------------
# Phase 4: the Converter (DONE -> portrait sent straight to Telegram)
# ---------------------------------------------------------------------------


class TestConverterRouting:
    """DONE with a wired Converter fuses photo + dossier + animal and sends it.

    SPECS/2026-10-17-converter: auto-trigger when the interview finishes,
    one multimodal request with all three inputs, the image returned directly
    to Telegram, no intermediate text hop, and graceful failure.
    """

    def _finish_interview(
        self, sender: RecordingSender, chat_id: int, animal: str = "Red Fox"
    ) -> FakeInterviewer:
        """Drive a chat through admission to DONE, storing a photo along the way."""
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
                _update("an answer", chat_id=chat_id),
                sender,
                interviewer=interviewer,
            )
        assert get_store().get(chat_id).phase is SessionPhase.DONE
        return interviewer

    def test_done_phase_sends_the_portrait_and_returns_no_text(self) -> None:
        store = get_store()
        sender = RecordingSender()
        converter = FakeConverter(image_bytes=b"PORTRAIT")
        chat_id = 700
        self._finish_interview(sender, chat_id, animal="Red Fox")
        sender.photos.clear()

        reply = handle_update(
            _update("what now?", chat_id=chat_id), sender, converter=converter
        )

        assert reply is None, "the portrait is the reply, not a text message"
        assert len(sender.photos) == 1
        assert sender.photos[0].image_bytes == b"PORTRAIT"
        assert sender.photos[0].chat_id == chat_id
        assert store.get(chat_id).phase is SessionPhase.DONE

    def test_converter_receives_all_three_inputs_together(self) -> None:
        sender = RecordingSender()
        converter = FakeConverter()
        chat_id = 701
        self._finish_interview(sender, chat_id, animal="Arctic Fox")

        handle_update(_update("go", chat_id=chat_id), sender, converter=converter)

        assert len(converter.calls) == 1
        call = converter.calls[0]
        # (1) the original photo, fetched at admission
        assert call["original_photo_bytes"] == b"\xff\xd8ORIGINAL"
        assert call["original_photo_mime"] == "image/jpeg"
        # (2) the dossier summary, derived from the interview answers
        assert "an answer" in call["dossier_text"]
        # (3) the suggested animal from the Interviewer
        assert call["suggested_animal"] == "Arctic Fox"

    def test_portrait_caption_names_the_suggested_animal(self) -> None:
        sender = RecordingSender()
        converter = FakeConverter()
        chat_id = 702
        self._finish_interview(sender, chat_id, animal="Snowy Owl")
        sender.photos.clear()

        handle_update(_update("go", chat_id=chat_id), sender, converter=converter)

        assert sender.photos[0].caption is not None
        assert "Snowy Owl" in sender.photos[0].caption

    def test_converter_failure_degrades_without_killing_the_session(self) -> None:
        store = get_store()
        sender = RecordingSender()
        converter = ExplodingConverter()
        chat_id = 703
        self._finish_interview(sender, chat_id, animal="Red Fox")
        sender.sent.clear()

        reply = handle_update(_update("go", chat_id=chat_id), sender, converter=converter)

        assert reply is not None, "never leave the user in silence on a failure"
        assert sender.photos == []
        assert store.get(chat_id).phase is SessionPhase.DONE

    def test_missing_original_photo_teases_instead_of_calling_the_converter(self) -> None:
        store = get_store()
        sender = RecordingSender()
        converter = FakeConverter()
        chat_id = 704
        self._finish_interview(sender, chat_id, animal="Red Fox")
        # Simulate a photo that could not be fetched at admission time.
        store.get(chat_id).original_photo_bytes = None

        reply = handle_update(_update("go", chat_id=chat_id), sender, converter=converter)

        assert converter.calls == []
        assert reply is not None
        assert reply.text == PORTRAIT_PENDING_REPLY

    def test_done_without_a_converter_still_teases(self) -> None:
        sender = RecordingSender()
        chat_id = 705
        self._finish_interview(sender, chat_id)

        reply = handle_update(_update("go", chat_id=chat_id), sender, converter=None)

        assert reply is not None
        assert reply.text == PORTRAIT_PENDING_REPLY

    def test_admission_stores_the_original_photo(self) -> None:
        store = get_store()
        sender = RecordingSender()
        fetcher = FakePhotoFetcher(data=b"\xff\xd8ORIGINAL", mime="image/jpeg")
        chat_id = 706

        handle_update(
            _photo_update(chat_id),
            sender,
            fetcher=fetcher,
            classifier=FakeClassifier(human_verdict()),
            interviewer=FakeInterviewer(),
        )

        session = store.get(chat_id)
        assert session.original_photo_bytes == b"\xff\xd8ORIGINAL"
        assert session.original_photo_mime == "image/jpeg"
        assert session.original_photo_file_id == "largest"

    def test_admission_survives_a_photo_fetch_failure(self) -> None:
        store = get_store()
        sender = RecordingSender()
        chat_id = 707

        reply = handle_update(
            _photo_update(chat_id),
            sender,
            fetcher=FlakyPhotoFetcher(),
            classifier=FakeClassifier(human_verdict()),
            interviewer=FakeInterviewer(),
        )

        assert reply is not None, "a fetch failure must not stall the interview"
        assert store.get(chat_id).original_photo_bytes is None
        assert store.get(chat_id).phase is SessionPhase.AWAITING_CONFIRMATION


class TestTelegramSenderSendPhoto:
    def test_ok_true_returns_without_raising(self) -> None:
        client = _client_returning(
            {"ok": True, "result": {"message_id": 1, "chat": {"id": 555}, "date": 1}}
        )
        sender = TelegramSender(token="123:TOKEN", client=client)

        sender.send_photo(
            OutboundPhoto(chat_id=555, image_bytes=b"\x89PNG", mime_type="image/png")
        )  # must not raise

    def test_ok_false_raises_with_telegrams_description(self) -> None:
        client = _client_returning(
            {"ok": False, "description": "Bad Request: chat not found"}
        )
        sender = TelegramSender(token="123:TOKEN", client=client)

        with pytest.raises(RuntimeError, match="sendPhoto failed") as exc_info:
            sender.send_photo(OutboundPhoto(chat_id=555, image_bytes=b"x"))

        assert "chat not found" in str(exc_info.value)

    def test_sends_image_bytes_as_multipart(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["content_type"] = request.headers.get("content-type", "")
            captured["body"] = request.read()
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 1}}, request=request
            )

        sender = TelegramSender(
            token="123:TOKEN", client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        sender.send_photo(
            OutboundPhoto(chat_id=555, image_bytes=b"IMAGEDATA", caption="hi")
        )

        assert "multipart/form-data" in captured["content_type"]
        assert b"IMAGEDATA" in captured["body"]


# ---------------------------------------------------------------------------
# B3: one failing update must not abort the batch or roll back the offset
# ---------------------------------------------------------------------------


def _text_raw(update_id: int, chat_id: int, text: str = "hi") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "date": 1759750000,
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


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


class TestPollOnceBatchIsolation:
    TOKEN = "123456789:AAExampleTokenForTestsOnlyNotARealSecret"

    def test_happy_path_replies_to_every_chat_and_advances_the_offset(self) -> None:
        sender = RecordingSender()
        client = _client_returning(
            {"ok": True, "result": [_text_raw(10, 101), _text_raw(11, 202)]}
        )

        next_offset = poll_once(self.TOKEN, client, offset=10, sender=sender)

        assert next_offset == 12, "offset advances past every processed update"
        assert [m.chat_id for m in sender.sent] == [101, 202]

    def test_one_failing_send_does_not_abort_the_batch_or_duplicate_replies(
        self, caplog
    ) -> None:
        """Update #1 succeeds, update #2 raises: the offset must still advance
        past BOTH, #1's reply must have gone out exactly once, and no
        exception may escape poll_once."""
        sender = RecordingSender(fail_on={202})
        client = _client_returning(
            {"ok": True, "result": [_text_raw(10, 101), _text_raw(11, 202)]}
        )

        with caplog.at_level(logging.ERROR, logger="telegram_documentaries"):
            next_offset = poll_once(self.TOKEN, client, offset=10, sender=sender)

        assert next_offset == 12, "offset advanced past both updates"
        assert [m.chat_id for m in sender.attempts] == [101, 202]
        assert [m.chat_id for m in sender.sent] == [101], (
            "update #1's reply must be sent exactly once — never duplicated"
        )
        assert any(
            "event=update_failed" in r.message for r in caplog.records
        ), "the failing update must be logged loudly"

    def test_a_blocked_chat_does_not_stop_later_chats(self, caplog) -> None:
        """A 403-blocked chat fails instantly; chats later in the batch must
        still be handled (and the offset must still advance)."""
        sender = RecordingSender(fail_on={101})  # the FIRST chat fails
        client = _client_returning(
            {"ok": True, "result": [_text_raw(10, 101), _text_raw(11, 202)]}
        )

        with caplog.at_level(logging.ERROR, logger="telegram_documentaries"):
            next_offset = poll_once(self.TOKEN, client, offset=10, sender=sender)

        assert next_offset == 12
        assert [m.chat_id for m in sender.sent] == [202], "later chat still answered"
        assert any("event=update_failed" in r.message for r in caplog.records)

    def test_a_failure_never_rolls_the_offset_back_to_the_old_value(
        self, caplog
    ) -> None:
        """Re-polling with the old offset would make Telegram re-deliver the
        whole batch — the livelock that produces duplicate replies."""
        sender = RecordingSender(fail_on={202})
        client = _client_returning(
            {"ok": True, "result": [_text_raw(10, 101), _text_raw(11, 202)]}
        )

        with caplog.at_level(logging.ERROR, logger="telegram_documentaries"):
            first = poll_once(self.TOKEN, client, offset=10, sender=sender)
            # Simulate the outer loop re-polling with whatever poll_once
            # returned: a rolled-back offset would re-deliver update 10.
            second = poll_once(self.TOKEN, client, offset=first, sender=sender)

        assert first == 12
        assert second == 12, "the offset must not move backwards"
        assert [m.chat_id for m in sender.sent] == [101], (
            "re-polling must not re-deliver already-answered updates"
        )


# ---------------------------------------------------------------------------
# B4: validate at the edge BEFORE acting on update_id
# ---------------------------------------------------------------------------


class TestPollOnceValidatesAtTheEdge:
    TOKEN = "123456789:AAExampleTokenForTestsOnlyNotARealSecret"

    def test_validation_failure_still_advances_the_offset(self, caplog) -> None:
        """A malformed update must be logged and skipped — but the offset must
        still move past it so the poll loop can never be pinned on it."""
        malformed = {"update_id": 41, "message": {"message_id": 1}}  # no chat
        client = _client_returning({"ok": True, "result": [malformed]})

        with caplog.at_level(logging.WARNING, logger="telegram_documentaries"):
            next_offset = poll_once(self.TOKEN, client, offset=41, sender=RecordingSender())

        assert next_offset >= 42, "offset must advance past the malformed update"
        assert any("event=invalid_update" in r.message for r in caplog.records)

    def test_non_integer_update_id_does_not_crash_the_poll(self, caplog) -> None:
        """`raw.get("update_id") + 1` on a string used to raise TypeError and
        escape poll_once entirely. Validate first; never act on the raw value."""
        batch = [
            {"update_id": "not-an-int", "message": _text_raw(1, 555)["message"]},
            _text_raw(11, 202),
        ]
        client = _client_returning({"ok": True, "result": batch})

        with caplog.at_level(logging.WARNING, logger="telegram_documentaries"):
            next_offset = poll_once(self.TOKEN, client, offset=10, sender=RecordingSender())

        assert next_offset == 12, "the valid sibling still advances the offset"
        assert any("event=invalid_update" in r.message for r in caplog.records)

    def test_a_non_dict_entry_in_the_batch_is_skipped(self, caplog) -> None:
        """`result` is untrusted: Telegram-or-a-proxy could send anything."""
        client = _client_returning(
            {"ok": True, "result": ["garbage", _text_raw(11, 202)]}
        )

        with caplog.at_level(logging.WARNING, logger="telegram_documentaries"):
            next_offset = poll_once(self.TOKEN, client, offset=10, sender=RecordingSender())

        assert next_offset == 12
        assert any("event=invalid_update" in r.message for r in caplog.records)

    def test_non_json_getUpdates_body_returns_the_offset_and_logs(self, caplog) -> None:
        """A proxy error page must not raise out of the poll loop."""
        client = _client_returning("<html>502 Bad Gateway</html>", status=502)

        with caplog.at_level(logging.ERROR, logger="telegram_documentaries"):
            next_offset = poll_once(self.TOKEN, client, offset=9, sender=RecordingSender())

        assert next_offset == 9, "nothing was processed; the offset stands"
        assert any("event=getUpdates_failed" in r.message for r in caplog.records)

    def test_ok_false_body_returns_the_offset_and_logs(self, caplog) -> None:
        client = _client_returning(
            {"ok": False, "description": "Conflict: another request is pending"}
        )

        with caplog.at_level(logging.ERROR, logger="telegram_documentaries"):
            next_offset = poll_once(self.TOKEN, client, offset=9, sender=RecordingSender())

        assert next_offset == 9
        assert any(
            "event=getUpdates_failed" in r.message and "Conflict" in r.message
            for r in caplog.records
        ), "Telegram's description must reach the log without a traceback"


# ---------------------------------------------------------------------------
# TelegramSender: robust against non-JSON bodies and ok:false (B3/NB4)
# ---------------------------------------------------------------------------


class TestTelegramSender:
    def test_ok_true_returns_without_raising(self) -> None:
        client = _client_returning(
            {"ok": True, "result": {"message_id": 1, "chat": {"id": 555}, "date": 1}}
        )
        sender = TelegramSender(token="123:TOKEN", client=client)

        sender.send(OutboundMessage(chat_id=555, text="hey mate!"))  # must not raise

    def test_ok_false_raises_with_telegrams_description(self) -> None:
        client = _client_returning(
            {"ok": False, "description": "Bad Request: chat not found"}
        )
        sender = TelegramSender(token="123:TOKEN", client=client)

        with pytest.raises(RuntimeError, match="sendMessage failed") as exc_info:
            sender.send(OutboundMessage(chat_id=555, text="hey mate!"))

        assert "chat not found" in str(exc_info.value)

    def test_ok_false_is_logged_with_the_description(self, caplog) -> None:
        client = _client_returning(
            {"ok": False, "description": "Bad Request: chat not found"}
        )
        sender = TelegramSender(token="123:TOKEN", client=client)

        with caplog.at_level(logging.ERROR, logger="telegram_documentaries"), \
                pytest.raises(RuntimeError):
            sender.send(OutboundMessage(chat_id=555, text="hey mate!"))

        assert any(
            "event=send_failed" in r.message and "chat not found" in r.message
            for r in caplog.records
        )

    def test_non_json_body_raises_runtime_error_and_is_logged(self, caplog) -> None:
        """A raw `response.json()` on an HTML error page raises JSONDecodeError,
        which is neither logged as a send failure nor a sane error."""
        client = _client_returning("<html>502 Bad Gateway</html>", status=502)
        sender = TelegramSender(token="123:TOKEN", client=client)

        with caplog.at_level(logging.ERROR, logger="telegram_documentaries"), \
                pytest.raises(RuntimeError, match="sendMessage failed"):
            sender.send(OutboundMessage(chat_id=555, text="hey mate!"))

        assert any("event=send_failed" in r.message for r in caplog.records)

    def test_error_status_with_a_json_description_raises_with_it(self) -> None:
        client = _client_returning(
            {"ok": False, "description": "Unauthorized"}, status=401
        )
        sender = TelegramSender(token="123:TOKEN", client=client)

        with pytest.raises(RuntimeError, match="sendMessage failed") as exc_info:
            sender.send(OutboundMessage(chat_id=555, text="hey mate!"))

        assert "Unauthorized" in str(exc_info.value)


# ---------------------------------------------------------------------------
# run(): the polling loop must survive failures (B3)
# ---------------------------------------------------------------------------


class TestRunSurvivesPollFailures:
    def test_run_keeps_polling_after_a_poll_failure(
        self, monkeypatch, caplog, bot_token
    ) -> None:
        """The first poll fails with an httpx error (logged loudly), the loop
        keeps going; the second poll raises KeyboardInterrupt (a BaseException,
        so it escapes run) to end the test deterministically."""
        attempts = {"n": 0}

        def flaky_poll_once(**kwargs) -> int:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("connection refused")
            raise KeyboardInterrupt

        monkeypatch.setattr("app.gateway.poll_once", flaky_poll_once)

        with caplog.at_level(logging.DEBUG, logger="telegram_documentaries"), \
                pytest.raises(KeyboardInterrupt):
            run(bot_token)

        assert attempts["n"] == 2, "the loop must keep polling after a failure"
        assert any("event=poll_failed" in r.message for r in caplog.records)
        assert bot_token not in caplog.text, "never log the bot token"
