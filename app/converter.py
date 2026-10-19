"""Converter agent: photo + dossier + suggested animal -> hybrid portrait.

SPECS/2026-10-17-converter: the Converter is fed **all three inputs at
once** — the user's original photo, the interview dossier/personality
summary, and the suggested animal — in a single multimodal request to
Gemini 3.1 Flash Image. No intermediate text step. The generated image is
validated (Pydantic) and returned for delivery straight back to Telegram.

The model's response is parsed defensively: any missing candidate, part,
or inline image data surfaces as a loud ``RuntimeError`` that the gateway
turns into a friendly reply (SPECS/TECH.md error policy).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.logging_utils import get_logger
from app.resilience import call_with_retries

if TYPE_CHECKING:
    from app.models import Dossier

logger = get_logger("converter")

#: The image model (SPECS/TECH.md, model table).
CONVERTER_MODEL = "gemini-3.1-flash-image"

#: System-style instruction that keeps likeness while blending the animal.
_CONVERTER_PROMPT = (
    "You are a wildlife documentary portrait artist. Create a single "
    "animal-hybrid portrait of the person in the attached photo.\n\n"
    "Requirements:\n"
    "- Preserve the person's recognizable likeness (face, features).\n"
    "- Blend the suggested animal's traits into them so the result reads as "
    "a hybrid of the two.\n"
    "- Reflect the person's personality and habits described in the dossier "
    "below through pose, expression, and setting.\n"
    "- Return only the final portrait image; no extra text.\n\n"
    "Suggested animal: {animal}\n\n"
    "Personality / dossier summary:\n{dossier}"
)


class ConverterResult(BaseModel):
    """Validated output of a conversion.

    Carries both the image bytes and the animal the portrait embodies, so a
    caller can verify the animal actually made it into the request.
    """

    model_config = ConfigDict(extra="ignore")

    suggested_animal: str | None = None
    image_mime_type: str = Field(default="image/png")
    image_base64: str | None = None
    image_bytes: bytes | None = None


class Converter(Protocol):
    """What the gateway needs from the image stage — narrow for testing."""

    def generate(
        self,
        *,
        original_photo_bytes: bytes,
        original_photo_mime: str,
        dossier_text: str,
        suggested_animal: str,
    ) -> ConverterResult: ...


def dossier_to_summary(dossier: Dossier) -> str:
    """Flatten a dossier into the plain-text personality summary.

    Kept here (next to the agent that consumes it) so the Converter's input
    shape lives with the Converter. Only the user's own answers and the
    rationale — never any secret.
    """
    parts: list[str] = []
    for index, item in enumerate(dossier.answers, start=1):
        parts.append(f"Q{index}: {item.question}\nA{index}: {item.answer}")
    if dossier.animal_rationale:
        parts.append(f"Why this animal fits: {dossier.animal_rationale}")
    return "\n\n".join(parts)


class GenAiConverter:
    """Concrete Converter backed by the google-genai SDK.

    A transient Gemini timeout is retried with capped backoff (ROADMAP §7);
    exhaustion re-raises for the gateway's portrait fallback.
    """

    #: Class-level default so instances built via ``__new__`` (tests that
    #: bypass the genai-client construction) still resolve a sleeper.
    _sleeper: Callable[[float], None] = time.sleep

    def __init__(self, *, sleeper: Callable[[float], None] = time.sleep) -> None:
        from google import genai

        self._sleeper = sleeper
        self._client = genai.Client()

    def generate(
        self,
        *,
        original_photo_bytes: bytes,
        original_photo_mime: str,
        dossier_text: str,
        suggested_animal: str,
    ) -> ConverterResult:
        from google.genai import types

        def _generate() -> ConverterResult:
            prompt = _CONVERTER_PROMPT.format(
                animal=suggested_animal, dossier=dossier_text
            )

            # All three inputs go out together in one multimodal call.
            contents = [
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(
                            data=original_photo_bytes, mime_type=original_photo_mime
                        ),
                        types.Part.from_text(text=prompt),
                    ],
                )
            ]

            logger.info(
                "event=converter_request model=%s animal=%s photo_bytes=%d",
                CONVERTER_MODEL,
                suggested_animal,
                len(original_photo_bytes),
            )

            response = self._client.models.generate_content(
                model=CONVERTER_MODEL,
                contents=contents,
            )

            image_bytes, mime_type = _extract_image(response)
            if image_bytes is None:
                raise RuntimeError("converter returned no image data")

            return ConverterResult(
                suggested_animal=suggested_animal,
                image_bytes=image_bytes,
                image_mime_type=mime_type,
            )

        return call_with_retries(_generate, sleeper=self._sleeper)


def _extract_image(response: object) -> tuple[bytes | None, str]:
    """Pull the first inline image out of a genai response.

    Defensive by design: the SDK shape differs across versions and the model
    may return text-only or nothing. Returns ``(None, "image/png")`` when no
    image is present; the caller turns that into a loud error.
    """
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is None:
                continue
            data = getattr(inline, "data", None)
            if not data:
                continue
            mime = getattr(inline, "mime_type", None) or "image/png"
            return data, mime
    return None, "image/png"
