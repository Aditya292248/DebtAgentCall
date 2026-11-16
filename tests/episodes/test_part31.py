"""Payments: the operator records what a debtor paid, only after approval.

What it checks:
  - Parsing: amounts, days ("yesterday" in the operator's time zone), ranges,
    and the on-time rules (``classify_plan``), with no database.
  - ``record_payment`` from the model never writes: it opens an approval that
    shows the exact debtor, amount and date. Zero, negative, unparseable or
    future amounts and days open nothing and never touch the database.
  - Against the real local database (``db``), on made-up debtors created
    inside a transaction that is rolled back at the end, so nothing is left:
      - without approval, and on a denial, no payment row and no audit row;
        approving writes one of each and lowers the balance;
      - an amount above the balance, a day before the debt opened, or an
        unknown or ambiguous debtor is refused; a second approval that would
        overpay after the first is refused when approved, and audited;
      - ``update`` and ``delete`` on ``payment`` raise, and so does an insert
        above the balance, straight to the table;
      - a reversal restores the balance, keeps the payment, and can't repeat;
      - the balance a live call reads (``find_debtor_by_phone``, so the call
        tools and the sentence filter) reflects payments, and the old amount
        is blocked by the filter;
      - ``payment_history`` answers "how much in the last 3 days";
      - ``on_time_report`` classifies a plan and lists debts without one.
"""

from __future__ import annotations

import json
import secrets
from contextlib import nullcontext
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest

from harness import payments as pay
from harness.memory import JsonlSession
from harness.operator import Operator, pending_approvals
from model.fake import fake, tool_call, tool_reply
from model.provider import LLMResponse
from phone.client import FakePhoneClient

# 22:00 on Nov 15 in New York, already Nov 16 in UTC.
NOW = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
NY = ZoneInfo("America/New_York")


def no_database():
    raise AssertionError("this path must not touch the database")


def make(tmp_path: Path, script: list, *, connect=no_database, tz=NY) -> Operator:
    return Operator(
        JsonlSession.for_call(tmp_path / "chats", "chat-pay"),
        provider=fake(scripted=script),
        connect=connect,
        phone=FakePhoneClient(),
        clock=lambda: NOW,
        lookup=lambda _account: None,
        tz=tz,
    )


def asks_to_record(debtor_id: str, amount="20", paid_on="yesterday", method="cash") -> list:
    call = tool_call(
        "record_payment", debtor_id=debtor_id, amount=amount, paid_on=paid_on, method=method
    )
    return [tool_reply(call), "Waiting for your approval."]


# --- offline: parsing and the on-time rules ---------------------------------------


@pytest.mark.parametrize(
    ("text", "cents"),
    [("20", 2000), ("$20.5", 2050), ("1,250.00", 125000), ("20 dollars", 2000), ("0.01", 1)],
)
def test_amounts_parse_to_cents(text, cents):
    assert pay.parse_amount(text) == cents


@pytest.mark.parametrize("text", ["0", "0.00", "-5", "twenty", "20.505", "1e3", "", "$"])
def test_bad_amounts_are_refused(text):
    with pytest.raises(pay.PaymentError):
        pay.parse_amount(text)


def test_days_are_counted_from_the_operators_today():
    today = date(2026, 11, 15)
    assert pay.parse_day("yesterday", today) == date(2026, 11, 14)
    assert pay.parse_day("Today", today) == today
    assert pay.parse_day("3 days ago", today) == date(2026, 11, 12)
    assert pay.parse_day("2026-11-01", today) == date(2026, 11, 1)
    for bad in ("last tuesday", "2026-02-30", "soon"):
        with pytest.raises(pay.PaymentError):
            pay.parse_day(bad, today)
    assert pay.parse_since("last 3 days", today) == date(2026, 11, 13)
    assert pay.parse_since("all", today) is None


def test_on_time_late_and_missed():
    start = date(2026, 9, 1)
    schedule = pay.plan_schedule(start, 1000, 3, "monthly", 3000, 3001)
    assert [(d.due_on, d.amount_cents) for d in schedule] == [
        (date(2026, 9, 1), 1000),
        (date(2026, 10, 1), 3000),
        (date(2026, 11, 1), 3000),
        (date(2026, 12, 1), 3001),
    ]
    today = date(2026, 11, 15)
    on_time = [(date(2026, 9, 1), 1000), (date(2026, 9, 30), 3000), (date(2026, 11, 1), 3000)]
    assert pay.classify_plan(schedule, on_time, today)[0] == "on_time"
    late = [(date(2026, 9, 1), 1000), (date(2026, 10, 5), 3000), (date(2026, 11, 1), 3000)]
    standing, items = pay.classify_plan(schedule, late, today)
    assert standing == "late"
    assert [i["status"] for i in items] == ["on_time", "late", "on_time", "upcoming"]
    missed = [(date(2026, 9, 1), 1000), (date(2026, 10, 1), 3000)]
    assert pay.classify_plan(schedule, missed, today)[0] == "missed"
    assert pay.classify_plan(schedule, [], date(2026, 8, 1))[0] == "not_due_yet"


def test_month_ends_are_clamped():
    schedule = pay.plan_schedule(date(2026, 1, 31), 0, 2, "monthly", 100, 100)
    assert [d.due_on for d in schedule] == [date(2026, 2, 28), date(2026, 3, 31)]


# --- offline: asking opens nothing that writes -----------------------------------


@pytest.mark.parametrize(
    ("amount", "paid_on"),
    [
        ("0", "today"),
        ("-20", "today"),
        ("abc", "today"),
        ("20", "tomorrow"),
        ("20", "2026-11-16"),
    ],  # Nov 16 is tomorrow in New York at NOW
)
def test_bad_payments_open_no_approval_and_never_read_the_database(tmp_path, amount, paid_on):
    op = make(tmp_path, asks_to_record("ACC-1001", amount=amount, paid_on=paid_on))

    turn = op.chat("Sam paid")

    assert turn.pending_approvals == []
    assert turn.tool_calls[0]["result"].startswith("error:")
    assert "nothing was recorded" in turn.tool_calls[0]["result"]


def test_unparseable_record_payment_arguments_open_nothing(tmp_path):
    bad = LLMResponse(
        content="",
        tool_calls=[{"id": "c1", "function": {"name": "record_payment", "arguments": "{x"}}],
        finish_reason="tool_calls",
    )
    op = make(tmp_path, [bad, "That didn't work."])
    turn = op.chat("Sam paid 20")
    assert turn.pending_approvals == []
    assert turn.tool_calls[0]["result"].startswith("error:")


def test_old_call_approvals_still_read_as_calls():
    body = {
        "status": "needs_approval",
        "approval_id": "apr_0123456789abcdef",
        "debtor_id": "ACC-1001",
        "name": "Sam Doe",
        "phone": "***-***-0101",
    }
    (a,) = pending_approvals([{"role": "tool", "tool_call_id": "c1", "content": json.dumps(body)}])
    assert (a.action, a.details) == ("place_call", {})


def test_an_unknown_action_opens_no_approval():
    body = {
        "status": "needs_approval",
        "action": "delete_debt",
        "approval_id": "apr_0123456789abcdef",
        "debtor_id": "ACC-1001",
        "name": "x",
    }
    msgs = [{"role": "tool", "tool_call_id": "c1", "content": json.dumps(body)}]
    assert pending_approvals(msgs) == []


# --- the real local database, rolled back ----------------------------------------


@pytest.fixture
def conn():
    from harness.db import connect

    c = connect()
    # Everything below runs in this one transaction, rolled back at the end, so the
    # made-up debtors and their append-only payments are never committed.
    c.execute("select 1")
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def add_debtor(conn, last: str, balance_cents=10000, opened_on="2026-10-01") -> dict:
    tag = secrets.token_hex(4)
    phone = "+1555" + str(int(tag, 16) % 10_000_000).zfill(7)
    (person,) = conn.execute(
        "insert into public.debtor (first_name, last_name, dob, address, zip, state) "
        "values ('Testpay', %s, '1980-01-01', '1 Made Up Way', '00000', 'CA') returning id",
        (last,),
    ).fetchone()
    conn.execute(
        "insert into public.phone (debtor_id, e164, line_type) values (%s, %s, 'cell')",
        (person, phone),
    )
    account = f"TST-{tag}"
    (debt,) = conn.execute(
        "insert into public.debt (debtor_id, account_number, creditor, balance_cents, opened_on)"
        " values (%s, %s, 'Made Up Store', %s, %s) returning id::text",
        (person, account, balance_cents, opened_on),
    ).fetchone()
    return {"account": account, "debt_id": debt, "phone": phone, "name": f"Testpay {last}"}


def db_operator(tmp_path, conn, script, tz=NY) -> Operator:
    return make(tmp_path, script, connect=lambda: nullcontext(conn), tz=tz)


def payments_for(conn, debt_id) -> list[tuple]:
    return conn.execute(
        "select amount_cents, paid_on, method, reverses_payment_id::text from public.payment "
        "where debt_id = %s order by reverses_payment_id is not null, paid_on, amount_cents",
        (debt_id,),
    ).fetchall()


def audits_for(conn, debt_id) -> list[tuple]:
    return conn.execute(
        "select kind, rule_id, decision from public.audit_event where debt_id = %s order by seq",
        (debt_id,),
    ).fetchall()


def balance(conn, debt_id) -> int:
    return conn.execute(
        "select public.current_balance_cents(d) from public.debt d where id = %s", (debt_id,)
    ).fetchone()[0]


@pytest.mark.db
def test_record_payment_without_approval_writes_nothing(tmp_path, conn):
    """The negative case: the model asks, nobody approves, nothing is written."""
    who = add_debtor(conn, "Ledgerly")
    op = db_operator(tmp_path, conn, asks_to_record(who["account"]))

    turn = op.chat("Testpay Ledgerly paid 20 yesterday")

    (pending,) = turn.pending_approvals
    assert pending.action == "record_payment"
    assert pending.name == who["name"]
    # The card shows exactly what will be written: New York's yesterday, not UTC's.
    assert pending.details["amount"] == "$20.00"
    assert pending.details["paid_on"] == "2026-11-14"
    assert payments_for(conn, who["debt_id"]) == []
    assert audits_for(conn, who["debt_id"]) == []

    denied = op.decide(pending.id, approve=False)
    assert denied.reply.startswith(f"[approval {pending.id}] Not recorded")
    assert payments_for(conn, who["debt_id"]) == []
    assert audits_for(conn, who["debt_id"]) == []
    assert balance(conn, who["debt_id"]) == 10000


@pytest.mark.db
def test_approving_writes_the_payment_and_its_audit_event_once(tmp_path, conn):
    who = add_debtor(conn, "Payson")
    op = db_operator(tmp_path, conn, asks_to_record(who["account"], method="card"))
    (pending,) = op.chat("paid 20 yesterday").pending_approvals

    done = op.decide(pending.id, approve=True)

    assert "Balance now $80.00" in done.reply
    assert payments_for(conn, who["debt_id"]) == [(2000, date(2026, 11, 14), "card", None)]
    assert audits_for(conn, who["debt_id"]) == [("payment", "record_payment", "allow")]
    assert balance(conn, who["debt_id"]) == 8000
    with pytest.raises(LookupError):
        op.decide(pending.id, approve=True)
    assert len(payments_for(conn, who["debt_id"])) == 1


@pytest.mark.db
def test_the_day_follows_the_operators_time_zone(tmp_path, conn):
    who = add_debtor(conn, "Zoneman")
    op = db_operator(tmp_path, conn, asks_to_record(who["account"]), tz=ZoneInfo("UTC"))
    (pending,) = op.chat("paid 20 yesterday").pending_approvals
    assert pending.details["paid_on"] == "2026-11-15"


@pytest.mark.db
@pytest.mark.parametrize(
    ("amount", "paid_on", "why"),
    [("100.01", "yesterday", "more than the balance"), ("20", "2026-09-30", "before the debt")],
)
def test_over_the_balance_or_before_the_debt_is_refused(tmp_path, conn, amount, paid_on, why):
    who = add_debtor(conn, "Overby")
    op = db_operator(tmp_path, conn, asks_to_record(who["account"], amount, paid_on))
    turn = op.chat("record it")
    assert turn.pending_approvals == []
    assert why in turn.tool_calls[0]["result"]
    assert payments_for(conn, who["debt_id"]) == []


@pytest.mark.db
def test_unknown_or_ambiguous_debtors_are_refused(tmp_path, conn):
    tag = secrets.token_hex(3)
    add_debtor(conn, f"Twin{tag}")
    add_debtor(conn, f"Twin{tag}")
    args = {"amount": "20", "paid_on": "today", "method": "cash"}
    script = [
        tool_reply(
            tool_call("record_payment", "c1", debtor_id=f"Twin{tag}", **args),
            tool_call("record_payment", "c2", debtor_id="TST-nobody", **args),
        ),
        "Which one?",
    ]
    op = db_operator(tmp_path, conn, script)
    turn = op.chat("Twin paid 20")
    results = [c["result"] for c in turn.tool_calls]
    assert "more than one debt" in results[0]
    assert "no debtor matches" in results[1]
    assert turn.pending_approvals == []


@pytest.mark.db
def test_a_second_approval_that_would_overpay_is_refused_and_audited(tmp_path, conn):
    who = add_debtor(conn, "Twicer")
    script = [
        tool_reply(
            tool_call(
                "record_payment",
                "c1",
                debtor_id=who["account"],
                amount="60",
                paid_on="today",
                method="cash",
            ),
            tool_call(
                "record_payment",
                "c2",
                debtor_id=who["account"],
                amount="60",
                paid_on="today",
                method="cash",
            ),
        ),
        "Two waiting.",
    ]
    op = db_operator(tmp_path, conn, script)
    first, second = op.chat("record 60 twice").pending_approvals

    assert "Recorded" in op.decide(first.id, approve=True).reply
    refused = op.decide(second.id, approve=True)

    assert "Not recorded" in refused.reply and "more than the balance" in refused.reply
    assert [p[0] for p in payments_for(conn, who["debt_id"])] == [6000]
    assert audits_for(conn, who["debt_id"]) == [
        ("payment", "record_payment", "allow"),
        ("payment", "record_payment", "block"),
    ]


@pytest.mark.db
def test_payments_cannot_be_updated_deleted_or_overpaid_in_the_table(conn):
    who = add_debtor(conn, "Fixedrow")
    (pid,) = conn.execute(
        "insert into public.payment (debt_id, amount_cents, paid_on, method, recorded_by) "
        "values (%s, 500, '2026-11-01', 'cash', 'test') returning id",
        (who["debt_id"],),
    ).fetchone()
    for sql in (
        "update public.payment set amount_cents = 1 where id = %s",
        "delete from public.payment where id = %s",
    ):
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute(sql, (pid,))
    with pytest.raises(psycopg.Error, match="more than the balance"), conn.transaction():
        conn.execute(
            "insert into public.payment (debt_id, amount_cents, paid_on, method, recorded_by) "
            "values (%s, 9501, '2026-11-01', 'cash', 'test')",
            (who["debt_id"],),
        )
    with pytest.raises(psycopg.Error, match="future"), conn.transaction():
        conn.execute(
            "insert into public.payment (debt_id, amount_cents, paid_on, method, recorded_by) "
            "values (%s, 1, current_date + 5, 'cash', 'test')",
            (who["debt_id"],),
        )
    assert payments_for(conn, who["debt_id"]) == [(500, date(2026, 11, 1), "cash", None)]


@pytest.mark.db
def test_a_reversal_restores_the_balance_and_keeps_the_payment(tmp_path, conn):
    who = add_debtor(conn, "Bounced")
    op = db_operator(tmp_path, conn, asks_to_record(who["account"], amount="30"))
    (pending,) = op.chat("paid 30").pending_approvals
    op.decide(pending.id, approve=True)
    (payment_id,) = conn.execute(
        "select id::text from public.payment where debt_id = %s", (who["debt_id"],)
    ).fetchone()
    assert balance(conn, who["debt_id"]) == 7000

    rev = db_operator(
        tmp_path,
        conn,
        [
            tool_reply(tool_call("reverse_payment", payment_id=payment_id, reason="check bounced")),
            "Waiting.",
        ],
    )
    (undo,) = rev.chat("that check bounced").pending_approvals
    assert undo.action == "reverse_payment" and undo.details["amount"] == "$30.00"
    assert balance(conn, who["debt_id"]) == 7000  # nothing until approved
    assert "Balance now $100.00" in rev.decide(undo.id, approve=True).reply

    assert balance(conn, who["debt_id"]) == 10000
    rows = payments_for(conn, who["debt_id"])
    assert [(r[0], r[3]) for r in rows] == [(3000, None), (3000, payment_id)]
    again = db_operator(
        tmp_path,
        conn,
        [tool_reply(tool_call("reverse_payment", payment_id=payment_id, reason="again")), "No."],
    )
    turn = again.chat("reverse it again")
    assert "already reversed" in turn.tool_calls[0]["result"]


@pytest.mark.db
def test_the_balance_a_live_call_speaks_reflects_payments(conn):
    from harness.call_state import CallState
    from harness.db import find_debtor_by_phone
    from phone.webhook import filter_reply, speech_state

    who = add_debtor(conn, "Spoken")
    pay.insert_payment(
        conn, pay.account_by_number(conn, who["account"]), 2500, date(2026, 11, 2), "ach", "test"
    )

    debtor = find_debtor_by_phone(who["phone"], conn)
    assert debtor is not None and debtor.balance_cents == 7500
    state = CallState.new("call-test", debtor)
    from dataclasses import replace

    speech = speech_state(replace(state, verified=True, disclosed=True), debtor)
    said, blocked = filter_reply("Your balance with Made Up Store is $75.00.", speech)
    assert "$75.00" in said and not blocked
    _, stale = filter_reply("Your balance with Made Up Store is $100.00.", speech)
    assert [d.rule_id for d in stale] == ["amount_mismatch"]


@pytest.mark.db
def test_payment_history_answers_the_last_three_days(tmp_path, conn):
    who = add_debtor(conn, "Historia")
    account = pay.account_by_number(conn, who["account"])
    for day, cents in (("2026-11-15", 1000), ("2026-11-13", 500), ("2026-11-12", 700)):
        pay.insert_payment(conn, account, cents, date.fromisoformat(day), "cash", "test")
    script = [
        tool_reply(tool_call("payment_history", debtor_id=who["account"], since="last 3 days")),
        "35 dollars.",
    ]
    op = db_operator(tmp_path, conn, script)

    body = json.loads(op.chat("how much in the last 3 days?").tool_calls[0]["result"])

    assert (body["from"], body["to"]) == ("2026-11-13", "2026-11-15")
    assert body["total"] == "$15.00" and body["count"] == 2
    assert body["balance_now"] == "$78.00"


@pytest.mark.db
def test_on_time_report_classifies_plans_and_lists_the_rest(tmp_path, conn):
    late = add_debtor(conn, "Laterly")
    none = add_debtor(conn, "Planless")
    conn.execute(
        "insert into public.payment_plan (call_id, debt_id, down_cents, installments, frequency,"
        " installment_cents, last_installment_cents, status, created_at)"
        " values ('test', %s, 1000, 3, 'monthly', 3000, 3000, 'approved',"
        " '2026-10-01 16:00:00+00')",
        (late["debt_id"],),
    )
    account = pay.account_by_number(conn, late["account"])
    pay.insert_payment(conn, account, 1000, date(2026, 10, 1), "cash", "test")
    pay.insert_payment(conn, account, 3000, date(2026, 11, 5), "cash", "test")
    op = db_operator(tmp_path, conn, [tool_reply(tool_call("on_time_report")), "Done."])

    report = json.loads(op.chat("who is paying on time?").tool_calls[0]["result"])

    (row,) = [r for r in report["with_plan"] if r["debtor_id"] == late["account"]]
    assert row["status"] == "late"
    assert (row["on_time"], row["late"], row["missed"]) == (1, 1, 0)
    assert none["account"] in {r["debtor_id"] for r in report["without_plan"]}
