# Requirements — Resilience (ROADMAP Phase 7)

## Context

Phases 1–6 delivered the full happy path: Bouncer → Interviewer → Converter →
Scripter → Narrator. Every stage already degrades to a friendly reply instead
of raising into the poll loop (SPECS/TECH.md error policy), and `/start` /
`/restart` already purge the session (`SessionStore.reset`).

Three resilience gaps remain against ROADMAP §7 and SPECS/MISSION.md success
criteria:

1. **Reset media cleanup is implicit.** `SessionStore.reset` pops the session;
   the in-memory photo bytes it owned are dropped with it, but nothing states
   or logs that "temporary media" was purged, and there is no test that media
   (rather than just the dict entry) is gone.
2. **Stage guards are partial.** A photo/sticker *mid-interview* is already
   nudged (`handle_wrong_payload`), but a text or a photo arriving during the
   **image stage** (`GENERATING_IMAGE` / `SCRIPTING` / `NARRATING`) is not
   explicitly guarded — a stray photo there would be treated as a brand-new
   Bouncer submission and clobber the in-flight run.
3. **No retry/backoff on Gemini calls.** A transient Gemini timeout currently
   fails the stage on the first attempt. TECH.md error policy is explicitly
   *"Retry with backoff first; if it still fails, send a friendly message and
   keep the session alive."*

ROADMAP §7 acceptance: *"`/restart` leaves no residual state or temp media; a
follow-up run starts clean; out-of-order input continues the conversation; a
Gemini timeout never raises into the user's flow."*

## Scope

**In scope (minimal changes, reusing the existing architecture)**

- Explicit, logged purging of a chat's temporary media on `/start` / `/restart`,
  alongside the existing session purge — without restarting the process.
- Stage guards for out-of-order input:
  - **text during the image stage** (`GENERATING_IMAGE`, `SCRIPTING`,
    `NARRATING`) → a helpful "sit tight" message; the in-flight session is left
    untouched.
  - **a photo / non-text payload during the image stage** → the same helpful
    nudge, and it must **not** be treated as a fresh Bouncer submission.
  - **a photo / non-text payload during the interview**
    (`AWAITING_CONFIRMATION`, `INTERVIEWING`) → the existing "answer in words"
    nudge (already implemented; keep and cover it).
- A single shared retry-with-backoff helper applied to every **Gemini** call
  (Bouncer classification, Interviewer turns, Converter generation, Scripter,
  Narrator synthesis): limited attempts, exponential backoff, capped delay,
  retry only on transient/timeout errors. After the attempts are exhausted the
  error still propagates to the existing per-stage degrade path, so the user
  gets a friendly message and the conversation continues.
- Unit tests covering all four acceptance tests; docs updates.

**Out of scope**

- New agents, new pipeline stages, or changes to happy-path behaviour/messages.
- On-disk temp-file storage of any kind — the architecture keeps everything in
  memory (SPECS/TECH.md), so "temporary media" means the media held on the
  session. No new storage layer.
- Retrying Telegram transport failures (send/fetch) beyond what already exists;
  this phase retries **Gemini** calls only.
- Circuit breakers, retry queues, persistence, or per-user rate limiting.
- Live Gemini tests for the retry path — the retry helper is deterministic and
  unit-tested with an injected sleeper; no live calls needed.

## Decisions

| # | Decision | Rationale |
| --- | --- | --- |
| R1 | "Temporary media" is the media held on the in-memory session (original photo bytes + mime + file_id, and the script). Add `Session.clear_media()` and have `SessionStore.reset()` call it (after logging) before dropping the session; log `event=temp_media_purged chat_id=…`. | There are no on-disk temp files anywhere (Bouncer fetches to memory; Narrator encodes in memory). The session is the only owner of media, so reset already disposes of it — this makes the cleanup explicit, logged, and independently testable, and gives future media a single purge point. |
| R2 | Guard routing is decided by the session phase, in one place in `handle_update`. Define `IMAGE_STAGE_PHASES = {GENERATING_IMAGE, SCRIPTING, NARRATING}`. Text **or** a photo arriving in these phases returns a single helpful "portrait is in progress" reply and touches no session state; a photo there is never re-gated by the Bouncer. | Out-of-order input must continue the conversation, not restart it or stall (MISSION "Out-of-order input"). A stray photo during generation must not clobber the run in progress. |
| R3 | One shared helper module, `app/resilience.py`, owns the retry policy: `RetryPolicy(max_attempts=3, base_delay=0.5, factor=2.0, max_delay=4.0)` and `call_with_retries(call, *, policy=…, sleeper=time.sleep, is_retryable=is_transient_error)`. Only transient/timeout errors are retried; deterministic errors (validation, bad request) propagate immediately. The sleeper is injectable so tests are instant and deterministic. | Minimal change: one helper, applied at the five existing Gemini boundaries. TECH error policy: "Retry with backoff first; if it still fails … keep the session alive." Name-based transient detection avoids hard-coupling to SDK exception classes that differ across versions. |
| R4 | Retries live inside the concrete Gemini adapters (`AdkHumanPresenceClassifier.classify`, `AdkInterviewer.next_turn`/`final_turn`, `GenAiConverter.generate`, `AdkScripter.generate_script`, `GeminiTtsNarrator.synthesise`) — **not** in the gateway. | Those adapters are the exact Gemini boundary; the gateway already catches everything they raise and degrades. Wrapping here keeps the retry behaviour identical no matter which caller invokes the stage, and leaves gateway routing untouched. |
| R5 | After retries are exhausted, the adapter re-raises the last error unchanged; the existing per-stage degrade path (Bouncer fallback, Interviewer `RETRY_PROMPT`, Converter/Scripter/Narrator friendly fallback) notifies the user and keeps the session alive. | "If retries fail, notify the user and recover gracefully without crashing or breaking the conversation." No new user-facing copy is introduced. |
| R6 | The retry helper never sleeps longer than `max_delay` and makes at most `max_attempts` total calls (1 initial + `max_attempts - 1` retries). | Bounded worst-case latency on the synchronous long-poll loop; "limited retries". |

## Contracts

`app/resilience.py`:

- `RetryPolicy` (frozen dataclass): `max_attempts: int = 3`,
  `base_delay: float = 0.5`, `factor: float = 2.0`, `max_delay: float = 4.0`.
- `is_transient_error(exc: BaseException) -> bool` — True for builtin
  `TimeoutError`, `concurrent.futures.TimeoutError`, `httpx.TimeoutException`,
  and any exception whose type name contains `timeout`/`deadline`
  (case-insensitive) or that reports a retryable status of 429/500/502/503/504.
- `call_with_retries(call, *args, policy=RetryPolicy(), sleeper=time.sleep, is_retryable=is_transient_error, **kwargs)` —
  calls `call(*args, **kwargs)`; on a retryable error, sleeps
  `min(base_delay * factor**n, max_delay)` between attempts; re-raises the last
  error once attempts are exhausted. Non-retryable errors re-raise immediately.
  Logs `event=gemini_retry attempt=N delay=S` and, at exhaustion,
  `event=gemini_retry_exhausted attempts=N`.

`app/state.py`:

- `Session.clear_media()` — sets `original_photo_bytes`,
  `original_photo_mime`, `original_photo_file_id`, and `script` to `None` and
  touches the session. (Media only; leaves phase/dossier/pending question.)
- `Session.purge()` — `clear_media()` **plus** conversation history
  (`dossier`, `pending_question`). The explicit clear-point `reset()` uses.
- `SessionStore.reset(chat_id)` — `purge()`s the session (media + conversation
  history), logs `event=temp_media_purged`, then drops it. Idempotent; unknown
  chats are a no-op. There are no background tasks to cancel: the loop is
  synchronous and single-threaded, so an in-flight `handle_update` completes
  before any later reset is processed.

`app/interviewer.py`:

- `IMAGE_STAGE_PHASES` and a `handle_wrong_stage(chat_id)` helper returning a
  helpful `InterviewDecision` reply (constant copy), leaving state untouched.
  (Name/placement at the implementer's discretion so long as the routed
  behaviour and the single reply constant are testable.)

## Behaviour

- `/start` or `/restart` in **any** phase purges the session and its media, then
  replies with `PHOTO_PROMPT` (unchanged user-visible behaviour). The process is
  never restarted; the next run starts clean.
- A text message arriving while `phase in IMAGE_STAGE_PHASES` → the helpful
  "portrait in progress" reply; session and phase unchanged.
- A photo/sticker/other non-text arriving while `phase in IMAGE_STAGE_PHASES`
  → the same helpful reply; the Bouncer is **not** invoked and the session is
  unchanged.
- A photo/sticker arriving in `AWAITING_CONFIRMATION` or `INTERVIEWING` → the
  existing "answer in words" nudge; the pending question is preserved
  (unchanged).
- Any Gemini call that raises a transient/timeout error is retried up to
  `max_attempts` with capped exponential backoff. On success, behaviour is
  exactly as before. On exhaustion, the error reaches the existing degrade path:
  the user gets a friendly reply and the session is not broken.
- Retry sleeps are bounded (`<= max_delay`) so the long-poll loop is never
  blocked indefinitely.

## Acceptance criteria

1. **Restart leaves no residual user state or temporary media.** After
   `/start` / `/restart`, the chat has no session and no residual artifacts
   retained anywhere — conversation history (dossier, open question) and
   temporary media (photo bytes, mime, file_id, script) are all cleared, even
   via a held reference to the old session; the purge is logged. No background
   task survives (none exist: the loop is synchronous).
2. **The next run starts cleanly.** A photo + interview immediately after a
   reset behaves exactly like a first ever run (fresh dossier, `AWAITING_PHOTO`
   → admission → interview).
3. **Out-of-order inputs are handled without getting the conversation stuck.**
   Text during the image stage and an image during the interview each return a
   helpful reply and leave the conversation able to continue; a photo during the
   image stage is not treated as a new submission.
4. **Gemini timeouts never crash the bot or raise unhandled errors into the user
   flow.** A transient Gemini timeout is retried with backoff and, if it
   persists, degrades to a friendly reply with the session alive and no
   exception escaping into `handle_update` / the poll loop.
