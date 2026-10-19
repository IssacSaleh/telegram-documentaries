"""Unit tests for the Scripter agent (Activity 7)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models import Dossier, DossierAnswer
from app.scripter import (
    SCRIPTER_MODEL,
    AdkScripter,
    Script,
    dossier_to_personality_summary,
)
from tests.conftest import RecordingSleeper


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


def _script_event(text: str):
    """One final-response ADK event carrying ``text`` as its content."""
    from google.genai import types

    class _Event:
        def __init__(self) -> None:
            self.content = types.Content(
                role="model", parts=[types.Part.from_text(text=text)]
            )

        def is_final_response(self) -> bool:
            return True

    return _Event()


class TestAdkScripterRetries:
    """ROADMAP §7: a transient Gemini timeout in `generate_script` is
    retried with backoff; exhaustion re-raises for the gateway's degrade."""

    @staticmethod
    def _install_runner(monkeypatch, behaviour) -> None:
        """Swap InMemoryRunner for one whose run_async follows ``behaviour``.

        ``behaviour(attempt)`` returns the events for that attempt, or
        raises TimeoutError to model a transient Gemini timeout.
        """
        from google.adk import runners

        state = {"n": 0}

        class _Runner:
            def __init__(self, agent=None, app_name=None, **kwargs) -> None:
                self.session_service = self
                self.app_name = "scripter"

            async def create_session(self, **kwargs) -> SimpleNamespace:
                return SimpleNamespace(id="scripter-1")

            async def run_async(self, **kwargs):
                state["n"] += 1
                events = behaviour(state["n"])
                for event in events:
                    yield event

        monkeypatch.setattr(runners, "InMemoryRunner", _Runner)

    def test_transient_timeout_is_retried_then_the_script_returns(
        self, monkeypatch
    ) -> None:
        sleeper = RecordingSleeper()
        scripter = AdkScripter(sleeper=sleeper)
        ok = [_script_event('{"text": "A dramatic tale of a fox."}')]

        def behaviour(attempt: int):
            if attempt == 1:
                raise TimeoutError("gemini timed out")
            return ok

        self._install_runner(monkeypatch, behaviour)

        script = scripter.generate_script(Dossier())

        assert script == "A dramatic tale of a fox."
        assert sleeper.delays == [0.5]

    def test_a_first_try_success_sleeps_nothing(self, monkeypatch) -> None:
        sleeper = RecordingSleeper()
        scripter = AdkScripter(sleeper=sleeper)

        self._install_runner(
            monkeypatch, lambda attempt: [_script_event('{"text": "Instant."}')]
        )

        assert scripter.generate_script(Dossier()) == "Instant."
        assert sleeper.delays == []

    def test_exhaustion_reraises_the_timeout_unchanged(self, monkeypatch) -> None:
        sleeper = RecordingSleeper()
        scripter = AdkScripter(sleeper=sleeper)
        attempts = {"n": 0}

        def behaviour(attempt: int):
            attempts["n"] += 1
            raise TimeoutError("gemini timed out")

        self._install_runner(monkeypatch, behaviour)

        with pytest.raises(TimeoutError, match="timed out"):
            scripter.generate_script(Dossier())

        assert attempts["n"] == 3, "bounded: at most max_attempts calls"

    def test_a_deterministic_error_is_not_retried(self, monkeypatch) -> None:
        sleeper = RecordingSleeper()
        scripter = AdkScripter(sleeper=sleeper)
        attempts = {"n": 0}

        def behaviour(attempt: int):
            attempts["n"] += 1
            raise ValueError("validation failed")

        self._install_runner(monkeypatch, behaviour)

        with pytest.raises(ValueError, match="validation failed"):
            scripter.generate_script(Dossier())

        assert attempts["n"] == 1
        assert sleeper.delays == []
