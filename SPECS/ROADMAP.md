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
- **Delivered:** the Scripter lives in `app/scripter.py` as a plain module
  (direct `google-genai` client, no ADK agent): it builds a Pydantic-validated
  `Script` (`text`, 1–600 chars) from the dossier via the pinned
  `gemini-3.1-flash-lite` model, stores the finished narration on
  `session.script`, and the gateway sends it to the chat as plain text before
  the Narrator runs (spec `SPECS/2026-10-18-voiceover`, decision D2 — the
  text message stays). Verified by the offline suite
  (`tests/unit/test_scripter.py`, `tests/unit/test_gateway_scripter.py`,
  regression green in `./scripts/test`); `./scripts/hooks` clean.

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
