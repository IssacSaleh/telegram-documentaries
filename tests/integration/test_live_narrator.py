"""Live test for Phase 6 Narrator (opt-in).

Exercises the real ``gemini-3.1-flash-tts-preview`` synthesis end to end:
a short script goes in, verified OGG/Opus bytes come out. Never sends
anything to Telegram — the transport is covered by the offline suite.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="live Gemini calls; set RUN_LIVE_TESTS=1 to enable",
)


def test_live_narrator_synthesises_verified_ogg_opus(live_credentials: None) -> None:
    from app.narrator import (
        NARRATOR_MODEL,
        GeminiTtsNarrator,
        verify_ogg_opus,
    )

    # `live_credentials` (see conftest.py) loads `.env` so the genai client
    # finds GEMINI_API_KEY, and skips the test cleanly if it is absent.
    narrator = GeminiTtsNarrator()
    ogg = narrator.synthesise(
        "In the quiet hours of the evening, a most cunning fox stirs."
    )

    assert ogg, "synthesis must return bytes"
    assert ogg[:4] == b"OggS", "output must be an Ogg container, not MP3"
    verify_ogg_opus(ogg)  # OpusHead within the first page — the D5 contract

    # The pinned model is the one TECH.md names; asserted after the call so a
    # model swap shows up in this live check too.
    assert NARRATOR_MODEL == "gemini-3.1-flash-tts-preview"
