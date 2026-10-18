"""Telegram Bot API boundary models.

Every payload arriving from Telegram is untrusted and arbitrary
(SPECS/TECH.md, "Contracts at boundaries"). Nothing in this module
performs I/O; it only validates.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TelegramChat(BaseModel):
    """A Telegram chat. `id` scopes all session state."""

    model_config = ConfigDict(extra="ignore")

    id: int
    type: str = "private"


class TelegramPhotoSize(BaseModel):
    """One size variant of an uploaded photo.

    Telegram sends `message.photo` as an ARRAY of sizes, smallest first.
    """

    model_config = ConfigDict(extra="ignore")

    file_id: str
    file_unique_id: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    file_size: int | None = None


class TelegramMessage(BaseModel):
    """An incoming message. `text` is None for non-text payloads (photos etc.)."""

    model_config = ConfigDict(extra="ignore")

    message_id: int
    date: int
    chat: TelegramChat
    text: str | None = None
    photo: list[TelegramPhotoSize] = Field(default_factory=list)

    def largest_photo(self) -> TelegramPhotoSize | None:
        """The biggest variant Telegram offers — the one worth classifying.

        Telegram orders `photo` smallest-first, so the last element is the
        largest. Returns None when this message carries no photo.
        """
        return self.photo[-1] if self.photo else None


class TelegramUpdate(BaseModel):
    """One update pulled from `getUpdates`.

    `message` is None for update types we do not handle yet (callbacks,
    edits, channel posts).
    """

    model_config = ConfigDict(extra="ignore")

    update_id: int
    message: TelegramMessage | None = None


class OutboundMessage(BaseModel):
    """A message the gateway will send. Never a raw dict (TECH boundary rule)."""

    model_config = ConfigDict(extra="forbid")

    chat_id: int
    text: str = Field(min_length=1)


class BouncerVerdict(BaseModel):
    """Gemini's classification of one image, validated at the model boundary.

    SPECS/TECH.md: parse Gemini responses into typed models at the edge —
    a raw model string must never cross a module boundary.
    """

    model_config = ConfigDict(extra="ignore")

    human_present: bool
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    reason: str = Field(default="")


class DossierAnswer(BaseModel):
    """One Q/A pair in the interview dossier."""

    model_config = ConfigDict(extra="ignore")

    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _strip_non_empty(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for k in ("question", "answer"):
                v = data.get(k)
                if isinstance(v, str):
                    v = v.strip()
                    data[k] = v
                    if not v:
                        # Trigger min_length failure by passing empty string
                        continue
        return data


class Dossier(BaseModel):
    """Per-chat interview dossier accumulated over turns."""

    model_config = ConfigDict(extra="ignore")

    answers: list[DossierAnswer] = Field(default_factory=list)
    follow_ups_used: int = Field(default=0, ge=0)
    suggested_animal: str | None = None
    animal_rationale: str | None = None

    @property
    def asked(self) -> int:
        """Number of questions asked so far (derived from answers)."""
        return len(self.answers)


class OutboundPhoto(BaseModel):
    """A photo the gateway will send. Never a raw dict (TECH boundary rule)."""

    model_config = ConfigDict(extra="forbid")

    chat_id: int
    image_bytes: bytes
    caption: str | None = None
    filename: str = "portrait.png"
    mime_type: str = "image/png"


class InterviewTurn(BaseModel):
    """Structured output from the Interviewer per turn."""

    model_config = ConfigDict(extra="ignore")

    next_question: str | None = None
    is_complete: bool = False
    is_follow_up: bool = False
    suggested_animal: str | None = None
    animal_rationale: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _validate_cross_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            is_complete = data.get("is_complete", False)
            next_question = data.get("next_question")
            suggested_animal = data.get("suggested_animal")

            if is_complete:
                if suggested_animal is None or (
                    isinstance(suggested_animal, str) and not suggested_animal.strip()
                ):
                    raise ValueError(
                        "suggested_animal is required when is_complete is true"
                    )
            else:
                if next_question is None or (
                    isinstance(next_question, str) and not next_question.strip()
                ):
                    raise ValueError(
                        "next_question is required when not complete"
                    )
                if suggested_animal is not None and str(suggested_animal).strip():
                    raise ValueError(
                        "suggested_animal must be null when not complete"
                    )
        return data

class Script(BaseModel):
    """Narration script from Scripter."""
    model_config = ConfigDict(extra="ignore")
    text: str = Field(min_length=1, max_length=600)
