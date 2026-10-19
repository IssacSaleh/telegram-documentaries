"""Gateway: long-poll Telegram and route updates.

Phase 1 introduced the long-polling loop and the hardcoded text reply.
Phase 2 added **the Bouncer**: photo updates are downloaded and classified
by Gemini 3.1 Flash Lite (via Google ADK) to decide whether a human is
present. Non-human submissions get a cheeky rejection and a state reset.
Phase 3 adds **the Interviewer**: an admitted human is invited to be
interviewed, then guided through 5–7 questions to a suggested animal.
Phase 4 adds **the Converter**: once the interview closes, the original
photo, the dossier/personality summary, and the suggested animal go to
Gemini 3.1 Flash Image in one multimodal request, and the hybrid portrait
comes straight back to Telegram.
Phase 5 adds **the Scripter**: the finished interview is narrated as a
one-paragraph British-documentary script, stored on the session and sent
to the chat as plain text.
Phase 6 adds **the Narrator** (not an ADK agent): the script goes straight
to Gemini TTS, is encoded as OGG/Opus, and follows the script text as a
Telegram voice note.

Routing is driven by the chat's phase (SPECS/2026-10-16-interviewer and
SPECS/2026-10-17-converter):

  * **photo**  -> the Bouncer. Admitted humans enter the confirmation flow;
    rejections reset the chat.
  * **text**   -> phase-aware: no session gets the "send a photo" prompt;
    AWAITING_CONFIRMATION starts the interview on YES; INTERVIEWING records
    an answer and asks the next question; DONE triggers the Converter, which
    sends the portrait.
  * **other**  -> mid-interview, a gentle "answer in words"; otherwise logged
    and ignored.

Any failure (fetch, classification, conversion, script, TTS) degrades to a
friendly reply — the user is never left in silence (SPECS/TECH.md error
policy). A stage that is not wired is logged and ends the chain cleanly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import httpx
from pydantic import ValidationError

from app.bouncer import BOUNCER_MODEL, bounce
from app.config import TELEGRAM_API, telegram_payload
from app.converter import CONVERTER_MODEL, Converter, GenAiConverter
from app.interviewer import (
    IMAGE_STAGE_PHASES,
    INTERVIEWER_MODEL,
    PHOTO_PROMPT,
    AdkInterviewer,
    handle_answer,
    handle_confirmation,
    handle_photo_admitted,
    handle_portrait_pending,
    handle_wrong_payload,
    handle_wrong_stage,
)
from app.logging_utils import get_logger, logged
from app.models import Dossier, OutboundMessage, OutboundPhoto, TelegramUpdate
from app.narrator import NARRATOR_MODEL, GeminiTtsNarrator, Narrator, OutboundVoice
from app.scripter import AdkScripter, Scripter
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


class NarratorFallbackPolicy:
    """Friendly reply when the voice note cannot be delivered.

    The Scripter's text narration has already reached the chat, so a TTS
    failure (or a failed sendVoice) degrades to this reply instead of
    silence — the conversation continues and the session lands in DONE
    (SPECS/TECH.md error policy; decision D6).
    """

    ERROR_REPLY = (
        "The voice note didn't make it out this time — the narration above "
        "is still yours to enjoy. Better luck next run!"
    )


class ScripterFallbackPolicy:
    """Friendly reply when the narration could not be written.

    The portrait has already reached the chat, so a Scripter failure (or an
    empty script) degrades to this reply instead of silence — the user is
    told the narration is not coming and the session lands in DONE
    (SPECS/TECH.md error policy; SPECS/2026-10-20-phase-polish decision P2).
    """

    ERROR_REPLY = (
        "The narration didn't come together this time — but your portrait "
        "above is all yours. Better luck next run!"
    )


class MessageSender(Protocol):
    """What the handler needs from a transport — kept narrow for testing."""

    def send(self, message: OutboundMessage) -> None: ...

    def send_photo(self, message: OutboundPhoto) -> None: ...

    def send_voice(self, message: OutboundVoice) -> None: ...


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


def _build_dossier_text(dossier: Dossier) -> str:
    """Flatten a dossier into the personality summary the Converter reads.

    Question/answer pairs plus the animal rationale — everything the
    interviewer learned — as one plain-text block. Never includes any
    secret; only the user's own answers.
    """
    parts: list[str] = []
    for index, item in enumerate(dossier.answers, start=1):
        parts.append(f"Q{index}: {item.question}\nA{index}: {item.answer}")
    if dossier.animal_rationale:
        parts.append(f"Why this animal fits: {dossier.animal_rationale}")
    return "\n\n".join(parts)


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


def _run_converter(
    chat_id: int,
    session,
    sender: MessageSender | None,
    converter: Converter,
    scripter: Scripter | None = None,
    narrator: Narrator | None = None,
) -> OutboundMessage | None:
    """Run the Converter for a finished interview and deliver the full chain.

    Inputs go in together — the original photo, the dossier summary, and the
    suggested animal — in a single multimodal request (SPECS/2026-10-17).
    On success the portrait goes straight to Telegram, the Scripter's
    narration follows it as plain text (decision D2: the text message stays),
    and the Narrator's verified OGG/Opus voice note follows that — portrait
    -> script text -> voice note, one stage per try/except. Any failure in
    that chain degrades to a friendly reply (SPECS/TECH.md error policy); a
    stage that is not wired only logs and ends the chain at DONE.

    Phase transitions per SPECS/TECH.md (decision D6): GENERATING_IMAGE when
    the fusion starts, SCRIPTING once the portrait is out, NARRATING once
    the script is back, DONE once the voice note is delivered (or on any
    failure branch) — the session is never left stuck mid-chain.
    """
    dossier = session.dossier
    if dossier is None or not dossier.suggested_animal:
        logger.error(
            "event=converter_skipped chat_id=%s reason=missing_dossier", chat_id
        )
        return _send(
            chat_id,
            handle_portrait_pending(chat_id).reply.text,
            sender,
            event="converter_inputs_missing",
        )
    if not session.original_photo_bytes:
        logger.error(
            "event=converter_skipped chat_id=%s reason=missing_original_photo",
            chat_id,
        )
        return _send(
            chat_id,
            handle_portrait_pending(chat_id).reply.text,
            sender,
            event="converter_inputs_missing",
        )

    try:
        session.phase = SessionPhase.GENERATING_IMAGE
        result = converter.generate(
            original_photo_bytes=session.original_photo_bytes,
            original_photo_mime=session.original_photo_mime or "image/jpeg",
            dossier_text=_build_dossier_text(dossier),
            suggested_animal=dossier.suggested_animal,
        )
        if not result.image_bytes:
            raise RuntimeError("converter returned no image bytes")
        if sender is not None:
            sender.send_photo(
                OutboundPhoto(
                    chat_id=chat_id,
                    image_bytes=result.image_bytes,
                    mime_type=result.image_mime_type or "image/png",
                    caption=f"Here you are — {dossier.suggested_animal}.",
                )
            )
        logger.info(
            "event=converter_completed chat_id=%s animal=%s bytes=%d",
            chat_id,
            dossier.suggested_animal,
            len(result.image_bytes),
        )
        # Portrait delivered: the Scripter is next (TECH.md state table).
        session.phase = SessionPhase.SCRIPTING
        if scripter is None:
            # Not wired: no narration to write, so nothing to narrate —
            # the chain ends cleanly at DONE instead of idling in SCRIPTING.
            logger.warning("event=scripter_not_wired chat_id=%s", chat_id)
            session.phase = SessionPhase.DONE
            return None
        try:
            script = scripter.generate_script(dossier)
        except Exception:
            session.phase = SessionPhase.DONE
            logger.exception("event=scripter_error chat_id=%s", chat_id)
            return _send(
                chat_id,
                ScripterFallbackPolicy.ERROR_REPLY,
                sender,
                event="scripter_error",
            )
        if not script:
            session.phase = SessionPhase.DONE
            logger.error("event=scripter_empty chat_id=%s", chat_id)
            return _send(
                chat_id,
                ScripterFallbackPolicy.ERROR_REPLY,
                sender,
                event="scripter_empty",
            )
        # Capture the raw narration on the session so it is available to the
        # Narrator (Phase 6) for audio synthesis, then deliver it as text.
        session.script = script
        session.touch()
        logger.info(
            "event=script_captured chat_id=%s chars=%d words=%d "
            "session_script_stored=%s",
            chat_id,
            len(script),
            len(script.split()),
            session.script is not None,
        )
        if sender is not None:
            sender.send(OutboundMessage(chat_id=chat_id, text=script))
        logger.info("event=script_sent chat_id=%s", chat_id)
        # Script delivered as text: the Narrator is next.
        session.phase = SessionPhase.NARRATING
        return _deliver_voice_note(chat_id, session, sender, narrator, script)
    except Exception:
        # Fail loudly, never crash the poll loop (SPECS/TECH.md error policy).
        session.phase = SessionPhase.DONE
        logger.exception("event=converter_error chat_id=%s", chat_id)
        return _send(
            chat_id,
            "The portrait didn't come out this time — try again in a moment?",
            sender,
            event="converter_error",
        )


def _deliver_voice_note(
    chat_id: int,
    session,
    sender: MessageSender | None,
    narrator: Narrator | None,
    script: str,
) -> OutboundMessage | None:
    """Synthesise ``script`` and deliver it as a Telegram voice note.

    Every branch lands the session in DONE (decision D6): success, narrator
    not wired (logged only — the script text above is already the
    deliverable), TTS failure, and sendVoice failure (those two degrade
    with a friendly reply). Nothing here may raise into the caller: the
    script text has already been sent, so silence would break the
    conversation (SPECS/TECH.md error policy).
    """
    if narrator is None:
        logger.warning("event=narrator_not_wired chat_id=%s", chat_id)
        session.phase = SessionPhase.DONE
        return None
    try:
        audio = narrator.synthesise(script)
    except Exception:
        session.phase = SessionPhase.DONE
        logger.exception("event=narrator_error chat_id=%s", chat_id)
        return _send(
            chat_id,
            NarratorFallbackPolicy.ERROR_REPLY,
            sender,
            event="narrator_error",
        )
    try:
        if sender is not None:
            sender.send_voice(OutboundVoice(chat_id=chat_id, audio_bytes=audio))
    except Exception:
        session.phase = SessionPhase.DONE
        logger.exception("event=voice_send_failed chat_id=%s", chat_id)
        return _send(
            chat_id,
            NarratorFallbackPolicy.ERROR_REPLY,
            sender,
            event="voice_send_failed",
        )
    session.phase = SessionPhase.DONE
    logger.info("event=voice_sent chat_id=%s bytes=%d", chat_id, len(audio))
    return None


@logged(logger, event="handle_update")
def handle_update(
    update: TelegramUpdate,
    sender: MessageSender | None = None,
    *,
    fetcher: PhotoFetcher | None = None,
    classifier: HumanPresenceClassifier | None = None,
    interviewer: Interviewer | None = None,
    converter: Converter | None = None,
    scripter: Scripter | None = None,
    narrator: Narrator | None = None,
) -> OutboundMessage | None:
    """Route one validated update through the phase-aware pipeline.

    Most updates produce a single reply, but a closing interview answer can
    drive the whole tail chain (interview reply + portrait + script text +
    voice note) in one pass. Routing is phase-aware
    (SPECS/2026-10-16-interviewer, 2026-10-17-converter):

      * **reset commands** (`/start`, `/restart`) purge the chat in *any*
        phase and then point the user at the photo flow.
      * **image stage in flight** (GENERATING_IMAGE / SCRIPTING /
        NARRATING): text *and* photo get the single "sit tight" nudge;
        the Bouncer is never consulted and no state is touched (ROADMAP §7).
      * **photo**  -> the Bouncer (Phase 2) gates it. Rejection is sent and
        the chat's ephemeral state is purged; an admission invites an
        interview and stores the original photo for the Converter. A Bouncer
        failure sends a friendly fallback without touching state.
      * **text**   -> no session (or AWAITING_PHOTO) gets the "send a photo"
        prompt and creates NO session; AWAITING_CONFIRMATION starts the
        interview on YES and re-asks otherwise; INTERVIEWING records the
        answer and asks the next question; DONE runs the Converter ->
        Scripter -> Narrator chain (portrait, script text, voice note).
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

    # ---- Stage guard: the portrait pipeline is in flight ----------------
    # R2 (ROADMAP §7): text OR a photo arriving during GENERATING_IMAGE /
    # SCRIPTING / NARRATING gets the single "sit tight" nudge and touches
    # no state — checked BEFORE the Bouncer path so a stray photo is never
    # re-gated as a fresh submission (it would clobber the in-flight run).
    if session is not None and session.phase in IMAGE_STAGE_PHASES:
        decision = handle_wrong_stage(chat_id)
        if sender is not None:
            sender.send(decision.reply)
        logger.info("event=replied chat_id=%s via=wrong_stage", chat_id)
        return decision.reply

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
        _store_original_photo(chat_id, message, fetcher)
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
        # Auto-trigger: the moment the interview closes, fuse the portrait
        # and deliver it (plus the narration) without waiting for another
        # message (ROADMAP Phase 4/5 acceptance).
        if session.phase is SessionPhase.DONE and converter is not None:
            _run_converter(chat_id, session, sender, converter, scripter, narrator)
        return decision.reply

    # DONE (the interview just closed): run the Converter and send the
    # portrait. Every other later phase has no handler yet, so it just teases.
    if session.phase == SessionPhase.DONE and converter is not None:
        return _run_converter(chat_id, session, sender, converter, scripter, narrator)

    # Any later phase (or a Converter that is not wired): point at the
    # portrait without pretending it exists.
    decision = handle_portrait_pending(chat_id)
    if sender is not None:
        sender.send(decision.reply)
    logger.info("event=replied chat_id=%s via=portrait_pending", chat_id)
    return decision.reply


def _store_original_photo(chat_id: int, message, fetcher: PhotoFetcher | None) -> None:
    """Fetch and stash the admitted user's largest photo for the Converter.

    The Converter needs the original image as one of its three inputs
    (SPECS/2026-10-17). A fetch failure is logged and swallowed here: the
    interview can still proceed, and the Converter degrades gracefully.
    """
    photo = message.largest_photo()
    if photo is None or fetcher is None:
        return
    try:
        data, mime = fetcher.fetch(photo.file_id)
    except Exception:
        logger.exception("event=photo_store_failed chat_id=%s", chat_id)
        return
    session = get_store().get(chat_id)
    if session is None:
        logger.warning("event=photo_store_skipped chat_id=%s reason=no_session", chat_id)
        return
    session.original_photo_bytes = data
    session.original_photo_mime = mime
    session.original_photo_file_id = photo.file_id
    logger.info("event=photo_stored chat_id=%s bytes=%d mime=%s", chat_id, len(data), mime)


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

    def send_photo(self, message: OutboundPhoto) -> None:
        """Upload image bytes to `sendPhoto` as multipart form data.

        Same secret-safety rule as `send`: no `raise_for_status()`.
        """
        files = {
            "photo": (message.filename, message.image_bytes, message.mime_type)
        }
        data: dict[str, str] = {"chat_id": str(message.chat_id)}
        if message.caption:
            data["caption"] = message.caption
        response = self._client.post(
            f"{TELEGRAM_API}/bot{self._token}/sendPhoto",
            data=data,
            files=files,
            timeout=60.0,
        )
        payload = telegram_payload(response)
        description = payload.get("description") or (
            f"HTTP {response.status_code} non-json body"
        )
        if not response.is_success or not payload.get("ok"):
            logger.error(
                "event=send_failed chat_id=%s status=%d description=%r",
                message.chat_id,
                response.status_code,
                description,
            )
            raise RuntimeError(f"telegram sendPhoto failed: {description!r}")

    def send_voice(self, message: OutboundVoice) -> None:
        """Upload audio bytes to `sendVoice` as multipart form data.

        Telegram renders `sendVoice` + `audio/ogg` as a playable voice note —
        `sendAudio`/MP3 would arrive as a file attachment instead, and MP3 is
        not a valid voice note at all (TECH.md hard requirement). Same
        secret-safety rule as `send`/`send_photo`: never `raise_for_status()`
        — the URL carries the bot token (SPECS/MISSION.md).
        """
        files = {
            "voice": (message.filename, message.audio_bytes, message.mime_type)
        }
        data: dict[str, str] = {"chat_id": str(message.chat_id)}
        response = self._client.post(
            f"{TELEGRAM_API}/bot{self._token}/sendVoice",
            data=data,
            files=files,
            timeout=60.0,
        )
        payload = telegram_payload(response)
        description = payload.get("description") or (
            f"HTTP {response.status_code} non-json body"
        )
        if not response.is_success or not payload.get("ok"):
            logger.error(
                "event=send_failed chat_id=%s status=%d description=%r",
                message.chat_id,
                response.status_code,
                description,
            )
            raise RuntimeError(f"telegram sendVoice failed: {description!r}")


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
    converter: Converter | None = None,
    scripter: Scripter | None = None,
    narrator: Narrator | None = None,
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
                converter=converter,
                scripter=scripter,
                narrator=narrator,
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

        # The Converter is wired the same way, independently again: a failure
        # to construct it must not stop the rest of the pipeline.
        try:
            converter: Converter | None = GenAiConverter()
        except Exception:
            logger.exception("event=converter_init_failed")
            converter = None  # DONE falls back to the portrait tease

        # Scripter wired
        try:
            scripter: Scripter | None = AdkScripter()
        except Exception:
            logger.exception("event=scripter_init_failed")
            scripter = None

        # The Narrator is the same story again, independently: a failure to
        # construct the plain Gemini client must not stop the rest of the
        # pipeline (the script text alone is still a complete answer).
        try:
            narrator: Narrator | None = GeminiTtsNarrator()
        except Exception:
            logger.exception("event=narrator_init_failed")
            narrator = None

        logger.info(
            "event=gateway_started transport=long_polling tracked_chats=%d "
            "bouncer_model=%s bouncer_wired=%s interviewer_model=%s "
            "interviewer_wired=%s converter_model=%s converter_wired=%s "
            "scripter_wired=%s narrator_model=%s narrator_wired=%s",
            len(store.chat_ids),
            BOUNCER_MODEL,
            classifier is not None,
            INTERVIEWER_MODEL,
            interviewer is not None,
            CONVERTER_MODEL,
            converter is not None,
            scripter is not None,
            NARRATOR_MODEL,
            narrator is not None,
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
                    converter=converter,
                    scripter=scripter,
                    narrator=narrator,
                )
            except httpx.HTTPError:
                # Log loudly, keep polling (SPECS/TECH.md error policy).
                logger.exception("event=poll_failed")
            except Exception:  # the polling loop must not die
                logger.exception("event=unexpected_error")
