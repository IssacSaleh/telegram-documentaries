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

Later phases (interview, converter, narration, voice) are ordered in
[`SPECS/ROADMAP.md`](SPECS/ROADMAP.md).

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
./scripts/test    # 107 offline tests + 3 live (skipped by default)
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
