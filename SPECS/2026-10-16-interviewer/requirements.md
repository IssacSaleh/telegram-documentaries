# Requirements — Interviewer (ROADMAP Phase 3)

## Context

Phases 1–2 shipped the long-polling gateway and **the Bouncer**: a photo is
downloaded, classified by `gemini-3.1-flash-lite` (Google ADK), and either
rejected (cheeky message + state purge) or admitted. Today admission falls
through to Phase 1's placeholder reply (`hey mate!`), because the phase that
owns the conversation after admission does not exist yet. This spec builds it.

**Phase 3 — the Interviewer.** A sequential, stateful Q&A agent: 5–7 adaptive
questions, one at a time, accumulating a per-`chat_id` dossier, ending in a
suggested animal. It is the pipeline orchestrator (SPECS/TECH.md,
"hub-and-spoke").

ROADMAP §3 acceptance: *"5–7 questions asked in order; dossier persists across
turns; two concurrent chats never see each other's state; suggested animal
produced and passed to the Converter."*

## Scope

**In scope**

- An Interviewer ADK agent (`LlmAgent`, model `gemini-3.1-flash-lite` per
  SPECS/TECH.md) with a **structured output schema** for every turn.
- A per-chat **dossier** held on the existing `Session` (the single state
  driver — no second store, per SPECS/TECH.md).
- Phase routing in the gateway: confirmation → interview answers → completion.
- Wrong-payload guards for every "message arrives in the wrong phase" case.
- Deterministic question-count guard rails (5 minimum, 7 maximum).
- Graceful degradation on any model failure — the user is never left in
  silence and never loses their place (the Bouncer's established policy).
- Offline tests (Red/Green) plus live tests behind `RUN_LIVE_TESTS=1`.

**Out of scope (YAGNI)**

- Converter, Scripter, Narrator (Phases 4–6). No image generation, no TTS.
- Persistence of any kind — in-memory only (SPECS/TECH.md).
- Voice/sticker/document answers; free text is the only answer type.
- Poll-loop backoff (Phase 7), webhooks, databases.
- Multi-language interviews.

## Decisions

Confirmed with the user before implementation:

| # | Decision | Rationale |
| --- | --- | --- |
| D1 | Questions are **LLM-generated and adaptive** — the model reads the answers so far and chooses the next question. | ROADMAP says "adaptive"; a fixed list cannot be. |
| D2 | After the last question we **name the suggested animal and tease the portrait** ("…your portrait is next"). | The Converter does not exist yet; naming the animal is the phase's visible payoff instead of a dead end. |
| D3 | Admission sends a **confirmation prompt**; the interview starts on an explicit `YES`. | This is the "confirmation step" the operator expects after a photo is approved. |
| D4 | Text from a chat with **no session** replies "send me a photo to start" — replacing `hey mate!`. | A dead-end placeholder is worse than a signpost to the entrance. **No backward compatibility kept** (operator approved). |
| D5 | A **photo (or any non-text payload) mid-interview** gets a polite "answer in words" and the pending question stays open. | Non-destructive; the user keeps their place. |

Design calls made by this spec (flag any you disagree with):

| # | Decision | Rationale |
| --- | --- | --- |
| D6 | **Code owns the question counter; the model only proposes.** A `is_complete` claiming completion before 5 questions is ignored; at 7 the interview is forced to close. | Deterministic guard rails beat prompt-only instructions — the "adaptive" part stays with the model, the invariants stay with us. |
| D7 | The closing line is a **deterministic template** around the model's animal name (not free-form model prose). | Stable, testable copy; the adaptive part is the animal choice itself. |
| D8 | The Bouncer's admission reply (`hey mate!`) is **replaced** by the confirmation prompt (supersedes Phase 2's "flow unchanged"). | Phase 2 shipped a placeholder because the Interviewer did not exist; now it does. |
| D9 | Answer text is **not** logged verbatim; we log `answer_chars` and the question index. Animal, counts and phases are logged. | Session content is user speech; metadata is enough to operate the pipeline. |
| D10 | `/start` and `/restart` keep purging the whole session, including the dossier. | Unchanged MISSION non-negotiable; mid-interview reset returns the chat to "send a photo". |

## Contracts (typed, validated at the edge)

All model output is parsed into Pydantic models before it crosses a module
boundary (SPECS/TECH.md). Malformed output is a **degrade**, never a crash.

### `InterviewTurn` — the model's structured output per turn

| Field | Type | Rule |
| --- | --- | --- |
| `next_question` | `str \| None` | Required (non-empty) unless `is_complete` |
| `is_complete` | `bool` | The model *proposes* completion; code may override (D6) |
| `suggested_animal` | `str \| None` | Required (non-empty) when `is_complete`, else `None` |
| `animal_rationale` | `str \| None` | Optional one-liner; used only for logs |

Validation: a model validator enforces the two cross-field rules above.
Violations raise `ValidationError`, which the caller treats as a model failure.

### `Dossier` — per-chat accumulation

| Field | Type | Notes |
| --- | --- | --- |
| `answers` | `list[DossierAnswer]` | `DossierAnswer(question: str, answer: str)`; order = asked order |
| `follow_ups_used` | `int` | Incremented when the model marks a turn a follow-up |
| `suggested_animal` | `str \| None` | Set once on completion |
| `animal_rationale` | `str \| None` | Set once on completion |

`asked` is a derived property (`len(answers)`), never stored twice.

### `SessionPhase`

`AWAITING_CONFIRMATION` is added between `AWAITING_PHOTO` and `INTERVIEWING`
(session schema version bumps to 2). Transitions:

```
AWAITING_PHOTO ──photo admitted──▶ AWAITING_CONFIRMATION ──YES──▶ INTERVIEWING ──complete──▶ DONE
       ▲                                   │                          │
       └──────── /start · /restart · Bouncer rejection ───────────────┘   (state purged)
```

`GENERATING_IMAGE`, `SCRIPTING`, `NARRATING` stay unused until Phases 4–6.

### Confirmation token

`AWAITING_CONFIRMATION` accepts `yes` or `y`, compared case-insensitively after
stripping whitespace. Anything else re-sends the confirmation prompt (logged as
`event=confirmation_reasked`). No attempt is made to parse free-form consent.

## Behaviour rules

| Situation | Behaviour |
| --- | --- |
| Photo admitted (Bouncer YES) | Phase → `AWAITING_CONFIRMATION`; reply with the confirmation prompt. |
| `yes` / `y` at `AWAITING_CONFIRMATION` | Phase → `INTERVIEWING`; ask question 1. |
| Other text at `AWAITING_CONFIRMATION` | Re-send the confirmation prompt; phase unchanged. |
| Text while `INTERVIEWING` | Record it against the pending question, ask the next one (or close at the cap). |
| Photo / voice / sticker during `AWAITING_CONFIRMATION` or `INTERVIEWING` | Polite "answer in words"; phase and pending question unchanged; no model call. |
| Text with no session | "Send me a photo to start."; no session created. |
| `is_complete` before 5 answers | Ignored: log `event=premature_completion_ignored`, ask another question. |
| 7 answers reached | Forced completion: if the model has not supplied an animal, request the final call once; if that fails, degrade. |
| Model error / malformed output | Friendly "lost my train of thought — try that answer again?"; phase and pending question unchanged; log `event=interviewer_error`. Never advance, never silence. |
| `/start` · `/restart` | Purge session + dossier (unchanged). |

## Acceptance criteria (ROADMAP §3)

1. 5–7 questions asked in order, one at a time, none skipped.
2. The dossier persists across turns and holds every Q/A pair.
3. Two concurrent chats never see each other's state (isolation test).
4. A suggested animal is produced at the end and **stored on the dossier**
   (`event=interview_completed animal=<name>` in the log).

> **Spec drift to flag:** ROADMAP §3 says the animal is "passed to the
> Converter". The Converter is Phase 4 and does not exist, so Phase 3 delivers
> the handoff *point* — a validated `suggested_animal` on the session, ready to
> be consumed — and logs it. Wiring is Phase 4's first task. Operator approved
> via D2.

## Logging

Decorator-based (`@logged(logger, event=...)`), matching the gateway/bouncer.
Events: `interview_confirmation_sent`, `confirmation_reasked`,
`interview_started`, `interview_question_asked`, `interview_answer_recorded`,
`premature_completion_ignored`, `interview_completed`,
`wrong_payload_ignored`, `interviewer_error`, `interviewer_fallback_sent`.
Never log raw answer text (D9).
