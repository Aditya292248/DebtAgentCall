"""Shared pytest setup for the episode tests."""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest


@cache
def _database_unreachable() -> str | None:
    """Why the database can't be used, or None if it answers. Checked once per run."""
    from psycopg.conninfo import conninfo_to_dict

    from harness.db import connect, database_url

    try:
        with connect() as conn:
            conn.execute("select 1 from public.debt limit 1")
    except Exception as exc:  # noqa: BLE001 - any failure means "skip", with the reason
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
        # Host and port only: a hosted DATABASE_URL carries a password.
        info = conninfo_to_dict(database_url())
        where = f"{info.get('host')}:{info.get('port', 5432)}"
        return f"no database at {where} ({first_line}); run `supabase start`"
    return None


@pytest.fixture(autouse=True)
def _skip_db_tests_without_a_database(request: pytest.FixtureRequest) -> None:
    """Tests marked ``db`` use the real local database, or are skipped, never faked."""
    if request.node.get_closest_marker("db") and (reason := _database_unreachable()):
        pytest.skip(reason)


@pytest.fixture(autouse=True)
def no_default_skills(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agents in tests see no skills unless a test passes ``skills_dir``.

    Otherwise the repo's skills/ folder would add a menu to the system prompt and
    a load_skill tool to every exact-payload check. Agent reads DEFAULT_SKILLS_DIR
    when it is built, so this covers every agent.
    """
    import harness.agent

    monkeypatch.setattr(harness.agent, "DEFAULT_SKILLS_DIR", tmp_path_factory.mktemp("no-skills"))


@pytest.fixture
def no_instructions(tmp_path: Path) -> Path:
    """A system-prompt path that doesn't exist, so the payload is history only.

    Tests that check the exact messages sent to the model use this so they
    don't depend on whatever is in the repo's instructions/system.md.
    """
    return tmp_path / "missing.md"
