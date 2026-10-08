"""Gateway: long-poll Telegram and route updates.

Phase 1 introduced the long-polling loop and the hardcoded text reply.
Phase 2 added **the Bouncer**: photo updates are downloaded and classified
by Gemini 3.1 Flash Lite (via Google ADK) to decide whether a human is
present. Non-human submissions get a cheeky rejection and a state reset;
humans continue the confirmation flow unchanged.

A Bouncer failure (fetch, classification, or ADK not wired) degrades to a
friendly reply — the user is never left in silence (SPECS/TECH.md error
policy).

Text behaviour is unchanged from Phase 1 — every text message still gets
the same hardcoded reply.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import httpx
from pydantic import ValidationError

from app.bouncer import BOUNCER_MODEL, bounce
from app.config import TELEGRAM_API, telegram_payload
from app.logging_utils import get_logger, logged
from app.models import OutboundMessage, TelegramUpdate
from app.state import get_store

if TYPE_CHECKING:
    from app.bouncer import HumanPresenceClassifier, PhotoFetcher

logger = get_logger("gateway")

#: Commands that purge this chat's state and then get the normal reply.
#: SPECS/MISSION.md: "/start and /restart purge the session ... without
#: restarting the process."
RESET_COMMANDS = frozenset({"/start", "/restart"})


class ReplyPolicy:
    """The hardcoded reply. Phase 1 has exactly one, by design."""

    HARDCODED_REPLY = "hey mate!"


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


@logged(logger, event="handle_update")
def handle_update(
    update: TelegramUpdate,
    sender: MessageSender | None = None,
    *,
    fetcher: PhotoFetcher | None = None,
    classifier: HumanPresenceClassifier | None = None,
) -> OutboundMessage | None:
    """Turn one validated update into at most one outbound reply.

    Routing:
      * **photo**  -> the Bouncer (Phase 2) gates it. Rejection is sent and
        the chat's ephemeral state is purged; an admission falls through to
        the existing confirmation flow *unchanged*. A Bouncer failure sends
        a friendly fallback reply without touching state.
      * **text**   -> Phase 1 behaviour: hardcoded reply, plus `/start` and
        `/restart` resets.
      * anything else -> logged and ignored.

    The optional ``sender`` performs the actual transport. Returns the reply
    so callers (and tests) can observe it.
    """
    message = update.message
    if message is None:
        logger.info("event=skipped reason=no_message update_id=%s", update.update_id)
        return None

    chat_id = message.chat.id
    text = message.text

    # ---- Phase 2: photo -> Bouncer -------------------------------------
    if message.largest_photo() is not None:
        if fetcher is None or classifier is None:
            # Not wired (ADK failed to initialise, or a unit test exercising
            # the text path only). The user still gets a friendly reply.
            logger.warning(
                "event=skipped reason=bouncer_not_wired chat_id=%s", chat_id
            )
            return _send_bouncer_fallback(
                chat_id, sender, reason="bouncer_not_wired"
            )

        decision = bounce(update, fetcher=fetcher, classifier=classifier)

        if decision.error:
            # bounce() already logged the underlying failure loudly; degrade
            # gracefully with a friendly reply — never stall (TECH policy).
            logger.error("event=bouncer_error chat_id=%s", chat_id)
            return _send_bouncer_fallback(chat_id, sender, reason="bouncer_error")

        if decision.reply is not None:
            # Rejection: bounce() already reset this chat's state.
            if sender is not None:
                sender.send(decision.reply)
            return decision.reply

        # Admitted — continue the confirmation flow unchanged.
        get_store().get_or_create(chat_id)
        reply = OutboundMessage(chat_id=chat_id, text=ReplyPolicy.HARDCODED_REPLY)
        if sender is not None:
            sender.send(reply)
        logger.info("event=replied chat_id=%s via=bouncer_admission", chat_id)
        return reply

    if text is None:
        # Some other non-text payload at AWAITING_PHOTO. Log it; do not crash.
        logger.info("event=skipped reason=non_text chat_id=%s", chat_id)
        return None

    # ---- Phase 1: text path --------------------------------------------
    store = get_store()

    command = _command_token(text)
    if command in RESET_COMMANDS:
        store.reset(chat_id)
        logger.info("event=state_reset command=%r chat_id=%s", command, chat_id)
    else:
        store.get_or_create(chat_id)

    reply = OutboundMessage(chat_id=chat_id, text=ReplyPolicy.HARDCODED_REPLY)

    if sender is not None:
        sender.send(reply)

    # A reply is a user-visible side effect — log it at INFO so operators can
    # audit what the bot actually sent without turning on DEBUG.
    logger.info("event=replied chat_id=%s", chat_id)
    return reply


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
            handle_update(update, sender, fetcher=fetcher, classifier=classifier)
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

        logger.info(
            "event=gateway_started transport=long_polling tracked_chats=%d "
            "bouncer_model=%s bouncer_wired=%s",
            len(store.chat_ids),
            BOUNCER_MODEL,
            classifier is not None,
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
                )
            except httpx.HTTPError:
                # Log loudly, keep polling (SPECS/TECH.md error policy).
                logger.exception("event=poll_failed")
            except Exception:  # the polling loop must not die
                logger.exception("event=unexpected_error")
