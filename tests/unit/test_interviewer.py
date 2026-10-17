"""Unit tests for the Interviewer (ROADMAP Phase 3)."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from app.interviewer import (
    COMPLETION_TEMPLATE,
    CONFIRMATION_PROMPT,
    INTERVIEWER_INSTRUCTION,
    INTERVIEWER_MODEL,
    PORTRAIT_PENDING_REPLY,
    RETRY_PROMPT,
    WRONG_PAYLOAD_PROMPT,
    AdkInterviewer,
    handle_answer,
    handle_confirmation,
    handle_photo_admitted,
    handle_portrait_pending,
    handle_wrong_payload,
)
from app.models import DossierAnswer, InterviewTurn, OutboundMessage
from app.state import SessionPhase, get_store
from tests.conftest import (
    ExplodingInterviewer,
    FakeInterviewer,
)


def _strip(text: str) -> str:
    return text.lower()


class TestInterviewModels:
    """Dossier/InterviewTurn validation: cross-field guards."""

    def test_dossier_answer_requires_both_fields(self) -> None:
        with pytest.raises(ValidationError):
            DossierAnswer(question="Q", answer="   ")
        with pytest.raises(ValidationError):
            DossierAnswer(question="  ", answer="A")
        DossierAnswer(question="Q", answer="A")

    def test_interview_turn_requires_next_question_when_incomplete(self) -> None:
        with pytest.raises(ValidationError):
            InterviewTurn.model_validate(
                {"is_complete": False, "next_question": None}
            )
        with pytest.raises(ValidationError):
            InterviewTurn.model_validate({"is_complete": False})

    def test_interview_turn_rejects_incomplete_with_animal(self) -> None:
        with pytest.raises(ValidationError):
            InterviewTurn.model_validate(
                {
                    "is_complete": False,
                    "next_question": "what?",
                    "suggested_animal": "Fox",
                }
            )

    def test_interview_turn_requires_animal_when_complete(self) -> None:
        with pytest.raises(ValidationError):
            InterviewTurn.model_validate(
                {"is_complete": True, "suggested_animal": None}
            )
        with pytest.raises(ValidationError):
            InterviewTurn.model_validate({"is_complete": True})

    def test_follow_ups_used_non_negative(self) -> None:
        from app.models import Dossier

        with pytest.raises(ValidationError):
            Dossier(follow_ups_used=-1)


class TestAdkInterviewerWiring:
    """Wiring matches SPECS/TECH.md (offline)."""

    def test_wired_with_pinned_model_and_structured_output(self) -> None:
        agent = AdkInterviewer()._agent  # type: ignore[attr-defined]

        assert agent.name == "interviewer"
        assert INTERVIEWER_MODEL == "gemini-3.1-flash-lite"
        assert agent.model == INTERVIEWER_MODEL
        assert agent.output_schema is InterviewTurn

    def test_instruction_contains_interview_rules(self) -> None:
        inst = _strip(INTERVIEWER_INSTRUCTION)
        assert "interview" in inst or "question" in inst
        assert "one question" in inst or "one at a time" in inst
        assert "5" in inst and "7" in inst
        assert "is_complete" in inst


class TestHandlePhotoAdmitted:
    def test_sets_phase_and_sends_confirmation_and_starts_dossier(self) -> None:
        store = get_store()
        decision = handle_photo_admitted(42)

        assert isinstance(decision.reply, OutboundMessage)
        assert decision.reply.chat_id == 42
        assert decision.reply.text == CONFIRMATION_PROMPT
        session = store.get(42)
        assert session is not None
        assert session.phase is SessionPhase.AWAITING_CONFIRMATION
        assert session.dossier is not None
        assert session.dossier.asked == 0

    def test_a_fresh_dossier_is_created_each_time(self) -> None:
        store = get_store()
        handle_photo_admitted(50)
        assert store.get(50).dossier is not None
        store.get(50).dossier.answers.append(
            DossierAnswer(question="Q", answer="A")
        )

        handle_photo_admitted(50)  # second admission
        assert store.get(50).dossier is not None
        assert store.get(50).dossier.asked == 0, "dossier is fresh, not resumed"


class TestHandleConfirmation:
    def test_non_yes_re_asks_confirmation(self) -> None:
        store = get_store()
        handle_photo_admitted(60)
        decision = handle_confirmation(60, "maybe later", interviewer=FakeInterviewer())

        assert decision.reply.text == CONFIRMATION_PROMPT
        assert store.get(60).phase is SessionPhase.AWAITING_CONFIRMATION

    def test_yes_starts_interview_and_asks_first_question(self) -> None:
        store = get_store()
        handle_photo_admitted(61)
        interviewer = FakeInterviewer()
        decision = handle_confirmation(61, "yes", interviewer=interviewer)

        assert decision.reply.text == "Question 1?"
        assert store.get(61).phase is SessionPhase.INTERVIEWING
        assert store.get(61).pending_question == "Question 1?"
        assert interviewer.next_turn_calls == [0]

    @pytest.mark.parametrize("text", ["YES", " y ", "Y", "y"])
    def test_yes_is_case_insensitive_and_trimmed(self, text: str) -> None:
        handle_photo_admitted(620 + len(text))
        interviewer = FakeInterviewer()
        handle_confirmation(620 + len(text), text, interviewer=interviewer)

        store = get_store().get(620 + len(text))
        assert store is not None
        assert store.phase is SessionPhase.INTERVIEWING


class TestHandleAnswer:
    def test_records_answer_and_asks_next(self) -> None:
        store = get_store()
        handle_photo_admitted(70)
        interviewer = FakeInterviewer()
        handle_confirmation(70, "yes", interviewer=interviewer)
        pending = store.get(70).pending_question

        decision = handle_answer(70, "I like long walks", interviewer=interviewer)

        assert decision.reply.text == "Question 2?"
        dossier = store.get(70).dossier
        assert dossier is not None
        assert dossier.asked == 1
        assert dossier.answers[0].question == pending
        assert dossier.answers[0].answer == "I like long walks"
        assert store.get(70).pending_question == "Question 2?"

    def test_stray_answer_without_pending_still_asks_next(self, caplog) -> None:
        store = get_store()
        handle_photo_admitted(71)
        handle_confirmation(71, "yes", FakeInterviewer())
        store.get(71).pending_question = None

        with caplog.at_level(logging.WARNING, logger="telegram_documentaries.interviewer"):
            decision = handle_answer(71, "hello", interviewer=FakeInterviewer())

        assert decision.reply.text == "Question 1?"
        assert store.get(71).pending_question == "Question 1?"

    def test_error_reverts_the_last_answer_and_retries(self, caplog) -> None:
        store = get_store()
        handle_photo_admitted(72)
        interviewer = FakeInterviewer()
        handle_confirmation(72, "yes", interviewer=interviewer)
        pending = store.get(72).pending_question

        with caplog.at_level(logging.ERROR, logger="telegram_documentaries.interviewer"):
            decision = handle_answer(72, "bad answer", interviewer=ExplodingInterviewer())

        assert decision.reply.text == RETRY_PROMPT
        assert decision.error is True
        dossier = store.get(72).dossier
        assert dossier is not None
        assert dossier.asked == 0, "answer reverted"
        assert store.get(72).pending_question == pending, "pending preserved"
        assert any("event=interviewer_error" in r.message for r in caplog.records)
        assert "bad answer" not in caplog.text

    def test_no_raw_answer_text_is_logged(self, caplog) -> None:
        handle_photo_admitted(73)
        handle_confirmation(73, "yes", FakeInterviewer())

        secret = "super-secret-long-answer-that-should-not-appear"
        with caplog.at_level(logging.INFO, logger="telegram_documentaries.interviewer"):
            handle_answer(73, secret, interviewer=FakeInterviewer())

        assert secret not in caplog.text
        assert any("answer_chars" in r.message for r in caplog.records)


class TestCompletionAndGuards:
    def test_premature_completion_is_ignored_and_a_question_is_asked(self) -> None:
        store = get_store()
        handle_photo_admitted(80)
        interviewer = FakeInterviewer(
            turns=[
                InterviewTurn(next_question="Q1?"),
                InterviewTurn(is_complete=True, suggested_animal="Fox"),  # premature
                InterviewTurn(next_question="Q2?"),
            ],
            completion_after=10,  # default won't complete early
        )
        handle_confirmation(80, "yes", interviewer=interviewer)
        decision = handle_answer(80, "A1", interviewer=interviewer)

        assert decision.reply.text == "Q2?"
        dossier = store.get(80).dossier
        assert dossier is not None
        assert dossier.asked == 1

    def test_completion_at_five_questions_sets_done_and_names_animal(self) -> None:
        store = get_store()
        handle_photo_admitted(81)
        interviewer = FakeInterviewer([], animal="Arctic Fox")
        handle_confirmation(81, "yes", interviewer=interviewer)
        for i in range(5):
            handle_answer(81, f"A{i+1}", interviewer=interviewer)

        session = store.get(81)
        assert session.phase is SessionPhase.DONE
        assert session.pending_question is None
        assert session.dossier is not None
        assert session.dossier.suggested_animal == "Arctic Fox"
        assert session.dossier.asked == 5

    def test_completion_template_includes_the_animal(self) -> None:

        assert "{animal}" in COMPLETION_TEMPLATE

    def test_two_chats_remain_isolated(self) -> None:
        store = get_store()
        handle_photo_admitted(82)
        handle_photo_admitted(83)
        handle_confirmation(82, "yes", FakeInterviewer())
        handle_answer(82, "A1", interviewer=FakeInterviewer(completion_after=6))
        handle_confirmation(83, "yes", FakeInterviewer())
        handle_answer(83, "B1", interviewer=FakeInterviewer(completion_after=6))

        dossier_a = store.get(82).dossier
        dossier_b = store.get(83).dossier
        assert dossier_a is not None and dossier_b is not None
        assert dossier_a.answers != dossier_b.answers
        assert store.get(82).phase is SessionPhase.INTERVIEWING
        assert store.get(83).phase is SessionPhase.INTERVIEWING

    def test_follow_ups_used_increments_when_marked(self) -> None:
        store = get_store()
        handle_photo_admitted(84)
        interviewer = FakeInterviewer(
            turns=[
                InterviewTurn(next_question="Q1?", is_follow_up=False),
                InterviewTurn(next_question="Q2?", is_follow_up=True),
            ],
            completion_after=10,
        )
        handle_confirmation(84, "yes", interviewer=interviewer)
        handle_answer(84, "A1", interviewer=interviewer)
        assert store.get(84).dossier is not None
        assert store.get(84).dossier.follow_ups_used == 1

    def test_handle_wrong_payload_keeps_pending(self) -> None:
        store = get_store()
        handle_photo_admitted(85)
        handle_confirmation(85, "yes", FakeInterviewer())
        pending = store.get(85).pending_question

        decision = handle_wrong_payload(85)
        assert decision.reply.text == WRONG_PAYLOAD_PROMPT
        assert store.get(85).pending_question == pending
        assert store.get(85).phase is SessionPhase.INTERVIEWING

    def test_handle_portrait_pending_sends_tease(self) -> None:
        decision = handle_portrait_pending(86)
        assert decision.reply.chat_id == 86
        assert decision.reply.text == PORTRAIT_PENDING_REPLY

    def test_errors_log_loudly_and_never_leave_silence(self, caplog) -> None:
        handle_photo_admitted(87)
        with caplog.at_level(logging.ERROR, logger="telegram_documentaries.interviewer"):
            decision = handle_confirmation(87, "yes", interviewer=ExplodingInterviewer())
        assert decision.reply.text == RETRY_PROMPT
        assert decision.error is True
        assert any("event=interviewer_error" in r.message for r in caplog.records)


class TestAdkInterviewerPromptFormatting:
    def test_prompt_includes_transcript_and_question_count(self) -> None:
        from app.models import Dossier, DossierAnswer

        dossier = Dossier(
            answers=[
                DossierAnswer(question="Q1", answer="A1"),
                DossierAnswer(question="Q2", answer="A2"),
            ]
        )
        prompt = AdkInterviewer._prompt(dossier)
        assert "Q1" in prompt and "A1" in prompt
        assert "Questions asked: 2" in prompt
        assert "minimum 5" in prompt and "maximum 7" in prompt

    def test_empty_transcript_noted_when_no_answers(self) -> None:
        from app.models import Dossier

        prompt = AdkInterviewer._prompt(Dossier())
        assert "no answers yet" in prompt
