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

## 6. Narrator

**Not an agent.** Script is routed directly to Gemini TTS
(`gemini-3.1-flash-tts-preview`), rendered to **OGG/Opus**, and sent to the chat.

- **Acceptance:** the user receives a playable voice note in Telegram; output is
  valid OGG/Opus (**not** MP3 — Telegram voice notes require OGG/Opus); a TTS
  failure degrades gracefully.
- **Serves:** happy path (final deliverable).

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
