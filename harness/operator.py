"""The operator: the chat agent behind the text box where a person types "call Sam Doe".

``Operator`` wraps the same ``Agent`` the calls use, with its own system prompt
(``instructions/operator.md``) and these tools:

  - ``find_debtor(query)``: debts whose debtor's name or phone number matches,
    each as ``{id, name, phone}`` with the phone masked to its last four digits.
    The ``id`` is the debt's account number; it names one debt and its debtor,
    and it is what the other tools take as ``debtor_id``.
  - ``check_call(debtor_id)``: the pre-call checks as they stand right now
    (``dialer.service.plan_queue``, the dialer's own dry run): allow or block,
    with each rule id and reason. It only reads, like ``GET /api/precall``.
  - ``place_call(debtor_id)``: does **not** dial. It looks the debt up and
    records a pending approval that names the debtor and their masked phone, so
    the person approves who will be called, not just an id the model chose. An
    unknown id opens nothing, and nor do arguments that don't parse (``run_tool``
    answers those with an error).
  - ``call_status(call_id)``: one ``call_attempt`` row, found by its own id or
    by the AgentPhone call id.
  - ``record_payment(debtor_id, amount, paid_on, method)``: does **not** write.
    Code resolves the debt (an id, or a name with one match), parses the amount
    and the day ("yesterday" is the operator's yesterday, in ``tz``) and checks
    them against the current balance, then opens an approval showing the exact
    debtor, amount and date. Zero, negative, above the balance, a future day,
    a day before the debt was opened, an unknown or ambiguous debtor, or
    arguments that don't parse open nothing.
  - ``reverse_payment(payment_id, reason)``: the same, for a reversing row.
  - ``payment_history(debtor_id, since)``: payments since a day, with their
    total. Only reads.
  - ``on_time_report()``: each debt with a plan as on time, late or missed
    (``harness.payments.classify_plan``), and the debts without one. Only reads.
  - Account reads (``harness/operator_reads.py``), all capped and with phones
    masked: ``list_debtors(sort, filter, limit)``, ``account_summary``,
    ``call_history`` (with the 7-in-7 count as the rule sees it),
    ``next_allowed_call`` (stepped through ``check_before_call`` and confirmed
    by the dialer's own dry run, so it can never be a way around the checks),
    ``portfolio_summary(period)`` and ``audit_for(debtor_id, limit)``.
  - ``add_note(debtor_id, text)`` and ``schedule_callback(debtor_id, when)``
    open an approval like ``place_call``; nothing is written before it. A
    callback the pre-call checks would block at that moment is refused, both
    when it is asked for and again when it is approved.
  - ``record_stop(debtor_id, kind)``: cease, dispute, attorney or bankruptcy.
    It writes at once, with an audit row, and needs no approval, because a stop
    can only ever mean fewer calls. No tool removes a stop, records consent,
    edits a balance or deletes anything.

Only ``decide(approval_id, approve)`` dials or writes a payment, note or
callback, and only for an approval that is still pending. It is called by code
(``POST /api/chat/approve``), never by the model. A payment approval runs every
check again against the balance as it is then, inside one transaction with the
debt locked, and writes the payment row and its audit event together; a check
that fails then writes nothing but the refusal in the audit log. Notes and
callbacks work the same way. For a call, it first makes sure AgentPhone is set
up, so a missing key fails
before any attempt is recorded (and leaves the approval pending). Then the
decision is written to the history, so a second
click on the same approval finds nothing pending. Then it goes through
``dialer.service.dial_one``, which locks the debt, runs and records every
pre-call check again, records the attempt, and dials at most once. A debt the
checks block is not dialed, approval or not. The model never decides whether a
call is allowed: the person approves, the checks allow.

Pending approvals are not kept anywhere new: they are read back from the chat
history in the session store. A ``place_call``, ``record_payment``,
``reverse_payment``, ``add_note`` or ``schedule_callback`` result opens one; a message
that starts with ``[approval <id>]`` closes it. Only code writes tool results,
so only code can open one. Anyone (the model, or text typed in the box) could
write a closing marker, but closing one only ever means fewer calls.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg

from compliance import Decision
from compliance.audit import record as audit_record
from harness import operator_reads as reads
from harness import payments as pay
from harness.agent import Agent
from harness.memory import Session
from harness.tools import Tool, ToolRegistry
from model import Provider
from phone.client import PhoneClient

DEFAULT_OPERATOR_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "instructions" / "operator.md"
)
# The operator gets no skills menu: the call skills are for the phone agent.
NO_SKILLS_DIR = Path(__file__).resolve().parent / "_operator_has_no_skills"
NEEDS_APPROVAL = "needs_approval"
PLACE_CALL = "place_call"
RECORD_PAYMENT = "record_payment"
REVERSE_PAYMENT = "reverse_payment"
ADD_NOTE = "add_note"
SCHEDULE_CALLBACK = "schedule_callback"
PAYMENT_ACTIONS = (RECORD_PAYMENT, REVERSE_PAYMENT)
ACTIONS = (PLACE_CALL, *PAYMENT_ACTIONS, ADD_NOTE, SCHEDULE_CALLBACK)
DEFAULT_OPERATOR_TZ = "America/New_York"
MAX_MATCHES = 10
_ACCOUNT = re.compile(r"[A-Za-z0-9-]{1,40}")
_MARKER = re.compile(r"^\[approval (apr_[0-9a-f]{16})\]")

FIND_SQL = """
    select d.account_number, p.first_name || ' ' || p.last_name, ph.e164
    from public.debt d
    join public.debtor p on p.id = d.debtor_id
    left join lateral (
        select e164 from public.phone where debtor_id = p.id order by e164 limit 1
    ) ph on true
    where lower(p.first_name || ' ' || p.last_name) like %s
       or (%s <> '' and exists (
            select 1 from public.phone x
            where x.debtor_id = p.id and x.e164 like %s))
    order by d.account_number
    limit %s
"""

LOOKUP_SQL = """
    select p.first_name || ' ' || p.last_name, ph.e164
    from public.debt d
    join public.debtor p on p.id = d.debtor_id
    left join lateral (
        select e164 from public.phone where debtor_id = p.id order by e164 limit 1
    ) ph on true
    where d.account_number = %s
"""

STATUS_SQL = """
    select a.id::text, d.account_number, a.outcome, a.placed_at, a.connected,
           a.agentphone_call_id
    from public.call_attempt a
    join public.debt d on d.id = a.debt_id
    where a.id::text = %s or a.agentphone_call_id = %s
    order by a.placed_at desc
    limit 1
"""


def mask_phone(e164: str | None) -> str | None:
    """Only the last four digits, so the chat never shows a whole number."""
    if not e164:
        return None
    return "***-***-" + e164[-4:]


def operator_tz(name: str | None = None) -> ZoneInfo:
    """The operator's time zone: ``name`` (an IANA name such as ``America/Chicago``),
    else ``OPERATOR_TZ``, else New York. Raises ``ValueError`` for an unknown name."""
    wanted = name or os.environ.get("OPERATOR_TZ") or DEFAULT_OPERATOR_TZ
    try:
        return ZoneInfo(wanted)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone {wanted!r}") from exc


class PhoneUnavailable(RuntimeError):
    """AgentPhone isn't set up (keys or SDK missing), so no call can be placed."""


@dataclass(frozen=True)
class Approval:
    id: str
    debtor_id: str
    name: str
    phone: str | None  # masked
    tool_call_id: str
    action: str = PLACE_CALL
    # For a payment: amount_cents, amount, paid_on, method, balance_before and
    # balance_after; for a reversal also payment_id and reason. Written by code.
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatTurn:
    """What one chat message (or one approve/deny) produced, for the API."""

    reply: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_approvals: list[Approval] = field(default_factory=list)
    dial: dict[str, Any] | None = None  # what decide() did, if it ran


def pending_approvals(messages: list[dict]) -> list[Approval]:
    """Approvals opened by a ``place_call`` result and not yet closed, oldest first."""
    closed = set()
    for m in messages:
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            if match := _MARKER.match(m["content"]):
                closed.add(match.group(1))
    opened: list[Approval] = []
    for m in messages:
        if m.get("role") != "tool":
            continue
        try:
            body = json.loads(m.get("content") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(body, dict) and body.get("status") == NEEDS_APPROVAL:
            action = body.get("action", PLACE_CALL)
            if action not in ACTIONS:
                continue
            approval = Approval(
                id=str(body.get("approval_id")),
                debtor_id=str(body.get("debtor_id")),
                name=str(body.get("name")),
                phone=body.get("phone"),
                tool_call_id=m.get("tool_call_id", ""),
                action=action,
                details=dict(body.get("details") or {}),
            )
            if approval.id not in closed:
                opened.append(approval)
    return opened


def _tool_calls_since(messages: list[dict], start: int) -> list[dict[str, Any]]:
    """Each tool request made in ``messages[start:]``, with the result it got."""
    results = {m.get("tool_call_id"): m.get("content") for m in messages if m.get("role") == "tool"}
    out = []
    for m in messages[start:]:
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            out.append(
                {
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments"),
                    "result": results.get(call.get("id")),
                }
            )
    return out


class _PhoneFromEnv:
    """AgentPhone, made from the environment only when a call is really placed, so
    chatting, and approving a call the checks then block, need no AgentPhone keys."""

    def __init__(self) -> None:
        self._client: PhoneClient | None = None

    def ready(self) -> None:
        """Build the client now; raise ``PhoneUnavailable`` if AgentPhone isn't set up."""
        if self._client is not None:
            return
        from phone.client import AgentPhoneClient

        try:
            client = AgentPhoneClient.from_env()
            client._client()  # imports the SDK
        except RuntimeError as exc:
            raise PhoneUnavailable(str(exc)) from exc
        self._client = client

    def make_call(
        self,
        to_number: str,
        *,
        disable_recording: bool | None = None,
        ring_timeout_seconds: int | None = None,
    ) -> str:
        self.ready()
        assert self._client is not None
        return self._client.make_call(
            to_number,
            disable_recording=disable_recording,
            ring_timeout_seconds=ring_timeout_seconds,
        )


class Operator:
    """One chat session's operator agent. Make a new one per request; the session keeps the rest.

    ``connect`` opens the database, ``phone`` places calls (by default AgentPhone,
    made when a call is approved), and ``clock`` says what time the checks run at.
    ``lookup(account_number)`` gives ``(name, e164)`` for a debt, or None; it
    reads the database unless a test passes its own. ``tz`` is the operator's
    time zone (``operator_tz``), which turns "yesterday" into a date.
    """

    def __init__(
        self,
        session: Session,
        *,
        provider: Provider | None = None,
        model: str | None = None,
        connect: Callable[[], AbstractContextManager[psycopg.Connection]] | None = None,
        phone: PhoneClient | None = None,
        clock: Callable[[], datetime] | None = None,
        lookup: Callable[[str], tuple[str, str | None] | None] | None = None,
        system_prompt_path: str | Path = DEFAULT_OPERATOR_PROMPT_PATH,
        tz: ZoneInfo | None = None,
    ) -> None:
        if connect is None:
            from harness.db import connect as connect_db

            connect = connect_db
        self._connect = connect
        self._phone = phone or _PhoneFromEnv()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lookup = lookup or self._lookup_in_db
        self.tz = tz or operator_tz()
        self.session = session
        self.agent = Agent(
            model=model,
            provider=provider,
            system_prompt_path=system_prompt_path,
            tools=self.tools(),
            session=session,
            skills_dir=NO_SKILLS_DIR,
        )

    # --- the tools -----------------------------------------------------------

    def tools(self) -> ToolRegistry:
        debtor = "The id find_debtor returned, for example 'ACC-1001'."
        return ToolRegistry(
            [
                Tool(
                    "find_debtor",
                    "Search debtors by name or phone number. Returns each match's id, "
                    "name and masked phone number.",
                    {"query": "A name, part of a name, or phone digits."},
                    self.find_debtor,
                ),
                Tool(
                    "check_call",
                    "Run the pre-call checks for one debtor right now. Returns whether "
                    "a call is allowed and, for each rule, its id and reason.",
                    {"debtor_id": debtor},
                    self.check_call,
                ),
                Tool(
                    "place_call",
                    "Ask to call one debtor. This does not dial: the person must approve "
                    "it first, and the pre-call checks run again before any dial.",
                    {"debtor_id": debtor},
                    self.place_call,
                ),
                Tool(
                    "call_status",
                    "The status of a call attempt, by attempt id or call id.",
                    {"call_id": "The attempt id or call id from an earlier result."},
                    self.call_status,
                ),
                Tool(
                    "record_payment",
                    "Ask to record a payment a debtor made. This does not write: the "
                    "person must approve the exact debtor, amount and date first.",
                    {
                        "debtor_id": debtor,
                        "amount": "Dollars as the person said them, e.g. '20' or '20.50'.",
                        "paid_on": "The day as the person said it: 'today', 'yesterday', "
                        "'3 days ago' or YYYY-MM-DD. Do not work out the date yourself.",
                        "method": "cash, check, card, ach or other.",
                    },
                    self.record_payment,
                ),
                Tool(
                    "reverse_payment",
                    "Ask to cancel a recorded payment with a reversing entry. Nothing "
                    "is deleted, and the person must approve it first.",
                    {
                        "payment_id": "The payment_id from payment_history.",
                        "reason": "Why it is being reversed, e.g. 'check bounced'.",
                    },
                    self.reverse_payment,
                ),
                Tool(
                    "payment_history",
                    "A debtor's payments since a day, newest first, with their total "
                    "and the balance now. Reversed payments are marked and not counted.",
                    {
                        "debtor_id": debtor,
                        "since": "'last 3 days', 'yesterday', YYYY-MM-DD, or 'all'.",
                    },
                    self.payment_history,
                ),
                Tool(
                    "on_time_report",
                    "Debts with a payment plan, each on time, late or missed against "
                    "the plan's due dates, and the debts that have no plan.",
                    {},
                    self.on_time_report,
                ),
                Tool(
                    "list_debtors",
                    f"List debts, sorted and filtered, at most {reads.MAX_LIST}. Each has its "
                    "id, name, state, balance, status, masked phone and last payment day.",
                    {
                        "sort": "balance (largest first), last_payment (newest first), "
                        "status or name.",
                        "filter": "Empty, or a comma list of state:XX, status:active (or "
                        "disputed, paused, closed), ready (every pre-call check passes now) "
                        "or blocked.",
                        "limit": f"How many to show, up to {reads.MAX_LIST}.",
                    },
                    self.list_debtors,
                ),
                Tool(
                    "account_summary",
                    "One debt in full: balance and original amount, status, stop flags, "
                    "payments, plan, calls, whether a call is allowed now, recent notes "
                    "and upcoming callbacks.",
                    {"debtor_id": debtor},
                    self.account_summary,
                ),
                Tool(
                    "call_history",
                    "One debt's call attempts and outcomes, newest first, with how many "
                    "calls in the last 7 days count toward the 7-in-7 limit.",
                    {"debtor_id": debtor},
                    self.call_history,
                ),
                Tool(
                    "next_allowed_call",
                    "The next time every pre-call check would pass for one debt, or why "
                    "no time will (a stop that waiting does not clear).",
                    {"debtor_id": debtor},
                    self.next_allowed_call,
                ),
                Tool(
                    "portfolio_summary",
                    "Totals for every debt by status, and payments and calls in a period.",
                    {"period": "'last 7 days', 'this month', 'today', YYYY-MM-DD, or 'all'."},
                    self.portfolio_summary,
                ),
                Tool(
                    "audit_for",
                    f"One debt's audit log, newest first, at most {reads.MAX_AUDIT} rows.",
                    {"debtor_id": debtor, "limit": f"Up to {reads.MAX_AUDIT}."},
                    self.audit_for,
                ),
                Tool(
                    "add_note",
                    "Ask to add a note to a debt. This does not write: the person must "
                    "approve it. Notes are kept forever and can't be edited.",
                    {"debtor_id": debtor, "text": "The note, in the person's words."},
                    self.add_note,
                ),
                Tool(
                    "schedule_callback",
                    "Ask to schedule a callback. This does not write: the person must "
                    "approve it, and it is refused if a call then would break the "
                    "pre-call checks (calling hours, a cease, and the rest).",
                    {
                        "debtor_id": debtor,
                        "when": "YYYY-MM-DD HH:MM in the person's time zone, or an ISO "
                        "time with an offset.",
                    },
                    self.schedule_callback,
                ),
                Tool(
                    "record_stop",
                    "Record that a debtor asked us to stop (cease), disputes the debt, "
                    "has an attorney, or filed for bankruptcy. Takes effect at once; "
                    "calls are blocked from then on. It can't be undone in chat.",
                    {
                        "debtor_id": debtor,
                        "kind": "cease, dispute, attorney or bankruptcy.",
                    },
                    self.record_stop,
                ),
            ]
        )

    def find_debtor(self, query: str) -> str:
        words = " ".join(query.split()).lower()
        digits = re.sub(r"\D", "", query)
        if not words:
            return "error: give a name or phone digits to search for"
        # A phone search needs a few digits, so "1" doesn't match every number.
        phone_like = f"%{digits}%" if len(digits) >= 4 else ""
        with self._connect() as conn:
            rows = conn.execute(
                FIND_SQL, (f"%{words}%", phone_like, phone_like, MAX_MATCHES)
            ).fetchall()
        matches = [
            {"id": account, "name": name, "phone": mask_phone(e164)} for account, name, e164 in rows
        ]
        if not matches:
            return f"error: no debtor matches {query!r}"
        return json.dumps({"matches": matches})

    def check_call(self, debtor_id: str) -> str:
        from dialer.service import plan_queue

        if not _ACCOUNT.fullmatch(debtor_id):
            return f"error: {debtor_id!r} is not a debtor id"
        when = self._clock()
        with self._connect() as conn:
            (outcome,) = plan_queue([debtor_id], when, conn).outcomes  # writes nothing
        return json.dumps(
            {
                "debtor_id": debtor_id,
                "at": when.isoformat(),
                "decision": "block" if outcome.blocked else "allow",
                "rules": [
                    {"rule_id": d.rule_id, "allow": d.allow, "reason": d.reason}
                    for d in outcome.decisions
                ],
            }
        )

    def place_call(self, debtor_id: str) -> str:
        if not _ACCOUNT.fullmatch(debtor_id):
            return f"error: {debtor_id!r} is not a debtor id; nothing was dialed"
        found = self._lookup(debtor_id)
        if found is None:
            return f"error: no debtor {debtor_id!r}; nothing was dialed"
        name, e164 = found
        return json.dumps(
            {
                "status": NEEDS_APPROVAL,
                "approval_id": "apr_" + secrets.token_hex(8),
                "debtor_id": debtor_id,
                "name": name,
                "phone": mask_phone(e164),
                "note": "Nothing has been dialed. The person must approve this call, "
                "and the pre-call checks run again before dialing.",
            }
        )

    def _lookup_in_db(self, account_number: str) -> tuple[str, str | None] | None:
        with self._connect() as conn:
            row = conn.execute(LOOKUP_SQL, (account_number,)).fetchone()
        return (row[0], row[1]) if row else None

    def call_status(self, call_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(STATUS_SQL, (call_id, call_id)).fetchone()
        if row is None:
            return f"error: no call attempt {call_id!r}"
        attempt_id, account, outcome, placed_at, connected, agentphone_id = row
        return json.dumps(
            {
                "attempt_id": attempt_id,
                "debtor_id": account,
                "outcome": outcome,
                "placed_at": placed_at.isoformat(),
                "connected": connected,
                "call_id": agentphone_id,
            }
        )

    # --- payments ----------------------------------------------------------------

    def _today(self) -> date:
        return pay.local_today(self._clock(), self.tz)

    def _recorded_by(self) -> str:
        return f"operator:{self.session.call_id}"

    def record_payment(self, debtor_id: str, amount: str, paid_on: str, method: str) -> str:
        nothing = "; nothing was recorded"
        today = self._today()
        try:
            cents = pay.parse_amount(amount)
            day = pay.parse_day(paid_on, today)
            how = pay.parse_method(method)
            if day > today:
                raise pay.PaymentError(f"{day.isoformat()} is in the future (today is {today})")
            with self._connect() as conn:
                account = pay.resolve_debt(conn, debtor_id)
            pay.check_payment(account, cents, day, today)
        except pay.PaymentError as exc:
            return f"error: {exc}{nothing}"
        return json.dumps(
            {
                "status": NEEDS_APPROVAL,
                "action": RECORD_PAYMENT,
                "approval_id": "apr_" + secrets.token_hex(8),
                "debtor_id": account.account_number,
                "name": account.name,
                "phone": None,
                "details": {
                    "amount_cents": cents,
                    "amount": pay.money(cents),
                    "paid_on": day.isoformat(),
                    "method": how,
                    "balance_before": pay.money(account.balance_cents),
                    "balance_after": pay.money(account.balance_cents - cents),
                    "time_zone": self.tz.key,
                },
                "note": "Nothing has been recorded. The person must approve this payment, "
                "and it is checked against the balance again before it is written.",
            }
        )

    def reverse_payment(self, payment_id: str, reason: str) -> str:
        nothing = "; nothing was recorded"
        try:
            with self._connect() as conn:
                found = pay.payment_by_id(conn, payment_id.strip())
            payment = pay.check_reversal(found, reason)
        except pay.PaymentError as exc:
            return f"error: {exc}{nothing}"
        return json.dumps(
            {
                "status": NEEDS_APPROVAL,
                "action": REVERSE_PAYMENT,
                "approval_id": "apr_" + secrets.token_hex(8),
                "debtor_id": payment.account_number,
                "name": payment.name,
                "phone": None,
                "details": {
                    "payment_id": payment.id,
                    "amount_cents": payment.amount_cents,
                    "amount": pay.money(payment.amount_cents),
                    "paid_on": payment.paid_on.isoformat(),
                    "method": payment.method,
                    "reason": reason.strip(),
                },
                "note": "Nothing has been recorded. The person must approve this reversal. "
                "It adds a reversing entry; the payment itself is kept.",
            }
        )

    def payment_history(self, debtor_id: str, since: str) -> str:
        today = self._today()
        try:
            start = pay.parse_since(since, today)
            with self._connect() as conn:
                account = pay.resolve_debt(conn, debtor_id)
                rows, total, count = pay.history(conn, account, start)
        except pay.PaymentError as exc:
            return f"error: {exc}"
        return json.dumps(
            {
                "debtor_id": account.account_number,
                "name": account.name,
                "from": start.isoformat() if start else None,
                "to": today.isoformat(),
                "time_zone": self.tz.key,
                "total": pay.money(total),
                "count": count,
                "payments": [r.as_json() for r in rows],
                "truncated": count > len(rows),
                "balance_now": pay.money(account.balance_cents),
                "original_balance": pay.money(account.original_cents),
            }
        )

    def on_time_report(self) -> str:
        with self._connect() as conn:
            report = pay.on_time_report(conn, self._today(), self.tz)
        return json.dumps(report)

    def _write_payment(self, approval: Approval) -> dict[str, Any]:
        """Run the checks again and write the row and its audit event in one transaction.

        Returns ``{"written": bool, "reason": str, "payment_id": ..., "balance_after": ...}``.
        A refusal is audited too (``decision = block``) and writes no payment.
        """
        d = approval.details
        today = self._today()
        with self._connect() as conn, conn.transaction():
            account = pay.account_by_number(conn, approval.debtor_id, lock=True)
            try:
                if account is None:
                    raise pay.PaymentError(f"no debtor {approval.debtor_id!r}")
                if approval.action == RECORD_PAYMENT:
                    cents = int(d["amount_cents"])
                    day = date.fromisoformat(str(d["paid_on"]))
                    method = pay.parse_method(str(d["method"]))
                    pay.check_payment(account, cents, day, today)
                    payment_id = pay.insert_payment(
                        conn, account, cents, day, method, self._recorded_by()
                    )
                    after = account.balance_cents - cents
                else:
                    found = pay.payment_by_id(conn, str(d["payment_id"]))
                    payment = pay.check_reversal(found, str(d.get("reason", "")))
                    if payment.debt_id != account.debt_id:
                        raise pay.PaymentError("that payment is on another debt")
                    payment_id = pay.insert_reversal(
                        conn, payment, str(d["reason"]), self._recorded_by(), today
                    )
                    after = account.balance_cents + payment.amount_cents
            except (pay.PaymentError, KeyError, ValueError) as exc:
                reason = str(exc)
                audit_record(
                    conn,
                    Decision(False, approval.action, reason, {"approval_id": approval.id, **d}),
                    kind="payment",
                    call_id=self.session.call_id,
                    debt_id=account.debt_id if account else None,
                )
                return {"written": False, "reason": reason}
            audit_record(
                conn,
                Decision(
                    True,
                    approval.action,
                    "approved by the operator",
                    {
                        **d,
                        "approval_id": approval.id,
                        "payment_id": payment_id,
                        "recorded_by": self._recorded_by(),
                        "balance_before_cents": account.balance_cents,
                        "balance_after_cents": after,
                    },
                ),
                kind="payment",
                call_id=self.session.call_id,
                debt_id=account.debt_id,
            )
        return {
            "written": True,
            "reason": "",
            "payment_id": payment_id,
            "balance_after": pay.money(after),
        }

    # --- account reads ---------------------------------------------------------

    def _read(self, fn: Callable[[psycopg.Connection], Any]) -> str:
        """Run one read and answer JSON, or an ``error:`` line the model can act on."""
        try:
            with self._connect() as conn:
                result = fn(conn)
        except (reads.ReadError, pay.PaymentError) as exc:
            return f"error: {exc}"
        return reads.dumps(result)

    def list_debtors(self, sort: str, filter: str, limit: str) -> str:  # noqa: A002
        try:
            n = reads.parse_limit(limit, reads.MAX_LIST)
        except reads.ReadError as exc:
            return f"error: {exc}"
        now = self._clock()
        return self._read(lambda conn: reads.list_debtors(conn, sort, filter, n, now))

    def account_summary(self, debtor_id: str) -> str:
        now = self._clock()
        return self._read(lambda conn: reads.account_summary(conn, debtor_id, now, self.tz))

    def call_history(self, debtor_id: str) -> str:
        now = self._clock()
        return self._read(lambda conn: reads.call_history(conn, debtor_id, now))

    def next_allowed_call(self, debtor_id: str) -> str:
        now = self._clock()

        def run(conn: psycopg.Connection) -> dict[str, Any]:
            account = pay.resolve_debt(conn, debtor_id).account_number
            return reads.next_allowed_call(conn, account, now)

        return self._read(run)

    def portfolio_summary(self, period: str) -> str:
        now = self._clock()
        try:
            since = pay.parse_since(period, self._today())
        except pay.PaymentError as exc:
            return f"error: {exc}"
        return self._read(lambda conn: reads.portfolio_summary(conn, since, now, self.tz))

    def audit_for(self, debtor_id: str, limit: str) -> str:
        try:
            n = reads.parse_limit(limit, reads.MAX_AUDIT)
        except reads.ReadError as exc:
            return f"error: {exc}"
        return self._read(lambda conn: reads.audit_for(conn, debtor_id, n))

    # --- notes, callbacks and stops ----------------------------------------------

    def _approval(self, action: str, row: dict[str, Any], details: dict[str, Any]) -> str:
        return json.dumps(
            {
                "status": NEEDS_APPROVAL,
                "action": action,
                "approval_id": "apr_" + secrets.token_hex(8),
                "debtor_id": row["debtor_id"],
                "name": row["name"],
                "phone": reads.mask_phone(row["e164"]),
                "details": details,
                "note": "Nothing has been written. The person must approve this first.",
            }
        )

    def add_note(self, debtor_id: str, text: str) -> str:
        try:
            body = reads.check_note(text)
            with self._connect() as conn:
                row = reads.debt_row(conn, debtor_id)
        except (reads.ReadError, pay.PaymentError) as exc:
            return f"error: {exc}; nothing was written"
        return self._approval(ADD_NOTE, row, {"text": body})

    def schedule_callback(self, debtor_id: str, when: str) -> str:
        now = self._clock()
        try:
            at = reads.parse_when(when, now, self.tz)
            with self._connect() as conn:
                row = reads.debt_row(conn, debtor_id)
                reads.check_callback(conn, row["debtor_id"], at)
        except (reads.ReadError, pay.PaymentError) as exc:
            return f"error: {exc}; nothing was written"
        local = at.astimezone(self.tz)
        return self._approval(
            SCHEDULE_CALLBACK,
            row,
            {"when": local.isoformat(), "shown": f"{local:%Y-%m-%d %H:%M} {self.tz.key}"},
        )

    def record_stop(self, debtor_id: str, kind: str) -> str:
        kind = " ".join(kind.lower().split())
        if kind not in reads.STOP_KINDS:
            return f"error: kind must be one of {', '.join(reads.STOP_KINDS)}"
        source = self._recorded_by()
        try:
            with self._connect() as conn, conn.transaction():
                row = reads.debt_row(conn, debtor_id)
                what = reads.record_stop(conn, row, kind, source, self.session.call_id)
                audit_record(
                    conn,
                    Decision(True, f"record_stop:{kind}", what, {"source": source}),
                    kind="stop",
                    call_id=self.session.call_id,
                    debt_id=row["debt_id"],
                )
        except (reads.ReadError, pay.PaymentError) as exc:
            return f"error: {exc}; nothing was written"
        return json.dumps(
            {"debtor_id": row["debtor_id"], "name": row["name"], "kind": kind, "done": what}
        )

    def _write_other(self, approval: Approval) -> dict[str, Any]:
        """Write an approved note or callback with its audit row, checking again first."""
        d = approval.details
        with self._connect() as conn, conn.transaction():
            row: dict[str, Any] | None = None
            try:
                row = reads.debt_row(conn, approval.debtor_id)
                if approval.action == ADD_NOTE:
                    body = reads.check_note(str(d.get("text", "")))
                    written = reads.insert_note(conn, row["debt_id"], body, self._recorded_by())
                else:
                    at = datetime.fromisoformat(str(d["when"]))
                    if at <= self._clock():
                        raise reads.ReadError("that time has passed")
                    reads.check_callback(conn, row["debtor_id"], at)
                    written = reads.insert_callback(
                        conn, self.session.call_id, row["debt_id"], at, self.tz
                    )
            except (reads.ReadError, pay.PaymentError, KeyError, ValueError) as exc:
                audit_record(
                    conn,
                    Decision(False, approval.action, str(exc), {"approval_id": approval.id}),
                    kind="operator",
                    call_id=self.session.call_id,
                    debt_id=row["debt_id"] if row else None,
                )
                return {"written": False, "reason": str(exc)}
            audit_record(
                conn,
                Decision(
                    True,
                    approval.action,
                    "approved by the operator",
                    {"approval_id": approval.id, "id": written, **reads.mask_in(d)},
                ),
                kind="operator",
                call_id=self.session.call_id,
                debt_id=row["debt_id"],
            )
        return {"written": True, "reason": "", "id": written}

    # --- chatting and approving ------------------------------------------------

    def pending(self) -> list[Approval]:
        return pending_approvals(self.agent.messages)

    def chat(self, message: str) -> ChatTurn:
        """One message from the person: the agent loop, then what it asked for."""
        start = len(self.agent.messages)
        reply = self.agent.run(message)
        return ChatTurn(
            reply=reply,
            tool_calls=_tool_calls_since(self.agent.messages, start),
            pending_approvals=self.pending(),
        )

    def decide(self, approval_id: str, approve: bool) -> ChatTurn:
        """Approve or deny a pending approval. Approving a call dials through
        ``dial_one``; approving a payment or reversal writes it (``_write_payment``).

        Raises ``LookupError`` if ``approval_id`` is not pending (never opened,
        already decided, or closed some other way), and ``PhoneUnavailable`` if
        AgentPhone isn't set up; then nothing is recorded and it stays pending.
        """
        approval = next((a for a in self.pending() if a.id == approval_id), None)
        if approval is None:
            raise LookupError(f"no pending approval {approval_id!r}")
        if approve and approval.action == PLACE_CALL and isinstance(self._phone, _PhoneFromEnv):
            # Before any attempt is recorded: dial_one would take a setup error for
            # a call that may have rung, and count it against the debtor's limit.
            self._phone.ready()
        marker = f"[approval {approval.id}]"
        verb = "approved" if approve else "denied"
        # Close it before dialing, so a repeat of this request finds nothing to dial.
        self.agent._add(
            {
                "role": "user",
                "content": f"{marker} {verb}: {approval.action} for {approval.debtor_id}",
            }
        )
        if approval.action in PAYMENT_ACTIONS:
            return self._decide_payment(approval, approve, marker)
        if approval.action != PLACE_CALL:
            return self._decide_other(approval, approve, marker)
        if not approve:
            reply = f"{marker} Not dialed: you denied the call to {approval.name}."
            self.agent._add({"role": "assistant", "content": reply})
            return ChatTurn(reply=reply, pending_approvals=self.pending())

        dial = self._dial(approval.debtor_id)
        if dial["blocked"]:
            reasons = "; ".join(dial["reasons"])
            reply = f"{marker} Not dialed: the checks block {approval.name} ({reasons})."
        elif dial["outcome"] == "placed":
            reply = f"{marker} Calling {approval.name}. Call id {dial['call_id']}."
        else:
            reply = (
                f"{marker} The call to {approval.name} came back {dial['outcome']}. "
                f"Attempt {dial['attempt_id']}."
            )
        self.agent._add({"role": "assistant", "content": reply})
        return ChatTurn(reply=reply, pending_approvals=self.pending(), dial=dial)

    def _decide_payment(self, approval: Approval, approve: bool, marker: str) -> ChatTurn:
        d = approval.details
        what = (
            f"the {d.get('amount')} payment from {approval.name} on {d.get('paid_on')}"
            if approval.action == RECORD_PAYMENT
            else f"the reversal of payment {d.get('payment_id')} ({d.get('amount')}) "
            f"for {approval.name}"
        )
        if not approve:
            reply = f"{marker} Not recorded: you denied {what}."
        else:
            done = self._write_payment(approval)
            if done["written"]:
                reply = (
                    f"{marker} Recorded {what}. Entry {done['payment_id']}. "
                    f"Balance now {done['balance_after']}."
                )
            else:
                reply = f"{marker} Not recorded: {what} failed a check ({done['reason']})."
        self.agent._add({"role": "assistant", "content": reply})
        return ChatTurn(reply=reply, pending_approvals=self.pending())

    def _decide_other(self, approval: Approval, approve: bool, marker: str) -> ChatTurn:
        d = approval.details
        what = (
            f"the note for {approval.name}"
            if approval.action == ADD_NOTE
            else f"the callback to {approval.name} at {d.get('shown')}"
        )
        if not approve:
            reply = f"{marker} Not saved: you denied {what}."
        else:
            done = self._write_other(approval)
            if done["written"]:
                reply = f"{marker} Saved {what}. Entry {done['id']}."
            else:
                reply = f"{marker} Not saved: {what} failed a check ({done['reason']})."
        self.agent._add({"role": "assistant", "content": reply})
        return ChatTurn(reply=reply, pending_approvals=self.pending())

    def _dial(self, debtor_id: str) -> dict[str, Any]:
        from dialer.service import dial_one

        with self._connect() as conn:
            o = dial_one(conn, debtor_id, self._clock(), self._phone)
        return {
            "debtor_id": debtor_id,
            "blocked": o.blocked,
            "reasons": o.reasons,
            "outcome": o.outcome,
            "attempt_id": o.attempt_id,
            "call_id": o.call_id,
        }
