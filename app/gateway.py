"""Gateway: long-poll Telegram and route updates.

Phase 1 introduced the long-polling loop and the hardcoded text reply.
Phase 2 added **the Bouncer**: photo updates are downloaded and classified
by Gemini 3.1 Flash Lite (via Google ADK) to decide whether a human is
present. Non-human submissions get a cheeky rejection and a state reset.
Phase 3 adds **the Interviewer**: an admitted human is invited to be
interviewed, then guided through 5–7 questions to a suggested animal.

Routing is driven by the chat's phase (SPECS/2026-10-16-interviewer):

  * **photo**  -> the Bouncer. Admitted humans enter the confirmation flow;
    rejections reset the chat.
  * **text**   -> phase-aware: no session gets the "send a photo" prompt;
    AWAITING_CONFIRMATION starts the interview on YES; INTERVIEWING records
    an answer and asks the next question; DONE points ahead to the portrait.
  * **other**  -> mid-interview, a gentle "answer in words"; otherwise logged
    and ignored.

Any failure (fetch, classification, ADK not wired) degrades to a friendly
reply — the user is never left in silence (SPECS/TECH.md error policy).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import httpx
from pydantic import ValidationError

from app.bouncer import BOUNCER_MODEL, bounce
from app.config import TELEGRAM_API, telegram_payload
from app.interviewer import (
    INTERVIEWER_MODEL,
    PHOTO_PROMPT,
    AdkInterviewer,
    handle_answer,
    handle_confirmation,
    handle_photo_admitted,
    handle_portrait_pending,
    handle_wrong_payload,
)
from app.logging_utils import get_logger, logged
from app.models import OutboundMessage, TelegramUpdate
from app.state import SessionPhase, get_store

if TYPE_CHECKING:
    from app.bouncer import HumanPresenceClassifier, PhotoFetcher
    from app.interviewer import Interviewer

logger = get_logger("gateway")

#: Commands that purge this chat's state and then get the normal reply.
#: SPECS/MISSION.md: "/start and /restart purge the session ... without
#: restarting the process."
RESET_COMMANDS = frozenset({"/start", "/restart"})

#: Phases in which a stray non-text payload is worth a gentle nudge, because
#: the user is meant to be typing an answer or a YES.
_ANSWER_PHASES = frozenset({SessionPhase.AWAITING_CONFIRMATION, SessionPhase.INTERVIEWING})



class BouncerFallbackPolicy:
    """Friendly reply when the Bouncer cannot deliver a real verdict.

    SPECS/TECH.md error policy: on a validated user's conversation path,
    catch, log loudly, and **degrade gracefully** so the conversation
    continues — send a friendly message, never leave the user in silence.
    The fallback never touches session state.
    """

    ERROR_REPLY = (
        "Bear with me a moment — something went wrong looking at that photo. "
        "Try sending it again?"
    )


class MessageSender(Protocol):
    """What the handler needs from a transport — kept narrow for testing."""

    def send(self, message: OutboundMessage) -> None: ...


def _command_token(text: str) -> str | None:
    """The command word of `text`, with any `@BotName` suffix stripped.

    Telegram clients send `/start@MyBot` (group chats always mention the
    bot) and `/start some args`; only the first token, sans mention, decides
    whether this is a reset command.
    """
    words = text.split()
    if not words:
        return None
    return words[0].split("@", 1)[0]


def _send_bouncer_fallback(
    chat_id: int, sender: MessageSender | None, *, reason: str
) -> OutboundMessage:
    """Reply with the friendly degrade message. Touches NO session state."""
    reply = OutboundMessage(chat_id=chat_id, text=BouncerFallbackPolicy.ERROR_REPLY)
    if sender is not None:
        sender.send(reply)
    logger.info("event=degraded chat_id=%s reason=%s reply=sent", chat_id, reason)
    return reply


def _send(
    chat_id: int, text: str, sender: MessageSender | None, *, event: str
) -> OutboundMessage:
    """Send a plain reply and return it. The single reply choke point."""
    reply = OutboundMessage(chat_id=chat_id, text=text)
    if sender is not None:
        sender.send(reply)
    logger.info("event=replied chat_id=%s via=%s", chat_id, event)
    return reply


@logged(logger, event="handle_update")
def handle_update(
    update: TelegramUpdate,
    sender: MessageSender | None = None,
    *,
    fetcher: PhotoFetcher | None = None,
    classifier: HumanPresenceClassifier | None = None,
    interviewer: Interviewer | None = None,
) -> OutboundMessage | None:
    """Turn one validated update into at most one outbound reply.

    Routing is phase-aware (SPECS/2026-10-16-interviewer):

      * **reset commands** (`/start`, `/restart`) purge the chat in *any*
        phase and then point the user at the photo flow.
      * **photo**  -> the Bouncer (Phase 2) gates it. Rejection is sent and
        the chat's ephemeral state is purged; an admission invites an
        interview. A Bouncer failure sends a friendly fallback without
        touching state.
      * **text**   -> no session (or AWAITING_PHOTO) gets the "send a photo"
        prompt and creates NO session; AWAITING_CONFIRMATION starts the
        interview on YES and re-asks otherwise; INTERVIEWING records the
        answer and asks the next question; later phases point at the portrait.
      * anything else -> a "answer in words" nudge mid-interview, else logged
        and ignored.

    The optional ``sender`` performs the actual transport. Returns the reply
    so callers (and tests) can observe it.
    """
    message = update.message
    if message is None:
        logger.info("event=skipped reason=no_message update_id=%s", update.update_id)
        return None

    chat_id = message.chat.id
    text = message.text
    store = get_store()

    # ---- Reset commands: valid in any phase ----------------------------
    if text is not None and _command_token(text) in RESET_COMMANDS:
        command = _command_token(text)
        store.reset(chat_id)
        logger.info("event=state_reset command=%r chat_id=%s", command, chat_id)
        # A reset leaves no session, so the next step is a photo again.
        return _send(chat_id, PHOTO_PROMPT, sender, event="state_reset")

    session = store.get(chat_id)
    answering = session is not None and session.phase in _ANSWER_PHASES

    # ---- Photo -> Bouncer, unless an interview is in progress ----------
    if message.largest_photo() is not None:
        if answering:
            # D5: mid-interview a photo is a mistake, not a new submission.
            # Nudge, keep the pending question, and never call the model.
            decision = handle_wrong_payload(chat_id)
            if sender is not None:
                sender.send(decision.reply)
            logger.info("event=replied chat_id=%s via=wrong_payload", chat_id)
            return decision.reply

        if fetcher is None or classifier is None:
            # Not wired (ADK failed to initialise, or a unit test exercising
            # the text path only). The user still gets a friendly reply.
            logger.warning(
                "event=skipped reason=bouncer_not_wired chat_id=%s", chat_id
            )
            return _send_bouncer_fallback(
                chat_id, sender, reason="bouncer_not_wired"
            )

        bouncer_decision = bounce(update, fetcher=fetcher, classifier=classifier)

        if bouncer_decision.error:
            # bounce() already logged the underlying failure loudly; degrade
            # gracefully with a friendly reply — never stall (TECH policy).
            logger.error("event=bouncer_error chat_id=%s", chat_id)
            return _send_bouncer_fallback(chat_id, sender, reason="bouncer_error")

        if bouncer_decision.reply is not None:
            # Rejection: bounce() already reset this chat's state.
            if sender is not None:
                sender.send(bouncer_decision.reply)
            return bouncer_decision.reply

        # Admitted: invite the interview.
        if interviewer is None:
            logger.warning(
                "event=skipped reason=interviewer_not_wired chat_id=%s", chat_id
            )
            return _send_bouncer_fallback(
                chat_id, sender, reason="interviewer_not_wired"
            )
        admission = handle_photo_admitted(chat_id)
        if sender is not None:
            sender.send(admission.reply)
        logger.info("event=replied chat_id=%s via=bouncer_admission", chat_id)
        return admission.reply

    # ---- Non-text, non-photo ------------------------------------------
    if text is None:
        if answering:
            # The user is expected to be typing; nudge instead of silence.
            decision = handle_wrong_payload(chat_id)
            if sender is not None:
                sender.send(decision.reply)
            logger.info("event=replied chat_id=%s via=wrong_payload", chat_id)
            return decision.reply
        logger.info("event=skipped reason=non_text chat_id=%s", chat_id)
        return None

    # ---- Text: phase-aware --------------------------------------------
    if session is None or session.phase == SessionPhase.AWAITING_PHOTO:
        # No interview in progress: point at the photo flow. Creating no
        # session keeps the store honest about who is actually waiting.
        return _send(chat_id, PHOTO_PROMPT, sender, event="awaiting_photo")

    if session.phase == SessionPhase.AWAITING_CONFIRMATION:
        if interviewer is None:
            logger.warning(
                "event=skipped reason=interviewer_not_wired chat_id=%s", chat_id
            )
            return _send_bouncer_fallback(
                chat_id, sender, reason="interviewer_not_wired"
            )
        decision = handle_confirmation(chat_id, text, interviewer)
        if sender is not None:
            sender.send(decision.reply)
        logger.info("event=replied chat_id=%s via=interview_confirmation", chat_id)
        return decision.reply

    if session.phase == SessionPhase.INTERVIEWING:
        if interviewer is None:
            logger.warning(
                "event=skipped reason=interviewer_not_wired chat_id=%s", chat_id
            )
            return _send_bouncer_fallback(
                chat_id, sender, reason="interviewer_not_wired"
            )
        decision = handle_answer(chat_id, text, interviewer)
        if sender is not None:
            sender.send(decision.reply)
        logger.info("event=replied chat_id=%s via=interview_answer", chat_id)
        return decision.reply

    # DONE and any later pipeline phase: the portrait comes next.
    decision = handle_portrait_pending(chat_id)
    if sender is not None:
        sender.send(decision.reply)
    logger.info("event=replied chat_id=%s via=portrait_pending", chat_id)
    return decision.reply



class TelegramSender:
    """Sends outbound messages via the Telegram Bot API."""

    def __init__(self, token: str, client: httpx.Client) -> None:
        self._token = token
        self._client = client

    def send(self, message: OutboundMessage) -> None:
        response = self._client.post(
            f"{TELEGRAM_API}/bot{self._token}/sendMessage",
            json={"chat_id": message.chat_id, "text": message.text},
            timeout=30.0,
        )
        # Never `raise_for_status()` here: the request URL carries the bot
        # token and `str(HTTPStatusError)` embeds it — that would leak the
        # secret into the logs (SPECS/MISSION.md). Report status +
        # Telegram's `description` instead.
        payload = telegram_payload(response)
        description = payload.get("description") or (
            f"HTTP {response.status_code} non-json body"
        )
        if not response.is_success or not payload.get("ok"):
            # Fail loudly and log — never swallow (SPECS/TECH.md).
            logger.error(
                "event=send_failed chat_id=%s status=%d description=%r",
                message.chat_id,
                response.status_code,
                description,
            )
            raise RuntimeError(f"telegram sendMessage failed: {description!r}")


def _raw_update_id(raw: object) -> int | None:
    """`update_id` from an untrusted batch entry, or None if it isn't an int.

    Used ONLY to acknowledge past updates and to advance the offset for a
    malformed entry — never to dispatch. Dispatch always goes through the
    validated `TelegramUpdate` (SPECS/TECH.md: parse at the edge first).
    """
    if not isinstance(raw, dict):
        return None
    value = raw.get("update_id")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def poll_once(
    token: str,
    client: httpx.Client,
    offset: int,
    sender: MessageSender,
    *,
    fetcher: PhotoFetcher | None = None,
    classifier: HumanPresenceClassifier | None = None,
    interviewer: Interviewer | None = None,
) -> int:
    """Fetch one batch of updates, reply to each, return the next offset.

    Two hard guarantees, both learned from a real failure mode:

    * **The offset never rolls back.** It is committed per update, before
      that update's handling is attempted, and a failure never discards it.
      Re-polling with a stale offset makes Telegram re-deliver the batch,
      which answers already-answered chats again — duplicate replies, and a
      livelock on a permanently undeliverable chat.
    * **One bad update never aborts the batch.** Each update is isolated in
      its own try/except; later chats in the same batch still get answered.
    """
    response = client.get(
        f"{TELEGRAM_API}/bot{token}/getUpdates",
        params={"offset": offset, "timeout": 30},
        timeout=45.0,
    )
    # No raise_for_status(): the URL carries the bot token and httpx embeds
    # it in the exception message (SPECS/MISSION.md — never leak secrets).
    body = telegram_payload(response)
    if not response.is_success or not body.get("ok"):
        logger.error(
            "event=getUpdates_failed status=%d description=%r",
            response.status_code,
            body.get("description") or "non-json body",
        )
        return offset

    next_offset = offset
    result = body.get("result", [])
    for raw in result if isinstance(result, list) else []:
        raw_id = _raw_update_id(raw)
        # Telegram's offset contract: everything below `offset` was already
        # acknowledged and must never be re-answered.
        if raw_id is not None and raw_id < offset:
            continue

        try:
            update = TelegramUpdate.model_validate(raw)
        except ValidationError as exc:
            # Malformed input: log loudly, and still advance past it when we
            # can — otherwise one bad update pins the poll loop forever.
            if raw_id is not None:
                next_offset = max(next_offset, raw_id + 1)
            logger.warning("event=invalid_update error=%r raw=%r", exc, raw)
            continue

        # Commit the offset BEFORE handling: a failure below must not roll
        # the batch back and duplicate every reply already sent.
        next_offset = max(next_offset, update.update_id + 1)

        try:
            handle_update(
                update,
                sender,
                fetcher=fetcher,
                classifier=classifier,
                interviewer=interviewer,
            )
        except Exception:
            # Isolated: log loudly, keep answering the rest of the batch.
            logger.exception("event=update_failed update_id=%s", update.update_id)

    return next_offset


def run(token: str) -> None:
    """Long-poll forever. Never raises into the user's flow."""
    from app.bouncer import AdkHumanPresenceClassifier, TelegramPhotoFetcher

    store = get_store()

    with httpx.Client() as client:
        sender = TelegramSender(token=token, client=client)
        fetcher = TelegramPhotoFetcher(token=token, client=client)

        # ADK wires up once at startup; classification then happens per photo.
        try:
            classifier: HumanPresenceClassifier | None = AdkHumanPresenceClassifier()
        except Exception:
            logger.exception("event=bouncer_init_failed")
            classifier = None  # photos degrade to "not wired" instead of crashing

        # The Interviewer is wired the same way, independently: a Bouncer that
        # failed to initialise must not take the Interviewer down with it.
        try:
            interviewer: Interviewer | None = AdkInterviewer()
        except Exception:
            logger.exception("event=interviewer_init_failed")
            interviewer = None  # text mid-interview degrades gracefully

        logger.info(
            "event=gateway_started transport=long_polling tracked_chats=%d "
            "bouncer_model=%s bouncer_wired=%s interviewer_model=%s "
            "interviewer_wired=%s",
            len(store.chat_ids),
            BOUNCER_MODEL,
            classifier is not None,
            INTERVIEWER_MODEL,
            interviewer is not None,
        )

        offset = 0
        while True:
            try:
                offset = poll_once(
                    token=token,
                    client=client,
                    offset=offset,
                    sender=sender,
                    fetcher=fetcher,
                    classifier=classifier,
                    interviewer=interviewer,
                )
            except httpx.HTTPError:
                # Log loudly, keep polling (SPECS/TECH.md error policy).
                logger.exception("event=poll_failed")
            except Exception:  # the polling loop must not die
                logger.exception("event=unexpected_error")
