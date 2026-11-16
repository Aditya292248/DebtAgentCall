"""Payments: parsing what the operator typed, the ledger, and the on-time report.

The ledger is ``public.payment`` (``supabase/migrations/20261116120000_payments.sql``):
append-only, a correction is a reversing row. ``debt.balance_cents`` is the
amount the debt was placed with and never changes; the balance owed now is the
SQL function ``public.current_balance_cents(debt)``, the only place it is
computed. Every query here and in ``harness.db`` that shows or speaks a balance
selects that function, never ``balance_cents`` alone.

Pure parts, no database:
  - ``parse_amount("$1,250.50")`` -> 125050 cents. Rejects zero, negatives,
    more than two decimals and anything that isn't a plain amount.
  - ``parse_day("yesterday", today)`` -> a date. Takes ``today``, ``yesterday``,
    ``N days ago`` or ``YYYY-MM-DD``. ``today`` comes from the caller, in the
    operator's own time zone, so "yesterday" means the operator's yesterday.
  - ``parse_since("last 3 days", today)`` -> the first day of a range that ends today.
  - ``plan_schedule`` and ``classify_plan``: the on-time rules, below.

Database parts take an open connection and leave committing to the caller,
like ``compliance.audit.record``:
  - ``resolve_debt(conn, ref)``: an account number, or a name that matches one
    debt only. Unknown or ambiguous is a ``PaymentError``.
  - ``check_payment``: every rule a new payment must pass, against the current
    balance. ``insert_payment`` and ``insert_reversal`` write one row each. The
    database's insert trigger checks the balance, the dates and the reversal
    again, under a lock on the debt, so a stale check can't overpay.
  - ``history`` and ``on_time_report`` only read.

On time, late, missed (``classify_plan``):
  A debt's plan is its newest ``payment_plan`` that is ``approved`` or
  ``confirmed``. Its schedule starts on the day the plan was made (in the
  operator's time zone): the down payment, if any, is due that day, and
  installment ``k`` is due ``k`` weeks, ``2k`` weeks or ``k`` months later for a
  weekly, biweekly or monthly plan. The last installment carries the leftover
  cents. Payments count toward the plan if they are dated on or after its
  start and were not reversed; they fill the schedule in order, oldest due
  first. For each installment due on or before today:
    - on time: the payments dated on or before its due day cover it and every
      earlier one;
    - late: they only cover it with a payment dated after its due day, but on
      or before today;
    - missed: today's payments still don't cover it.
  There is no grace period. Installments due after today are "upcoming". The
  debt as a whole is "missed" if any installment is missed, else "late" if any
  is late, else "on_time" if any was due, else "not_due_yet".
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

METHODS = ("cash", "check", "card", "ach", "other")
MAX_HISTORY_ROWS = 50
MAX_REPORT_DEBTS = 50
_ACCOUNT = re.compile(r"[A-Za-z0-9-]{1,40}")
_AMOUNT = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
_DAYS_AGO = re.compile(r"(\d{1,4}) days? ago")
_LAST_DAYS = re.compile(r"(?:the )?(?:last|past) (\d{1,4}) days?")
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
MAX_AMOUNT_CENTS = 100_000_000_00  # a hundred million dollars: past this it's a typo


class PaymentError(ValueError):
    """Why a payment, or the request for one, can't go ahead. The text is shown as it is."""


def money(cents: int) -> str:
    """``$1,250.00``, the way the chat shows an amount."""
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


# --- parsing ---------------------------------------------------------------------


def parse_amount(text: str) -> int:
    """Cents in a plain positive amount: ``20``, ``$20.5``, ``1,250.00``."""
    words = " ".join(str(text).lower().split())
    for unit in (" dollars", " dollar", " usd"):
        words = words.removesuffix(unit)
    match = _AMOUNT.fullmatch(words.strip())
    if not match:
        raise PaymentError(f"{text!r} is not an amount; give dollars like 20 or 20.50")
    whole, frac = match.group(1).replace(",", ""), match.group(2) or ""
    cents = int(whole) * 100 + int(frac.ljust(2, "0") or 0)
    if cents <= 0:
        raise PaymentError("the amount must be more than zero")
    if cents > MAX_AMOUNT_CENTS:
        raise PaymentError(f"{money(cents)} is too large to be a payment")
    return cents


def parse_day(text: str, today: date) -> date:
    """The day ``text`` names, counted from ``today`` (the operator's today)."""
    words = " ".join(str(text).lower().split())
    if words == "today":
        return today
    if words == "yesterday":
        return today - timedelta(days=1)
    if match := _DAYS_AGO.fullmatch(words):
        return today - timedelta(days=int(match.group(1)))
    if _ISO_DAY.fullmatch(words):
        try:
            return date.fromisoformat(words)
        except ValueError:
            pass
    raise PaymentError(f"{text!r} is not a day; give today, yesterday, N days ago or YYYY-MM-DD")


def parse_since(text: str, today: date) -> date | None:
    """The first day of a range ending today, or None for all time.

    ``last 3 days`` is today and the two days before it. Anything ``parse_day``
    takes is the first day itself; ``all`` or empty means no start.
    """
    words = " ".join(str(text).lower().split())
    if words in ("", "all", "all time", "ever"):
        return None
    if match := _LAST_DAYS.fullmatch(words):
        n = int(match.group(1))
        if n < 1:
            raise PaymentError("give at least 1 day")
        return today - timedelta(days=n - 1)
    if words in ("this week", "last week", "past week"):
        return today - timedelta(days=6)
    if words in ("this month", "last month", "past month"):
        return today - timedelta(days=29)
    return parse_day(words, today)


def parse_method(text: str) -> str:
    words = " ".join(str(text).lower().split())
    aliases = {
        "credit card": "card",
        "debit card": "card",
        "debit": "card",
        "bank": "ach",
        "bank transfer": "ach",
        "transfer": "ach",
        "cheque": "check",
        "": "other",
    }
    method = aliases.get(words, words)
    if method not in METHODS:
        raise PaymentError(f"{text!r} is not a method; use one of {', '.join(METHODS)}")
    return method


def local_today(now: datetime, tz: ZoneInfo) -> date:
    return now.astimezone(tz).date()


# --- the database ----------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    debt_id: str
    account_number: str
    name: str
    opened_on: date
    original_cents: int
    balance_cents: int  # current: original minus payments plus reversals
    status: str


ACCOUNT_SELECT = """
    select d.id::text, d.account_number, p.first_name || ' ' || p.last_name,
           d.opened_on, d.balance_cents, public.current_balance_cents(d), d.status
    from public.debt d
    join public.debtor p on p.id = d.debtor_id
"""


def _account(row: tuple) -> Account:
    return Account(*row)


def account_by_number(
    conn: psycopg.Connection, account_number: str, *, lock: bool = False
) -> Account | None:
    sql = ACCOUNT_SELECT + " where d.account_number = %s"
    if lock:
        sql += " for update of d"
    row = conn.execute(sql, (account_number,)).fetchone()
    return _account(row) if row else None


def resolve_debt(conn: psycopg.Connection, ref: str) -> Account:
    """The one debt ``ref`` names: an account number, or a name with a single match."""
    ref = " ".join(str(ref).split())
    if not ref:
        raise PaymentError("give the debtor's id from find_debtor")
    if _ACCOUNT.fullmatch(ref) and (found := account_by_number(conn, ref)):
        return found
    rows = conn.execute(
        ACCOUNT_SELECT
        + " where lower(p.first_name || ' ' || p.last_name) like %s order by d.account_number"
        " limit 6",
        (f"%{ref.lower()}%",),
    ).fetchall()
    if not rows:
        raise PaymentError(f"no debtor matches {ref!r}")
    if len(rows) > 1:
        ids = ", ".join(f"{r[1]} ({r[2]})" for r in rows[:5])
        raise PaymentError(f"{ref!r} matches more than one debt: {ids}; ask which one")
    return _account(rows[0])


def check_payment(account: Account, amount_cents: int, paid_on: date, today: date) -> None:
    """Every rule a new payment must pass. Raises ``PaymentError`` with the first that fails."""
    if amount_cents <= 0:
        raise PaymentError("the amount must be more than zero")
    if amount_cents > account.balance_cents:
        raise PaymentError(
            f"{money(amount_cents)} is more than the balance of {money(account.balance_cents)}"
        )
    if paid_on > today:
        raise PaymentError(f"{paid_on.isoformat()} is in the future (today is {today.isoformat()})")
    if paid_on < account.opened_on:
        raise PaymentError(
            f"{paid_on.isoformat()} is before the debt was opened on "
            f"{account.opened_on.isoformat()}"
        )


INSERT_PAYMENT_SQL = """
    insert into public.payment (debt_id, amount_cents, paid_on, method, recorded_by,
                                reverses_payment_id, reason)
    values (%s, %s, %s, %s, %s, %s, %s)
    returning id::text
"""


def insert_payment(
    conn: psycopg.Connection,
    account: Account,
    amount_cents: int,
    paid_on: date,
    method: str,
    recorded_by: str,
) -> str:
    """Add one payment row; returns its id. Committing is left to the caller."""
    row = conn.execute(
        INSERT_PAYMENT_SQL,
        (account.debt_id, amount_cents, paid_on, method, recorded_by, None, None),
    ).fetchone()
    assert row is not None
    return row[0]


@dataclass(frozen=True)
class PaymentRow:
    id: str
    debt_id: str
    account_number: str
    name: str
    amount_cents: int
    paid_on: date
    method: str
    recorded_at: datetime
    recorded_by: str
    reverses_payment_id: str | None
    reason: str | None
    reversed_by: str | None  # the reversal's id, if this payment was reversed

    def as_json(self) -> dict[str, Any]:
        return {
            "payment_id": self.id,
            "debtor_id": self.account_number,
            "amount": money(self.amount_cents),
            "amount_cents": self.amount_cents,
            "paid_on": self.paid_on.isoformat(),
            "method": self.method,
            "recorded_by": self.recorded_by,
            "reverses_payment_id": self.reverses_payment_id,
            "reason": self.reason,
            "reversed": self.reversed_by is not None,
        }


PAYMENT_SELECT = """
    select x.id::text, x.debt_id::text, d.account_number, p.first_name || ' ' || p.last_name,
           x.amount_cents, x.paid_on, x.method, x.recorded_at, x.recorded_by,
           x.reverses_payment_id::text, x.reason,
           (select r.id::text from public.payment r where r.reverses_payment_id = x.id)
    from public.payment x
    join public.debt d on d.id = x.debt_id
    join public.debtor p on p.id = d.debtor_id
"""


def payment_by_id(conn: psycopg.Connection, payment_id: str) -> PaymentRow | None:
    row = conn.execute(PAYMENT_SELECT + " where x.id::text = %s", (payment_id,)).fetchone()
    return PaymentRow(*row) if row else None


def check_reversal(payment: PaymentRow | None, reason: str) -> PaymentRow:
    if payment is None:
        raise PaymentError("no such payment; use payment_history to find its id")
    if payment.reverses_payment_id is not None:
        raise PaymentError("that row is itself a reversal; it can't be reversed")
    if payment.reversed_by is not None:
        raise PaymentError(f"payment {payment.id} was already reversed")
    if not reason.strip():
        raise PaymentError("give a reason for the reversal")
    return payment


def insert_reversal(
    conn: psycopg.Connection, payment: PaymentRow, reason: str, recorded_by: str, today: date
) -> str:
    """Add a row that cancels ``payment``, dated ``today``. Nothing is deleted."""
    row = conn.execute(
        INSERT_PAYMENT_SQL,
        (
            payment.debt_id,
            payment.amount_cents,
            max(today, payment.paid_on),
            payment.method,
            recorded_by,
            payment.id,
            reason.strip(),
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def history(
    conn: psycopg.Connection, account: Account, since: date | None, limit: int = MAX_HISTORY_ROWS
) -> tuple[list[PaymentRow], int, int]:
    """Payments (not reversals) dated on or after ``since``, newest first, capped at
    ``limit``; with the total of those not reversed and how many rows there were."""
    where = " where x.debt_id = %s and x.reverses_payment_id is null"
    params: list[Any] = [account.debt_id]
    if since is not None:
        where += " and x.paid_on >= %s"
        params.append(since)
    rows = [
        PaymentRow(*r)
        for r in conn.execute(
            PAYMENT_SELECT + where + " order by x.paid_on desc, x.recorded_at desc", params
        ).fetchall()
    ]
    total = sum(r.amount_cents for r in rows if r.reversed_by is None)
    return rows[:limit], total, len(rows)


# --- the on-time report ----------------------------------------------------------


@dataclass(frozen=True)
class Due:
    number: int  # 0 for the down payment
    due_on: date
    amount_cents: int


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def plan_schedule(
    start: date,
    down_cents: int,
    installments: int,
    frequency: str,
    installment_cents: int,
    last_installment_cents: int,
) -> list[Due]:
    """Every amount the plan asks for and the day it is due (see the module docstring)."""
    out = [Due(0, start, down_cents)] if down_cents > 0 else []
    for k in range(1, installments + 1):
        if frequency == "weekly":
            due = start + timedelta(weeks=k)
        elif frequency == "biweekly":
            due = start + timedelta(weeks=2 * k)
        elif frequency == "monthly":
            due = _add_months(start, k)
        else:
            raise PaymentError(f"unknown plan frequency {frequency!r}")
        amount = last_installment_cents if k == installments else installment_cents
        out.append(Due(k, due, amount))
    return out


def classify_plan(
    schedule: list[Due], payments: list[tuple[date, int]], today: date
) -> tuple[str, list[dict[str, Any]]]:
    """The debt's standing and each installment's, from ``(paid_on, cents)`` payments
    that count toward the plan. See the module docstring for the rules."""
    paid = sorted(payments)
    needed = 0
    rows: list[dict[str, Any]] = []
    for item in schedule:
        needed += item.amount_cents
        entry: dict[str, Any] = {
            "installment": item.number,
            "due_on": item.due_on.isoformat(),
            "amount": money(item.amount_cents),
        }
        if item.due_on > today:
            entry["status"] = "upcoming"
            rows.append(entry)
            continue
        covered_on = None
        running = 0
        for paid_on, cents in paid:
            if paid_on > today:
                break
            running += cents
            if running >= needed:
                covered_on = paid_on
                break
        if covered_on is None:
            entry["status"] = "missed"
        elif covered_on <= item.due_on:
            entry["status"] = "on_time"
        else:
            entry["status"] = "late"
            entry["paid_on"] = covered_on.isoformat()
        rows.append(entry)
    seen = {r["status"] for r in rows}
    for standing in ("missed", "late", "on_time"):
        if standing in seen:
            return standing, rows
    return "not_due_yet", rows


PLANS_SQL = """
    select distinct on (d.id) d.id::text, d.account_number, p.first_name || ' ' || p.last_name,
           pp.created_at, pp.down_cents, pp.installments, pp.frequency, pp.installment_cents,
           pp.last_installment_cents
    from public.debt d
    join public.debtor p on p.id = d.debtor_id
    join public.payment_plan pp on pp.debt_id = d.id and pp.status in ('approved', 'confirmed')
    order by d.id, pp.created_at desc
"""

NO_PLAN_SQL = """
    select d.account_number, p.first_name || ' ' || p.last_name
    from public.debt d
    join public.debtor p on p.id = d.debtor_id
    where not exists (
        select 1 from public.payment_plan pp
        where pp.debt_id = d.id and pp.status in ('approved', 'confirmed'))
    order by d.account_number
"""

COUNTED_PAYMENTS_SQL = """
    select x.paid_on, x.amount_cents
    from public.payment x
    where x.debt_id = %s and x.reverses_payment_id is null and x.paid_on >= %s
      and not exists (select 1 from public.payment r where r.reverses_payment_id = x.id)
"""


def on_time_report(
    conn: psycopg.Connection, today: date, tz: ZoneInfo, limit: int = MAX_REPORT_DEBTS
) -> dict[str, Any]:
    """Every debt with a plan, classified; and the ones without a plan, listed apart."""
    with_plan = []
    for debt_id, account, name, created_at, down, count, freq, each, last in conn.execute(
        PLANS_SQL
    ).fetchall():
        start = created_at.astimezone(tz).date()
        schedule = plan_schedule(start, down, count, freq, each, last)
        payments = conn.execute(COUNTED_PAYMENTS_SQL, (debt_id, start)).fetchall()
        standing, items = classify_plan(schedule, [(d, c) for d, c in payments], today)
        due = [i for i in items if i["status"] != "upcoming"]
        with_plan.append(
            {
                "debtor_id": account,
                "name": name,
                "status": standing,
                "plan_start": start.isoformat(),
                "frequency": freq,
                "due_so_far": len(due),
                "on_time": sum(i["status"] == "on_time" for i in due),
                "late": sum(i["status"] == "late" for i in due),
                "missed": sum(i["status"] == "missed" for i in due),
                "next_due": next((i for i in items if i["status"] == "upcoming"), None),
            }
        )
    order = {"missed": 0, "late": 1, "on_time": 2, "not_due_yet": 3}
    with_plan.sort(key=lambda r: (order[r["status"]], r["debtor_id"]))
    without = [{"debtor_id": a, "name": n} for a, n in conn.execute(NO_PLAN_SQL).fetchall()]
    return {
        "today": today.isoformat(),
        "with_plan": with_plan[:limit],
        "with_plan_total": len(with_plan),
        "without_plan": without[:limit],
        "without_plan_total": len(without),
    }
