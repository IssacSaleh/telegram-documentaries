"""Live test for Phase 6 Narrator (opt-in).

Exercises the real ``gemini-3.1-flash-tts-preview`` synthesis end to end:
a short script goes in, verified OGG/Opus bytes come out. Never sends
anything to Telegram — the transport is covered by the offline suite.
"""

from __future__ import annotations

import os

import pytest


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1", reason="RUN_LIVE_TESTS!=1"
)
def test_live_narrator_synthesises_verified_ogg_opus() -> None:
    from app.config import load_settings
    from app.narrator import (
        NARRATOR_MODEL,
        GeminiTtsNarrator,
        verify_ogg_opus,
    )

    # Load `.env` like the other live tests do: the genai client reads
    # GEMINI_API_KEY from the process environment at construction time.
    load_settings()

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
