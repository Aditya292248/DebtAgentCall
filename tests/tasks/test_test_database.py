"""The test session's own database, and the guard that keeps tests off the app's.

What it checks: the database name must end in ``_test`` or the session refuses to
start (for ``TEST_DATABASE_URL`` and for the one derived from ``DATABASE_URL``);
a server that is not on this machine is refused; this very session is running on a
``_test`` database whatever ``DATABASE_URL`` was; migrations are applied once.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from tasks import dbsetup

APP = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"


def test_the_test_database_is_derived_from_the_app_url_with_a_test_name():
    url = dbsetup.resolve_test_database_url({}, APP)
    assert dbsetup.database_name(url) == "debtagentcall_test"


def test_an_explicit_test_database_is_used_when_it_ends_in_test():
    url = "postgresql://u:p@localhost:5432/other_test"
    assert dbsetup.resolve_test_database_url({"TEST_DATABASE_URL": url}, APP) == url


@pytest.mark.parametrize("name", ["postgres", "debtagentcall", "test_data", "app_test_old"])
def test_the_session_refuses_a_target_whose_name_does_not_end_in_test(name):
    url = f"postgresql://postgres:postgres@127.0.0.1:54322/{name}"
    with pytest.raises(dbsetup.UnsafeDatabase, match="must end with '_test'"):
        dbsetup.resolve_test_database_url({"TEST_DATABASE_URL": url}, APP)


def test_a_database_on_another_machine_is_refused_even_if_named_test():
    url = "postgresql://postgres:secret@db.example.com:5432/x_test"
    with pytest.raises(dbsetup.UnsafeDatabase, match="only a server on this machine"):
        dbsetup.resolve_test_database_url({"TEST_DATABASE_URL": url}, APP)
    with pytest.raises(dbsetup.UnsafeDatabase):
        dbsetup.resolve_test_database_url({}, url.replace("x_test", "postgres"))


def test_only_a_matching_name_can_be_dropped():
    with pytest.raises(dbsetup.UnsafeDatabase):
        dbsetup.drop_database(APP, "_test")


def test_a_pytest_run_aborts_before_any_test_when_the_target_is_not_a_test_database():
    env = {**os.environ, "TEST_DATABASE_URL": APP}  # the app database itself
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/compliance/test_calling_hours.py"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 3
    assert "must end with '_test'" in run.stdout + run.stderr
    assert "passed" not in run.stdout


def test_this_session_runs_on_a_test_database_not_the_app_database():
    from harness.db import database_url

    assert dbsetup.database_name(database_url()).endswith("_test")


@pytest.mark.db
def test_migrations_apply_once_and_the_demo_seed_is_loaded():
    from harness.db import connect

    with connect() as conn:
        assert dbsetup.apply_migrations(conn) == []  # the session already applied them all
        applied = {r[0] for r in conn.execute("select name from public._applied_migration")}
        assert applied == {p.name for p in dbsetup.migration_files()}
        assert dbsetup.demo_debtor_count(conn) == len(dbsetup.DEMO_DEBTOR_IDS) >= 9


@pytest.mark.db
def test_preparing_a_database_twice_changes_nothing(empty_database_url):
    dbsetup.prepare(empty_database_url, seed=False)
    with psycopg.connect(empty_database_url) as conn:
        assert dbsetup.apply_migrations(conn) == []
