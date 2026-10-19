# Validation — Resilience (ROADMAP Phase 7)

## 1. Automated checks
| Check | Command | Expected |
| --- | --- | --- |
| Unit + integration | `./scripts/test` | All pass; live tests skipped by default |
| Lint + types | `./scripts/hooks` | ruff clean, mypy clean |

## 2. Acceptance tests (from the phase brief)
- [x] **A1 — Restart leaves no residual user state or temporary media.**
  `/start` and `/restart` (in any phase) drop the session and its media
  (photo bytes/mime/file_id, script); the purge is logged
  (`event=temp_media_purged`); no media is retained anywhere.
- [x] **A2 — The next run starts cleanly.** After a reset, an immediate
  photo + interview proceeds exactly like a first run (fresh dossier,
  `AWAITING_PHOTO` → admitted → interviewing).
- [x] **A3 — Out-of-order inputs are handled without getting the conversation
  stuck.** Text during the image stage and an image during the interview each
  return a helpful reply and leave the conversation able to continue; a photo
  during the image stage is not re-gated as a new submission.
- [x] **A4 — Gemini timeouts never crash the bot or raise unhandled errors into
  the user flow.** A transient Gemini timeout is retried with backoff; on
  exhaustion the stage degrades to a friendly reply, the session stays alive,
  and no exception escapes `handle_update` / `poll_once`.

## 3. Contract checks
- [x] `app/resilience.py` exposes `RetryPolicy`, `is_transient_error`,
  `call_with_retries`; only transient errors are retried; at most
  `max_attempts` calls; delay capped at `max_delay`; sleeper injectable.
- [x] `Session.clear_media()` clears every media field and preserves phase/dossier.
- [x] `SessionStore.reset` remains idempotent and per-chat isolated.
- [x] Stage guards mutate no session state and invoke no model/transport.
- [x] Existing user-facing copy for the guards is a single named constant.
- [x] No secrets logged; no new on-disk temp files.

## 4. Behaviour checks
- [x] Happy path unchanged: Bouncer → Interviewer → Converter → Scripter →
  Narrator still runs end to end, with the portrait, script text, and voice note
  delivered in order.
- [x] A successful stage call after retries is byte-for-byte the same behaviour
  as a first-try success.
- [x] Retry sleeps are bounded so the long-poll loop is never blocked
  indefinitely.
- [x] Session isolation preserved (unchanged).
- [x] `/restart` never restarts the Python process.

## 5. Merge gate
- [x] `./scripts/test` green (offline)
- [x] `./scripts/hooks` clean
- [x] ROADMAP.md §7 marked delivered/verified
- [x] TECH.md / MISSION.md / README.md in sync
- [x] This validation.md updated with actual results
- [x] No secrets in tracked files or logs

## 6. Results
_(Filled in by the verifier after running the checklist.)_

- Automated checks: `./scripts/test` → **368 passed, 8 skipped** (the 8 are the
  `RUN_LIVE_TESTS=1`-gated live Gemini tests). `./scripts/hooks` → ruff
  **All checks passed!**, mypy **Success: no issues found in 14 source files**.
- Acceptance tests: **A1–A4 all pass.**
  - A1 — `test_state.py::TestReset::test_reset_purges_the_chats_temporary_media`,
    `test_reset_clears_media_fields_before_dropping_the_session`,
    `TestPurge::test_clears_conversation_history_and_media`,
    `test_reset_clears_a_held_reference_too`; and
    `test_gateway.py::TestRestartCommand::test_reset_purges_temporary_media_and_logs_the_purge`
    + `test_reset_mid_interview_clears_history_and_media` (mid-pipeline and
    mid-interview runs; `/start` and `/restart`; conversation history, media,
    and held references all cleared; `event=temp_media_purged`). No background
    tasks exist (synchronous loop), so none can survive.
  - A2 — `test_state.py::TestReset::test_reset_purges_the_session_so_the_next_run_starts_clean`
    and `test_gateway.py::TestEndToEndResilience::test_a_fresh_run_after_restart_behaves_like_a_first_run`
    (full run → `/restart` → fresh photo + interview, fresh dossier).
  - A3 — `test_gateway.py::TestImageStageGuards` (text/photo/sticker in each
    image phase get the nudge, no state mutation, Bouncer/fetcher untouched)
    + the mid-interview wrong-payload regression.
  - A4 — `test_resilience.py` (retry/backoff/exhaustion/non-transient/logs),
    the five adapter retry suites (`test_bouncer.py`, `test_interviewer.py`,
    `test_converter.py`, `test_scripter.py`, `test_narrator.py`), and
    `test_gateway.py::TestEndToEndResilience` (exhausted timeouts through
    `handle_update` degrade to `RETRY_PROMPT` / Bouncer fallback without raising).
- Notes: The retry helper wraps only the single Gemini call inside each of the
  five adapters; the gateway's existing per-stage degrade paths are unchanged.
  `/start`/`/restart` are still evaluated before the new stage guard, so a reset
  wins in any phase. `MISSION.md` needed no wording change (reset + temporary
  media cleanup and wrong-stage handling were already in scope). During the
  build the `spec-implementer`/`code-reviewer` subagents aborted repeatedly, so
  the review/verification passes and the group-5 end-to-end tests were completed
  directly; all checks above were run locally against this branch.
