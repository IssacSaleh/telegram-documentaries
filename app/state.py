"""In-memory session state, keyed by chat_id.

SPECS/TECH.md, Session state:

- **In-memory only.** No database, no Redis, no on-disk session store.
  A process restart wipes all state — that is expected, not a bug.
- Versioned, per-`chat_id` schema. Never share state across chat IDs.
- **One shared state driver** — stages do not each invent their own storage.
- `/start` and `/restart` purge session state and temporary media, without
  restarting the process.

Non-negotiable (SPECS/MISSION.md): never leak another user's session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from threading import RLock

from app.logging_utils import get_logger
from app.models import Dossier

logger = get_logger("state")


class SessionPhase(Enum):
    """Named state-machine phases (SPECS/TECH.md, "State machine phases").

    Ordered — `auto()` assigns increasing values so phases can be compared by
    progression without hardcoding integers.
    """

    AWAITING_PHOTO = auto()
    AWAITING_CONFIRMATION = auto()
    INTERVIEWING = auto()
    GENERATING_IMAGE = auto()
    SCRIPTING = auto()
    NARRATING = auto()
    DONE = auto()


#: Version of the per-chat session schema. Bump on a breaking shape change so
#: stale in-memory sessions are recognisably obsolete.
SESSION_SCHEMA_VERSION = 2


@dataclass
class Session:
    """One chat's state. Never shared across chat IDs."""

    chat_id: int
    phase: SessionPhase = SessionPhase.AWAITING_PHOTO
    schema_version: int = SESSION_SCHEMA_VERSION
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    dossier: Dossier | None = None
    pending_question: str | None = None
    original_photo_bytes: bytes | None = None
    original_photo_mime: str | None = None
    original_photo_file_id: str | None = None
    #: Final narration script, ready for audio synthesis (Phase 5 output).
    script: str | None = None

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)

    def clear_media(self) -> None:
        """Purge the temporary media this session owns (ROADMAP §7).

        "Temporary media" is everything the pipeline stashed in memory for
        this chat — the original photo bytes/mime/file_id and the finished
        script. There are no on-disk temp files anywhere (SPECS/TECH.md),
        so this session is their only owner; clearing here is the single
        purge point. Phase/dossier/pending question are left alone — media
        cleanup is not a state reset.
        """
        self.original_photo_bytes = None
        self.original_photo_mime = None
        self.original_photo_file_id = None
        self.script = None
        self.touch()


class SessionStore:
    """The single in-memory state driver.

    Thread-safe: Telegram long polling and any worker threads both touch it.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, Session] = {}
        self._lock = RLock()

    @property
    def chat_ids(self) -> set[int]:
        with self._lock:
            return set(self._sessions)

    def get_or_create(self, chat_id: int) -> Session:
        with self._lock:
            session = self._sessions.get(chat_id)
            if session is None:
                session = Session(chat_id=chat_id)
                self._sessions[chat_id] = session
            session.touch()
            return session

    def get(self, chat_id: int) -> Session | None:
        with self._lock:
            return self._sessions.get(chat_id)

    def start_dossier(self, chat_id: int) -> Dossier:
        """Start a fresh dossier for the given chat."""
        with self._lock:
            session = self._sessions.get(chat_id)
            if session is None:
                session = Session(chat_id=chat_id)
                self._sessions[chat_id] = session
            dossier = Dossier()
            session.dossier = dossier
            session.touch()
            return dossier

    def get_dossier(self, chat_id: int) -> Dossier | None:
        """Get the dossier for the given chat if it exists."""
        with self._lock:
            session = self._sessions.get(chat_id)
            if session is None:
                return None
            return session.dossier

    def reset(self, chat_id: int) -> None:
        """Purge one chat's state and its temporary media. Idempotent.

        Used by `/start` and `/restart` (SPECS/MISSION.md, Reset criteria),
        and by the Bouncer on rejection. Never raises on unknown chats.
        The media purge is explicit and logged before the session is
        dropped, so operators can see temporary media was disposed of
        (ROADMAP §7 acceptance: "/restart leaves no residual state or
        temp media").
        """
        with self._lock:
            session = self._sessions.get(chat_id)
            if session is None:
                return
            session.clear_media()
            logger.info("event=temp_media_purged chat_id=%s", chat_id)
            self._sessions.pop(chat_id, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


_store = SessionStore()


def get_store() -> SessionStore:
    """The one shared state driver (SPECS/TECH.md)."""
    return _store
