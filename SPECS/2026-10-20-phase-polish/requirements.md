# Requirements — Phase polish: interview escape hatch + Scripter failure notice

## Context

Phases 1–7 delivered the full happy path and the Phase 7 resilience work
(`SPECS/2026-10-19-resilience`). Every stage degrades to a friendly reply
instead of raising into the poll loop, out-of-order input is nudged, and
Gemini calls are retried with backoff.

Two small polish items remain, both raised against the delivered behaviour:

1. **The mid-interview nudge offers no escape hatch.** A photo/non-text
   payload during `AWAITING_CONFIRMATION` / `INTERVIEWING` gets
   `WRONG_PAYLOAD_PROMPT` — *"No photos needed right now — just answer in
   words, please."* It tells the user to use text but never mentions
   `/restart`, even though `/restart` works at that point and is the natural
   way out for a user who has changed their mind.
2. **A Scripter failure is silent.** In `_run_converter`, when
   `scripter.generate_script(...)` raises — or returns an empty script — the
   session lands in `DONE` and the failure is logged, but **no message is
   sent to the user**. The portrait has already been delivered, so the user
   is left waiting for a narration that will never arrive. This contradicts
   TECH.md error policy ("catch, log loudly, **degrade gracefully** … send a
   friendly message") and is inconsistent with the Converter and Narrator
   paths, which both notify.

## Scope

**In scope (minimal changes, reusing the existing architecture)**

- Append a `/restart` hint to the existing mid-interview nudge constant so
  the reply both asks for a text answer and offers the fresh-start command.
- Add a `ScripterFallbackPolicy` friendly-reply constant (mirroring
  `BouncerFallbackPolicy` / `NarratorFallbackPolicy`) and send it from the
  Scripter failure branches in `_run_converter` (exception **and** empty
  script), logging the failure as today.
- Unit tests for both behaviours; doc updates.

**Out of scope**

- Any change to the happy path, to other user-facing copy, or to
  `PHOTO_PROMPT` (a separate, purely cosmetic rewording was considered and
  deliberately dropped).
- New agents, stages, storage, retry logic, or phase-transition changes.
- Changing when `/restart` is *recognised* — it already works in every phase;
  this only advertises it in the nudge copy.

## Decisions

| # | Decision | Rationale |
| --- | --- | --- |
| P1 | Keep the single `WRONG_PAYLOAD_PROMPT` constant (shared by `AWAITING_CONFIRMATION` and `INTERVIEWING`, and by photo/sticker/voice) and append one sentence offering `/restart`. Do not fork the copy per phase. | Smallest change; one constant stays the single source of truth, and the nudge already has to make sense for both phases. |
| P2 | A Scripter failure and an empty Scripter result are the same user-visible outcome ("no narration this time") and share one reply constant. The "Scripter not wired" branch stays silent (logged only), matching the "Narrator not wired" branch. | Consistency: a *runtime failure* notifies; an *unwired stage* only logs. Mirrors the established Converter/Narrator fallback pattern exactly. |
| P3 | The fallback is sent through the existing `_send(chat_id, text, sender, event=…)` choke point and returned, exactly like the Converter and Narrator fallbacks (which already send via `_send` and let the caller treat the return value as the reply). | No new plumbing; keeps the "single reply choke point" and the existing `event=` audit convention. |

## Contracts

`app/interviewer.py`:

- `WRONG_PAYLOAD_PROMPT` — unchanged meaning, extended copy: asks the user to
  answer in words **and** tells them they can type `/restart` to start over.

`app/gateway.py`:

- `ScripterFallbackPolicy.ERROR_REPLY: str` — a friendly reply sent when the
  Scripter produces no narration (failure or empty result).
- `_run_converter` Scripter branches:
  - `scripter.generate_script(...)` raises → phase `DONE`, log
    `event=scripter_error`, send `ScripterFallbackPolicy.ERROR_REPLY`
    (`event=scripter_error`) and return it.
  - `generate_script(...)` returns a falsy script → phase `DONE`, log
    `event=scripter_empty`, send the same reply (`event=scripter_empty`) and
    return it.
  - `scripter is None` → no message (user-visible behaviour unchanged);
    now logged as `event=scripter_not_wired` (the log line is new).

## Behaviour

- A photo / non-text payload during `AWAITING_CONFIRMATION` or `INTERVIEWING`
  → the nudge reply, which now also offers `/restart`; phase and pending
  question unchanged; no model call.
- A Scripter failure (or empty script) after the portrait has been sent → the
  user receives a friendly reply; the session lands in `DONE`; the failure is
  logged; no exception escapes into `handle_update` / the poll loop.
- The happy path (portrait → script text → voice note), the "not wired"
  branches, and all other replies are unchanged.

## Acceptance criteria

1. **The interview nudge advertises the escape hatch.** The reply sent for a
   photo/sticker/voice during `AWAITING_CONFIRMATION` or `INTERVIEWING`
   mentions `/restart` (and still asks for a text answer); the pending
   question and phase are preserved.
2. **A Scripter failure is never silent.** When the Scripter raises or returns
   an empty script, the user gets a friendly reply, the session is `DONE`, the
   failure is logged, and nothing escapes `handle_update`.
3. **No regressions.** The happy path and every other stage's behaviour and
   copy are unchanged; the whole existing suite still passes.
