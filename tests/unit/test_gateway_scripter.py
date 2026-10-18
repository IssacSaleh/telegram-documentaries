"""Test gateway integration for Scripter."""

from __future__ import annotations


def _photo_update(chat_id: int = 100):
    from app.models import TelegramUpdate
    return TelegramUpdate.model_validate({
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 1,
            "chat": {"id": chat_id, "type": "private"},
            "photo": [
                {"file_id": "small", "file_unique_id": "u1", "width": 90, "height": 90},
                {"file_id": "largest", "file_unique_id": "u2", "width": 900, "height": 900},
            ],
        },
    })


def _update(text: str, chat_id: int = 100):
    from app.models import TelegramUpdate
    return TelegramUpdate.model_validate({
        "update_id": 2,
        "message": {
            "message_id": 2,
            "date": 1,
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    })
