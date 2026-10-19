# Telegram Documentaries

Interactive learning applets explaining how Telegram bots work — part of the
Telegram Documentaries project.

## Constitution

The project contract lives in [`SPECS/`](SPECS/) — read these first:

| File | Purpose |
| --- | --- |
| [`SPECS/MISSION.md`](SPECS/MISSION.md) | What the product does, scope, and success criteria |
| [`SPECS/TECH.md`](SPECS/TECH.md) | Stack, architecture, and the policies that are enforced |
| [`SPECS/ROADMAP.md`](SPECS/ROADMAP.md) | Ordered build plan, phase by phase |

Every feature spec and implementation decision defers to these three files.

## Project status

**Phase 1 — Repository & gateway.** The bot long-polls Telegram and replies
to every text message with a hardcoded `hey mate!`.

**Phase 2 — The Bouncer (vision gate).** Upload a photo and the bot downloads
the image bytes, then hands them to **Gemini 3.1 Flash Lite** through a Google
ADK agent for multimodal classification: is a discernible human face or body
present?

- **Human detected** → the admission falls through to the confirmation flow,
  which continues unchanged.
- **No human detected** → a cheeky rejection is sent and that chat's ephemeral
  state is reset. Rejects are logged.
- **Bouncer failure** (download or classification error) → a friendly
  fallback reply, never silence; the session is left untouched.

`GEMINI_API_KEY` is required: it authorises the Bouncer's Gemini calls.

**Phase 3 — The Interviewer.** Once admitted, the bot runs a short adaptive
interview (5–7 questions, one at a time), building a behavioural dossier
and suggesting an animal — state is per-chat and never shared.

**Phase 4 — The Converter.** When the interview finishes, the original
photo, the dossier, and the suggested animal are fused into a hybrid animal
portrait (Gemini image generation) and sent to the chat — no intermediate
text hop.

**Phase 5 — The Scripter.** The dossier becomes a ~60–90-word dramatic
British-documentary narration, validated as text and sent to the chat.

**Phase 6 — The Narrator.** The finished script is rendered to audio by
Gemini TTS (not an agent — a direct client call), converted to OGG/Opus,
and delivered as a **playable Telegram voice note** after the script text.
A TTS or send failure degrades to a friendly reply; the session always ends
in `DONE`.

**Phase 7 — Resilience.** `/start` and `/restart` purge session state *and*
temporary media (photo bytes and script) in any phase, without restarting the
process, and log the purge. Stage guards answer text or a stray photo during
the image stage with one "sit tight" nudge, leaving the run untouched. A
transient Gemini timeout is retried with capped backoff at each model
boundary; on exhaustion the stage degrades to a friendly reply and the
session stays alive — no exception ever escapes into the user's flow.

**Phase 8 — Polish.** Two small refinements: the mid-interview out-of-order
nudge now also offers `/restart` as an escape hatch, and a Scripter failure
(or empty narration) degrades to a friendly reply instead of silence — the
portrait is already out, so the user is told the narration is not coming.

The full pipeline: `Bouncer → Interviewer → Converter → Scripter →
Narrator` — send a portrait, answer the interview, and receive the hybrid
portrait, the script text, and a playable voice note.

Every phase is now delivered; see [`SPECS/ROADMAP.md`](SPECS/ROADMAP.md) for
the ordered plan and delivered notes.

## Contents

| Path | Description |
| --- | --- |
| `app/` | Gateway source: models, state, config, logging, polling loop |
| `app/main.py` | **Entrypoint** — `python -m app.main` |
| `app/bouncer.py` | **The Bouncer** — photo download + Gemini vision classification |
| `tests/` | Test suite (`unit/` = no real I/O, `integration/` = real API) |
| `tests/fixtures/` | Content-verified images for the live Bouncer tests |
| `scripts/test`, `scripts/hooks` | Dev scripts — ground truth for checks |
| `requirements.txt`, `requirements-dev.txt` | Reproducible dependencies |
| `telegram-arch.html` | Interactive diagram: **Polling vs Webhooks** for the Telegram Bot API |
| `applet_prompts.md` | The generation prompts used to produce the applets |
| `.env.example` | Template for required secrets |
| `SPECS/` | The constitution (see above) |

### `telegram-arch.html`

A self-contained, dependency-free page covering:

- **Short polling** — repeated rapid requests; wasteful and rate-limit prone
- **Long polling** — a single hanging request that streams updates; the standard
  approach for local/development bots
- **Webhooks** — Telegram pushes updates to a public HTTPS endpoint; requires a
  static IP and valid TLS certificate

Open it directly in a browser — no build step or server required.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt        # runtime
pip install -r requirements-dev.txt    # + test/lint/type tools
```

### 2. Configure `.env`

Copy the example env file and fill in your own credentials:

```bash
cp .env.example .env
```

```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
GEMINI_API_KEY=your_gemini_api_key_here
```

Both keys are **required** by the loader:

- `TELEGRAM_BOT_TOKEN` — the transport.
- `GEMINI_API_KEY` — the Bouncer's Gemini vision classification.

> **Note:** `.env` is gitignored and must never be committed. Secret values are
> never logged — `Settings.__repr__` redacts them, and no Telegram request
> failure may surface the token-bearing URL in a log line or exception.

### 3. Run the bot

```bash
python -m app.main
```

It long-polls Telegram and replies `hey mate!` to every text message. Send it
a photo to exercise the Bouncer: a portrait is admitted, anything else gets
the rejection. Stop it with `Ctrl-C`.

## Development

Two scripts are the **ground truth** for checks (SPECS/TECH.md). Run both
before every commit:

| Script | What it runs | When |
| --- | --- | --- |
| `./scripts/test` | `pytest tests/` | Whenever you change behaviour |
| `./scripts/hooks` | `ruff check app tests` + `mypy app` | Before every commit |

```bash
./scripts/test    # 372 offline tests + 8 live (skipped by default)
./scripts/hooks   # lint + type checks
```

The default run is **offline and deterministic**: unit tests make no network
calls, and the live Gemini tests are skipped.

### Live Bouncer tests

Three integration tests drive the real Bouncer end to end against Gemini 3.1
Flash Lite — a person is admitted, a landscape is rejected (with state reset),
and a rejection never touches another chat's state. Enable them explicitly:

```bash
RUN_LIVE_TESTS=1 ./scripts/test
```

They read `tests/fixtures/person.jpg` and `tests/fixtures/landscape.jpg`,
whose contents were verified against the same model.

Tests are written **before** code (Red/Green TDD — SPECS/TECH.md). See
[`SPECS/TECH.md`](SPECS/TECH.md) for the full policy.

## Licence

All rights reserved unless otherwise stated.
