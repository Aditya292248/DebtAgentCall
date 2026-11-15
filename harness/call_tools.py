"""Call tools: what the voice agent may ask for during a call, and who says yes.

These tools are built on ``harness/tools.py`` (``Tool``, ``ToolRegistry``,
``run_tool``), so a failure always comes back as an ``error: ...`` string and
never raises into the call loop. They are not used on live calls yet: the
webhook does not offer them to the model.

``CallContext`` is everything one call's tools may touch: the person and the
one debt the call is about, the call state (stage, verified, disclosed), the
debtor's flags and contact facts, a ``CallStore`` for the rows the tools write,
and the audit writer. It plays the part codingHarness's Workspace played. A
tool never takes an account id or a name from the model: it can only reach
this call's debt.

Every tool call goes through two steps, in this order, and both are audited:

  1. The compliance gate (``CallTools.gate``): is this tool allowed right now?
     It looks at the stage, a dispute (``debt.status = 'disputed'``, or one
     logged on this call), a cease flag, and for a callback the calling hours.
     A block returns ``error: blocked ...`` and nothing else happens.
  2. The approver (``PolicyApprover.approve(name, args) -> bool``, the shape
     codingHarness uses). Arguments it can't parse are always denied. A payment
     plan inside the plan policy is approved; one outside it is denied, which
     here does not mean "no": a live call can't wait for a person, so the plan
     is saved as ``pending`` and the caller hears that a specialist will confirm.

The tools:
  ``lookup_account()``      this call's debt; only in DISCUSS or WRAP once the
                            person is verified and the disclosure was said
  ``offer_payment_plan``    down payment, number of installments and frequency,
                            checked against the plan policy
  ``schedule_callback``     a local date and time plus a time zone, checked with
                            ``compliance.hours``; not after a cease
  ``log_dispute(reason)``   always allowed; afterwards offers and callbacks are
                            blocked for the rest of the call, even if saving or
                            auditing it failed. From the verified debtor it also
                            marks the debt disputed, which lasts until a person
                            sets the status back. Before verification it is saved
                            for a person to review, and its result never says
                            anything about a debt.
  ``escalate_to_human``     always allowed; saves a request for a person
  ``end_call(reason)``      always allowed; moves ``ctx.state`` to WRAP, which
                            the caller still has to save
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from compliance.decision import Decision, allow, block
from compliance.flags import Flag, check_cease
from compliance.hours import Contact, check_calling_hours
from harness.call_state import CallState, Debtor, Stage
from harness.tools import Tool, ToolRegistry

AuditWriter = Callable[[Decision, str, str | None], None]  # (decision, call_id, debt_id)

GATE_RULE = "call_tool_gate"
APPROVER_RULE = "call_tool_approver"
# Stages where the person is known to be the debtor and has heard the disclosure.
TALK_STAGES = (Stage.DISCUSS, Stage.WRAP)
FREQUENCIES = {"weekly": 7, "biweekly": 14, "monthly": 30}  # days between installments
DAYS_PER_MONTH = 365.25 / 12  # so 52 weekly, 26 biweekly and 12 monthly all fit 12 months
MAX_CALLBACK_DAYS = 30

PENDING_PLAN = (
    "pending: this plan is outside what I can approve on the call. A specialist will "
    "confirm it with you; nothing is agreed until they do."
)
DISPUTE_LOGGED = (
    "ok: the dispute is recorded. Collection on this debt stops until it has been reviewed."
)
# Before verification the person may not be the debtor, so the result must not
# say there is a debt.
DISPUTE_NOTED = "ok: noted. A specialist will look into it and follow up."
# Said when the dispute could not be saved. The call still treats the debt as
# disputed, and the error tells the model not to promise anything else.
DISPUTE_NOT_SAVED = (
    "error: this could not be saved ({exc}). Offers stay blocked on this call; tell the "
    "person a specialist will follow up."
)
ESCALATED = "ok: a specialist has been asked to follow up with this person."
BLOCKED_BY_DISPUTE = "error: blocked by dispute"


@dataclass(frozen=True)
class PlanPolicy:
    """What a plan needs to be approved on the call, without a person."""

    min_down_pct: int = 10  # down payment, as a percent of the balance
    max_months: int = 12  # the last installment within this many months
    max_installments: int = 52


@dataclass(frozen=True)
class PaymentPlan:
    down_cents: int
    installments: int
    frequency: str
    installment_cents: int  # each installment after the down payment, rounded down
    last_installment_cents: int  # the last one also takes the leftover cents

    @property
    def total_cents(self) -> int:
        return (
            self.down_cents
            + self.installment_cents * (self.installments - 1)
            + (self.last_installment_cents)
        )


class CallStore(Protocol):
    """Where the tools' rows go. ``PgCallStore`` is the database; ``MemoryCallStore``
    keeps them in a list, for demos and offline runs.

    ``save_dispute`` must have made the dispute durable when it returns, because
    the tool then tells the caller it is recorded. ``verified`` says whether it
    came from the verified debtor; only then is the debt itself marked disputed.
    ``is_disputed`` is true if the debt is marked disputed, or a dispute was
    logged on this call by anyone.
    """

    def save_plan(self, ctx: CallContext, plan: PaymentPlan, status: str) -> None: ...
    def save_callback(self, ctx: CallContext, at: datetime, tz: str) -> None: ...
    def save_dispute(self, ctx: CallContext, reason: str, verified: bool) -> None: ...
    def save_escalation(self, ctx: CallContext, reason: str) -> None: ...
    def is_disputed(self, ctx: CallContext) -> bool: ...


@dataclass
class MemoryCallStore:
    rows: list[dict[str, Any]] = field(default_factory=list)
    disputed_debts: set[str] = field(default_factory=set)  # stands in for debt.status

    def _add(self, kind: str, ctx: CallContext, **data: Any) -> None:
        self.rows.append({"kind": kind, "call_id": ctx.call_id, "debt_id": ctx.debt_id, **data})

    def save_plan(self, ctx: CallContext, plan: PaymentPlan, status: str) -> None:
        self._add("payment_plan", ctx, status=status, **asdict(plan))

    def save_callback(self, ctx: CallContext, at: datetime, tz: str) -> None:
        self._add("callback", ctx, at=at.isoformat(), tz=tz)

    def save_dispute(self, ctx: CallContext, reason: str, verified: bool) -> None:
        self._add("dispute", ctx, reason=reason, verified=verified)
        if verified:
            self.disputed_debts.add(ctx.debt_id)

    def save_escalation(self, ctx: CallContext, reason: str) -> None:
        self._add("escalation", ctx, reason=reason)

    def is_disputed(self, ctx: CallContext) -> bool:
        return ctx.debt_id in self.disputed_debts or any(
            r["kind"] == "dispute" and r["call_id"] == ctx.call_id for r in self.rows
        )


@dataclass
class CallContext:
    """One call's facts and handles. ``state`` and ``disputed`` change as tools run.

    A context is built fresh for each caller turn, so ``disputed`` only covers
    this turn; ``CallTools`` also asks the store, which is what lasts.
    """

    debtor: Debtor
    state: CallState
    store: CallStore
    audit: AuditWriter
    contact: Contact
    flags: list[Flag] | None  # None: not loaded, so the cease check blocks
    policy: PlanPolicy = field(default_factory=PlanPolicy)
    disputed: bool = False
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    @property
    def call_id(self) -> str:
        return self.state.call_id

    @property
    def debt_id(self) -> str:
        return self.debtor.debt_id

    def record(self, decision: Decision) -> None:
        self.audit(decision, self.call_id, self.debt_id)


# --- parsing the model's arguments -------------------------------------------


class BadArgs(ValueError):
    pass


def _as_dict(args: str | dict | None) -> dict:
    if isinstance(args, dict):
        return args
    try:
        parsed = json.loads(args or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise BadArgs(f"not valid JSON ({exc})") from exc
    if not isinstance(parsed, dict):
        raise BadArgs("not a JSON object")
    return parsed


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise BadArgs(f"{name} must be a whole number")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise BadArgs(f"{name} must be a whole number")


def parse_plan(args: str | dict | None, balance_cents: int) -> PaymentPlan:
    a = _as_dict(args)
    if set(a) != {"down_cents", "installments", "frequency"}:
        raise BadArgs("expected down_cents, installments and frequency")
    down = _int(a["down_cents"], "down_cents")
    count = _int(a["installments"], "installments")
    frequency = str(a["frequency"]).strip().lower()
    if frequency not in FREQUENCIES:
        raise BadArgs(f"frequency must be one of {', '.join(FREQUENCIES)}")
    if count < 1 or down < 0 or down > balance_cents:
        raise BadArgs("installments must be at least 1 and down_cents within the balance")
    # Round down and put the leftover cents on the last installment, so the plan
    # adds up to exactly the balance, never a cent more.
    each, leftover = divmod(balance_cents - down, count)
    return PaymentPlan(down, count, frequency, each, each + leftover)


def parse_callback(args: str | dict | None) -> tuple[datetime, str]:
    """``datetime`` is local wall time (no offset); ``tz`` is an IANA zone name."""
    a = _as_dict(args)
    if set(a) != {"datetime", "tz"}:
        raise BadArgs("expected datetime and tz")
    try:
        zone = ZoneInfo(str(a["tz"]))
        local = datetime.fromisoformat(str(a["datetime"]))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise BadArgs(f"bad datetime or tz ({exc})") from exc
    if local.tzinfo is not None:
        raise BadArgs("datetime is local time; give the zone in tz")
    return local.replace(tzinfo=zone), str(a["tz"])


def _reason(args: str | dict | None) -> str:
    a = _as_dict(args)
    if set(a) != {"reason"} or not str(a["reason"]).strip():
        raise BadArgs("expected a non-empty reason")
    return str(a["reason"]).strip()


_PARSERS: dict[str, Callable[[CallContext, Any], object]] = {
    "lookup_account": lambda ctx, a: None if _as_dict(a) == {} else _fail("takes no arguments"),
    "offer_payment_plan": lambda ctx, a: parse_plan(a, ctx.debtor.balance_cents),
    "schedule_callback": lambda ctx, a: parse_callback(a),
    "log_dispute": lambda ctx, a: _reason(a),
    "escalate_to_human": lambda ctx, a: _reason(a),
    "end_call": lambda ctx, a: _reason(a),
}


def _fail(why: str) -> None:
    raise BadArgs(why)


# --- the approver -------------------------------------------------------------


class PolicyApprover:
    """``approve(name, args) -> bool``, deciding from the plan policy alone.

    Unknown tools and arguments that don't parse are always denied. Payment
    plans are approved only inside the policy band. Every answer is audited.
    A lock makes it safe to share between threads, as in codingHarness.
    """

    def __init__(self, ctx: CallContext) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()

    def approve(self, name: str, args: str | dict | None) -> bool:
        with self._lock:
            decision = self._decide(name, args)
            self.ctx.record(decision)
            return decision.allow

    def _decide(self, name: str, args: str | dict | None) -> Decision:
        evidence: dict[str, Any] = {"tool": name}
        parser = _PARSERS.get(name)
        if parser is None:
            return block(APPROVER_RULE, f"unknown tool {name!r}", evidence)
        try:
            parsed = parser(self.ctx, args)
        except BadArgs as exc:
            return block(APPROVER_RULE, f"arguments not understood: {exc}", evidence)
        if isinstance(parsed, PaymentPlan):
            evidence["plan"] = asdict(parsed)
            evidence["policy"] = asdict(self.ctx.policy)
            if why := outside_policy(parsed, self.ctx.debtor.balance_cents, self.ctx.policy):
                return block(APPROVER_RULE, f"outside plan policy: {why}", evidence)
            return allow(APPROVER_RULE, "inside plan policy", evidence)
        return allow(APPROVER_RULE, "arguments understood", evidence)


def outside_policy(plan: PaymentPlan, balance_cents: int, policy: PlanPolicy) -> str | None:
    """Why ``plan`` needs a person, or None if it's inside the policy."""
    if plan.down_cents * 100 < balance_cents * policy.min_down_pct:
        return f"down payment under {policy.min_down_pct}% of the balance"
    if plan.installments > policy.max_installments:
        return f"more than {policy.max_installments} installments"
    days = plan.installments * FREQUENCIES[plan.frequency]
    months = plan.installments if plan.frequency == "monthly" else days / DAYS_PER_MONTH
    if months > policy.max_months:
        return f"longer than {policy.max_months} months"
    return None


# --- the tools ----------------------------------------------------------------


class CallTools:
    """The tools for one call, bound to its ``CallContext`` and approver."""

    def __init__(self, ctx: CallContext, approver: PolicyApprover | None = None) -> None:
        self.ctx = ctx
        self.approver = approver or PolicyApprover(ctx)

    def disputed(self) -> bool:
        """Disputed on this turn or before. If the store can't say, assume it is."""
        if self.ctx.disputed:
            return True
        try:
            return bool(self.ctx.store.is_disputed(self.ctx))
        except Exception:  # noqa: BLE001 - unknown is treated as disputed (fail closed)
            return True

    # The compliance gate: first, before the approver sees anything.
    def gate(self, name: str, when: datetime | None = None) -> Decision:
        ctx, s = self.ctx, self.ctx.state
        evidence: dict[str, Any] = {
            "tool": name,
            "stage": s.stage.value,
            "verified": s.verified,
            "disclosed": s.disclosed,
            "disputed": self.disputed(),
        }
        if name in ("log_dispute", "escalate_to_human", "end_call"):
            return allow(GATE_RULE, f"{name} is always allowed", evidence)
        if s.stage not in TALK_STAGES or not (s.verified and s.disclosed):
            return block(GATE_RULE, f"not verified and disclosed (stage {s.stage.value})", evidence)
        if name == "lookup_account":
            return allow(GATE_RULE, "verified and disclosed", evidence)
        if evidence["disputed"]:
            return block(GATE_RULE, "dispute", evidence)
        cease = check_cease(ctx.flags, ctx.debt_id, ctx.contact.phone_e164)
        if not cease.allow:
            return block(GATE_RULE, f"cease: {cease.reason}", evidence | cease.evidence)
        if name == "schedule_callback" and when is not None:
            now = ctx.clock()
            if not now < when <= now + timedelta(days=MAX_CALLBACK_DAYS):
                return block(
                    GATE_RULE, f"callback not in the next {MAX_CALLBACK_DAYS} days", evidence
                )
            hours = check_calling_hours(ctx.contact, when)
            if not hours.allow:
                return block(GATE_RULE, f"calling hours: {hours.reason}", evidence | hours.evidence)
        return allow(GATE_RULE, "allowed", evidence)

    def _gated(self, name: str, when: datetime | None = None) -> str | None:
        """The error to return if the gate blocks, after auditing the decision."""
        decision = self.gate(name, when)
        self.ctx.record(decision)
        if decision.allow:
            return None
        if decision.reason == "dispute":
            return BLOCKED_BY_DISPUTE
        return f"error: blocked ({decision.reason})"

    def lookup_account(self) -> str:
        if err := self._gated("lookup_account"):
            return err
        if not self.approver.approve("lookup_account", {}):
            return "error: not approved"
        d = self.ctx.debtor
        return json.dumps(
            {
                "creditor": d.creditor,
                "balance": f"{d.balance_cents // 100}.{d.balance_cents % 100:02d}",
                "currency": "USD",
                "disputed": self.disputed(),
            }
        )

    def offer_payment_plan(self, down_cents: str, installments: str, frequency: str) -> str:
        if err := self._gated("offer_payment_plan"):
            return err
        args = {"down_cents": down_cents, "installments": installments, "frequency": frequency}
        try:
            plan = parse_plan(args, self.ctx.debtor.balance_cents)
        except BadArgs as exc:
            self.approver.approve("offer_payment_plan", args)  # audited as denied
            return f"error: offer_payment_plan arguments not understood: {exc}"
        if self.approver.approve("offer_payment_plan", args):
            self.ctx.store.save_plan(self.ctx, plan, "approved")
            return "approved: " + json.dumps(asdict(plan) | {"total_cents": plan.total_cents})
        self.ctx.store.save_plan(self.ctx, plan, "pending")
        return PENDING_PLAN

    def schedule_callback(self, datetime: str, tz: str) -> str:  # noqa: A002 - the tool's own name
        args = {"datetime": datetime, "tz": tz}
        try:
            when, zone = parse_callback(args)
        except BadArgs as exc:
            if err := self._gated("schedule_callback"):
                return err
            self.approver.approve("schedule_callback", args)  # audited as denied
            return f"error: schedule_callback arguments not understood: {exc}"
        if err := self._gated("schedule_callback", when):
            return err
        if not self.approver.approve("schedule_callback", args):
            return "error: not approved"
        self.ctx.store.save_callback(self.ctx, when, zone)
        return f"ok: callback booked for {when:%Y-%m-%d %H:%M} {zone}"

    def log_dispute(self, reason: str) -> str:
        # The flag goes up first, so nothing below (a failed audit write, a failed
        # save) can leave offers open after the person said they dispute the debt.
        self.ctx.disputed = True
        problems: list[str] = []
        try:
            self._gated("log_dispute")  # always allows; called for its audit row
            # The reason is saved even if the approver can't parse it.
            self.approver.approve("log_dispute", {"reason": reason})
        except Exception as exc:  # noqa: BLE001 - auditing must not lose the dispute
            problems.append(f"audit failed: {exc!r}")
        verified = self.ctx.state.verified
        try:
            self.ctx.store.save_dispute(self.ctx, reason.strip() or "(no reason given)", verified)
        except Exception as exc:  # noqa: BLE001 - say so, and stay blocked
            return DISPUTE_NOT_SAVED.format(exc=repr(exc))
        said = DISPUTE_LOGGED if verified else DISPUTE_NOTED
        if problems:
            return said + " (" + "; ".join(problems) + ")"
        return said

    def escalate_to_human(self, reason: str) -> str:
        if err := self._gated("escalate_to_human"):
            return err
        self.approver.approve("escalate_to_human", {"reason": reason})
        self.ctx.store.save_escalation(self.ctx, reason.strip() or "(no reason given)")
        return ESCALATED

    def end_call(self, reason: str) -> str:
        if err := self._gated("end_call"):
            return err
        self.approver.approve("end_call", {"reason": reason})
        if self.ctx.state.stage != Stage.STOPPED:
            self.ctx.state = replace(self.ctx.state, stage=Stage.WRAP)
        return "ok: ending the call"

    def registry(self) -> ToolRegistry:
        reason = {"reason": "One short line saying why."}
        return ToolRegistry(
            [
                Tool(
                    "lookup_account",
                    "The creditor and balance of the debt this call is about.",
                    {},
                    self.lookup_account,
                ),
                Tool(
                    "offer_payment_plan",
                    "Offer a payment plan: a down payment, then equal installments.",
                    {
                        "down_cents": "Down payment in cents, for example '12500'.",
                        "installments": "How many installments after the down payment.",
                        "frequency": "weekly, biweekly or monthly.",
                    },
                    self.offer_payment_plan,
                ),
                Tool(
                    "schedule_callback",
                    "Book a time to call the person back.",
                    {
                        "datetime": "Local date and time, for example '2026-11-20T10:30'.",
                        "tz": "IANA time zone, for example 'America/Los_Angeles'.",
                    },
                    self.schedule_callback,
                ),
                Tool(
                    "log_dispute",
                    "Record that the person disputes the debt.",
                    reason,
                    self.log_dispute,
                ),
                Tool(
                    "escalate_to_human",
                    "Ask a specialist to follow up.",
                    reason,
                    self.escalate_to_human,
                ),
                Tool("end_call", "End the call politely.", reason, self.end_call),
            ]
        )


def call_tools(ctx: CallContext, approver: PolicyApprover | None = None) -> ToolRegistry:
    """The tool registry for one call."""
    return CallTools(ctx, approver).registry()


# --- the database -------------------------------------------------------------


class PgCallStore:
    """Writes the tools' rows to Postgres on an open connection.

    The caller commits, except for a dispute: ``save_dispute`` commits before it
    returns, because the caller is about to hear that it was recorded. That
    commit also commits anything written earlier on the same connection.
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def save_plan(self, ctx: CallContext, plan: PaymentPlan, status: str) -> None:
        self.conn.execute(
            "insert into public.payment_plan (call_id, debt_id, down_cents, installments,"
            " frequency, installment_cents, last_installment_cents, status)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                ctx.call_id,
                ctx.debt_id,
                plan.down_cents,
                plan.installments,
                plan.frequency,
                plan.installment_cents,
                plan.last_installment_cents,
                status,
            ),
        )

    def save_callback(self, ctx: CallContext, at: datetime, tz: str) -> None:
        self.conn.execute(
            "insert into public.callback (call_id, debt_id, scheduled_at, tz)"
            " values (%s, %s, %s, %s)",
            (ctx.call_id, ctx.debt_id, at, tz),
        )

    def save_dispute(self, ctx: CallContext, reason: str, verified: bool) -> None:
        # A savepoint, so a failure here leaves the connection usable.
        with self.conn.transaction():
            self.conn.execute(
                "insert into public.dispute (call_id, debt_id, reason, verified)"
                " values (%s, %s, %s, %s)",
                (ctx.call_id, ctx.debt_id, reason, verified),
            )
            if verified:  # later turns and later calls see it, until a person clears it
                self.conn.execute(
                    "update public.debt set status = 'disputed' where id = %s", (ctx.debt_id,)
                )
        self.conn.commit()

    def is_disputed(self, ctx: CallContext) -> bool:
        row = self.conn.execute(
            "select d.status = 'disputed' or exists"
            " (select 1 from public.dispute x where x.debt_id = d.id and x.call_id = %s)"
            " from public.debt d where d.id = %s",
            (ctx.call_id, ctx.debt_id),
        ).fetchone()
        return row is None or bool(row[0])  # no such debt: treat as disputed

    def save_escalation(self, ctx: CallContext, reason: str) -> None:
        self.conn.execute(
            "insert into public.escalation (call_id, debt_id, reason) values (%s, %s, %s)",
            (ctx.call_id, ctx.debt_id, reason),
        )


def load_plan_policy(conn: Any, creditor: str) -> PlanPolicy:
    """The creditor's policy row, else the default row (creditor null), else code's default."""
    row = conn.execute(
        "select min_down_pct, max_months, max_installments from public.plan_policy"
        " where creditor = %s or creditor is null order by creditor nulls last limit 1",
        (creditor,),
    ).fetchone()
    return PlanPolicy(*row) if row else PlanPolicy()


def audit_to_conn(conn: Any) -> AuditWriter:
    """An audit writer that adds ``call_tool`` rows on ``conn``.

    Each row is written in its own savepoint. The caller commits, unless no
    transaction was open, in which case psycopg commits the row at once.
    """
    from compliance.audit import record

    def write(decision: Decision, call_id: str, debt_id: str | None) -> None:
        # A savepoint, so a failed audit write can't spoil the rest of the turn's writes.
        with conn.transaction():
            record(conn, decision, kind="call_tool", call_id=call_id, debt_id=debt_id)

    return write
