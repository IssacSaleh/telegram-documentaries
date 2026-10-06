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

## Contents

| File | Description |
| --- | --- |
| `telegram-arch.html` | Interactive diagram: **Polling vs Webhooks** for the Telegram Bot API |
| `applet_prompts.md` | The generation prompts used to produce the applets |
| `.env.example` | Template for required secrets |

### `telegram-arch.html`

A self-contained, dependency-free page covering:

- **Short polling** — repeated rapid requests; wasteful and rate-limit prone
- **Long polling** — a single hanging request that streams updates; the standard
  approach for local/development bots
- **Webhooks** — Telegram pushes updates to a public HTTPS endpoint; requires a
  static IP and valid TLS certificate

Open it directly in a browser — no build step or server required.

## Setup

Copy the example env file and fill in your own credentials:

```bash
cp .env.example .env
```

```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
GEMINI_API_KEY=your_gemini_api_key_here
```

> **Note:** `.env` is gitignored and must never be committed.

## License

All rights reserved unless otherwise stated.
