# MISSION

## Vision

Send a portrait photo to a Telegram bot, get back a narrated BBC-style wildlife
documentary about yourself: a hybrid animal portrait plus a dramatic voice note.

The user's experience, end to end:

1. `/start` → bot greets them.
2. User uploads a portrait photo.
3. **Bouncer** confirms a human is present (cheekily rejects anything else and
   resets state).
4. **Interviewer** asks 5–7 questions, one at a time, building a behavioural
   dossier tied to `chat_id`.
5. **Converter** fuses the original photo + interview dossier + the
   Interviewer's suggested animal into a hybrid animal portrait, sent straight
   back to Telegram.
6. **Scripter** writes a one-paragraph (~60–90 word) British-documentary
   narration from the dossier.
7. **Narrator** renders that script to audio and delivers it as a voice note.

`/restart` (and `/start`) purge the session and any temporary media so the user
can begin again without restarting the process.

## In scope

- The five-stage pipeline above (Bouncer → Interviewer → Converter → Scripter →
  Narrator).
- Telegram **long polling** transport.
- In-memory session state keyed by `chat_id`.
- `/start` and `/restart` reset semantics, including temporary media cleanup.
- Graceful handling of text or media arriving at the wrong stage.

## Out of scope (v1)

These are explicitly **not** built. Do not add them.

- **Video generation** — output is images + audio only.
- **Webhooks / public URL** — no ngrok, no public endpoint, no TLS cert.
- **Persistent DB / sessions** — no Redis, no Postgres. A restart wipes state.

YAGNI defaults, also out of scope unless the user asks:

- Multi-image or album intake (one portrait per run).
- Accounts, payments, or any auth beyond Telegram's own.
- Any hypothetical future feature not named above.

## Success criteria

What "working" means:

- **Happy path:** a portrait goes in; a hybrid portrait and a narrated voice note
  come out, with no manual intervention.
- **Reset:** `/restart` and `/start` purge session state *and* temporary media
  files without restarting the process. A subsequent run starts clean.
- **Out-of-order input:** text sent mid-image-stage, or an image sent mid-
  interview, is handled gracefully — the conversation continues rather than
  crashing or silently stalling.

## Non-negotiables

- **Never leak another user's session.** State is strictly per-`chat_id`.
- **Never hardcode or commit secrets.** `.env` stays in `.gitignore`;
  `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY` are read from the environment only.
