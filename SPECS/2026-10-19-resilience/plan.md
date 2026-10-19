# Plan — Resilience (ROADMAP Phase 7)

Red/Green TDD. Run `./scripts/test` + `./scripts/hooks` after each group. All
changes on feature branch `feature/2026-10-19-resilience`. Minimal changes,
reusing the existing architecture — no new agents, no new storage.

## 1. Retry helper module (`app/resilience.py`)
- [x] 1.1 Red: tests for `is_transient_error` — True for `TimeoutError`,
  `concurrent.futures.TimeoutError`, `httpx.TimeoutException`, and a
  name-based timeout (e.g. a `class GeminiTimeoutError(Exception)`); False for
  `ValueError`, `RuntimeError("validation failed")`.
- [x] 1.2 Red: tests for `call_with_retries` — success on first try; retry then
  success (records sleeps); exhausts after `max_attempts` and re-raises the
  last error; non-retryable error re-raises immediately with no sleep; delay is
  `min(base_delay * factor**n, max_delay)`; sleeper injected (no real sleeping).
- [x] 1.3 Green: implement `app/resilience.py` (`RetryPolicy`,
  `is_transient_error`, `call_with_retries`) with structured logging
  (`event=gemini_retry`, `event=gemini_retry_exhausted`).
- [x] 1.4 Run `./scripts/test` + `./scripts/hooks`.

## 2. Reset purges temporary media explicitly
- [x] 2.1 Red: `Session.clear_media()` clears `original_photo_bytes`,
  `original_photo_mime`, `original_photo_file_id`, `script` and leaves
  phase/dossier intact.
- [x] 2.2 Red: `SessionStore.reset` removes the session, is idempotent, leaves
  other chats intact, and logs `event=temp_media_purged`.
- [x] 2.3 Red: gateway `/start` and `/restart` purge media — after a mid-pipeline
  run (photo stored, script set) the chat has no session and no media kept.
- [x] 2.4 Green: add `Session.clear_media()`; call it (with a log line) inside
  `SessionStore.reset` before dropping the session.
- [x] 2.5 Green: extend the existing gateway reset branch to log the purge
  (routing/copy unchanged).
- [x] 2.6 Run `./scripts/test` + `./scripts/hooks`.

## 3. Stage guards for out-of-order input
- [x] 3.1 Red: text in `GENERATING_IMAGE` / `SCRIPTING` / `NARRATING` returns a
  helpful reply and leaves phase + state untouched (no model call).
- [x] 3.2 Red: a photo in `GENERATING_IMAGE` / `SCRIPTING` / `NARRATING` returns
  the same helpful reply, does **not** invoke the Bouncer, and does not clobber
  the session (phase unchanged, stored photo/script unchanged).
- [x] 3.3 Red: a photo/sticker in `AWAITING_CONFIRMATION` / `INTERVIEWING`
  returns the wrong-payload nudge and preserves the pending question
  (regression coverage for the existing guard).
- [x] 3.4 Green: add `IMAGE_STAGE_PHASES` + `handle_wrong_stage` (single reply
  constant, no state mutation) and route text/photo in those phases to it in
  `handle_update`, before the Bouncer path.
- [x] 3.5 Run `./scripts/test` + `./scripts/hooks`.

## 4. Apply retry/backoff at the Gemini boundaries
- [x] 4.1 Red: `AdkHumanPresenceClassifier.classify` retries a transient error
  and returns the verdict on success; propagates after exhaustion.
- [x] 4.2 Red: `AdkInterviewer.next_turn` / `final_turn` retry a transient error
  and return the turn on success; propagate after exhaustion.
- [x] 4.3 Red: `GenAiConverter.generate` retries the timeout then succeeds;
  propagates after exhaustion.
- [x] 4.4 Red: `AdkScripter.generate_script` retries the timeout then succeeds;
  propagates after exhaustion.
- [x] 4.5 Red: `GeminiTtsNarrator.synthesise` retries the timeout then succeeds;
  propagates after exhaustion.
- [x] 4.6 Green: wrap each adapter's single Gemini call in
  `call_with_retries(...)` (keep the existing body as the inner call; retry only
  the probing wrapper). Inject/patch the sleeper so tests never really sleep.
- [x] 4.7 Run `./scripts/test` + `./scripts/hooks`.

## 5. End-to-end resilience through the gateway
- [x] 5.1 Red: an interview turn whose Gemini call times out repeatedly degrades
  to `RETRY_PROMPT`, keeps phase/pending, and never raises out of
  `handle_update`.
- [x] 5.2 Red: a Bouncer classification timeout degrades to the Bouncer fallback
  reply; the session is untouched (unchanged) and no exception escapes.
- [x] 5.3 Red: after a full run + `/restart`, an immediate fresh photo+interview
  behaves like a first run (acceptance test 1 & 2 combined).
- [x] 5.4 Green: wiring only as needed (e.g. sleeper injection) — no routing
  rewrite.
- [x] 5.5 Run `./scripts/test` + `./scripts/hooks`.

## 6. Docs & verification
- [x] 6.1 Update ROADMAP.md §7 with a **Delivered** block naming the modules,
  policy, and test coverage; mark §7 verified.
- [x] 6.2 Update TECH.md: retry policy + reset-purges-media note in the error
  policy / session-state sections; note stage guards.
- [x] 6.3 Update MISSION.md if any reset/out-of-order wording needs sharpening
  (should already hold).
- [x] 6.4 Update README.md: Phase 7 status; test count.
- [x] 6.5 Full `./scripts/test` green and `./scripts/hooks` clean.
- [x] 6.6 Update this spec's validation.md with actual results.
