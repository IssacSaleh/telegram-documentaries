# TECH

## Stack

| Concern | Choice |
| --- | --- |
| Language | Python |
| Transport | Telegram Bot API, **long polling** |
| Framework | Google Agent Development Kit (ADK) |
| Vision gate / interview / script | `gemini-3.1-flash-lite` |
| Image generation | Gemini 3.1 Flash Image |
| Narration | `gemini-3.1-flash-tts-preview` → **OGG/Opus** |
| Validation | Pydantic |
| Secrets | `.env` (`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`) |

> **Note:** these model IDs are dated versions. If Google retires one, this file
> is the single place that must be updated — every other document defers here.

## Architecture

ADK **hub-and-spoke**:

- The **Interviewer** is the orchestrator. It owns the conversation loop,
  accumulates the dossier, and decides when to advance.
- Every other stage is a discrete agent or module with one job.
- The **Narrator is not an agent.** The Scripter's output is routed directly to
  Gemini TTS and sent to Telegram without an intermediate text hop.
- **Audio format is OGG/Opus, not MP3.** Telegram voice notes require OGG/Opus;
  an MP3 is not a valid voice note. This is a hard requirement, not a choice.
- Stages are driven by an explicit **state machine** with named phases, not by
  inference over the raw message history.

Pipeline: `Bouncer → Interviewer → Converter → Scripter → Narrator`.

## Contracts at boundaries

- Parse Telegram updates, Gemini responses, and TTS output into **typed Pydantic
  models at the edge.**
- **Never pass raw dicts or unvalidated payloads across module boundaries.**
- Treat all external input — Telegram messages, file contents, model output — as
  **untrusted and arbitrary.** Validate before acting on it.

## Logging & error policy

- Comprehensive, **structured** logging.
- Prefer **decorators** over threading logging calls through business logic.
- **Off the user's conversation path** (background, user-invisible work): fail
  loudly and log.
- **On a validated user's conversation path**: catch, log loudly, and **degrade
  gracefully** so the conversation continues. Retry with backoff first; if it
  still fails, send a friendly message and keep the session alive. **Never raise
  into the user's flow.**
- Prohibited: bare `except: pass`, swallowed exceptions, un-logged fallbacks.

## Session state

- **In-memory only.** No database, no Redis, no on-disk session store. A process
  restart wipes all state — that is expected, not a bug.
- Versioned, per-`chat_id` schema. Never share state across chat IDs.
- **One shared state driver** — stages do not each invent their own storage.
- `/start` and `/restart` purge session state and temporary media, without
  restarting the process.

### State machine phases

The state driver holds exactly one phase per session. Stages transition it
explicitly; nothing advances by inferring progress from message history.

| Phase | Entered when | Next phase |
| --- | --- | --- |
| `AWAITING_PHOTO` | `/start` or `/restart` | `INTERVIEWING` |
| `INTERVIEWING` | Bouncer passes a human portrait | `GENERATING_IMAGE` |
| `GENERATING_IMAGE` | Interviewer finishes the dossier | `SCRIPTING` |
| `SCRIPTING` | Converter returns a hybrid portrait | `NARRATING` |
| `NARRATING` | Scripter returns 60–90 words | `DONE` |
| `DONE` | Narrator delivers the voice note | `AWAITING_PHOTO` (on `/restart`) |

Any message arriving in the wrong phase is handled by the wrong-payload guard
(ROADMAP phase 7), not by crashing or silently stalling.

## Testing

- **Red/Green TDD.** Tests are written *before* code; a failing test precedes
  each implementation.
- Dev scripts live in `scripts/` and are the **ground truth** for tests, lint,
  and type checks:
  - `scripts/test`
  - `scripts/hooks`
- These scripts are documented here **and** in the README, and kept in sync.

## Repo hygiene

- `.env` is in `.gitignore` and must never be committed.
- Dependencies and environment are reproducible from a clean checkout.

## README policy

The README must document, and stay in sync with:

- How to configure `.env`.
- What `scripts/test` and `scripts/hooks` run, and when to run them.
- Any developer-facing pipeline behaviour that changes how the bot is run or
  verified.
