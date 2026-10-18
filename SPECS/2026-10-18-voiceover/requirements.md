# Requirements — Voice-over stage / Narrator (ROADMAP Phase 6)

## Context

The pipeline `Bouncer → Interviewer → Converter → Scripter` is built (Phases 4–5;
Phase 5 sits uncommitted on this branch). The Scripter stores the finished
narration on `session.script` and sends it to the chat as plain text. The final
missing deliverable is Phase 6:

> **ROADMAP §6 — Narrator:** "**Not an agent.** Script is routed directly to
> Gemini TTS (`gemini-3.1-flash-tts-preview`), rendered to **OGG/Opus**, and sent
> to the chat. Acceptance: the user receives a playable voice note in Telegram;
> output is valid OGG/Opus (**not** MP3); a TTS failure degrades gracefully."

Two facts established by live probe before this spec was written:

1. `gemini-3.1-flash-tts-preview` works with the project's `GEMINI_API_KEY` and
   returns **raw PCM** — `audio/l16; rate=24000; channels=1`, little-endian
   16-bit mono — *not* an Ogg container. Conversion to OGG/Opus is therefore
   mandatory, not optional.
2. This machine has **no ffmpeg, no libopus, no opus tools**. The pip package
   `soundfile` (bundles libsndfile) was proven in an isolated `/tmp` probe to
   write a valid Ogg container with an `OpusHead` page header.

`SPECS/TECH.md` hard requirement: *"Audio format is OGG/Opus, not MP3 … a hard
requirement, not a choice."*

## Scope

**In scope**

- A **Narrator module** (not an ADK agent): takes the finished script text,
  calls Gemini TTS directly via the `google-genai` client, and returns
  OGG/Opus bytes.
- **Voice direction:** a deep male voice with a dramatic, posh British
  wildlife-documentary accent (prebuilt deep-male voice preset + explicit
  style instruction in the synthesis request).
- **Edge contracts (typed, Pydantic):** parse the TTS response at the boundary
  (audio payload + mime), convert, and *verify the produced bytes are actually
  an Ogg/Opus stream* (`OggS` capture tag + `OpusHead`) before anything is
  handed to Telegram.
- **Transport:** extend the existing `MessageSender` protocol with
  `send_voice`, implemented on `TelegramSender` as multipart `sendVoice` with a
  `.ogg` file — a playable **voice note**, never `sendAudio`/MP3.
- **Integration:** auto-trigger immediately after the Scripter succeeds,
  reusing the existing session state (`session.script`), the existing
  auto-trigger path in `_run_converter`, and the existing handlers. The script
  text message is **kept**, the voice note follows it (user decision).
- **State machine:** enter `SCRIPTING` when the Converter returns the portrait,
  `NARRATING` when the Scripter returns the script, `DONE` when the Narrator
  delivers the voice note — per `SPECS/TECH.md`. Every failure path lands in
  `DONE` so the session is never stuck.
- **Error handling:** any TTS, conversion, format-verification, or Telegram
  failure logs loudly, degrades gracefully with a friendly reply on the user's
  conversation path, and never crashes the poll loop (`SPECS/TECH.md` error
  policy; mirrors the Converter's handling).
- **Dependency:** add `soundfile` to `requirements.txt` (user-approved). It
  bundles libsndfile; no system-level packages.
- **Tests:** offline unit tests (Red/Green TDD) for module, conversion, format
  contract, sender, gateway wiring, phase transitions, and error paths; one
  gated live test (`RUN_LIVE_TESTS=1`) that exercises the real TTS and asserts
  OGG/Opus output.

**Out of scope**

- Removing the Scripter's plain-text script message (user chose: keep text,
  then voice note).
- Any new ADK agent — the Narrator is a plain module (constitution).
- MP3 output, `sendAudio`, duration/preview tags, voice caching, temp-file
  storage of audio, config toggles, voice selection UI.
- Resilience-phase work (`/restart` temp-media cleanup guards, wrong-payload
  guards) — ROADMAP §7, separate spec.
- Changes to Bouncer/Interviewer/Converter behaviour.

## Decisions

| # | Decision | Rationale |
| --- | --- | --- |
| D1 | Encoder: `soundfile>=0.13,<1` added to `requirements.txt` and pip-installed | User-approved; proven in this environment to emit `OggS`+`OpusHead`; no system packages. |
| D2 | Script text message stays; voice note follows it | User-approved; minimal change preserving completed Scripter work. |
| D3 | Narrator calls `client.models.generate_content` directly (no `LlmAgent`, no runner) | TECH.md: "The Narrator is not an agent"; mirrors `GenAiConverter`'s direct-client style. |
| D4 | Deep-male prebuilt voice (e.g. `Charon`) + a style instruction wrapping the script ("deep, posh British, dramatic wildlife-documentary narrator …") | The accent/character comes from the style directive; the preset supplies the deep male timbre. Verified live by the gated test. |
| D5 | Two-stage audio contract: (a) TTS payload must declare `audio/l16 … rate=24000` and be non-empty; (b) produced bytes must start with `OggS` and contain `OpusHead` in the first page — checked by a dedicated verifier before send | TECH.md: parse/validate TTS output at the edge with typed models; never hand unverified bytes to Telegram. |
| D6 | Phase transitions `GENERATING_IMAGE → SCRIPTING → NARRATING → DONE` inside `_run_converter`; every failure branch sets `DONE` | TECH.md state-machine table; session must never be stuck mid-chain. |
| D7 | `send_voice` mirrors `send_photo`: multipart POST, `telegram_payload()`, no `raise_for_status()`, raise `RuntimeError` carrying Telegram's description | Secret-safe transport policy (TECH.md); consistent with existing transport code. |
| D8 | Narrator wired like every other stage: independent `try/except` at startup, `narrator_wired` + model id in `gateway_started`, `narrator=None` degrades to "not wired" | Consistency with Bouncer/Interviewer/Converter/Scripter wiring; one stage's init failure never kills the loop. |

## Contracts (input/output shapes)

- **Input:** `script: str` — non-empty, ≤ 600 chars (same bound as the
  `Script` model), already validated by the Scripter.
- **TTS response (untrusted):** must yield exactly one audio part whose
  `inline_data.mime_type` matches `audio/l16` with `rate=24000`,
  `channels=1`; data non-empty. Anything else (missing part, text-only
  response, unexpected mime, empty data) raises a typed error at the edge.
- **Output:** `bytes` — Ogg container, Opus payload, verified by
  magic-byte contract before send.
- **Transport request:** `sendVoice` multipart with
  `voice=(“voice.ogg”, bytes, “audio/ogg”)` + `chat_id`.
- **Failure behaviour:** every internal failure is caught, logged with a
  structured event, degrades to a friendly text reply, leaves the session in
  `DONE` and usable. No bare `except`, no swallowed exceptions.

## Logging (structured events)

`narration_synthesised` (model, script words, pcm bytes) and
`narration_converted` (ogg bytes, duration) — emitted from the Narrator
module, which has no chat context; `voice_sent` (chat_id, bytes),
`narrator_error`, `voice_send_failed`, `narrator_not_wired`, and
`narrator_init_failed` (all chat_id) — emitted at the gateway layer, where
the chat is known. Same event style as the existing stages; logging kept
out of business logic where the existing `logged` decorator pattern fits.
(Verifier note: the original draft listed `chat_id` on the two Narrator
module events; as implemented, `chat_id` lives only on the gateway-side
events — this section was corrected to match the code.)
