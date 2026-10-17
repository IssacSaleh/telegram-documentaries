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

from app.state import SessionPhase, SessionStore, get_store


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
