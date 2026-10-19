# Implementation Plan — Phase polish

Small, surgical change. Two behaviours, one Red/Green slice each, then a
regression pass. No new modules, no new pipeline stages.

## Slice 1 — Mid-interview nudge offers `/restart`

**Red**

- In `tests/unit/test_gateway.py`, extend the existing wrong-payload nudge
  tests (and add a dedicated assertion) that the reply for a photo/sticker
  during `AWAITING_CONFIRMATION` / `INTERVIEWING` contains `/restart` and
  still asks for a text answer. Fails against the current copy.
- Add a focused test on the constant in `tests/unit/test_interviewer.py` so
  the contract lives next to the other interview copy.

**Green**

- Append one sentence to `WRONG_PAYLOAD_PROMPT` in `app/interviewer.py`.

**Notes**

- Tests compare against the imported constant where they already do, so no
  structural test change is needed; the new assertions check the `/restart`
  hint specifically.

## Slice 2 — Scripter failure notifies the user

**Red**

- Add `ExplodingScripter` to `tests/conftest.py` (mirrors `ExplodingConverter`
  / `ExplodingNarrator`).
- In `tests/unit/test_gateway.py`, add `TestScripterFailure`:
  - a raising Scripter after the portrait → a friendly reply matching
    `ScripterFallbackPolicy.ERROR_REPLY` reaches the sender, the session is
    `DONE`, and `event=scripter_error` is logged; `handle_update` does not
    raise.
  - an empty-script Scripter → the same friendly reply, session `DONE`,
    `event=scripter_empty` logged.
  - the reply is sent exactly once (through `_send`), portrait still delivered.
- Both fail against the current code (which returns `None` silently).

**Green**

- Add `ScripterFallbackPolicy` beside `BouncerFallbackPolicy` /
  `NarratorFallbackPolicy` in `app/gateway.py`.
- In `_run_converter`, restructure the Scripter block so only
  `scripter.generate_script(dossier)` is inside the `try`; send
  `ScripterFallbackPolicy.ERROR_REPLY` (via `_send`) for the exception
  (`event=scripter_error`) and empty-script (`event=scripter_empty`) cases,
  keeping the `DONE` transition and the logging.
- Log-and-silence the `scripter is None` (not wired) branch with
  `event=scripter_not_wired`, matching the `Narrator not wired` branch
  (`logger.warning`, no reply).

## Regression pass

- `./scripts/test` — whole suite green, no changed expectations elsewhere.
- `./scripts/hooks` — ruff + mypy clean.
- Confirm no other test asserts the old nudge copy literally (they use the
  constant) and that no test relied on the Scripter failure being silent.

## Docs

- `README.md`: note the interview nudge now offers `/restart`, and that a
  Scripter failure degrades with a friendly reply.
- `SPECS/TECH.md`: update the error-policy / per-stage degrade table row for
  the Scripter to say it notifies (matching Converter/Narrator).
- `SPECS/2026-10-20-phase-polish/validation.md`: tick off.

## Risks

- Only `scripter.generate_script(...)` runs inside the Scripter `try`; the
  fallback sends for the failure/empty cases happen **outside** it, so a
  fallback send can never be re-caught and double-sent. The Scripter's
  success-path `sender.send(script)` now sits outside that `try` too, so a
  script-send transport failure falls through to the outer converter guard
  (a friendly reply) rather than being mislabelled `scripter_error` — an
  improvement over the previous silent `return None`, and consistent with the
  Narrator path.
- The `scripter is None` (not wired) branch stays silent to the user (no
  reply) but **is logged** (`event=scripter_not_wired`), matching
  `narrator_not_wired` and TECH.md's ban on un-logged fallbacks.

## As implemented (verification notes)

- Code matched the plan exactly: one `try` around `generate_script(...)` only,
  both fallback sends outside it (so a fallback can never be re-caught), and
  `event=scripter_not_wired` added to the previously un-logged not-wired branch.
- **Docs deviation:** TECH.md has no "per-stage degrade table row" (the plan's
  wording) — it has no such table. The update landed as prose instead: the
  wrong-payload paragraph after the phase table (nudge now offers `/restart`)
  plus a paragraph enumerating every stage's runtime-failure fallback.
- TECH.md's first draft of that paragraph claimed an unwired *Converter* also
  "only logs and ends the chain at `DONE`". Verification against
  `handle_update` showed a not-wired Converter actually falls back to the
  portrait-pending tease (logged `via=portrait_pending`); only the Scripter and
  Narrator are log-only. The sentence was corrected during verification.
- README's offline test count moved 368 → 372 (four new tests), and ROADMAP
  gained a `## 8. Polish` section so README's "Phase 8" note has a roadmap
  entry to point at. MISSION needed no change (product scope/behaviour
  unchanged — this only closes a gap in an existing success criterion).
