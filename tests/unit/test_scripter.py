"""Unit tests for the Scripter agent (Activity 7)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import Dossier, DossierAnswer
from app.scripter import (
    SCRIPTER_MODEL,
    Script,
    dossier_to_personality_summary,
)


class TestScriptModel:
    def test_script_requires_text(self) -> None:
        with pytest.raises(ValidationError):
            Script()

    def test_script_allows_reasonable_length(self) -> None:
        s = Script(text="A dramatic tale of a curious human-fox hybrid.")
        assert s.text


class TestPersonalitySummary:
    def test_includes_answers_and_rationale_and_animal(self) -> None:
        dossier = Dossier(
            answers=[
                DossierAnswer(question="What quirks?", answer="late-night snacker"),
            ],
            animal_rationale="clever and nocturnal",
            suggested_animal="Red Fox",
        )
        summary = dossier_to_personality_summary(dossier)
        assert "late-night snacker" in summary
        assert "nocturnal" in summary
        assert "Red Fox" in summary


class TestModel:
    def test_pinned_model(self) -> None:
        assert SCRIPTER_MODEL == "gemini-3.1-flash-lite"
