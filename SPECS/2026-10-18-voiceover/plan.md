# Plan — Voice-over stage / Narrator (ROADMAP Phase 6)

Red/Green TDD throughout: **write the failing test first, then the minimum
code that makes it pass** (`SPECS/TECH.md`). Ground truth for every check:
`./scripts/test` (pytest) and `./scripts/hooks` (ruff + mypy) — run both in
Group 8 before anything is considered done.

## Group 1 — Dependency (unblocks all Green runs)

1. Add `soundfile>=0.13,<1` to `requirements.txt` with a one-line comment
   naming it as the Narrator's PCM→OGG/Opus encoder (user-approved, D1).
2. `pip install soundfile` in the project environment. No other dependency or
   system changes; do not touch `requirements-dev.txt`.

## Group 2 — Audio contract: models + format verification (Red → Green)

3. **Red:** unit tests for the edge contracts:
   - `OutboundVoice` Pydantic model (chat_id, audio_bytes, mime_type
     constrained to `audio/ogg`, filename `voice.ogg`).
   - The OGG/Opus verifier: accepts a crafted minimal byte string with
     `OggS` at offset 0 and `OpusHead` within the first page; rejects
     empty bytes, raw PCM, MP3 (`ID3`/`0xFFEx`), truncated headers.
   - The TTS-response parser: a valid audio part (`audio/l16; rate=24000;
     channels=1`, non-empty bytes) parses; missing part, wrong mime, wrong
     rate, and empty data each raise a typed error (not `KeyError`/
     `IndexError`).
4. **Green:** implement the `app/narrator.py` contracts (models/verifier/
   parser) so the tests pass.

## Group 3 — Narrator synthesis + conversion (Red → Green)

5. **Red:** unit tests for `GeminiTtsNarrator.synthesise(script)` with the
   `google-genai` client faked at module boundary:
   - happy path: fake PCM (24 kHz s16le) comes back → the method returns
     bytes that pass the OGG/Opus verifier; the request sent to the model
     carries the deep-male voice preset + a style instruction wrapping the
     script (D4), `response_modalities=["AUDIO"]`, and the pinned model id
     `gemini-3.1-flash-tts-preview` (TECH.md).
   - error paths: client raises → typed failure logged, no partial bytes
     escape; malformed response (see Group 2) → same.
6. **Green:** implement the Narrator module — direct
   `client.models.generate_content` call (D3), PCM→OGG/Opus conversion via
   `soundfile`, magic-byte verification of its own output before returning.
   `Narrator` Protocol alongside the existing `Converter`/`Scripter`
   protocols; `NARRATOR_MODEL = "gemini-3.1-flash-tts-preview"`.

## Group 4 — Transport: `send_voice` (Red → Green)

7. **Red:** unit tests mirroring the `send_photo` tests: an
   `httpx.MockTransport` asserting POST to `…/sendVoice` with `chat_id` +
   a multipart `voice` part named `voice.ogg`, content type `audio/ogg`;
   `ok:false` and non-JSON responses raise `RuntimeError` carrying
   Telegram's description; no URL with the token ever reaches a log line or
   exception message (secret hygiene).
8. **Green:** extend the `MessageSender` protocol with `send_voice` and
   implement it on `TelegramSender` exactly like `send_photo` (D7).

## Group 5 — Gateway integration (Red → Green)

9. **Red:** gateway tests (`tests/unit/…`):
   - auto-trigger: when the Scripter produces a script, the Narrator is
     invoked with exactly that script and the voice is sent to **the same
     `chat_id`** — portrait → script text → voice note, in that order,
     with no extra user message (D2).
   - phase transitions: `SCRIPTING` after the portrait,
     `NARRATING` after the script, `DONE` after the voice note (D6).
   - TTS failure: friendly reply is sent, session ends in `DONE`, the poll
     loop never sees the exception; a structured `narrator_error`-style
     event is logged.
   - narrator not wired (`narrator=None`): script text still delivered,
     friendly degrade logged (`narrator_not_wired`), no crash.
   - existing scripter/converter tests keep passing unchanged (behaviour
     preserved, only extended).
10. **Green:** wire the Narrator through `_run_converter`, `handle_update`,
    `poll_once`, and `run()` — independent `try/except` init,
    `narrator_model`/`narrator_wired` in the `gateway_started` line (D8).
    Phase assignments per D6 on both the success and failure branches.

## Group 6 — Live TTS test (gated, off by default)

11. `tests/integration/test_live_narrator.py` behind `RUN_LIVE_TESTS=1`:
    real `gemini-3.1-flash-tts-preview` synthesis of a short script → assert
    non-empty bytes and the OGG/Opus contract. Never sends to Telegram.

## Group 7 — Ground-truth checks + hand-off

12. `./scripts/test` — full suite green (offline default).
13. `./scripts/hooks` — ruff + mypy clean.
14. Report to the orchestrator: what was implemented, what each check
    actually verified, and anything that drifted from this plan (the verifier
    then updates TECH.md/MISSION/ROADMAP/README and the spec files).
