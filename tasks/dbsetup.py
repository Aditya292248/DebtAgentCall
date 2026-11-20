"""Create, migrate and seed a throwaway database, and the guards around doing so.

Tests, demos and ``seed-demo`` all need a database with the tables from
``supabase/migrations/``. The app's own database holds real people, so anything
that writes for testing goes to a separate database on the same local server,
whose name must end in ``_test`` (tests) or ``_demo`` (demos). The guards live
here so every caller shares them:

- ``require_name_suffix`` refuses any other database name.
- ``require_local_server`` refuses to create a database on a host that is not
  this machine, so a hosted ``DATABASE_URL`` can never be used to make one.
- ``resolve_test_database_url`` picks the test database: ``TEST_DATABASE_URL``,
  else ``DATABASE_URL`` with the database name replaced. A ``DATABASE_URL`` that
  names an application database is never used as is.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"
DEMO_SEED = ROOT / "supabase" / "demo_seed.sql"

DEFAULT_TEST_DATABASE = "debtagentcall_test"
DEFAULT_DEMO_DATABASE = "debtagentcall_demo"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", ""}


def _demo_debtor_ids() -> tuple[str, ...]:
    """The ids ``supabase/demo_seed.sql`` gives its made-up debtors, read from that file.

    Any debtor not in this set is somebody a person added, and seed-demo leaves
    that database alone. Reading the file keeps the two from drifting apart.
    """
    text = DEMO_SEED.read_text(encoding="utf-8")
    block = text.split("insert into public.debtor", 1)[1].split("insert into public.phone", 1)[0]
    return tuple(re.findall(r"\('([0-9a-f-]{36})',\s*'[A-Z]", block))


DEMO_DEBTOR_IDS = _demo_debtor_ids()
if not DEMO_DEBTOR_IDS:  # an empty set would make "all demo people present" true of any database
    raise RuntimeError("could not read the demo debtor ids from supabase/demo_seed.sql")


class UnsafeDatabase(RuntimeError):
    """The database named is not one this tool may create or write test data to."""


def database_name(url: str) -> str:
    return str(conninfo_to_dict(url).get("dbname") or "")


def with_database(url: str, name: str) -> str:
    """``url`` pointing at database ``name`` instead (same server, user and password)."""
    return make_conninfo(url, dbname=name)


def require_name_suffix(url: str, suffix: str) -> str:
    name = database_name(url)
    if not name.endswith(suffix):
        raise UnsafeDatabase(
            f"refusing to use database {name!r}: its name must end with {suffix!r}, "
            "so that test and demo data can never reach a database the app uses"
        )
    return name


def require_local_server(url: str) -> None:
    host = str(conninfo_to_dict(url).get("host") or "")
    if host not in LOCAL_HOSTS:
        raise UnsafeDatabase(
            f"refusing to create a database on {host!r}: only a server on this machine"
        )


def resolve_test_database_url(env: Mapping[str, str], app_url: str) -> str:
    """The database the tests will use, or ``UnsafeDatabase`` if it is not a ``_test`` one."""
    explicit = env.get("TEST_DATABASE_URL")
    url = explicit or with_database(app_url, DEFAULT_TEST_DATABASE)
    require_name_suffix(url, "_test")
    require_local_server(url)
    return url


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def _maintenance(url: str) -> psycopg.Connection:
    return psycopg.connect(with_database(url, "postgres"), autocommit=True, connect_timeout=3)


def create_database(url: str) -> bool:
    """Create the database in ``url`` if it is missing; True if it was created."""
    name = database_name(url)
    require_local_server(url)
    with _maintenance(url) as c:
        if c.execute("select 1 from pg_database where datname = %s", (name,)).fetchone():
            return False
        c.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        return True


def drop_database(url: str, suffix: str) -> None:
    """Drop a throwaway database. Only a name ending in ``suffix`` is ever dropped."""
    name = require_name_suffix(url, suffix)
    require_local_server(url)
    with _maintenance(url) as c:
        c.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply the migrations this database has not had yet, in order; returns their names.

    A bookkeeping table records what ran, so a database made last week picks up
    only the migrations added since.
    """
    conn.execute("create table if not exists public._applied_migration (name text primary key)")
    done = {r[0] for r in conn.execute("select name from public._applied_migration")}
    applied = []
    for path in migration_files():
        if path.name in done:
            continue
        conn.execute(path.read_text(encoding="utf-8"))  # type: ignore[call-overload]
        conn.execute("insert into public._applied_migration (name) values (%s)", (path.name,))
        applied.append(path.name)
    conn.commit()
    return applied


def demo_debtor_count(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "select count(*) from public.debtor where id = any(%s::uuid[])", (list(DEMO_DEBTOR_IDS),)
    ).fetchone()
    return int(row[0]) if row else 0


def other_debtor_count(conn: psycopg.Connection) -> int:
    """Debtors that are not the made-up demo ones."""
    row = conn.execute(
        "select count(*) from public.debtor where not (id = any(%s::uuid[]))",
        (list(DEMO_DEBTOR_IDS),),
    ).fetchone()
    return int(row[0]) if row else 0


def load_demo_seed(conn: psycopg.Connection) -> str:
    """Load ``supabase/demo_seed.sql`` once, in one transaction; returns what happened.

    Safe to repeat: when every demo debtor is already there it does nothing, and
    a half-loaded set is refused rather than guessed at.
    """
    have = demo_debtor_count(conn)
    if have == len(DEMO_DEBTOR_IDS):
        return "already loaded"
    if have:
        raise UnsafeDatabase(
            f"only {have} of {len(DEMO_DEBTOR_IDS)} demo debtors are present; "
            "use a fresh database instead of guessing which rows to repair"
        )
    conn.execute(DEMO_SEED.read_text(encoding="utf-8"))  # type: ignore[call-overload]
    conn.commit()
    return "loaded"


def demo_seed_fingerprint() -> str:
    return hashlib.sha256(DEMO_SEED.read_bytes()).hexdigest()


def prepare_test_database(url: str) -> None:
    """Make the test database current: migrated, with exactly today's demo seed.

    The database is reused between runs, so a changed ``demo_seed.sql`` would
    never reach it. A marker records which seed it was built from; when that is
    not the current file, the database (only ever a ``_test`` one) is dropped
    and built again, so a test run never sees rows from an older seed.
    """
    require_name_suffix(url, "_test")
    create_database(url)
    fingerprint = demo_seed_fingerprint()
    with psycopg.connect(url, connect_timeout=3) as conn:
        built = conn.execute("select to_regclass('public._applied_migration')").fetchone()
        marker = conn.execute("select to_regclass('public._demo_seed_marker')").fetchone()
        current = False
        if marker and marker[0]:
            row = conn.execute("select sha from public._demo_seed_marker").fetchone()
            current = bool(row and row[0] == fingerprint)
        used_before = bool(built and built[0])
    if used_before and not current:
        drop_database(url, "_test")
        create_database(url)
    prepare(url, seed=True)
    with psycopg.connect(url, connect_timeout=3) as conn:
        conn.execute("create table if not exists public._demo_seed_marker (sha text not null)")
        conn.execute("delete from public._demo_seed_marker")
        conn.execute("insert into public._demo_seed_marker (sha) values (%s)", (fingerprint,))


def prepare(url: str, *, seed: bool) -> None:
    """Create ``url``'s database if missing, bring it up to date, and optionally seed it."""
    create_database(url)
    with psycopg.connect(url, connect_timeout=3) as conn:
        apply_migrations(conn)
        if seed:
            load_demo_seed(conn)


# --- demos and acceptance checks ------------------------------------------------

NEEDS_DATABASE_FROM = 6  # `part-06` and later read, and some write, the sample people


def part_needs_demo_data(part: str) -> bool:
    digits = part.rpartition("-")[2]
    return digits.isdigit() and int(digits) >= NEEDS_DATABASE_FROM


def use_demo_database() -> str:
    """Point DATABASE_URL at a throwaway ``_demo`` database with the sample people in it."""
    from harness.db import database_url

    url = with_database(database_url(), DEFAULT_DEMO_DATABASE)
    require_name_suffix(url, "_demo")
    prepare(url, seed=True)
    os.environ["DATABASE_URL"] = url
    return DEFAULT_DEMO_DATABASE


def demo_data_problem() -> str | None:
    """Why this part can't run against DATABASE_URL, or None if it can.

    A demo needs the sample people, and some of them write, so the database must
    hold the sample people and nobody else.
    """
    from harness.db import connect

    try:
        with connect() as conn:
            demo, others = demo_debtor_count(conn), other_debtor_count(conn)
    except Exception as exc:  # noqa: BLE001 - say what is wrong in one line
        return f"cannot reach the database ({exc}); start it with `npx supabase start`"
    if others:
        return (
            f"this database holds {others} real or non-demo person(s), and this part writes "
            "sample rows. Add --demo-db to run it on a throwaway database instead."
        )
    if demo < len(DEMO_DEBTOR_IDS):
        return (
            "this database has no sample people (a fresh start is empty on purpose). "
            "Run `uv run seed-demo` to load them, or add --demo-db to use a throwaway "
            "database."
        )
    return None


def prepare_part(part: str, use_demo_db: bool) -> int | None:
    """Get ready to run ``part``; an exit code if it must not run, else None."""
    if not part_needs_demo_data(part):
        return None
    try:
        if use_demo_db:
            print(f"using the throwaway database {use_demo_database()!r}\n")
    except Exception as exc:  # noqa: BLE001
        print(f"could not prepare the demo database: {exc}")
        return 1
    if problem := demo_data_problem():
        print(f"{part} needs the sample people, but {problem}")
        return 1
    return None
