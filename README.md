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

**Phase 1 — Repository & gateway (in progress).** The bot long-polls Telegram
and replies to every message with a hardcoded `hey mate!`. **No Gemini/model
API calls yet**; `GEMINI_API_KEY` is loaded from `.env` but unused.

Next phases are ordered in [`SPECS/ROADMAP.md`](SPECS/ROADMAP.md).

## Contents

| Path | Description |
| --- | --- |
| `app/` | Gateway source: models, state, config, logging, polling loop |
| `app/main.py` | **Entrypoint** — `python -m app.main` |
| `tests/` | Test suite (`unit/` = no real I/O, `integration/` = real API) |
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

Both keys are **required** by the loader, even though Phase 1 only uses
`TELEGRAM_BOT_TOKEN` — the config surface matches `.env.example` so later
phases need no changes.

> **Note:** `.env` is gitignored and must never be committed. Secret values are
> never logged — `Settings.__repr__` redacts them.

### 3. Run the bot

```bash
python -m app.main
```

It long-polls Telegram and replies `hey mate!` to every text message. Stop it
with `Ctrl-C`.

## Development

Two scripts are the **ground truth** for checks (SPECS/TECH.md). Run both
before every commit:

| Script | What it runs | When |
| --- | --- | --- |
| `./scripts/test` | `pytest tests/` | Whenever you change behaviour |
| `./scripts/hooks` | `ruff check app tests` + `mypy app` | Before every commit |

```bash
./scripts/test    # 40 tests
./scripts/hooks   # lint + type checks
```

Tests are written **before** code (Red/Green TDD — SPECS/TECH.md). See
[`SPECS/TECH.md`](SPECS/TECH.md) for the full policy.

## Licence

All rights reserved unless otherwise stated.
