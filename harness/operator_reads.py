"""What the operator chat may read about accounts, and the few writes it may ask for.

These are the database halves of the operator's account tools
(``harness/operator.py`` wires them up as tools). Each takes an open connection
and returns plain JSON-ready data; none of them commits. The reads change
nothing. The writes are:

  - ``insert_note`` and ``insert_callback``, which the operator runs only after
    the person approves (``add_note``, ``schedule_callback``);
  - ``record_stop``, which needs no approval because it can only ever mean
    fewer calls: a cease, attorney or bankruptcy flag, or a dispute.

There is deliberately no way here, or in any operator tool, to remove a flag,
record consent, change a balance or delete anything. Notes and payments are
append-only in the database itself.

Every result is capped (``MAX_LIST``, ``MAX_AUDIT``, ``MAX_HISTORY``) so a big
portfolio can't flood the model's context, and phone numbers are masked to
their last four digits, in plain fields and inside audit evidence alike.

``next_allowed_call`` never decides by itself. It steps forward in time
through ``compliance.check_before_call`` with the facts the dialer loads, stops
at the first moment every rule allows, and then confirms that moment with
``dialer.service.plan_queue``, the dialer's own dry run. A block that waiting
can't fix (cease, consent, attorney, a disputed debt...) gives no time at all.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from compliance import Flag, check_before_call, may_call
from harness import payments as pay

MAX_LIST = 25
MAX_SCAN = 500  # debts looked at for one list call; past this the result says so
MAX_AUDIT = 50
MAX_HISTORY = 20
MAX_NOTE_CHARS = 2000
STEP = timedelta(minutes=15)
HORIZON = timedelta(days=14)
# Only these rules can pass later without anyone doing anything.
TIME_RULES = frozenset({"calling_hours", "call_frequency"})
STOP_KINDS = ("cease", "dispute", "attorney", "bankruptcy")
SORTS = ("balance", "last_payment", "status", "name")
_PHONE_TEXT = re.compile(r"\+\d{8,15}")


class ReadError(ValueError):
    """Why a request can't be answered. The text is shown as it is."""


def mask_phone(e164: str | None) -> str | None:
    if not e164:
        return None
    return "***-***-" + e164[-4:]


def mask_in(value: Any) -> Any:
    """``value`` with every phone number in it masked, however deeply it is nested."""
    if isinstance(value, str):
        return _PHONE_TEXT.sub(lambda m: mask_phone(m.group(0)) or "", value)
    if isinstance(value, dict):
        return {k: mask_in(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [mask_in(v) for v in value]
    return value


def parse_limit(text: str, cap: int) -> int:
    """A positive whole number, never above ``cap``; empty means ``cap``."""
    words = str(text).strip()
    if not words:
        return cap
    if not words.isdigit() or int(words) < 1:
        raise ReadError(f"limit must be a positive whole number, got {text!r}")
    return min(int(words), cap)


# --- one debt --------------------------------------------------------------------

ROW_SELECT = """
    select d.id::text, d.account_number, p.first_name || ' ' || p.last_name, p.state,
           d.creditor, d.balance_cents, public.current_balance_cents(d), d.status,
           d.opened_on, ph.e164,
           (select max(x.paid_on) from public.payment x
             where x.debt_id = d.id and x.reverses_payment_id is null
               and not exists (select 1 from public.payment r
                               where r.reverses_payment_id = x.id)) as last_paid_on,
           p.id::text, p.zip
    from public.debt d
    join public.debtor p on p.id = d.debtor_id
    left join lateral (
        select e164 from public.phone where debtor_id = p.id order by e164 limit 1
    ) ph on true
"""


def _row(r: tuple) -> dict[str, Any]:
    (debt_id, account, name, state, creditor, original, current, status, opened_on, e164,
     last_paid, person_id, zip_code) = r  # fmt: skip
    return {
        "debt_id": debt_id,
        "debtor_id": account,
        "name": name,
        "state": state,
        "creditor": creditor,
        "original_cents": original,
        "balance_cents": current,
        "status": status,
        "opened_on": opened_on,
        "e164": e164,
        "last_paid_on": last_paid,
        "person_id": person_id,
        "zip": zip_code,
    }


def debt_row(conn: psycopg.Connection, debtor_id: str) -> dict[str, Any]:
    """The one debt ``debtor_id`` names (an account number, or a name with one match)."""
    account = pay.resolve_debt(conn, debtor_id)
    row = conn.execute(ROW_SELECT + " where d.id = %s", (account.debt_id,)).fetchone()
    if row is None:
        raise ReadError(f"no debtor {debtor_id!r}")
    return _row(row)


def _public(r: dict[str, Any]) -> dict[str, Any]:
    """What the chat may see of a row: phone masked, ids and amounts formatted."""
    return {
        "debtor_id": r["debtor_id"],
        "name": r["name"],
        "state": r["state"],
        "creditor": r["creditor"],
        "balance": pay.money(r["balance_cents"]),
        "status": r["status"],
        "phone": mask_phone(r["e164"]),
        "last_payment": r["last_paid_on"].isoformat() if r["last_paid_on"] else None,
    }


# --- the checks, the same way the dialer runs them ---------------------------------


def precall(conn: psycopg.Connection, account: str, when: datetime) -> tuple[bool, list]:
    """The dialer's own dry run (``plan_queue``) for one debt at ``when``."""
    from dialer.service import plan_queue

    (outcome,) = plan_queue([account], when, conn).outcomes
    return (not outcome.blocked), outcome.decisions


def _blocking(decisions: list) -> list[dict[str, str]]:
    return [{"rule_id": d.rule_id, "reason": mask_in(d.reason)} for d in decisions if not d.allow]


def next_allowed_call(conn: psycopg.Connection, account: str, now: datetime) -> dict[str, Any]:
    """The first moment, from ``now``, that every pre-call check passes for this debt."""
    from dialer.service import STATUS_SQL, check_debt_status
    from harness.db import load_call_facts

    allowed, decisions = precall(conn, account, now)
    if allowed:
        return {"debtor_id": account, "allowed_now": True, "next_allowed": now.isoformat()}
    static = [d for d in decisions if not d.allow and d.rule_id not in TIME_RULES]
    if static:
        return {
            "debtor_id": account,
            "allowed_now": False,
            "next_allowed": None,
            "why": "blocked by a rule that waiting does not clear",
            "blocked_by": _blocking(static),
        }
    facts = load_call_facts(account, conn)
    status_row = conn.execute(STATUS_SQL, (account,)).fetchone()
    status = check_debt_status(status_row[0] if status_row else None)
    # Start at the next quarter hour, so the answer is a time a person can use.
    t = now.replace(second=0, microsecond=0) + timedelta(minutes=15 - now.minute % 15)
    end = now + HORIZON
    while t <= end:
        if status.allow and may_call(check_before_call(facts, t)):
            # Confirm with the dialer's own dry run: the answer is only ever its answer.
            ok, confirm = precall(conn, account, t)
            if not ok:
                return {
                    "debtor_id": account,
                    "allowed_now": False,
                    "next_allowed": None,
                    "why": "the dialer's check disagrees; ask a person",
                    "blocked_by": _blocking(confirm),
                }
            return {
                "debtor_id": account,
                "allowed_now": False,
                "next_allowed": t.isoformat(),
                "blocked_now_by": _blocking(decisions),
            }
        t += STEP
    return {
        "debtor_id": account,
        "allowed_now": False,
        "next_allowed": None,
        "why": f"no allowed time in the next {HORIZON.days} days",
        "blocked_by": _blocking(decisions),
    }


# --- lists -------------------------------------------------------------------------


def list_debtors(
    conn: psycopg.Connection, sort: str, filters: str, limit: int, now: datetime
) -> dict[str, Any]:
    """Debts, filtered and sorted, at most ``limit`` (itself capped at ``MAX_LIST``).

    ``filters`` is a comma list of ``state:XX``, ``status:active`` (or disputed,
    paused, closed), ``ready`` and ``blocked``; ready and blocked come from the
    dialer's dry run at ``now``.
    """
    sort = (sort or "balance").strip().lower()
    if sort not in SORTS:
        raise ReadError(f"sort must be one of {', '.join(SORTS)}")
    where, params, want = [], [], None
    for raw in filter(None, (f.strip().lower() for f in (filters or "").split(","))):
        key, _, value = raw.partition(":")
        if key == "state" and re.fullmatch(r"[a-z]{2}", value):
            where.append("p.state = %s")
            params.append(value.upper())
        elif key == "status" and value in ("active", "disputed", "paused", "closed"):
            where.append("d.status = %s")
            params.append(value)
        elif raw in ("ready", "blocked") and want in (None, raw):
            want = raw
        else:
            raise ReadError(
                f"unknown filter {raw!r}; use state:XX, status:active|disputed|paused|closed, "
                "ready or blocked"
            )
    sql = ROW_SELECT + (" where " + " and ".join(where) if where else "")
    sql += " order by d.account_number limit %s"
    rows = [_row(r) for r in conn.execute(sql, (*params, MAX_SCAN + 1)).fetchall()]
    scanned_all = len(rows) <= MAX_SCAN
    rows = rows[:MAX_SCAN]
    checks: dict[str, tuple[bool, list]] = {}
    if want:
        checks = {r["debtor_id"]: precall(conn, r["debtor_id"], now) for r in rows}
        rows = [r for r in rows if checks[r["debtor_id"]][0] == (want == "ready")]
    keys = {
        "balance": lambda r: (-r["balance_cents"], r["debtor_id"]),
        "last_payment": lambda r: (
            r["last_paid_on"] is None,
            -(r["last_paid_on"] or date.min).toordinal(),
            r["debtor_id"],
        ),
        "status": lambda r: (r["status"], r["debtor_id"]),
        "name": lambda r: (r["name"].lower(), r["debtor_id"]),
    }
    rows.sort(key=keys[sort])
    shown = []
    for r in rows[:limit]:
        item = _public(r)
        if want:
            item["blocked_by"] = _blocking(checks[r["debtor_id"]][1])
        shown.append(item)
    return {
        "sort": sort,
        "filters": filters or "",
        "count": len(rows),
        "shown": len(shown),
        "limit": limit,
        "truncated": len(rows) > len(shown) or not scanned_all,
        "debtors": shown,
    }


ATTEMPTS_SQL = """
    select id::text, placed_at, outcome, connected, conversation_with_debtor
    from public.call_attempt where debt_id = %s order by placed_at desc
"""


def call_history(conn: psycopg.Connection, debtor_id: str, now: datetime) -> dict[str, Any]:
    """Attempts (newest first, capped), outcome counts, and the 7-in-7 count as the
    pre-call rule sees it at ``now``."""
    from compliance import check_call_frequency
    from harness.db import load_attempts

    r = debt_row(conn, debtor_id)
    rows = conn.execute(ATTEMPTS_SQL, (r["debt_id"],)).fetchall()
    week = now - timedelta(days=7)
    rule = check_call_frequency(load_attempts(conn, r["debt_id"]), r["debt_id"], now)
    return {
        "debtor_id": r["debtor_id"],
        "name": r["name"],
        "attempts_total": len(rows),
        "outcomes": dict(Counter(o for _, _, o, _, _ in rows)),
        "calls_last_7_days": sum(week < placed <= now for _, placed, _, _, _ in rows),
        "seven_in_seven": {"allow": rule.allow, "reason": mask_in(rule.reason)},
        "attempts": [
            {
                "attempt_id": i,
                "placed_at": placed.isoformat(),
                "outcome": outcome,
                "connected": connected,
                "talked_with_debtor": talked,
            }
            for i, placed, outcome, connected, talked in rows[:MAX_HISTORY]
        ],
        "truncated": len(rows) > MAX_HISTORY,
    }


def account_summary(
    conn: psycopg.Connection, debtor_id: str, now: datetime, tz: ZoneInfo
) -> dict[str, Any]:
    """Everything the chat may see about one debt, in one answer."""
    r = debt_row(conn, debtor_id)
    debt_id = r["debt_id"]
    flags = conn.execute(
        "select kind, set_at from public.flags where debtor_id = %s "
        "and (debt_id is null or debt_id = %s) order by set_at",
        (r["person_id"], debt_id),
    ).fetchall()
    plan = conn.execute(
        "select status, down_cents, installments, frequency, installment_cents, created_at "
        "from public.payment_plan where debt_id = %s order by created_at desc limit 1",
        (debt_id,),
    ).fetchone()
    paid = conn.execute(
        "select count(*), coalesce(sum(amount_cents), 0) from public.payment x "
        "where x.debt_id = %s and x.reverses_payment_id is null and not exists "
        "(select 1 from public.payment y where y.reverses_payment_id = x.id)",
        (debt_id,),
    ).fetchone()
    attempts = conn.execute(
        "select count(*), max(placed_at) from public.call_attempt where debt_id = %s",
        (debt_id,),
    ).fetchone()
    notes = conn.execute(
        "select body, created_at from public.debt_note where debt_id = %s "
        "order by created_at desc limit 3",
        (debt_id,),
    ).fetchall()
    callbacks = conn.execute(
        "select scheduled_at, tz from public.callback where debt_id = %s and scheduled_at > %s "
        "order by scheduled_at limit 3",
        (debt_id, now),
    ).fetchall()
    allowed, decisions = precall(conn, r["debtor_id"], now)
    return {
        **_public(r),
        "original_balance": pay.money(r["original_cents"]),
        "opened_on": r["opened_on"].isoformat(),
        "flags": [{"kind": k, "set_at": s.isoformat()} for k, s in flags],
        "payments": {"count": paid[0], "total": pay.money(paid[1])} if paid else None,
        "plan": (
            {
                "status": plan[0],
                "down": pay.money(plan[1]),
                "installments": plan[2],
                "frequency": plan[3],
                "installment": pay.money(plan[4]),
                "made_on": plan[5].astimezone(tz).date().isoformat(),
            }
            if plan
            else None
        ),
        "calls": {
            "attempts": attempts[0] if attempts else 0,
            "last_at": attempts[1].isoformat() if attempts and attempts[1] else None,
        },
        "may_call_now": allowed,
        "blocked_by": _blocking(decisions),
        "latest_notes": [{"text": mask_in(body), "at": at.isoformat()} for body, at in notes],
        "upcoming_callbacks": [
            {"at": at.astimezone(ZoneInfo(z)).isoformat(), "tz": z} for at, z in callbacks
        ],
    }


def portfolio_summary(
    conn: psycopg.Connection, since: date | None, now: datetime, tz: ZoneInfo
) -> dict[str, Any]:
    """Totals for the whole portfolio, and payments and calls since ``since``."""
    by_status = conn.execute(
        "select d.status, count(*), coalesce(sum(d.balance_cents), 0), "
        "coalesce(sum(public.current_balance_cents(d)), 0) from public.debt d group by 1"
    ).fetchall()
    start = since or date.min
    start_at = (
        datetime.combine(since, datetime.min.time(), tz)
        if since
        else datetime.min.replace(tzinfo=tz)
    )
    paid = conn.execute(
        "select count(*), coalesce(sum(case when reverses_payment_id is null "
        "then amount_cents else -amount_cents end), 0) from public.payment where paid_on >= %s",
        (start,),
    ).fetchone()
    calls = conn.execute(
        "select count(*), count(*) filter (where connected), "
        "count(*) filter (where conversation_with_debtor) "
        "from public.call_attempt where placed_at >= %s and placed_at <= %s",
        (start_at, now),
    ).fetchone()
    stops = conn.execute("select kind, count(*) from public.flags group by 1 order by 1").fetchall()
    return {
        "from": since.isoformat() if since else None,
        "to": now.astimezone(tz).date().isoformat(),
        "debts": {
            s: {"count": n, "original": pay.money(o), "balance": pay.money(c)}
            for s, n, o, c in by_status
        },
        "balance_total": pay.money(sum(c for _, _, _, c in by_status)),
        "payments": {"entries": paid[0], "net": pay.money(paid[1])} if paid else None,
        "calls": (
            {"attempts": calls[0], "connected": calls[1], "conversations": calls[2]}
            if calls
            else None
        ),
        "stop_flags": dict(stops),
    }


def audit_for(conn: psycopg.Connection, debtor_id: str, limit: int) -> dict[str, Any]:
    """The debt's audit rows, newest first, at most ``limit``, phones masked."""
    r = debt_row(conn, debtor_id)
    rows = conn.execute(
        "select seq, ts, kind, rule_id, decision, reason, evidence, call_id "
        "from public.audit_event where debt_id = %s order by seq desc limit %s",
        (r["debt_id"], limit + 1),
    ).fetchall()
    return {
        "debtor_id": r["debtor_id"],
        "name": r["name"],
        "events": [
            mask_in(
                {
                    "seq": seq,
                    "at": ts.isoformat(),
                    "kind": kind,
                    "rule_id": rule,
                    "decision": decision,
                    "reason": reason,
                    "evidence": evidence,
                    "call_id": call_id,
                }
            )
            for seq, ts, kind, rule, decision, reason, evidence, call_id in rows[:limit]
        ],
        "truncated": len(rows) > limit,
    }


# --- the few writes ------------------------------------------------------------------


def check_note(text: str) -> str:
    body = " ".join(str(text).split())
    if not body:
        raise ReadError("the note is empty")
    if len(body) > MAX_NOTE_CHARS:
        raise ReadError(f"the note is longer than {MAX_NOTE_CHARS} characters")
    return body


def insert_note(conn: psycopg.Connection, debt_id: str, body: str, created_by: str) -> str:
    row = conn.execute(
        "insert into public.debt_note (debt_id, body, created_by) values (%s, %s, %s) "
        "returning id::text",
        (debt_id, body, created_by),
    ).fetchone()
    assert row is not None
    return row[0]


def parse_when(text: str, now: datetime, tz: ZoneInfo) -> datetime:
    """A future moment: ``YYYY-MM-DDTHH:MM`` in the operator's zone, or with its own offset."""
    words = str(text).strip().replace(" ", "T", 1)
    try:
        when = datetime.fromisoformat(words)
    except ValueError as exc:
        raise ReadError(
            f"{text!r} is not a time; give YYYY-MM-DD HH:MM (your time zone) or an ISO time"
        ) from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    if when <= now:
        raise ReadError(f"{when.isoformat()} is not in the future")
    if when > now + timedelta(days=90):
        raise ReadError("a callback must be within 90 days")
    return when


def check_callback(conn: psycopg.Connection, account: str, when: datetime) -> None:
    """Refuse a callback the dialer's checks would block at that moment: calling
    hours, cease, consent and the rest (``plan_queue``, the dialer's dry run)."""
    ok, decisions = precall(conn, account, when)
    if not ok:
        reasons = "; ".join(f"{b['rule_id']}: {b['reason']}" for b in _blocking(decisions))
        raise ReadError(f"a call then would be blocked ({reasons})")


def insert_callback(
    conn: psycopg.Connection, call_id: str, debt_id: str, when: datetime, tz: ZoneInfo
) -> str:
    row = conn.execute(
        "insert into public.callback (call_id, debt_id, scheduled_at, tz) "
        "values (%s, %s, %s, %s) returning id::text",
        (call_id, debt_id, when, tz.key),
    ).fetchone()
    assert row is not None
    return row[0]


def record_stop(
    conn: psycopg.Connection, r: dict[str, Any], kind: str, source: str, call_id: str
) -> str:
    """Write the stop. Only adds: a flag (cease, attorney, bankruptcy) or a dispute
    that also marks an active debt disputed. Returns what changed, for the audit row."""
    from harness.db import mark_disputed, save_dispute, save_flag

    if kind == "dispute":
        save_dispute(conn, call_id, r["debt_id"], "recorded by the operator", True)
        changed = mark_disputed(conn, r["debt_id"])
        return "dispute logged; debt marked disputed" if changed else "dispute logged"
    # A cease, attorney or bankruptcy covers every debt of the debtor.
    save_flag(conn, Flag(kind=kind, debt_id=None, data={}, source=source), r["person_id"])
    return f"{kind} flag set for every debt of this debtor"


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)
