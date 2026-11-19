"""The operator chat's account tools: reads, notes, callbacks and stops.

What it checks:
  - No tool removes a stop flag, records consent, edits a balance or deletes
    anything: by name, by what the code says, and by running every tool (and
    approving everything they open) against a debtor with a cease flag, who is
    still flagged and still blocked afterwards.
  - ``next_allowed_call`` agrees with the dialer's own check: for a debtor
    blocked by calling hours, the dialer blocks just before the time it gives
    and allows at it; for a debtor with a cease, it gives no time at all.
  - ``add_note`` and ``schedule_callback`` write nothing without approval, or on
    a denial; approving writes one row and one audit row. A callback outside
    calling hours is refused before any approval opens.
  - A list call with a huge limit is capped; phones are masked everywhere,
    including inside audit evidence.
  - ``record_stop`` writes at once, with an audit row, and blocks calls.

The database tests use made-up debtors created in one transaction that is
rolled back at the end, so nothing is left behind.
"""

from __future__ import annotations

import inspect
import json
import secrets
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import harness.operator
import harness.operator_reads
from harness import operator_reads as reads
from harness.memory import JsonlSession
from harness.operator import Operator
from model.fake import fake, tool_call, tool_reply
from phone.client import FakePhoneClient

NIGHT = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)  # 04:00 in Los Angeles
DAY = datetime(2026, 11, 17, 19, 0, tzinfo=UTC)  # 11:00 in Los Angeles
LA = ZoneInfo("America/Los_Angeles")

# What the chat must never be able to do, as words that would be in a tool's name.
FORBIDDEN = ("remove", "delete", "clear", "unset", "lift", "undo_stop", "consent", "balance",
             "edit", "update", "erase", "purge")  # fmt: skip


def make(tmp_path: Path, script: list, conn=None, clock=NIGHT) -> Operator:
    return Operator(
        JsonlSession.for_call(tmp_path / "chats", "chat-reads"),
        provider=fake(scripted=script),
        connect=(lambda: nullcontext(conn)) if conn is not None else _no_database,
        phone=FakePhoneClient(),
        clock=lambda: clock,
        lookup=None if conn is not None else (lambda _a: None),
        tz=LA,
    )


def _no_database():
    raise AssertionError("this path must not touch the database")


def asks(name: str, cid: str = "c1", **args) -> list:
    return [tool_reply(tool_call(name, cid, **args)), "Done."]


# --- offline -------------------------------------------------------------------


def test_no_tool_can_remove_a_stop_or_change_what_it_must_not(tmp_path):
    """The negative case, by name and by code: nothing lifts a stop flag."""
    names = (
        Operator(JsonlSession.for_call(tmp_path, "unused"), provider=fake(), connect=_no_database)
        .tools()
        .names()
    )
    assert "record_stop" in names
    for name in names:
        assert not any(word in name for word in FORBIDDEN), name
    for module in (harness.operator, harness.operator_reads):
        source = inspect.getsource(module).lower()
        for sql in ("delete from", "update public.flags", "update public.phone",
                    "update public.debt set balance", "truncate public"):  # fmt: skip
            assert sql not in source, (module.__name__, sql)


def test_limits_are_capped_and_checked():
    assert reads.parse_limit("1000000", reads.MAX_LIST) == reads.MAX_LIST
    assert reads.parse_limit("", reads.MAX_LIST) == reads.MAX_LIST
    assert reads.parse_limit("3", reads.MAX_LIST) == 3
    for bad in ("0", "-1", "ten", "2.5"):
        with pytest.raises(reads.ReadError):
            reads.parse_limit(bad, reads.MAX_LIST)


def test_phones_are_masked_however_deep():
    masked = reads.mask_in({"a": ["call +12135550101 now", {"e164": "+16175550104"}]})
    assert masked == {"a": ["call ***-***-0101 now", {"e164": "***-***-0104"}]}


def test_a_callback_in_the_past_or_unparseable_opens_nothing(tmp_path):
    for when in ("2026-11-16 10:00", "tomorrow-ish"):
        op = make(tmp_path, asks("schedule_callback", debtor_id="ACC-1001", when=when))
        turn = op.chat("call them back")
        assert turn.pending_approvals == []
        assert turn.tool_calls[0]["result"].startswith("error:")


def test_an_unknown_stop_kind_writes_nothing(tmp_path):
    op = make(tmp_path, asks("record_stop", debtor_id="ACC-1001", kind="pause"))
    assert op.chat("pause them").tool_calls[0]["result"].startswith("error: kind must be")


@pytest.mark.parametrize("kind", ["attorney", "bankruptcy"])
def test_record_stop_refuses_the_removed_kinds(tmp_path, kind):
    op = make(tmp_path, asks("record_stop", debtor_id="ACC-1001", kind=kind))
    result = op.chat("record it").tool_calls[0]["result"]
    assert result == "error: kind must be one of cease, dispute"


@pytest.mark.db
@pytest.mark.parametrize("kind", ["cease", "dispute"])
def test_record_stop_still_accepts_cease_and_dispute(tmp_path, conn, kind):
    who = add_debtor(conn, "Stopkind")
    op = make(tmp_path, asks("record_stop", debtor_id=who["account"], kind=kind), conn, clock=DAY)
    result = op.chat("record it").tool_calls[0]["result"]
    assert not str(result).startswith("error:"), result
    assert json.loads(result)["kind"] == kind


# --- the real local database, rolled back ----------------------------------------


@pytest.fixture
def conn():
    from harness.db import connect

    c = connect()
    c.execute("select 1")  # one transaction for the whole test, rolled back at the end
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def add_debtor(conn, last: str, *, balance_cents=50000) -> dict:
    """A made-up debtor in Los Angeles with a consented cell phone and one debt."""
    tag = secrets.token_hex(4)
    phone = "+1213555" + str(int(tag, 16) % 10_000).zfill(4)
    (person,) = conn.execute(
        "insert into public.debtor (first_name, last_name, dob, address, zip, state) values "
        "('Testread', %s, '1980-01-01', '1 Made Up Way', '90012', 'CA') returning id::text",
        (last,),
    ).fetchone()
    conn.execute(
        "insert into public.phone (debtor_id, e164, line_type, consent_source, consent_at) "
        "values (%s, %s, 'cell', 'made-up application', '2026-01-01 12:00:00+00')",
        (person, phone),
    )
    account = f"TST-{tag}"
    (debt,) = conn.execute(
        "insert into public.debt (debtor_id, account_number, creditor, balance_cents, opened_on)"
        " values (%s, %s, 'Made Up Store', %s, '2026-01-01') returning id::text",
        (person, account, balance_cents),
    ).fetchone()
    return {"account": account, "debt_id": debt, "person": person, "phone": phone}


def add_flag(conn, who: dict, kind: str) -> None:
    conn.execute(
        "insert into public.flags (debtor_id, kind, source) values (%s, %s, 'test')",
        (who["person"], kind),
    )


def count(conn, sql: str, *params) -> int:
    return conn.execute(sql, params).fetchone()[0]


def dialer_allows(conn, account: str, when: datetime) -> bool:
    from dialer.service import plan_queue

    (outcome,) = plan_queue([account], when, conn).outcomes
    return not outcome.blocked


@pytest.mark.db
def test_next_allowed_call_agrees_with_the_dialer_for_a_blocked_debtor(tmp_path, conn):
    who = add_debtor(conn, "Nightowl")
    op = make(tmp_path, asks("next_allowed_call", debtor_id=who["account"]), conn)

    body = json.loads(op.chat("when can we call?").tool_calls[0]["result"])

    assert body["allowed_now"] is False
    at = datetime.fromisoformat(body["next_allowed"])
    assert at.astimezone(LA).strftime("%H:%M") == "08:00"
    # The time to say comes from code, already in the operator's zone.
    assert body["next_allowed_local"] == "2026-11-17 08:00 America/Los_Angeles"
    assert not dialer_allows(conn, who["account"], NIGHT)
    assert not dialer_allows(conn, who["account"], at - timedelta(minutes=15))
    assert dialer_allows(conn, who["account"], at)


@pytest.mark.db
def test_next_allowed_call_gives_no_time_for_a_stop_waiting_cant_clear(tmp_path, conn):
    who = add_debtor(conn, "Ceaseless")
    add_flag(conn, who, "cease")
    op = make(tmp_path, asks("next_allowed_call", debtor_id=who["account"]), conn)

    body = json.loads(op.chat("when can we call?").tool_calls[0]["result"])

    assert body["next_allowed"] is None
    assert "cease" in {b["rule_id"] for b in body["blocked_by"]}
    for hours in (0, 7, 24, 24 * 13):
        assert not dialer_allows(conn, who["account"], DAY + timedelta(hours=hours))


@pytest.mark.db
def test_seven_calls_in_seven_days_wait_for_the_oldest_to_age_out(tmp_path, conn):
    who = add_debtor(conn, "Busyline")
    for i in range(7):  # every 12 hours back to 6.5 days ago
        conn.execute(
            "insert into public.call_attempt (debt_id, placed_at, outcome) values (%s, %s, %s)",
            (who["debt_id"], DAY - timedelta(hours=12 + 24 * i), "no_answer"),
        )
    script = [
        tool_reply(
            tool_call("call_history", "c1", debtor_id=who["account"]),
            tool_call("next_allowed_call", "c2", debtor_id=who["account"]),
        ),
        "Done.",
    ]
    op = make(tmp_path, script, conn, clock=DAY)
    history, nxt = (json.loads(c["result"]) for c in op.chat("history?").tool_calls)

    assert history["calls_last_7_days"] == 7 and history["seven_in_seven"]["allow"] is False
    at = datetime.fromisoformat(nxt["next_allowed"])
    assert at > DAY and dialer_allows(conn, who["account"], at)
    assert not dialer_allows(conn, who["account"], at - timedelta(minutes=15))


@pytest.mark.db
def test_add_note_and_schedule_callback_without_approval_write_nothing(tmp_path, conn):
    """The negative case: both open approvals; nothing is written until one is approved."""
    who = add_debtor(conn, "Notewell")
    script = [
        tool_reply(
            tool_call("add_note", "c1", debtor_id=who["account"], text="Asked for a letter."),
            tool_call("schedule_callback", "c2", debtor_id=who["account"],
                      when="2026-11-18 10:30"),
        ),
        "Both wait for you.",
    ]  # fmt: skip
    op = make(tmp_path, script, conn)
    note, callback = op.chat("note it and call back tomorrow").pending_approvals

    notes_sql = "select count(*) from public.debt_note where debt_id = %s"
    callbacks_sql = "select count(*) from public.callback where debt_id = %s"
    audits_sql = "select count(*) from public.audit_event where debt_id = %s"
    assert (note.action, note.details["text"]) == ("add_note", "Asked for a letter.")
    assert callback.details["shown"] == "2026-11-18 10:30 America/Los_Angeles"
    for sql in (notes_sql, callbacks_sql, audits_sql):
        assert count(conn, sql, who["debt_id"]) == 0

    assert "Not saved" in op.decide(note.id, approve=False).reply
    assert count(conn, notes_sql, who["debt_id"]) == 0

    assert "Saved" in op.decide(callback.id, approve=True).reply
    assert count(conn, callbacks_sql, who["debt_id"]) == 1
    assert count(conn, audits_sql, who["debt_id"]) == 1
    (at,) = conn.execute(
        "select scheduled_at from public.callback where debt_id = %s", (who["debt_id"],)
    ).fetchone()
    assert at == datetime(2026, 11, 18, 10, 30, tzinfo=LA)


@pytest.mark.db
def test_an_approved_note_is_written_once_and_kept(tmp_path, conn):
    import psycopg

    who = add_debtor(conn, "Keeper")
    op = make(tmp_path, asks("add_note", debtor_id=who["account"], text="Prefers email."), conn)
    (note,) = op.chat("note it").pending_approvals
    assert "Saved" in op.decide(note.id, approve=True).reply
    with pytest.raises(LookupError):
        op.decide(note.id, approve=True)
    assert count(conn, "select count(*) from public.debt_note where debt_id = %s",
                 who["debt_id"]) == 1  # fmt: skip
    for sql in ("update public.debt_note set body = 'x' where debt_id = %s",
                "delete from public.debt_note where debt_id = %s"):  # fmt: skip
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute(sql, (who["debt_id"],))


@pytest.mark.db
def test_a_callback_outside_calling_hours_or_after_a_cease_is_refused(tmp_path, conn):
    late = add_debtor(conn, "Latenight")
    ceased = add_debtor(conn, "Stopped")
    add_flag(conn, ceased, "cease")
    script = [
        tool_reply(
            tool_call("schedule_callback", "c1", debtor_id=late["account"],
                      when="2026-11-18 22:30"),
            tool_call("schedule_callback", "c2", debtor_id=ceased["account"],
                      when="2026-11-18 10:30"),
        ),
        "Neither can be booked.",
    ]  # fmt: skip
    op = make(tmp_path, script, conn)
    turn = op.chat("call them back")
    first, second = (c["result"] for c in turn.tool_calls)
    assert "calling_hours" in first and "cease" in second
    assert turn.pending_approvals == []


@pytest.mark.db
def test_a_list_call_with_a_huge_limit_is_capped(tmp_path, conn):
    for i in range(reads.MAX_LIST + 3):
        add_debtor(conn, f"Many{i}")
    op = make(
        tmp_path,
        asks("list_debtors", sort="balance", filter="state:CA", limit="1000000"),
        conn,
        clock=DAY,
    )
    body = json.loads(op.chat("list everyone").tool_calls[0]["result"])
    assert body["limit"] == reads.MAX_LIST
    assert len(body["debtors"]) == reads.MAX_LIST
    assert body["truncated"] is True and body["count"] > reads.MAX_LIST
    assert all(d["phone"].startswith("***-***-") for d in body["debtors"])


@pytest.mark.db
def test_ready_and_blocked_lists_come_from_the_dialers_check(tmp_path, conn):
    ok = add_debtor(conn, "Readyone", balance_cents=999_999_00)
    stop = add_debtor(conn, "Blockone", balance_cents=999_998_00)
    add_flag(conn, stop, "cease")
    script = [
        tool_reply(
            tool_call("list_debtors", "c1", sort="balance", filter="ready", limit="3"),
            tool_call("list_debtors", "c2", sort="balance", filter="blocked", limit="50"),
        ),
        "Done.",
    ]
    op = make(tmp_path, script, conn, clock=DAY)
    ready, blocked = (json.loads(c["result"]) for c in op.chat("who is ready?").tool_calls)
    assert ready["debtors"][0]["debtor_id"] == ok["account"]
    assert stop["account"] not in {d["debtor_id"] for d in ready["debtors"]}
    assert stop["account"] in {d["debtor_id"] for d in blocked["debtors"]}
    assert dialer_allows(conn, ok["account"], DAY)
    assert not dialer_allows(conn, stop["account"], DAY)


@pytest.mark.db
def test_record_stop_writes_at_once_and_every_tool_leaves_it_in_place(tmp_path, conn):
    """No tool, approved or not, takes a stop flag away."""
    who = add_debtor(conn, "Holdfast")
    a = who["account"]
    script = [
        tool_reply(tool_call("record_stop", "s1", debtor_id=a, kind="cease")),
        tool_reply(
            tool_call("account_summary", "r1", debtor_id=a),
            tool_call("call_history", "r2", debtor_id=a),
            tool_call("next_allowed_call", "r3", debtor_id=a),
            tool_call("portfolio_summary", "r4", period="all"),
            tool_call("audit_for", "r5", debtor_id=a, limit="10"),
            tool_call("list_debtors", "r6", sort="status", filter="blocked", limit="5"),
            tool_call("add_note", "r7", debtor_id=a, text="Said to stop. Lift it later?"),
            tool_call("record_stop", "r8", debtor_id=a, kind="dispute"),
            tool_call("check_call", "r9", debtor_id=a),
            tool_call("place_call", "r10", debtor_id=a),
        ),
        "Done.",
    ]
    op = make(tmp_path, script, conn, clock=DAY)
    turn = op.chat("they said stop; then show me everything")

    results = {c["name"]: c["result"] for c in turn.tool_calls}
    assert not [r for r in results.values() if str(r).startswith("error:")], results
    first = json.loads(turn.tool_calls[0]["result"])
    assert first["kind"] == "cease"
    # Approve everything that writes; a call is left to the dialer's check below
    # (dial_one needs a connection of its own, outside this test's transaction).
    actions = {a.action for a in turn.pending_approvals}
    assert actions == {"add_note", "place_call"}
    for approval in turn.pending_approvals:
        if approval.action != "place_call":
            op.decide(approval.id, approve=True)

    flags = conn.execute(
        "select kind from public.flags where debtor_id = %s", (who["person"],)
    ).fetchall()
    assert ("cease",) in flags
    assert count(conn, "select count(*) from public.audit_event where debt_id = %s and "
                 "kind = 'stop'", who["debt_id"]) == 2  # fmt: skip
    status = conn.execute("select status from public.debt where id = %s", (who["debt_id"],))
    assert status.fetchone() == ("disputed",)
    assert not dialer_allows(conn, a, DAY)


@pytest.mark.db
def test_audit_and_summary_mask_phone_numbers(tmp_path, conn):
    from compliance import Decision
    from compliance.audit import record

    who = add_debtor(conn, "Maskme")
    record(
        conn,
        Decision(True, "test", f"dialed {who['phone']}", {"phone_e164": who["phone"]}),
        debt_id=who["debt_id"],
    )
    script = [
        tool_reply(
            tool_call("audit_for", "c1", debtor_id=who["account"], limit="999"),
            tool_call("account_summary", "c2", debtor_id=who["account"]),
        ),
        "Done.",
    ]
    op = make(tmp_path, script, conn, clock=DAY)
    results = [c["result"] for c in op.chat("show me").tool_calls]
    for text in results:
        assert who["phone"] not in text and who["phone"][-4:] in text
    audit = json.loads(results[0])
    assert len(audit["events"]) <= reads.MAX_AUDIT
    summary = json.loads(results[1])
    assert summary["balance"] == "$500.00" and summary["may_call_now"] is True


@pytest.mark.db
def test_record_stop_refuses_a_name_and_writes_nothing(tmp_path, conn):
    """A stop has no approval card, so a name that matches one debtor is not enough."""
    tag = secrets.token_hex(3)
    who = add_debtor(conn, f"Onlyone{tag}")
    script = [
        tool_reply(
            tool_call("record_stop", "c1", debtor_id=f"Onlyone{tag}", kind="cease"),
            tool_call("record_stop", "c2", debtor_id=f"Testread Onlyone{tag}", kind="cease"),
        ),
        "I need the id.",
    ]
    op = make(tmp_path, script, conn)
    results = [c["result"] for c in op.chat("they asked us to stop").tool_calls]
    assert all(r.startswith("error:") and "find_debtor" in r for r in results)
    flags = "select count(*) from public.flags where debtor_id = %s"
    assert count(conn, flags, who["person"]) == 0
    audits = "select count(*) from public.audit_event where debt_id = %s"
    assert count(conn, audits, who["debt_id"]) == 0
