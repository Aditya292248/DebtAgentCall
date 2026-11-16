"""Shared pytest setup for the episode tests."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import date
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from harness.call_state import Debtor


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
    """Tests marked ``db`` use the real local database, or are skipped, never faked.

    With ``REQUIRE_DATABASE=1`` (CI sets it) a missing database fails them instead,
    so CI can't go green with the database tests quietly skipped.
    """
    if request.node.get_closest_marker("db") and (reason := _database_unreachable()):
        if os.environ.get("REQUIRE_DATABASE") == "1":
            pytest.fail(f"REQUIRE_DATABASE=1 but {reason}")
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
def scratch_sam() -> Iterator[Debtor]:
    """A copy of seed Sam with fresh ids, in the real database, deleted afterwards.

    For ``db`` tests that write rows against a debt. They get their own debtor
    and debt instead of a seed row, so they pass on a fresh database, on a
    used one, and in CI, and never change the seed data or anything added by
    hand. Deleting the debtor takes its debt and the rows that point at it
    (plans, disputes, callbacks, ...) with it. Audit rows stay: that log can't be
    edited, and they are ordinary, valid entries.
    """
    from harness.call_state import Debtor
    from harness.db import connect

    tag = uuid.uuid4().hex[:12]
    sam = Debtor(
        debtor_id=str(uuid.uuid4()),
        debt_id=str(uuid.uuid4()),
        first_name="Sam",
        last_name=f"Doe-{tag}",
        dob=date(1985, 4, 12),
        zip="90012",
        creditor="Northwind Furniture",
        balance_cents=125000,
    )
    with connect() as conn:
        conn.execute(
            "insert into public.debtor (id, first_name, last_name, dob, address, zip, state)"
            " values (%s, %s, %s, %s, '100 Example St, Los Angeles', %s, 'CA')",
            (sam.debtor_id, sam.first_name, sam.last_name, sam.dob, sam.zip),
        )
        conn.execute(
            "insert into public.debt (id, debtor_id, account_number, creditor, balance_cents)"
            " values (%s, %s, %s, %s, %s)",
            (sam.debt_id, sam.debtor_id, f"TEST-{tag}", sam.creditor, sam.balance_cents),
        )
    try:
        yield sam
    finally:
        with connect() as conn:
            conn.execute("delete from public.debtor where id = %s", (sam.debtor_id,))


@pytest.fixture
def no_instructions(tmp_path: Path) -> Path:
    """A system-prompt path that doesn't exist, so the payload is history only.

    Tests that check the exact messages sent to the model use this so they
    don't depend on whatever is in the repo's instructions/system.md.
    """
    return tmp_path / "missing.md"
