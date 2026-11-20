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


_SETUP_ERROR: str | None = None  # why the test database could not be made, if it could not


def pytest_configure(config: pytest.Config) -> None:
    """Point every test at its own ``_test`` database, never the one the app uses.

    The database name must end in ``_test`` or the whole session aborts here, before
    a single test (or a single connection to anything else) runs. ``DATABASE_URL`` is
    replaced by the test database, so code under test cannot reach the app's data. The
    database is created on the same local server if it is missing, brought up to date
    from supabase/migrations/, and given the made-up people from supabase/demo_seed.sql.
    """
    global _SETUP_ERROR
    from harness.db import database_url
    from tasks import dbsetup

    app_url = database_url()  # also reads .env, so TEST_DATABASE_URL may live there
    try:
        test_url = dbsetup.resolve_test_database_url(os.environ, app_url)
    except dbsetup.UnsafeDatabase as exc:
        pytest.exit(f"test session aborted: {exc}", returncode=3)
    os.environ["DATABASE_URL"] = test_url
    try:
        dbsetup.prepare(test_url, seed=True)
    except dbsetup.UnsafeDatabase as exc:
        pytest.exit(f"test session aborted: {exc}", returncode=3)
    except Exception as exc:  # noqa: BLE001 - no server: db tests skip (or fail in CI)
        _SETUP_ERROR = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)


@cache
def _database_unreachable() -> str | None:
    """Why the database can't be used, or None if it answers. Checked once per run."""
    from psycopg.conninfo import conninfo_to_dict

    from harness.db import connect, database_url

    try:
        if _SETUP_ERROR:
            raise RuntimeError(_SETUP_ERROR)
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


@pytest.fixture(scope="session")
def empty_database_url() -> Iterator[str]:
    """A second, migrated test database with no people or debts in it.

    Made fresh for the session and dropped at the end. Only a name ending in
    ``_test`` is ever created or dropped here.
    """
    from tasks import dbsetup

    url = dbsetup.with_database(os.environ["DATABASE_URL"], "debtagentcall_empty_test")
    dbsetup.require_name_suffix(url, "_test")
    try:
        dbsetup.drop_database(url, "_test")
        dbsetup.prepare(url, seed=False)
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("REQUIRE_DATABASE") == "1":
            pytest.fail(f"REQUIRE_DATABASE=1 but the empty test database failed: {exc}")
        pytest.skip(f"no database server for the empty test database ({exc})")
    yield url
    dbsetup.drop_database(url, "_test")


@pytest.fixture
def empty_database(empty_database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point this test at the empty database: no people, phones, debts or calls.

    Whatever the test adds is removed again afterwards. This only ever touches
    the ``_test`` database made for the session (the name is checked first).
    """
    from tasks.dbsetup import require_name_suffix

    require_name_suffix(empty_database_url, "_test")
    _remove_all_people(empty_database_url)
    monkeypatch.setenv("DATABASE_URL", empty_database_url)
    yield empty_database_url
    _remove_all_people(empty_database_url)


def _remove_all_people(url: str) -> None:
    """Delete every person from ``url``; their phones, debts and calls go with them."""
    import psycopg

    with psycopg.connect(url) as conn:
        conn.execute("delete from public.debtor")


@pytest.fixture
def scratch_sam() -> Iterator[Debtor]:
    """A copy of seed Sam with fresh ids, in the test database, deleted afterwards.

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
