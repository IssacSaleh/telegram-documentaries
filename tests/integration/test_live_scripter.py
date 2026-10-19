"""Live test for Phase 5 Scripter (opt-in; see conftest.py).

Exercises the real ``gemini-3.1-flash-lite`` narration end to end: a dossier
goes in, a one-paragraph British-wildlife-documentary script comes out. Skipped
unless ``RUN_LIVE_TESTS=1`` and skips cleanly when credentials are absent — the
same contract as the other live tests.
"""

from __future__ import annotations

import os

import pytest

from app.models import Dossier, DossierAnswer

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="live Gemini calls; set RUN_LIVE_TESTS=1 to enable",
)


def _dossier() -> Dossier:
    """A believable, complete dossier the Scripter can narrate from."""
    return Dossier(
        answers=[
            DossierAnswer(question="Q1", answer="I enjoy hiking and being outdoors."),
            DossierAnswer(question="Q2", answer="I'm quite solitary but friendly."),
            DossierAnswer(question="Q3", answer="I like warm weather and shade."),
            DossierAnswer(question="Q4", answer="I prefer quiet spaces over crowds."),
            DossierAnswer(question="Q5", answer="I eat small portions and nap often."),
        ],
        suggested_animal="fox",
        animal_rationale="Solitary, adaptable, and fond of quiet nooks.",
    )


def test_live_scripter_narrates_the_dossier(live_credentials: None) -> None:
    from app.scripter import SCRIPTER_MODEL, AdkScripter

    # `live_credentials` (see conftest.py) loads `.env` so the ADK/genai client
    # finds GEMINI_API_KEY, and skips the test cleanly if it is absent.
    script = AdkScripter().generate_script(_dossier())

    assert isinstance(script, str)
    text = script.strip()
    assert text, "the Scripter must return non-empty narration"
    assert len(text) <= 600, "must satisfy the Script schema's 600-character cap"
    # A documentary paragraph, not a stub. Kept loose (>=15 words) so a slightly
    # short but valid generation is not treated as a failure.
    assert len(text.split()) >= 15, "narration should be a full paragraph"

    # The pinned model is the one ROADMAP §5 / TECH.md name; asserted after the
    # call so a model swap shows up in this live check too.
    assert SCRIPTER_MODEL == "gemini-3.1-flash-lite"
