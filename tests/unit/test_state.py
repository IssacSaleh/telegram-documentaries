"""Session state: in-memory, per-chat_id, one shared driver.

SPECS/TECH.md Session state:
- In-memory only. No database, no Redis, no on-disk session store.
- Versioned, per-chat_id schema. Never share state across chat IDs.
- One shared state driver.
- /start and /restart purge session state and temporary media.
- Named phases: AWAITING_PHOTO -> INTERVIEWING -> GENERATING_IMAGE
  -> SCRIPTING -> NARRATING -> DONE
"""

from __future__ import annotations

import logging

from app.state import Session, SessionPhase, SessionStore, get_store


def _populate_media(session: Session) -> Session:
    """Fill every media field the pipeline stashes on a live session."""
    session.original_photo_bytes = b"\xff\xd8photo"
    session.original_photo_mime = "image/jpeg"
    session.original_photo_file_id = "file-abc"
    session.script = "A dramatic narration."
    return session


class TestPhaseEnum:
    def test_expected_phases_exist_in_order(self) -> None:
        assert [p.name for p in SessionPhase] == [
            "AWAITING_PHOTO",
            "AWAITING_CONFIRMATION",
            "INTERVIEWING",
            "GENERATING_IMAGE",
            "SCRIPTING",
            "NARRATING",
            "DONE",
        ]

    def test_fresh_session_starts_awaiting_photo(self) -> None:
        store = SessionStore()
        assert store.get_or_create(111).phase is SessionPhase.AWAITING_PHOTO


class TestPerChatIsolation:
    def test_sessions_are_keyed_by_chat_id(self) -> None:
        store = SessionStore()
        store.get_or_create(111)
        store.get_or_create(222)

        assert set(store.chat_ids) == {111, 222}

    def test_state_for_one_chat_never_leaks_into_another(self) -> None:
        """Non-negotiable: never leak another user's session (MISSION)."""
        store = SessionStore()
        store.get_or_create(111).phase = SessionPhase.INTERVIEWING

        other = store.get_or_create(222)
        assert other.phase is SessionPhase.AWAITING_PHOTO

    def test_get_or_create_returns_the_same_object_for_the_same_chat(self) -> None:
        store = SessionStore()
        first = store.get_or_create(111)
        first.phase = SessionPhase.SCRIPTING

        assert store.get_or_create(111).phase is SessionPhase.SCRIPTING

    def test_stores_are_independent_instances(self) -> None:
        a, b = SessionStore(), SessionStore()
        a.get_or_create(111)

        assert b.chat_ids == set()


class TestReset:
    def test_reset_purges_the_session_so_the_next_run_starts_clean(self) -> None:
        """MISSION Success criteria -> Reset."""
        store = SessionStore()
        session = store.get_or_create(111)
        session.phase = SessionPhase.NARRATING

        store.reset(111)

        assert 111 not in store.chat_ids
        assert store.get_or_create(111).phase is SessionPhase.AWAITING_PHOTO

    def test_reset_of_an_unknown_chat_is_a_no_op(self) -> None:
        store = SessionStore()
        store.reset(999)  # must not raise
        assert store.chat_ids == set()

    def test_reset_leaves_other_chats_intact(self) -> None:
        store = SessionStore()
        store.get_or_create(111).phase = SessionPhase.DONE
        store.get_or_create(222).phase = SessionPhase.INTERVIEWING

        store.reset(111)

        assert 222 in store.chat_ids
        assert store.get_or_create(222).phase is SessionPhase.INTERVIEWING

    def test_reset_purges_the_chats_temporary_media(self, caplog) -> None:
        """ROADMAP §7 / acceptance 1: reset must drop the media the session
        owned (photo bytes/mime/file_id + script), not just the dict entry,
        and the purge must be logged."""
        store = SessionStore()
        _populate_media(store.get_or_create(111))

        with caplog.at_level(logging.INFO, logger="telegram_documentaries.state"):
            store.reset(111)

        assert 111 not in store.chat_ids
        assert any(
            "event=temp_media_purged" in r.message and "chat_id=111" in r.message
            for r in caplog.records
        ), "the media purge must be visible to operators"

    def test_reset_clears_media_fields_before_dropping_the_session(self) -> None:
        """The Session object must not keep media alive via any other
        reference after the purge — the bytes themselves must be gone."""
        store = SessionStore()
        session = _populate_media(store.get_or_create(111))

        store.reset(111)

        assert session.original_photo_bytes is None
        assert session.original_photo_mime is None
        assert session.original_photo_file_id is None
        assert session.script is None

    def test_reset_of_an_unknown_chat_logs_no_purge(self, caplog) -> None:
        store = SessionStore()

        with caplog.at_level(logging.INFO, logger="telegram_documentaries.state"):
            store.reset(999)

        assert not any("event=temp_media_purged" in r.message for r in caplog.records)

    def test_reset_is_idempotent(self) -> None:
        store = SessionStore()
        _populate_media(store.get_or_create(111))

        store.reset(111)
        store.reset(111)  # must not raise

        assert store.chat_ids == set()


class TestClearMedia:
    def test_clears_every_media_field(self) -> None:
        session = _populate_media(Session(chat_id=111))

        session.clear_media()

        assert session.original_photo_bytes is None
        assert session.original_photo_mime is None
        assert session.original_photo_file_id is None
        assert session.script is None

    def test_preserves_phase_dossier_and_pending_question(self) -> None:
        """Media purge is not a state reset — the interview in progress
        (phase, dossier, pending question) must survive."""
        session = _populate_media(Session(chat_id=111))
        session.phase = SessionPhase.NARRATING
        session.pending_question = "What is your spirit animal?"
        from app.models import Dossier

        dossier = Dossier(suggested_animal="Red Fox")
        session.dossier = dossier

        session.clear_media()

        assert session.phase is SessionPhase.NARRATING
        assert session.pending_question == "What is your spirit animal?"
        assert session.dossier is dossier

    def test_touches_the_session(self) -> None:
        session = _populate_media(Session(chat_id=111))
        before = session.updated_at

        session.clear_media()

        assert session.updated_at >= before

    def test_is_idempotent(self) -> None:
        session = _populate_media(Session(chat_id=111))

        session.clear_media()
        session.clear_media()  # must not raise

        assert session.original_photo_bytes is None


class TestPurge:
    """ROADMAP §7: a reset drops the *whole* chat — conversation history
    (dossier + open question) as well as temporary media — not just the dict
    entry. There are no background tasks to cancel (the loop is synchronous);
    `purge()` is the explicit, testable clear-point `reset()` uses."""

    def _mid_conversation(self) -> Session:
        from app.models import Dossier, DossierAnswer

        session = _populate_media(Session(chat_id=111))
        session.phase = SessionPhase.INTERVIEWING
        session.pending_question = "What is your spirit animal?"
        session.dossier = Dossier(
            answers=[DossierAnswer(question="Q1?", answer="A1")],
            suggested_animal="Red Fox",
        )
        return session

    def test_clears_conversation_history_and_media(self) -> None:
        session = self._mid_conversation()

        session.purge()

        assert session.dossier is None, "conversation history must be gone"
        assert session.pending_question is None, "the open question must be gone"
        assert session.original_photo_bytes is None
        assert session.original_photo_mime is None
        assert session.original_photo_file_id is None
        assert session.script is None

    def test_reset_clears_a_held_reference_too(self) -> None:
        """Dropping the store entry must not be the *only* cleanup: a held
        reference to the old Session must not retain the user's data."""
        from app.models import Dossier, DossierAnswer

        store = SessionStore()
        session = store.get_or_create(111)
        _populate_media(session)
        session.phase = SessionPhase.INTERVIEWING
        session.pending_question = "What is your spirit animal?"
        session.dossier = Dossier(
            answers=[DossierAnswer(question="Q1?", answer="A1")],
            suggested_animal="Red Fox",
        )

        store.reset(111)

        assert store.get(111) is None
        assert session.dossier is None
        assert session.pending_question is None
        assert session.original_photo_bytes is None
        assert session.script is None

    def test_is_idempotent(self) -> None:
        session = self._mid_conversation()

        session.purge()
        session.purge()  # must not raise

        assert session.dossier is None


class TestGlobalDriver:
    def test_get_store_returns_a_shared_singleton(self) -> None:
        """TECH: one shared state driver — stages do not invent their own."""
        assert get_store() is get_store()

    def test_shared_store_is_in_memory_only(self) -> None:
        # No persistence layer: clearing the dict is all it takes to wipe
        # everything. Clear first — earlier tests may have registered chats
        # into this same singleton.
        store = get_store()
        store.clear()
        assert store.chat_ids == set()

        store.get_or_create(4242)
        assert store.chat_ids == {4242}

        store.reset(4242)
        assert store.chat_ids == set()
