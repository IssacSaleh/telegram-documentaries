"""The Interviewer — ROADMAP Phase 3: sequential, stateful Q&A.

After the Bouncer admits a portrait, the Interviewer runs a 5–7 question
adaptive interview over plain text, one question at a time, accumulating a
per-`chat_id` dossier and ending in a suggested animal. It is the pipeline
orchestrator (SPECS/TECH.md, hub-and-spoke).

Design (SPECS/2026-10-16-interviewer/requirements.md):

* **Code owns the invariants; the model owns the content.** The model proposes
  the next question or a completion; this module enforces the 5–7 question
  window (a completion before 5 is ignored, the 7th answer forces a close).
* **The dossier lives on the session** — the one shared state driver, never a
  second store — so two chats can never see each other's answers.
* **Never raises into the user's flow.** Any model failure degrades to a
  friendly retry that leaves the phase and the pending question untouched
  (SPECS/TECH.md error policy). The user is never left in silence and never
  loses their place.

Answer text is user speech, so it is never logged verbatim — the structured
log records `answer_chars` and the question index instead.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from app.adk_utils import extract_json
from app.logging_utils import get_logger, logged
from app.models import Dossier, DossierAnswer, InterviewTurn, OutboundMessage
from app.state import Session, SessionPhase, get_store

logger = get_logger("interviewer")

#: Interview model (SPECS/TECH.md pins the exact ID).
INTERVIEWER_MODEL = "gemini-3.1-flash-lite"

#: Interview length window (ROADMAP §3: start at 5, up to 2 follow-ups).
MIN_QUESTIONS = 5
MAX_QUESTIONS = 7

#: Tokens accepted as confirmation of the invitation to be interviewed.
CONFIRMATION_TOKENS = frozenset({"yes", "y"})

#: User-facing copy. Named constants keep it greppable and testable, matching
#: the Phase 1 `ReplyPolicy` / Phase 2 `RejectionPolicy` pattern.
CONFIRMATION_PROMPT = (
    "Now then — a proper human! Ready for a few questions about yourself? "
    "Reply YES and we'll begin."
)
PHOTO_PROMPT = "Send me a photo of yourself first, and we'll get started."
WRONG_PAYLOAD_PROMPT = (
    "No photos needed right now — just answer in words, please."
)
RETRY_PROMPT = "Sorry, I lost my train of thought. Send that answer again?"
PORTRAIT_PENDING_REPLY = (
    "I've got everything I need — your portrait is next. Sit tight."
)
COMPLETION_TEMPLATE = (
    "Well, that's told me plenty. You're a {animal} — a fine specimen. "
    "Your portrait is next."
)

INTERVIEWER_INSTRUCTION = """You are the Interviewer for a wildlife \
documentary bot. The user has sent a photo of themselves and is ready to be \
interviewed so we can pick the animal they most resemble.

Ask between 5 and 7 questions, ONE at a time, adapting each question to what \
the user has already told you. Questions should explore personality, habits, \
lifestyle and temperament — the things that reveal an animal.

Rules:
- Ask exactly one question per turn. Never ask two at once.
- Read the answers so far and choose the next question accordingly.
- Set is_follow_up to true when a question digs deeper into the previous \
answer, and false when it moves to a new topic.
- Once you have asked at least 5 questions and have enough to judge, set \
is_complete to true, provide suggested_animal (a single animal name) and a \
short animal_rationale.
- While asking questions, set is_complete to false and suggested_animal to \
null.
- Respond with the structured output only."""


class Interviewer(Protocol):
    """Produces the next interview turn from the dossier so far."""

    def next_turn(self, dossier: Dossier) -> InterviewTurn:
        """The next question, or a proposed completion."""
        ...

    def final_turn(self, dossier: Dossier) -> InterviewTurn:
        """The closing turn, forced at the question cap.

        Used when the model still wants to ask a question but the 7-question
        ceiling has been reached: it must produce the animal now.
        """
        ...


@dataclass(frozen=True)
class InterviewDecision:
    """Typed result of one interview interaction. Never a raw dict."""

    reply: OutboundMessage
    error: bool = False


class AdkInterviewer:
    """The Google ADK wiring: one `LlmAgent` behind a typed boundary.

    Mirrors `AdkHumanPresenceClassifier`: lazy ADK import (unit tests never
    touch ADK or the network), synchronous `asyncio.run` from the gateway's
    long-poll loop, one runner per turn so sessions can never be shared
    across chats, and the shared `extract_json` prose-tolerance rule.
    """

    def __init__(self) -> None:
        from google.adk.agents import LlmAgent

        self._agent = LlmAgent(
            name="interviewer",
            model=INTERVIEWER_MODEL,
            instruction=INTERVIEWER_INSTRUCTION,
            output_schema=InterviewTurn,
            description="Adaptive interviewer: 5-7 questions, then a suggested animal.",
        )

    def next_turn(self, dossier: Dossier) -> InterviewTurn:
        return asyncio.run(self._run(self._prompt(dossier)))

    def final_turn(self, dossier: Dossier) -> InterviewTurn:
        prompt = (
            f"{self._prompt(dossier)}\n\n"
            f"You have asked {dossier.asked} questions, which is the maximum. "
            "You must finish now: set is_complete to true, give the "
            "suggested_animal and its rationale, and set next_question to null."
        )
        return asyncio.run(self._run(prompt))

    @staticmethod
    def _prompt(dossier: Dossier) -> str:
        if dossier.answers:
            transcript = "\n".join(
                f"Q{index}: {item.question}\nA{index}: {item.answer}"
                for index, item in enumerate(dossier.answers, start=1)
            )
        else:
            transcript = "(no answers yet — this is the first question)"

        return (
            "Interview transcript so far:\n"
            f"{transcript}\n\n"
            f"Questions asked: {dossier.asked} (minimum {MIN_QUESTIONS}, "
            f"maximum {MAX_QUESTIONS}).\n"
            "Return your next turn as structured output."
        )

    async def _run(self, prompt: str) -> InterviewTurn:
        import uuid

        from google.adk.runners import InMemoryRunner
        from google.genai import types

        runner = InMemoryRunner(agent=self._agent, app_name="interviewer")
        session_id = f"interviewer-{uuid.uuid4().hex}"
        await runner.session_service.create_session(
            app_name="interviewer", user_id="interviewer", session_id=session_id
        )

        content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=prompt)],
        )

        # The final response event carries the complete answer; earlier events
        # may be streamed fragments we must not concatenate.
        final_text: str | None = None
        latest_text: str | None = None
        async for event in runner.run_async(
            user_id="interviewer",
            session_id=session_id,
            new_message=content,
        ):
            if not (event.content and event.content.parts):
                continue
            chunk = "".join(part.text for part in event.content.parts if part.text)
            if not chunk:
                continue
            latest_text = chunk
            if event.is_final_response():
                final_text = chunk

        if final_text is None:
            final_text = latest_text
        if final_text is None:
            raise RuntimeError("interviewer agent returned no output")

        return InterviewTurn.model_validate_json(extract_json(final_text))


# ---------------------------------------------------------------------------
# Flow — pure over the session, injected agent, never raises
# ---------------------------------------------------------------------------


def _retry(chat_id: int) -> InterviewDecision:
    """The single degrade path: friendly retry, phase/pending untouched."""
    return InterviewDecision(
        reply=OutboundMessage(chat_id=chat_id, text=RETRY_PROMPT), error=True
    )


def _ask(session: Session, turn: InterviewTurn, chat_id: int) -> InterviewDecision:
    """Ask the question carried by `turn` and remember it as pending."""
    question = turn.next_question
    if question is None or not question.strip():
        # The contract says a non-completion always carries a question; a
        # model that breaks it is a failure, and we degrade rather than ask
        # the user an empty question.
        logger.error("event=interviewer_error chat_id=%s reason=missing_question", chat_id)
        return _retry(chat_id)

    session.pending_question = question
    if turn.is_follow_up and session.dossier is not None:
        session.dossier.follow_ups_used += 1
    logger.info(
        "event=interview_question_asked chat_id=%s question_index=%d follow_up=%s",
        chat_id,
        (session.dossier.asked if session.dossier else 0) + 1,
        turn.is_follow_up,
    )
    return InterviewDecision(reply=OutboundMessage(chat_id=chat_id, text=question))


def _close(session: Session, turn: InterviewTurn, chat_id: int) -> InterviewDecision:
    """Finish the interview: store the animal and name it in the reply."""
    dossier = session.dossier
    if dossier is None:
        logger.error("event=interviewer_error chat_id=%s reason=no_dossier", chat_id)
        return _retry(chat_id)

    if not turn.suggested_animal:
        # Only reachable if the forced final turn still refused to decide.
        logger.error("event=interviewer_error chat_id=%s reason=no_animal", chat_id)
        return _retry(chat_id)

    dossier.suggested_animal = turn.suggested_animal
    dossier.animal_rationale = turn.animal_rationale
    session.phase = SessionPhase.DONE
    session.pending_question = None

    logger.info(
        "event=interview_completed chat_id=%s animal=%s questions=%d",
        chat_id,
        dossier.suggested_animal,
        dossier.asked,
    )
    return InterviewDecision(
        reply=OutboundMessage(
            chat_id=chat_id,
            text=COMPLETION_TEMPLATE.format(animal=dossier.suggested_animal),
        )
    )


def _next_turn_or_degrade(
    interviewer: Interviewer, dossier: Dossier, chat_id: int, *, forced: bool = False
) -> InterviewTurn | None:
    """Call the agent, logging and returning None on any failure."""
    try:
        return interviewer.final_turn(dossier) if forced else interviewer.next_turn(dossier)
    except Exception:
        logger.exception("event=interviewer_error chat_id=%s", chat_id)
        return None


def _advance(session: Session, interviewer: Interviewer, chat_id: int) -> InterviewDecision:
    """Choose the next action from the dossier and the model's proposal.

    Guard rails live here, not in the prompt: a premature completion is
    ignored, and the ceiling forces a close. This is what keeps the 5–7
    window true regardless of what the model says.
    """
    dossier = session.dossier
    if dossier is None:
        logger.error("event=interviewer_error chat_id=%s reason=no_dossier", chat_id)
        return _retry(chat_id)

    turn = _next_turn_or_degrade(interviewer, dossier, chat_id)
    if turn is None:
        return _retry(chat_id)

    asked = dossier.asked
    if turn.is_complete and asked < MIN_QUESTIONS:
        logger.info(
            "event=premature_completion_ignored chat_id=%s asked=%d", chat_id, asked
        )
        turn = _next_turn_or_degrade(interviewer, dossier, chat_id)
        if turn is None or turn.is_complete or not turn.next_question:
            logger.error(
                "event=interviewer_error chat_id=%s reason=no_question_after_override",
                chat_id,
            )
            return _retry(chat_id)

    if not turn.is_complete and asked >= MAX_QUESTIONS:
        logger.info("event=question_cap_reached chat_id=%s", chat_id)
        forced = _next_turn_or_degrade(interviewer, dossier, chat_id, forced=True)
        if forced is None or not forced.suggested_animal:
            logger.error(
                "event=interviewer_error chat_id=%s reason=forced_completion_failed",
                chat_id,
            )
            return _retry(chat_id)
        turn = forced

    if turn.is_complete:
        return _close(session, turn, chat_id)
    return _ask(session, turn, chat_id)


@logged(logger, event="handle_photo_admitted")
def handle_photo_admitted(chat_id: int) -> InterviewDecision:
    """Bouncer admitted a portrait: invite the user to be interviewed."""
    store = get_store()
    session = store.get_or_create(chat_id)
    session.phase = SessionPhase.AWAITING_CONFIRMATION
    store.start_dossier(chat_id)  # a fresh interview, not a resumed one
    session.pending_question = None

    logger.info("event=interview_confirmation_sent chat_id=%s", chat_id)
    return InterviewDecision(
        reply=OutboundMessage(chat_id=chat_id, text=CONFIRMATION_PROMPT)
    )


def handle_confirmation(chat_id: int, text: str, interviewer: Interviewer) -> InterviewDecision:
    """Text arriving in AWAITING_CONFIRMATION: start on YES, else re-ask."""
    store = get_store()
    session = store.get_or_create(chat_id)

    if text.strip().lower() not in CONFIRMATION_TOKENS:
        logger.info("event=confirmation_reasked chat_id=%s", chat_id)
        return InterviewDecision(
            reply=OutboundMessage(chat_id=chat_id, text=CONFIRMATION_PROMPT)
        )

    session.phase = SessionPhase.INTERVIEWING
    if session.dossier is None:
        store.start_dossier(chat_id)
    logger.info("event=interview_started chat_id=%s", chat_id)

    # The first turn is a question: no answer has been recorded yet, so at
    # `asked == 0` neither guard rail can fire.
    return _advance(session, interviewer, chat_id)


@logged(logger, event="handle_interview_answer")
def handle_answer(chat_id: int, text: str, interviewer: Interviewer) -> InterviewDecision:
    """Record a text answer against the pending question, then advance."""
    store = get_store()
    session = store.get_or_create(chat_id)
    dossier = session.dossier or store.start_dossier(chat_id)
    session.dossier = dossier

    pending = session.pending_question
    if pending is None:
        # No open question — a stray text mid-interview. Ask the next one
        # rather than silently swallowing the message.
        logger.warning("event=no_pending_question chat_id=%s", chat_id)
        return _advance(session, interviewer, chat_id)

    dossier.answers.append(DossierAnswer(question=pending, answer=text))
    logger.info(
        "event=interview_answer_recorded chat_id=%s answer_chars=%d question_index=%d",
        chat_id,
        len(text),
        dossier.asked,
    )

    decision = _advance(session, interviewer, chat_id)
    if decision.error:
        # Degrade cleanly: undo the recorded answer so the user can retry it
        # against the same pending question.
        dossier.answers.pop()
        logger.info("event=answer_reverted chat_id=%s", chat_id)
    return decision


def handle_wrong_payload(chat_id: int) -> InterviewDecision:
    """A photo/sticker/voice arrived mid-interview: keep the question open."""
    logger.info("event=wrong_payload_ignored chat_id=%s", chat_id)
    return InterviewDecision(
        reply=OutboundMessage(chat_id=chat_id, text=WRONG_PAYLOAD_PROMPT)
    )


def handle_portrait_pending(chat_id: int) -> InterviewDecision:
    """Text after the interview closed: the Converter is still to come."""
    return InterviewDecision(
        reply=OutboundMessage(chat_id=chat_id, text=PORTRAIT_PENDING_REPLY)
    )
