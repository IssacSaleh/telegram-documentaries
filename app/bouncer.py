"""The Bouncer — ROADMAP Phase 2: the vision gate.

"A portrait passes through to the Interviewer; a non-human image gets a
rejection reply and state resets; rejects are logged."

Flow:

1. Take the largest photo Telegram sent.
2. Download its bytes from the Telegram Bot API.
3. Hand them to Gemini 3.1 Flash Lite via a Google ADK agent for
   multimodal classification: is a discernible human face or body present?
4. **No human** -> cheeky rejection + purge that chat's ephemeral state.
5. **Human**  -> admit, and let the existing confirmation flow continue
   unchanged (no reply of our own here).

Text/image classification only — no TTS, no video (SPECS/MISSION.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import TELEGRAM_API, telegram_payload
from app.logging_utils import get_logger, logged
from app.models import BouncerVerdict, OutboundMessage, TelegramUpdate
from app.state import get_store

logger = get_logger("bouncer")

#: Model used for the vision gate (SPECS/TECH.md exact ID).
BOUNCER_MODEL = "gemini-3.1-flash-lite"


class RejectionPolicy:
    """Cheeky rejection copy. Kept as a named constant, matching the
    Phase 1 `ReplyPolicy` pattern so it stays greppable and testable."""

    REJECTION_REPLY = (
        "I appreciate the ambition, but this documentary is about *humans*. "
        "That's not a person — it looks like you've submitted a landscape, "
        "an object, or possibly just vibes. Send me a photo with an actual "
        "human in it and we'll talk. 🦁"
    )


#: Module-level alias so tests and callers can import one obvious name.
REJECTION_REPLY = RejectionPolicy.REJECTION_REPLY

#: Instruction for the ADK classification agent.
BOUNCER_INSTRUCTION = """You are a strict image gatekeeper for a wildlife \
documentary bot.

Look at the attached image and decide whether a DISCERNIBLE HUMAN FACE or \
HUMAN BODY is present. A human must be reasonably clear — not a tiny \
background silhouette, not a statue, not an animal, not a drawing of a person.

Rules:
- Set human_present to true only if you can actually see a human.
- Report your confidence from 0.0 to 1.0.
- Give a short reason naming what you saw.
- Respond with the structured output only."""


class PhotoFetcher(Protocol):
    """Downloads a Telegram file's bytes. Narrow so tests can substitute."""

    def fetch(self, file_id: str) -> tuple[bytes, str]:
        """Return (image_bytes, mime_type)."""
        ...


class HumanPresenceClassifier(Protocol):
    """Classifies image bytes as human-present or not."""

    def classify(self, image_bytes: bytes, mime_type: str) -> BouncerVerdict:
        ...


@dataclass(frozen=True)
class BouncerDecision:
    """Typed result of bouncing one photo. Never a raw dict."""

    admitted: bool
    verdict: BouncerVerdict | None
    reply: OutboundMessage | None
    error: bool = False


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class TelegramPhotoFetcher:
    """Resolves `getFile` and downloads the actual image bytes."""

    def __init__(self, token: str, client: httpx.Client) -> None:
        self._token = token
        self._client = client

    def fetch(self, file_id: str) -> tuple[bytes, str]:
        # No raise_for_status() anywhere in here: every URL in this class
        # carries the bot token, and httpx embeds the full URL in the
        # exception message — which would leak the secret into the logs
        # (SPECS/MISSION.md). Failures are reported as status + description.
        info_response = self._client.get(
            f"{TELEGRAM_API}/bot{self._token}/getFile",
            params={"file_id": file_id},
            timeout=30.0,
        )
        info = telegram_payload(info_response)
        if not info_response.is_success or not info.get("ok"):
            raise RuntimeError(
                "getFile failed: "
                f"HTTP {info_response.status_code} "
                f"{info.get('description') or 'non-json body'}"
            )

        path = info["result"]["file_path"]
        response = self._client.get(
            f"{TELEGRAM_API}/bot{self._token}/file/{path}",
            timeout=60.0,
        )
        if not response.is_success:
            detail = telegram_payload(response).get("description")
            raise RuntimeError(
                f"file download failed: HTTP {response.status_code}"
                + (f" {detail}" if detail else "")
            )

        mime = _guess_mime_type(path, response.headers.get("content-type"))
        return response.content, mime


def _guess_mime_type(path: str, header: str | None) -> str:
    if header and header.startswith("image/"):
        return header.split(";")[0].strip()
    lowered = path.lower()
    if lowered.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith(".webp"):
        return "image/webp"
    if lowered.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a response that may carry prose around it.

    Structured output is the contract, but a model occasionally narrates
    ("Here is my verdict: {...}") before answering. Tolerating that is
    cheaper than a failed classification, and the object is still validated
    by `BouncerVerdict` on the way in.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


class AdkHumanPresenceClassifier:
    """The Google ADK wiring: one `LlmAgent` behind a typed boundary.

    ADK builds the multimodal request; we validate its structured output
    into `BouncerVerdict` before it crosses the boundary (TECH rule).
    """

    def __init__(self) -> None:
        # Imported lazily so unit tests never pay the ADK import cost and
        # never accidentally make a network call.
        from google.adk.agents import LlmAgent

        self._agent = LlmAgent(
            name="bouncer",
            model=BOUNCER_MODEL,
            instruction=BOUNCER_INSTRUCTION,
            output_schema=BouncerVerdict,
            description="Vision gate: is a human present?",
        )

    def classify(self, image_bytes: bytes, mime_type: str) -> BouncerVerdict:
        """Run the ADK agent to completion. Synchronous by design.

        The gateway is a synchronous long-poll loop, so there is no running
        event loop to hang this on — `asyncio.run` creates and closes its
        own (unlike the deprecated `get_event_loop().run_until_complete`,
        which leaks the loop and fails outside the main thread).
        """
        import asyncio

        return asyncio.run(self._classify_async(image_bytes, mime_type))

    async def _classify_async(self, image_bytes: bytes, mime_type: str) -> BouncerVerdict:
        import uuid

        from google.adk.runners import InMemoryRunner
        from google.genai import types

        # One runner per photo: cheap enough at Phase 2 volume, and it keeps
        # verdicts from separate chats from ever sharing a session. Revisit
        # when the Interviewer/Converter arrive and per-call setup gets dear.
        runner = InMemoryRunner(agent=self._agent, app_name="bouncer")
        session_id = f"bouncer-{uuid.uuid4().hex}"
        await runner.session_service.create_session(
            app_name="bouncer", user_id="bouncer", session_id=session_id
        )

        content = types.Content(
            role="user",
            parts=[
                types.Part.from_text(text="Is a discernible human present?"),
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            ],
        )

        # The final response event carries the complete answer; earlier
        # events may be streamed fragments or progress text we must not
        # concatenate into the structured output.
        final_text: str | None = None
        latest_text: str | None = None
        async for event in runner.run_async(
            user_id="bouncer",
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
            raise RuntimeError("bouncer agent returned no output")

        return BouncerVerdict.model_validate_json(_extract_json(final_text))


# ---------------------------------------------------------------------------
# Core decision — pure, testable, never raises into the user's flow
# ---------------------------------------------------------------------------


@logged(logger, event="bounce")
def bounce(
    update: TelegramUpdate,
    *,
    fetcher: PhotoFetcher,
    classifier: HumanPresenceClassifier,
) -> BouncerDecision:
    """Decide whether this photo is admitted. Never raises."""
    message = update.message
    if message is None:
        logger.info("event=skipped reason=no_message update_id=%s", update.update_id)
        return BouncerDecision(admitted=False, verdict=None, reply=None, error=True)

    chat_id = message.chat.id
    largest = message.largest_photo()

    if largest is None:
        # Wrong payload at this stage: no photo to gate.
        logger.warning("event=skipped reason=no_photo chat_id=%s", chat_id)
        return BouncerDecision(admitted=False, verdict=None, reply=None, error=True)

    try:
        image_bytes, mime_type = fetcher.fetch(largest.file_id)
    except Exception:
        logger.exception("event=photo_fetch_failed chat_id=%s", chat_id)
        # Degrade gracefully: no rejection, no crash (TECH error policy).
        return BouncerDecision(admitted=False, verdict=None, reply=None, error=True)

    try:
        verdict = classifier.classify(image_bytes, mime_type)
    except Exception:
        logger.exception("event=classification_failed chat_id=%s", chat_id)
        return BouncerDecision(admitted=False, verdict=None, reply=None, error=True)

    if verdict.human_present:
        logger.info(
            "event=admitted chat_id=%s confidence=%.2f reason=%r",
            chat_id,
            verdict.confidence,
            verdict.reason,
        )
        return BouncerDecision(admitted=True, verdict=verdict, reply=None, error=False)

    # Rejection: purge ephemeral state for THIS chat only, then reply.
    get_store().reset(chat_id)
    logger.info(
        "event=rejected chat_id=%s confidence=%.2f reason=%r state_reset=true",
        chat_id,
        verdict.confidence,
        verdict.reason,
    )
    reply = OutboundMessage(chat_id=chat_id, text=REJECTION_REPLY)
    return BouncerDecision(admitted=False, verdict=verdict, reply=reply, error=False)
