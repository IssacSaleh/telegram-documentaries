"""Live Interviewer integration tests — real Gemini calls (ROADMAP Phase 3).

These tests are SKIPPED by default so `./scripts/test` stays offline-green
and deterministic. Enable them explicitly:

    RUN_LIVE_TESTS=1 ./scripts/test

They verify the Interviewer produces a reasonable next turn/complete turn
against a simple dossier.
"""

from __future__ import annotations

import os

import pytest

from app.interviewer import AdkInterviewer, MIN_QUESTIONS
from app.models import Dossier, DossierAnswer, InterviewTurn
from app.state import get_store

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="live Gemini calls; set RUN_LIVE_TESTS=1 to enable",
)


@pytest.fixture(scope="module")
def interviewer() -> AdkInterviewer:
    from app.config import load_settings

    load_settings()  # ensures GEMINI_API_KEY present if possible
    return AdkInterviewer()


def test_next_turn_produces_one_question_when_answers_short(interviewer: AdkInterviewer) -> None:
    dossier = Dossier()
    turn = interviewer.next_turn(dossier)
    assert isinstance(turn, InterviewTurn)
    assert turn.is_complete is False
    assert turn.next_question is not None and turn.next_question.strip()


def test_next_turn_can_complete_after_min_questions(interviewer: AdkInterviewer) -> None:
    dossier = Dossier(
        answers=[
            DossierAnswer(question="Q1", answer="I enjoy hiking and being outdoors."),
            DossierAnswer(question="Q2", answer="I'm quite solitary but friendly."),
            DossierAnswer(question="Q3", answer="I like warm weather and shade."),
            DossierAnswer(question="Q4", answer="I prefer quiet spaces over crowds."),
            DossierAnswer(question="Q5", answer="I eat small portions and nap often."),
        ]
    )
    turn = interviewer.next_turn(dossier)
    assert isinstance(turn, InterviewTurn)
    assert turn.is_complete is True
    assert turn.suggested_animal is not None and turn.suggested_animal.strip()


def test_final_turn_is_forced_completion(interviewer: AdkInterviewer) -> None:
    dossier = Dossier(
        answers=[
            DossierAnswer(question="Q1", answer="A"),
            DossierAnswer(question="Q2", answer="B"),
            DossierAnswer(question="Q3", answer="C"),
            DossierAnswer(question="Q4", answer="D"),
            DossierAnswer(question="Q5", answer="E"),
            DossierAnswer(question="Q6", answer="F"),
            DossierAnswer(question="Q7", answer="G"),
        ]
    )
    turn = interviewer.final_turn(dossier)
    assert turn.is_complete is True
    assert turn.suggested_animal is not None and turn.suggested_animal.strip()
