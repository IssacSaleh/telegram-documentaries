"""Live test for Phase 4 Converter (opt-in; see conftest.py).

Exercises the real Gemini image model end to end: a portrait + dossier +
suggested animal go in, a generated image comes out. Uses the local
`person.jpg` fixture (the same content-verified portrait the live Bouncer
test uses) so the only network dependency is Gemini itself.
"""

from __future__ import annotations

import os
import pathlib

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="live Gemini calls; set RUN_LIVE_TESTS=1 to enable",
)

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"


def test_live_converter_produces_an_image(live_credentials: None) -> None:
    from app.converter import GenAiConverter, dossier_to_summary
    from app.models import Dossier, DossierAnswer

    # A real portrait from the local fixtures — no external CDN dependency.
    photo_bytes = (FIXTURES / "person.jpg").read_bytes()
    photo_mime = "image/jpeg"

    dossier = Dossier(
        answers=[
            DossierAnswer(
                question="Tell me about your personality",
                answer="Curious, observant, and playful",
            ),
            DossierAnswer(
                question="What kind of energy do you bring?",
                answer="Energetic and clever, puzzle-lover",
            ),
        ],
        suggested_animal="Red Fox",
        animal_rationale="clever and adaptable",
    )

    converter = GenAiConverter()
    result = converter.generate(
        original_photo_bytes=photo_bytes,
        original_photo_mime=photo_mime,
        dossier_text=dossier_to_summary(dossier),
        suggested_animal=dossier.suggested_animal or "Red Fox",
    )

    assert result.image_bytes is not None and len(result.image_bytes) > 0
    assert result.image_mime_type.startswith("image/")
    assert result.suggested_animal == "Red Fox"
