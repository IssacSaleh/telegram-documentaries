# Validation — Phase polish

Run from the repo root on `feature/2026-10-20-phase-polish`.

## Automated

- [x] `./scripts/test` — full suite passes (existing tests plus the new ones).
- [x] `./scripts/hooks` — `ruff check` clean, `mypy app` clean.

## Slice 1 — nudge advertises `/restart`

- [x] A test asserts the reply for a **photo** during `INTERVIEWING`
      mentions `/restart`.
- [x] A test asserts the reply for a **sticker** during
      `AWAITING_CONFIRMATION` mentions `/restart`.
- [x] The nudge still asks the user to answer in words.
- [x] The pending question and phase are unchanged after the nudge.
- [x] A constant-level test in `test_interviewer.py` locks the copy.

## Slice 2 — Scripter failure notifies

- [x] A raising Scripter after the portrait → `ScripterFallbackPolicy.ERROR_REPLY`
      is sent to the user.
- [x] The session lands in `DONE` and `event=scripter_error` is logged.
- [x] Nothing escapes `handle_update` (no exception).
- [x] An empty-script Scripter → the same friendly reply and `DONE`, with
      `event=scripter_empty` logged.
- [x] The reply is sent exactly once; the portrait is still delivered first.
- [x] The "Scripter not wired" branch remains silent (logged only).

## Manual / sanity

- [x] Happy path unchanged: portrait → script text → voice note, no extra
      message.
- [x] Converter and Narrator fallbacks unchanged.
- [x] `README.md` and `SPECS/TECH.md` reflect the two behaviours.

## Sign-off

- [x] Suite green, hooks clean, docs updated, ready for review.

## Verification record (2026-10-19)

- `./scripts/test` → **372 passed, 8 skipped** (the 8 are the gated live
  tests); `./scripts/hooks` → ruff + mypy clean.
- Evidence per item: nudge `/restart` assertions in
  `tests/unit/test_gateway.py` (`test_a_stray_photo_mid_interview_nudges_instead_of_silence`,
  `test_a_stray_sticker_mid_confirmation_nudges`, and the parametrized
  `test_wrong_payload_mid_interview_nudges_and_keeps_the_pending_question`
  over photo/sticker × `AWAITING_CONFIRMATION`/`INTERVIEWING`, which also
  asserts the pending question and phase are preserved); constant lock in
  `tests/unit/test_interviewer.py::test_wrong_payload_prompt_asks_for_text_and_offers_restart`.
  Scripter evidence in `tests/unit/test_gateway.py::TestScripterFailure`
  (raise → `ERROR_REPLY` + `DONE` + `event=scripter_error`; empty script →
  same reply + `event=scripter_empty`; exactly one reply, `sender.events ==
  ["photo", "send"]`; unwired → no reply + `event=scripter_not_wired`).
- Happy path lock: `tests/unit/test_gateway_narrator.py::TestNarratorAutoTrigger::test_portrait_script_and_voice_arrive_in_order_to_the_same_chat`
  asserts the exact event sequence `send, photo, send, voice` — nothing extra.
- Converter/Narrator fallback branches untouched by the diff; their existing
  failure tests are green.
- Doc fixes made during verification: README offline test count 368 → 372;
  TECH.md not-wired sentence corrected for the Converter; ROADMAP `## 8`
  added to match README's "Phase 8 — Polish" note. MISSION.md: no change
  warranted.
