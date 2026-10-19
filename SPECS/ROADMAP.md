# ROADMAP

Ordered build plan — one phase per pipeline capability. A phase is marked
complete **only when verified** (its acceptance criteria actually pass).

## 1. Repository & gateway

Project skeleton, `.env` loading, and a long-polling loop that replies to a
hardcoded message. Creates `scripts/test` and `scripts/hooks`.

- **Acceptance:** bot receives a message and replies; `scripts/test` and
  `scripts/hooks` run green from a clean checkout; `.env` is gitignored and no
  secret is committed.
- **Serves:** foundation for every later phase; README/`scripts/` contract.

## 2. Bouncer

Vision gate. Confirms a human is present; cheekily rejects non-human images and
resets state. Text/image classification only — **no TTS, no video.**

- **Acceptance:** a portrait passes through to the Interviewer; a non-human image
  gets a rejection reply and state resets; rejects are logged.
- **Serves:** happy-path intake; graceful wrong-payload handling.
- **Delivered:** the gate lives in `app/bouncer.py` as a Google ADK `LlmAgent`
  (`gemini-3.1-flash-lite`, structured `BouncerVerdict`); a portrait is admitted
  into the confirmation flow, a non-human is rejected and that chat's state is
  purged, rejects are logged, and Bouncer failures degrade to a friendly reply.
  Verified by 112 offline tests plus 3 live tests behind `RUN_LIVE_TESTS=1`
  (portrait admitted; landscape rejected with reset; cross-chat isolation), and
  `./scripts/hooks` clean.

## 3. Interviewer

Sequential, stateful Q&A — **5–7 questions, adaptive** (start at 5, add up to 2
follow-ups based on answers), one at a time. Accumulates the behavioural dossier
tied to `chat_id` and outputs a suggested animal. Acts as the orchestrator.

- **Acceptance:** 5–7 questions asked in order; dossier persists across turns;
  two concurrent chats never see each other's state; suggested animal produced
  **and passed to the Converter**.
- **Serves:** happy path; non-negotiable session isolation.

## 4. Converter

Native multimodal fusion of the original photo + interview dossier + the
Interviewer's suggested animal into a hybrid animal portrait, returned directly
to Telegram with **no intermediate text hop.**

- **Acceptance:** a portrait is returned to the chat after the interview
  completes; the suggested animal demonstrably influences the output; it is a
  Pydantic-validated model response; failure degrades gracefully without killing
  the session.
- **Serves:** happy path.

## 5. Scripter

One-paragraph (~60–90 words), dramatic British-documentary narration built from
the dossier.

- **Acceptance:** narration is 60–90 words, derived from the dossier (not a
  generic template), and validated as text before TTS.
- **Serves:** happy path.
- **Delivered:** the Scripter lives in `app/scripter.py` as an `AdkScripter`
  class wrapping a Google ADK `LlmAgent`: it builds a Pydantic-validated
  `Script` (`text`, 1–600 chars) from the dossier via the pinned
  `gemini-3.1-flash-lite` model, stores the finished narration on
  `session.script`, and the gateway sends it to the chat as plain text before
  the Narrator runs (spec `SPECS/2026-10-18-voiceover`, decision D2 — the
  text message stays). Verified by the offline suite — the Scripter module
  (`tests/unit/test_scripter.py`) and the gateway↔Scripter integration
  (`tests/unit/test_gateway_scripter.py`, regression green in `./scripts/test`)
  — plus the gated live test (`RUN_LIVE_TESTS=1`,
  `tests/integration/test_live_scripter.py`, real narration from the pinned
  model). `./scripts/hooks` clean.

## 6. Narrator

**Not an agent.** Script is routed directly to Gemini TTS
(`gemini-3.1-flash-tts-preview`), rendered to **OGG/Opus**, and sent to the chat.

- **Acceptance:** the user receives a playable voice note in Telegram; output is
  valid OGG/Opus (**not** MP3 — Telegram voice notes require OGG/Opus); a TTS
  failure degrades gracefully.
- **Serves:** happy path (final deliverable).
- **Delivered:** `app/narrator.py` (plain module, not an agent) calls
  `gemini-3.1-flash-tts-preview` directly through the `google-genai` client —
  pinned model, `response_modalities=["AUDIO"]`, prebuilt deep-male `Charon`
  voice plus a dramatic posh-British style instruction wrapping the script.
  The raw PCM it returns (`audio/l16; rate=24000; channels=1`) is converted
  to **OGG/Opus** in memory via the pip `soundfile` package (bundles
  libsndfile; this environment has no system Opus encoder) and
  magic-byte-verified (`OggS` + `OpusHead` in the first page) before being
  sent with `sendVoice` (multipart, `voice.ogg`, `audio/ogg`) so Telegram
  renders a playable voice note. Auto-triggered right after the Scripter
  (portrait → script text → voice note); phases
  `GENERATING_IMAGE → SCRIPTING → NARRATING → DONE`, every failure branch
  landing `DONE` with a friendly reply. Verified by the offline suite
  (`tests/unit/test_narrator.py`, `tests/unit/test_gateway_narrator.py`,
  `tests/unit/test_gateway_voice.py`, secret hygiene) plus the gated live
  TTS test (`RUN_LIVE_TESTS=1`, `tests/integration/test_live_narrator.py`
  — real synthesis to verified OGG/Opus). **Pending:** a human end-to-end
  playability check in the live Telegram bot (voice note rendering) was
  not performed in this environment. `./scripts/hooks` clean.

## 7. Resilience

- `/restart` and `/start` reset: purge state **and** temporary files without
  restarting the process.
- Wrong-payload-at-wrong-stage guards (text mid-image-stage, image mid-
  interview).
- API timeout/backoff fallbacks: retry, then degrade gracefully on the
  conversation path.

- **Acceptance:** `/restart` leaves no residual state or temp media; a follow-up
  run starts clean; out-of-order input continues the conversation; a Gemini
  timeout never raises into the user's flow.
- **Serves:** `/restart` reset criterion; graceful out-of-order handling;
  error-policy contract.
- **Delivered:** a shared retry helper, `app/resilience.py` —
  `RetryPolicy` (frozen; `max_attempts=3`, `base_delay=0.5`, `factor=2.0`,
  `max_delay=4.0`), name/status-based `is_transient_error`, and
  `call_with_retries(...)` (injectable sleeper; logs `event=gemini_retry` per
  retry and `event=gemini_retry_exhausted` on give-up, error *type* only). It
  wraps the single Gemini call inside all five boundaries — Bouncer
  `classify`, Interviewer `next_turn`/`final_turn`, Converter `generate`,
  Scripter `generate_script`, Narrator `synthesise` — never in the gateway,
  whose existing per-stage degrade paths turn an exhausted retry into a
  friendly reply. `Session.purge()` + `SessionStore.reset()` clear the
  session's conversation history (dossier, open question) and its
  photo bytes/mime/file_id and script, log `event=temp_media_purged`, then
  drop the session (no background tasks exist to cancel — the loop is
  synchronous). Stage guards
  (`IMAGE_STAGE_PHASES`, `handle_wrong_stage`, `IMAGE_STAGE_BUSY_PROMPT`)
  answer text *and* a stray photo during `GENERATING_IMAGE`/`SCRIPTING`/
  `NARRATING` with one "sit tight" nudge, mutating no state and never
  re-gating the photo through the Bouncer; `/start` and `/restart` still take
  precedence in any phase. Verified by the offline suite
  (`tests/unit/test_resilience.py`, `test_state.py`, `test_gateway.py`,
  `test_bouncer.py`, `test_interviewer.py`, `test_converter.py`,
  `test_scripter.py`, `test_narrator.py`) — all four acceptance criteria
  covered, including exhausted-timeout end-to-end through `handle_update` —
  `./scripts/hooks` clean.

## 8. Polish

Two small refinements raised against the delivered behaviour (spec
`SPECS/2026-10-20-phase-polish/`):

- The mid-interview wrong-payload nudge (`WRONG_PAYLOAD_PROMPT`) also offers
  `/restart` — the command already worked in every phase; the copy now
  advertises it as the way out.
- A Scripter failure (or an empty narration) degrades to
  `ScripterFallbackPolicy.ERROR_REPLY` instead of silence: the portrait is
  already out, so the user is told the narration is not coming, with
  `event=scripter_error` / `event=scripter_empty` logged and the session
  landing in `DONE`. The "not wired" branch stays log-only
  (`event=scripter_not_wired`), matching the Narrator.

- **Acceptance:** a photo/sticker/voice during `AWAITING_CONFIRMATION` or
  `INTERVIEWING` nudges *and* mentions `/restart`, with the pending question
  and phase preserved; a raising or empty Scripter never leaves the user in
  silence and never raises into `handle_update`; the happy path and every
  other stage's copy are unchanged.
- **Serves:** error-policy contract; graceful out-of-order handling.
- **Delivered:** `WRONG_PAYLOAD_PROMPT` extended in `app/interviewer.py`;
  `ScripterFallbackPolicy` added and sent from both Scripter failure branches
  in `app/gateway.py`. Verified by the offline suite
  (`tests/unit/test_gateway.py::TestScripterFailure`, the wrong-payload nudge
  tests, `tests/unit/test_interviewer.py`) — `./scripts/test` green (372
  passed, 8 skipped), `./scripts/hooks` clean.
