"""Live test for Phase 4 Converter (opt-in)."""

from __future__ import annotations

import os

import pytest


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1", reason="RUN_LIVE_TESTS!=1"
)
def test_live_converter_produces_an_image() -> None:
    import httpx

    from app.converter import GenAiConverter, dossier_to_summary
    from app.models import Dossier, DossierAnswer

    # Use a small reliable image (1x1 placeholder from a stable CDN-like source).
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        r = client.get("https://placehold.co/256x256/png")
        r.raise_for_status()
        photo_bytes = r.content
        photo_mime = "image/png"

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
