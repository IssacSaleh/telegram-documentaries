"""Shared helper for the opt-in live-Gemini tests.

Every live test carries a module-level `pytestmark` gate that skips it unless
`RUN_LIVE_TESTS=1` — the default suite stays offline and deterministic
(SPECS/TECH.md):

    RUN_LIVE_TESTS=1 ./scripts/test

When a live run *is* requested, `live_credentials` loads `.env` the way the
app does (`load_settings()`) and, if the credentials are absent, **skips
cleanly instead of erroring** — so a live run on a machine with no `.env`
degrades to a skip, never a red suite. (This is deliberately a plain fixture,
not a `pytest_collection_modifyitems` hook: a hook in a nested `conftest.py`
still runs session-wide and would swallow the *offline* suite too.)
"""

from __future__ import annotations

import pytest

from app.config import ConfigError, load_settings


@pytest.fixture(scope="session")
def live_credentials() -> None:
    """Load `.env`/environment for a live run, or skip if it is missing.

    Pulling `.env` into the process environment is what lets the `genai`
    client find `GEMINI_API_KEY`; a missing/placeholder value becomes a skip
    (with the reason), matching the offline-by-default contract.
    """
    try:
        load_settings()
    except ConfigError as exc:
        pytest.skip(f"no credentials for live calls: {exc}")
