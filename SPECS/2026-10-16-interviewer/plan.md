# Plan — Interviewer (ROADMAP Phase 3)

Red/Green TDD throughout: each group writes failing tests first, then the
minimum code to pass. Run the repo dev scripts after every group —
`./scripts/test` (pytest) and `./scripts/hooks` (ruff + mypy). Never hand-roll
`pytest`/`ruff` invocations in place of the scripts.

Naming: new module `app/interviewer.py` (agent wrapper + flow logic), shared
ADK helper extracted to `app/adk_utils.py`, gateway routing extended in
`app/gateway.py`, dossier state on `app/state.py`.

---

## 1. Typed contracts

- [ ] 1.1 Red: tests for `InterviewTurn` — accepts a normal turn; rejects
  `is_complete=true` without `suggested_animal`; rejects a non-complete turn
  that carries an animal; rejects empty `next_question` when incomplete.
- [ ] 1.2 Red: tests for `Dossier` / `DossierAnswer` — order preserved, `asked`
  derived, `follow_ups_used` defaults to 0.
- [ ] 1.3 Green: add `InterviewTurn`, `Dossier`, `DossierAnswer` to
  `app/models.py` with the model validators described in requirements.md.
- [ ] 1.4 Add `SessionPhase.AWAITING_CONFIRMATION` and bump
  `SESSION_SCHEMA_VERSION` to 2 (`app/state.py`), with `dossier: Dossier | None`
  on `Session`.
- [ ] 1.5 Run `./scripts/test` + `./scripts/hooks`.

## 2. Shared ADK output helper

- [ ] 2.1 Red: move-and-reuse test — `_extract_json` behaviour preserved
  (prose tolerance, first complete object, unbalanced passthrough) when called
  from its new home.
- [ ] 2.2 Green: extract `_extract_json` from `app/bouncer.py` into
  `app/adk_utils.py` as `extract_json`; import it in `bouncer.py` (no behaviour
  change) so the Interviewer reuses the identical parsing rule.
- [ ] 2.3 Run `./scripts/test` + `./scripts/hooks` (Bouncer tests must stay
  green — this is a pure move).

## 3. Dossier storage on the single state driver

- [ ] 3.1 Red: tests that a dossier created for chat A is invisible to chat B;
  `reset()` clears it; `get_or_create` does not create one implicitly.
- [ ] 3.2 Green: add `start_dossier(chat_id)` / `get_dossier(chat_id)` helpers
  on `SessionStore` (or `Session`) — one place, no second store.
- [ ] 3.3 Run `./scripts/test` + `./scripts/hooks`.

## 4. Interviewer agent wiring

- [ ] 4.1 Red: wiring test — the agent is `LlmAgent(name="interviewer",
  model="gemini-3.1-flash-lite", output_schema=InterviewTurn)`, with a
  non-empty instruction that states the 5–7 rule, one-question-at-a-time, and
  the ask-for-an-animal goal.
- [ ] 4.2 Red: parsing test with a stubbed runner — plain JSON and
  prose-wrapped JSON both validate into `InterviewTurn`; a malformed response
  raises (caller degrades).
- [ ] 4.3 Green: implement `AdkInterviewer` in `app/interviewer.py`, mirroring
  `AdkHumanPresenceClassifier` (lazy ADK import, `asyncio.run`, per-call
  runner, `extract_json`).
- [ ] 4.4 Run `./scripts/test` + `./scripts/hooks`.

## 5. Flow logic (pure, testable, never raises)

- [ ] 5.1 Red: `handle_photo_admitted(chat_id)` → phase
  `AWAITING_CONFIRMATION`, confirmation prompt returned.
- [ ] 5.2 Red: confirmation handling — `yes`/`YES`/` y ` start the interview;
  other text re-asks; no session change beyond phase.
- [ ] 5.3 Red: answer handling — each answer is appended as
  `DossierAnswer(pending_question, text)`; the next question is returned and
  stored as the new pending question; `follow_ups_used` increments when the
  model marks a follow-up.
- [ ] 5.4 Red: completion — at `is_complete` (and ≥5 answers) the phase becomes
  `DONE`, the animal/rationale are stored, and the templated closing reply
  names the animal.
- [ ] 5.5 Red: guard rails — premature `is_complete` (<5) is ignored and
  another question is asked; the 7th answer forces completion; a model failure
  leaves phase + pending question untouched and returns the fallback reply.
- [ ] 5.6 Red: wrong payloads — a photo/sticker mid-interview returns the
  "answer in words" reply, makes no model call, and preserves the pending
  question.
- [ ] 5.7 Green: implement the flow in `app/interviewer.py` as pure functions
  over `Session` + injected `Interviewer` protocol double, returning
  `InterviewDecision` (typed, never a raw dict), mirroring `BouncerDecision`.
- [ ] 5.8 Run `./scripts/test` + `./scripts/hooks`.

## 6. Gateway routing

- [ ] 6.1 Red: routing tests — admitted photo now replies with the
  confirmation prompt (D8, supersedes `hey mate!` on that path); text with no
  session replies "send me a photo to start" (D4); text in
  `AWAITING_CONFIRMATION`/`INTERVIEWING` routes to the Interviewer; a photo
  mid-interview gets the polite note; unknown payloads still ignored.
- [ ] 6.2 Red: `/start` and `/restart` mid-interview purge the dossier and the
  next text gets the "send me a photo" prompt.
- [ ] 6.3 Green: extend `handle_update` routing by phase; wire
  `AdkInterviewer` in `run()` alongside the Bouncer, with the same
  never-silence fallback policy (`InterviewerFallbackPolicy`).
- [ ] 6.4 Run `./scripts/test` + `./scripts/hooks`.

## 7. Live integration (real Gemini)

- [ ] 7.1 Add `tests/integration/test_live_interviewer.py` behind
  `RUN_LIVE_TESTS=1`, using the existing fixture photo for admission and canned
  answers for the interview:
  - a portrait is admitted → confirmation prompt → `yes` → 5–7 questions are
    asked one at a time → completion names an animal;
  - the dossier holds one Q/A pair per question and the stored animal matches
    the closing message;
  - two concurrent chats running interviews never see each other's dossier.
- [ ] 7.2 Run `RUN_LIVE_TESTS=1 ./scripts/test` and record the observed
  animal/questions in the validation notes.

## 8. Docs and constitution

- [ ] 8.1 Update `README.md`: phase status, the new flow (photo → confirm →
  interview → animal), test counts, and the corrected behaviour for text with
  no session.
- [ ] 8.2 Update `SPECS/TECH.md`: `AWAITING_CONFIRMATION` phase, the dossier as
  part of the session schema (version 2), the shared `extract_json` helper, and
  the wrong-payload guard table.
- [ ] 8.3 Update `SPECS/ROADMAP.md` §3 with a `**Delivered:**` note and the
  handoff-point drift (D2) once verified.
- [ ] 8.4 Write `validation.md` results, including the live transcript
  summary.
- [ ] 8.5 Run `./scripts/test` + `./scripts/hooks` one final time before the
  commit/PR step.
