"""Boundary contracts: untrusted Telegram payloads -> typed models.

SPECS/TECH.md: "Parse Telegram updates ... into typed Pydantic models at the
edge" and "Treat all external input as untrusted and arbitrary."
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import OutboundMessage, TelegramUpdate
from tests.conftest import SAMPLE_UPDATE


class TestParseTelegramUpdate:
    def test_parses_a_realistic_update(self) -> None:
        update = TelegramUpdate.model_validate(SAMPLE_UPDATE)

        assert update.update_id == 987654321
        assert update.message is not None
        assert update.message.chat.id == 1122334455
        assert update.message.text == "hello bot"

    def test_ignores_unknown_fields_it_does_not_care_about(self) -> None:
        # Telegram routinely adds fields; we must not blow up on them.
        update = TelegramUpdate.model_validate(SAMPLE_UPDATE)

        # `from`, `entities`, `first_name`, `username` were all present above
        # and are not modelled. Reaching here without ValidationError is the
        # assertion.
        assert update.message is not None
        assert update.message.chat.id == 1122334455

    def test_message_is_optional_for_non_message_updates(self) -> None:
        update = TelegramUpdate.model_validate({"update_id": 7})

        assert update.message is None

    def test_photo_message_has_no_text(self, photo_update_dict: dict) -> None:
        update = TelegramUpdate.model_validate(photo_update_dict)

        assert update.message is not None
        assert update.message.text is None

    @pytest.mark.parametrize(
        "bad_payload",
        [
            {},                                    # no update_id at all
            {"update_id": "not-an-int"},           # wrong type
            {"update_id": 1, "message": "a string"},  # message not an object
            {"update_id": 1, "message": {"message_id": 1}},  # missing chat
        ],
    )
    def test_rejects_malformed_payloads(self, bad_payload: dict) -> None:
        with pytest.raises(ValidationError):
            TelegramUpdate.model_validate(bad_payload)


class TestOutboundMessage:
    def test_carries_chat_id_and_text(self) -> None:
        outbound = OutboundMessage(chat_id=555, text="hey mate!")

        assert outbound.chat_id == 555
        assert outbound.text == "hey mate!"

    def test_rejects_empty_text(self) -> None:
        with pytest.raises(ValidationError):
            OutboundMessage(chat_id=555, text="")

    def test_rejects_unknown_fields_so_raw_dicts_cannot_leak_through(self) -> None:
        with pytest.raises(ValidationError):
            OutboundMessage.model_validate({"chat_id": 1, "text": "x", "reply_to": 9})


class TestPhotoSizes:
    """Telegram sends `photo` as an ARRAY of sizes; we must take the largest."""

    def test_parses_a_photo_array(self) -> None:
        update = TelegramUpdate.model_validate(_PHOTO_UPDATE)

        assert update.message is not None
        assert len(update.message.photo) == 4
        assert update.message.photo[0].file_id == "small"

    def test_largest_photo_is_the_last_and_biggest(self) -> None:
        update = TelegramUpdate.model_validate(_PHOTO_UPDATE)
        assert update.message is not None

        largest = update.message.largest_photo()

        assert largest is not None
        assert largest.file_id == "largest"
        assert largest.width == 900
        # it really is the biggest of the four
        assert largest.width > max(p.width for p in update.message.photo[:-1])

    def test_largest_photo_is_none_when_no_photo(self) -> None:
        update = TelegramUpdate.model_validate(
            {"update_id": 1, "message": {"message_id": 1, "date": 1,
                                         "chat": {"id": 5, "type": "private"}, "text": "hi"}}
        )
        assert update.message is not None
        assert update.message.largest_photo() is None

    def test_photo_message_has_no_text(self) -> None:
        update = TelegramUpdate.model_validate(_PHOTO_UPDATE)
        assert update.message is not None
        assert update.message.text is None
        assert update.message.largest_photo() is not None

    def test_rejects_photo_without_file_id(self) -> None:
        bad = {"update_id": 1, "message": {"message_id": 1, "date": 1,
               "chat": {"id": 5, "type": "private"},
               "photo": [{"width": 10, "height": 10}]}}

        with pytest.raises(ValidationError):
            TelegramUpdate.model_validate(bad)

    def test_outbound_rejection_text_is_not_empty(self) -> None:
        with pytest.raises(ValidationError):
            OutboundMessage(chat_id=1, text="")


class TestInterviewTurn:
    def test_accepts_normal_incomplete_turn(self) -> None:
        from app.models import InterviewTurn

        turn = InterviewTurn.model_validate(
            {
                "next_question": "Tell me about your favourite hobby.",
                "is_complete": False,
            }
        )
        assert turn.next_question == "Tell me about your favourite hobby."
        assert turn.is_complete is False
        assert turn.suggested_animal is None

    def test_accepts_complete_turn_with_animal(self) -> None:
        from app.models import InterviewTurn

        turn = InterviewTurn.model_validate(
            {
                "next_question": None,
                "is_complete": True,
                "suggested_animal": "Red Fox",
                "animal_rationale": "Cunning and adaptable",
            }
        )
        assert turn.is_complete is True
        assert turn.suggested_animal == "Red Fox"

    def test_rejects_complete_without_animal(self) -> None:
        from pydantic import ValidationError

        from app.models import InterviewTurn

        with pytest.raises(ValidationError):
            InterviewTurn.model_validate(
                {
                    "next_question": None,
                    "is_complete": True,
                    "suggested_animal": None,
                }
            )

    def test_rejects_incomplete_with_animal(self) -> None:
        from pydantic import ValidationError

        from app.models import InterviewTurn

        with pytest.raises(ValidationError):
            InterviewTurn.model_validate(
                {
                    "next_question": "What next?",
                    "is_complete": False,
                    "suggested_animal": "Fox",
                }
            )

    def test_rejects_empty_next_question_when_incomplete(self) -> None:
        from pydantic import ValidationError

        from app.models import InterviewTurn

        with pytest.raises(ValidationError):
            InterviewTurn.model_validate(
                {
                    "next_question": "",
                    "is_complete": False,
                }
            )

    def test_rejects_whitespace_only_next_question_when_incomplete(self) -> None:
        from pydantic import ValidationError

        from app.models import InterviewTurn

        with pytest.raises(ValidationError):
            InterviewTurn.model_validate(
                {
                    "next_question": "   ",
                    "is_complete": False,
                }
            )


class TestDossier:
    def test_dossier_preserves_answer_order(self) -> None:
        from app.models import Dossier, DossierAnswer

        dossier = Dossier(
            answers=[
                DossierAnswer(question="Q1", answer="A1"),
                DossierAnswer(question="Q2", answer="A2"),
                DossierAnswer(question="Q3", answer="A3"),
            ]
        )
        assert dossier.asked == 3
        assert dossier.answers[0].question == "Q1"
        assert dossier.answers[1].answer == "A2"

    def test_asked_is_derived(self) -> None:
        from app.models import Dossier, DossierAnswer

        dossier = Dossier(answers=[DossierAnswer(question="Q", answer="A")])
        assert dossier.asked == 1

    def test_follow_ups_used_defaults_to_zero(self) -> None:
        from app.models import Dossier

        dossier = Dossier()
        assert dossier.follow_ups_used == 0
        assert dossier.suggested_animal is None


_PHOTO_UPDATE = {
    "update_id": 42,
    "message": {
        "message_id": 7,
        "date": 1759750000,
        "chat": {"id": 6682234839, "type": "private"},
        "photo": [
            {"file_id": "small",  "file_unique_id": "u1", "width": 90,  "height": 90},
            {"file_id": "medium", "file_unique_id": "u2", "width": 320, "height": 320},
            {"file_id": "big",    "file_unique_id": "u3", "width": 640, "height": 640},
            {"file_id": "largest","file_unique_id": "u4", "width": 900, "height": 900},
        ],
    },
}
