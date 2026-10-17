# Validation — Interviewer (ROADMAP Phase 3)

How we know this phase is correct and can be merged. Work top to bottom; the
final section is the merge gate.

## 1. Automated ground truth

| Check | Command | Expected |
| --- | --- | --- |
| Unit + integration suite | `./scripts/test` | All pass, 0 failed; live tests skipped by default |
| Lint + types | `./scripts/hooks` | ruff clean, mypy clean |
| Live model calls | `RUN_LIVE_TESTS=1 ./scripts/test` | All pass, including the 3 Bouncer and the new Interviewer live tests |

Evidence to record: exact pass/skip/fail counts and the observed animal from the
live run.

## 2. Contract checklist

- [ ] `InterviewTurn` rejects a completion without a `suggested_animal` and a
  non-completion that carries one (validator fires).
- [ ] Malformed model output degrades: the user gets the fallback reply, the
  phase and pending question are unchanged, and `event=interviewer_error` is
  logged — never a crash, never silence.
- [ ] `Dossier` preserves Q/A order and exposes `asked` as a derived value.
- [ ] No raw dicts cross module boundaries (a typed `InterviewDecision` is
  returned by the flow, mirroring `BouncerDecision`).
- [ ] `SESSION_SCHEMA_VERSION` is 2 and `AWAITING_CONFIRMATION` exists.

## 3. Behaviour checklist

- [ ] Photo admitted by the Bouncer → confirmation prompt (not `hey mate!`).
- [ ] `yes` (any case) → question 1; anything else at confirmation → prompt
  re-sent, phase unchanged.
- [ ] Exactly one question per turn; 5 minimum, 7 maximum; a premature
  completion before 5 is ignored and logged.
- [ ] Each answer is stored against its question; the dossier grows by one
  per answer.
- [ ] On completion the reply **names the animal** and the dossier stores it.
- [ ] Photo/voice/sticker mid-interview → polite "answer in words" note, no
  model call, pending question preserved.
- [ ] Text with no session → "send me a photo to start", and **no** session is
  created.
- [ ] `/start` and `/restart` mid-interview purge the dossier; the next text
  gets the "send me a photo" prompt.
- [ ] Session isolation: chat A's interview never appears in chat B.

## 4. Manual live walkthrough (real Telegram)

Run the gateway (`python3 -m app.main`) and, from a real Telegram client:

1. **Entry:** send a text with no session → expect "send me a photo to start".
2. **Admission:** send a clear selfie → expect the confirmation prompt.
3. **Confirm:** send `yes` → expect question 1.
4. **Interview:** answer each question in a sentence; expect exactly one
   question per reply, no repeats, no skips. Count them: 5–7.
5. **Close:** after the last answer, expect a reply that names an animal and
   teases the portrait. Confirm the gateway log shows
   `event=interview_completed ... animal=<name>`.
6. **Wrong payload:** mid-interview, send a photo → expect the "answer in
   words" note and that the same question is still open.
7. **Isolation:** start an interview in a second chat; confirm neither chat's
   questions or answers leak into the other.
8. **Reset:** during an interview, send `/restart` → next text gets "send me a
   photo to start".
9. **Negative path unchanged:** a mug photo is still rejected with the cheeky
   message and a purged session.

Record the transcript highlights (question count, animal, any anomaly).

## 5. Log checklist

- [ ] `event=interview_started`, `interview_question_asked`,
  `interview_answer_recorded`, `interview_completed` all appear in order.
- [ ] Raw answer text never appears in the log (only `answer_chars`).
- [ ] No secret (token or API key) appears anywhere in the log.

## 6. Spec drift to surface and get approved

- [ ] ROADMAP §3 says the animal is "passed to the Converter"; Phase 3 stores
  and logs the handoff point and Phase 4 wires it (D2). Confirm the wording of
  ROADMAP §3 is updated to match.
- [ ] Phase 2's admission reply (`hey mate!`) is intentionally replaced by the
  confirmation prompt (D8). Confirm `SPECS/TECH.md`/`README.md` no longer claim
  admission falls through to the placeholder.
- [ ] "No session" text behaviour changed from `hey mate!` to the photo prompt
  (D4, no backward compatibility kept). Confirm the README says so.
- [ ] Docs updated and consistent: `README.md`, `SPECS/TECH.md`,
  `SPECS/ROADMAP.md`, and this folder.

## 7. Merge gate

All of the following must hold before the PR is opened:

- [ ] `./scripts/test` green (offline) and `RUN_LIVE_TESTS=1 ./scripts/test`
  green (live).
- [ ] `./scripts/hooks` clean.
- [ ] Every checklist item above ticked, or an explicit, user-approved
  exception recorded.
- [ ] Docs updated to match reality.
- [ ] No secret in any tracked file, log, or commit.
