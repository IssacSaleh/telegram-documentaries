"""Gateway: long-poll Telegram and route updates.

Phase 1 introduced the long-polling loop and the hardcoded text reply.
Phase 2 added **the Bouncer**: photo updates are downloaded and classified
by Gemini 3.1 Flash Lite (via Google ADK) to decide whether a human is
present. Non-human submissions get a cheeky rejection and a state reset;
humans continue the confirmation flow unchanged.

Text behaviour is unchanged from Phase 1 — every text message still gets
the same hardcoded reply.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import httpx
from pydantic import ValidationError

from app.bouncer import BOUNCER_MODEL, bounce
from app.logging_utils import get_logger, logged
from app.models import OutboundMessage, TelegramUpdate
from app.state import get_store

if TYPE_CHECKING:
    from app.bouncer import HumanPresenceClassifier, PhotoFetcher

logger = get_logger("gateway")

TELEGRAM_API = "https://api.telegram.org"

#: Commands that purge this chat's state and then get the normal reply.
#: SPECS/MISSION.md: "/start and /restart purge the session ... without
#: restarting the process."
RESET_COMMANDS = frozenset({"/start", "/restart"})


class ReplyPolicy:
    """The hardcoded reply. Phase 1 has exactly one, by design."""

    HARDCODED_REPLY = "hey mate!"


class MessageSender(Protocol):
    """What the handler needs from a transport — kept narrow for testing."""

    def send(self, message: OutboundMessage) -> None: ...


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
        the existing confirmation flow *unchanged*.
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
            # Not wired (e.g. a unit test exercising the text path only).
            logger.warning(
                "event=skipped reason=bouncer_not_wired chat_id=%s", chat_id
            )
            return None

        decision = bounce(update, fetcher=fetcher, classifier=classifier)

        if decision.error:
            # Logged inside bounce(); degrade gracefully, never crash.
            return None

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

    if text.strip() in RESET_COMMANDS:
        store.reset(chat_id)
        logger.info("event=state_reset command=%r chat_id=%s", text.strip(), chat_id)
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
        payload = response.json()
        if not payload.get("ok"):
            # Fail loudly and log — never swallow (SPECS/TECH.md).
            logger.error(
                "event=send_failed chat_id=%s description=%r",
                message.chat_id,
                payload.get("description"),
            )
            raise RuntimeError(f"telegram sendMessage failed: {payload.get('description')!r}")


def poll_once(
    token: str,
    client: httpx.Client,
    offset: int,
    sender: MessageSender,
    *,
    fetcher: PhotoFetcher | None = None,
    classifier: HumanPresenceClassifier | None = None,
) -> int:
    """Fetch one batch of updates, reply to each, return the next offset."""
    response = client.get(
        f"{TELEGRAM_API}/bot{token}/getUpdates",
        params={"offset": offset, "timeout": 30},
        timeout=45.0,
    )
    response.raise_for_status()
    body = response.json()

    if not body.get("ok"):
        logger.error("event=getUpdates_failed description=%r", body.get("description"))
        return offset

    next_offset = offset
    for raw in body.get("result", []):
        next_offset = max(next_offset, raw.get("update_id", 0) + 1)
        try:
            update = TelegramUpdate.model_validate(raw)
        except ValidationError as exc:
            # Untrusted input: log and skip the bad update, keep polling.
            logger.warning("event=invalid_update error=%r raw=%r", exc, raw)
            continue
        handle_update(update, sender, fetcher=fetcher, classifier=classifier)

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
