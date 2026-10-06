"""Telegram Bot API boundary models.

Every payload arriving from Telegram is untrusted and arbitrary
(SPECS/TECH.md, "Contracts at boundaries"). Nothing in this module
performs I/O; it only validates.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
