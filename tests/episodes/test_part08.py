"""The audit log: every pre-call decision in a hash chain nobody can edit.

What it checks: the Python hash is the documented one (offline, against a known
value), and against the real local database that recorded decisions form a
valid chain, that updates, deletes and truncates are refused, that a row edited
behind the database's back is caught by ``verify_chain`` and ``audit-verify``,
and that a blocked pre-call check leaves a ``block`` row naming its rule.

The database tests run inside transactions that are rolled back, so they never
leave rows in the append-only table (or a broken chain) behind.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import psycopg
import pytest

from compliance import allow, block
from compliance.audit import GENESIS, canonical, chain_hash, record, verify_chain

WHEN = datetime(2026, 11, 5, 19, 0, tzinfo=UTC)  # 14:00 in New York
KNOWN_ROW = (
    1,
    WHEN,
    None,
    "11111111-1111-1111-1111-111111111111",
    "gate",
    "calling_hours",
    "allow",
    "within 08:00-21:00",
    '{"when_utc": "2026-11-05T19:00:00+00:00"}',
)
KNOWN_HASH = "4313f5fe1956d4d2111ccaa8117c1bd2ce0adbf5f8cde7f8b7d35e6d2b8ba759"


# --- offline: the hash itself ------------------------------------------------------


def test_the_canonical_hash_matches_a_known_value():
    text = canonical(*KNOWN_ROW)
    assert text == (
        '[1, "2026-11-05T19:00:00.000000Z", null, "11111111-1111-1111-1111-111111111111", '
        '"gate", "calling_hours", "allow", "within 08:00-21:00", '
        '"{\\"when_utc\\": \\"2026-11-05T19:00:00+00:00\\"}"]'
    )
    assert chain_hash(GENESIS, text) == KNOWN_HASH


def test_the_time_is_hashed_in_utc_whatever_zone_it_comes_in():
    eastern = timezone(timedelta(hours=-5))
    as_eastern = (KNOWN_ROW[0], WHEN.astimezone(eastern), *KNOWN_ROW[2:])
    assert chain_hash(GENESIS, canonical(*as_eastern)) == KNOWN_HASH


def test_changing_any_field_changes_the_hash():
    for i, value in [(5, "cease"), (6, "block"), (7, "something else"), (8, "{}")]:
        changed = list(KNOWN_ROW)
        changed[i] = value
        assert chain_hash(GENESIS, canonical(*changed)) != KNOWN_HASH
    assert chain_hash("f" * 64, canonical(*KNOWN_ROW)) != KNOWN_HASH


def test_a_naive_time_raises():
    naive = (KNOWN_ROW[0], datetime(2026, 11, 5, 19, 0), *KNOWN_ROW[2:])
    with pytest.raises(ValueError, match="timezone-aware"):
        canonical(*naive)


# --- against the real local database -------------------------------------------------


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """The app's own login, inside a transaction that is always rolled back."""
    from harness.db import connect

    with connect() as c, c.transaction(force_rollback=True):
        yield c


@pytest.fixture
def admin() -> Iterator[psycopg.Connection]:
    """The local superuser, the only login that can switch triggers off. Rolled back."""
    from psycopg.conninfo import conninfo_to_dict

    from harness.db import CONNECT_TIMEOUT, database_url

    info = conninfo_to_dict(database_url())
    info["user"] = "supabase_admin"
    try:
        c = psycopg.connect(**info, connect_timeout=CONNECT_TIMEOUT)
    except psycopg.OperationalError as exc:
        pytest.skip(f"can't log in as the local superuser supabase_admin ({exc})")
    with c, c.transaction(force_rollback=True):
        yield c


def _record_a_few(conn: psycopg.Connection) -> None:
    record(conn, allow("calling_hours", "within 08:00-21:00", {"when_utc": WHEN.isoformat()}))
    record(conn, block("cease", "the debtor asked us to stop", {"flags": [{"kind": "cease"}]}))
    record(conn, allow("dnc", "no dnc flag"), call_id="call-1")


@pytest.mark.db
def test_the_table_has_row_level_security_and_no_update_or_delete_grants(conn):
    rls = conn.execute(
        "select relrowsecurity from pg_class where oid = 'public.audit_event'::regclass"
    ).fetchone()
    assert rls == (True,)
    for role in ("anon", "authenticated", "service_role", "postgres"):
        for privilege in ("update", "delete", "truncate"):
            row = conn.execute(
                "select has_table_privilege(%s, 'public.audit_event', %s)", (role, privilege)
            ).fetchone()
            assert row == (False,), f"{role} still has {privilege}"


@pytest.mark.db
def test_sql_computes_the_same_known_hash(conn):
    seq, ts, call_id, debt_id, kind, rule_id, decision, reason, evidence = KNOWN_ROW
    row = conn.execute(
        "select encode(sha256(convert_to(%s || public.audit_event_canonical("
        "%s, %s, %s, %s::uuid, %s, %s, %s, %s, %s::jsonb), 'UTF8')), 'hex')",
        (GENESIS, seq, ts, call_id, debt_id, kind, rule_id, decision, reason, evidence),
    ).fetchone()
    assert row == (KNOWN_HASH,)


@pytest.mark.db
def test_recorded_decisions_make_a_valid_chain(conn):
    before = conn.execute("select coalesce(max(seq), 0) from public.audit_event").fetchone()
    assert before is not None
    _record_a_few(conn)
    new = conn.execute(
        "select seq, rule_id, decision, call_id, prev_hash, hash from public.audit_event "
        "where seq > %s order by seq",
        before,
    ).fetchall()
    assert [(r[1], r[2], r[3]) for r in new] == [
        ("calling_hours", "allow", None),
        ("cease", "block", None),
        ("dnc", "allow", "call-1"),
    ]
    assert [r[0] for r in new] == [before[0] + 1, before[0] + 2, before[0] + 3]
    assert new[1][4] == new[0][5] and new[2][4] == new[1][5]  # each links to the one before
    assert verify_chain(conn) == []


@pytest.mark.db
def test_the_trigger_ignores_a_made_up_seq_and_hash(conn):
    conn.execute(
        "insert into public.audit_event (seq, ts, kind, rule_id, decision, reason, prev_hash, hash)"
        " values (999999, '2000-01-01', 'gate', 'x', 'allow', 'forged', 'bad', 'bad')"
    )
    row = conn.execute(
        "select seq, ts, hash from public.audit_event order by seq desc limit 1"
    ).fetchone()
    assert row is not None
    assert row[0] != 999999 and row[1].year != 2000 and row[2] != "bad"
    assert verify_chain(conn) == []


# One connection per test: an insert holds the chain's lock until its transaction
# ends, so a second connection inserting in the same test would wait forever.


@pytest.mark.db
def test_update_is_refused(conn):
    record(conn, allow("dnc", "no dnc flag"))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("update public.audit_event set reason = 'edited'")


@pytest.mark.db
def test_update_is_refused_even_for_the_superuser(admin):
    # The superuser skips grants, so here it is the trigger that says no.
    record(admin, allow("dnc", "no dnc flag"))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        with admin.transaction():
            admin.execute("update public.audit_event set reason = 'edited'")


@pytest.mark.db
def test_delete_is_refused(conn):
    record(conn, allow("dnc", "no dnc flag"))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("delete from public.audit_event")


@pytest.mark.db
def test_delete_and_truncate_are_refused_even_for_the_superuser(admin):
    record(admin, allow("dnc", "no dnc flag"))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        with admin.transaction():
            admin.execute("delete from public.audit_event")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        with admin.transaction():
            admin.execute("truncate public.audit_event")


@pytest.mark.db
def test_an_edit_behind_the_triggers_back_is_caught(admin, capsys):
    from tasks.audit_verify import run

    _record_a_few(admin)
    assert verify_chain(admin) == []
    assert run(admin) == 0

    # Replica mode switches ordinary triggers off; only a superuser can do this.
    admin.execute("set local session_replication_role = replica")
    seq_row = admin.execute(
        "select max(seq) from public.audit_event where rule_id = 'cease'"
    ).fetchone()
    assert seq_row is not None
    admin.execute(
        "update public.audit_event set reason = 'nothing to see here' where seq = %s", seq_row
    )
    admin.execute("set local session_replication_role = origin")

    problems = verify_chain(admin)
    assert problems == [f"seq {seq_row[0]}: hash does not match the row's contents (cease)"]
    assert run(admin) == 1
    assert "audit chain BROKEN" in capsys.readouterr().out


@pytest.mark.db
def test_a_removed_row_is_caught(admin):
    _record_a_few(admin)
    admin.execute("set local session_replication_role = replica")
    admin.execute(
        "delete from public.audit_event where seq = (select max(seq) - 1 from public.audit_event)"
    )
    admin.execute("set local session_replication_role = origin")
    problems = verify_chain(admin)
    assert any("rows are missing" in p for p in problems)
    assert any("prev_hash does not match" in p for p in problems)


@pytest.mark.db
def test_a_blocked_precall_check_leaves_a_block_row(conn):
    from harness.precall import check_and_record

    before = conn.execute("select coalesce(max(seq), 0) from public.audit_event").fetchone()
    decisions = check_and_record(conn, "ACC-1011", WHEN, call_id="call-drew")  # Drew: bankruptcy
    rows = conn.execute(
        "select rule_id, decision, debt_id::text, call_id, reason from public.audit_event "
        "where seq > %s order by seq",
        before,
    ).fetchall()

    # Every decision is recorded, allows as well as the block.
    assert [(r[0], r[1]) for r in rows] == [
        (d.rule_id, "allow" if d.allow else "block") for d in decisions
    ]
    assert len(rows) == 8
    blocks = [r for r in rows if r[1] == "block"]
    assert [r[0] for r in blocks] == ["bankruptcy"]
    debt = conn.execute("select id::text from public.debt where account_number = 'ACC-1011'")
    debt_row = debt.fetchone()
    assert debt_row is not None
    assert all(r[2] == debt_row[0] and r[3] == "call-drew" for r in rows)
    assert verify_chain(conn) == []


@pytest.mark.db
def test_an_unknown_account_records_the_block_too(conn):
    from harness.precall import check_and_record

    before = conn.execute("select coalesce(max(seq), 0) from public.audit_event").fetchone()
    check_and_record(conn, "ACC-9999", WHEN)
    rows = conn.execute(
        "select rule_id, decision, debt_id, evidence from public.audit_event where seq > %s",
        before,
    ).fetchall()
    # With no debt to point at, the row still says which account was checked.
    assert rows == [("precall", "block", None, {"account_number": "ACC-9999"})]
    assert verify_chain(conn) == []
