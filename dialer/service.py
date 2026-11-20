"""The dialer: the one safe way every call is placed.

``dial_one(conn, account_number, when, phone)`` places one call for one debt
(the account number names the debt, and so its debtor). It is the only code
that calls ``make_call``: the batch queue below uses it, and so will a later
``place_call`` tool. For that debt it does:

  1. **Check and reserve, in one transaction.** Lock the debt's row
     (``select ... for update``), so a second runner working on the same debt
     waits here. Then load the facts, run every pre-call check
     (``check_before_call``) and record each decision in the audit log. If all
     allow, insert the ``call_attempt`` row (outcome ``placing``) and commit.
     Because the 7-in-7 count is read under the lock, two runners can't both
     see "6 calls" and both place the 8th.
  2. **Blocked:** nothing is dialed. The result keeps the reasons.
  3. **Dial once.** ``phone.make_call`` is called exactly one time. On success
     the AgentPhone call id goes on the attempt (``placed``). On a
     ``CallError`` (429, 409, 402) the attempt gets that outcome. On anything
     else, such as a timeout, the attempt becomes ``placed_unknown``: the call
     may have rung, so we never try again by ourselves.

Besides the pre-call checks, only a debt whose status is ``active`` may be
called (``check_debt_status``), read under the same lock.

``dial_queue(debtors, when, phone, conn)`` calls ``dial_one`` for each of
``debtors`` (account numbers) in a fixed order: never-called debts first, then
the one whose last attempt is oldest, then by account number. It stops early
when AgentPhone's answer means every call would fail (``STOP_OUTCOMES``), and
lists the debts it didn't try.

The decision to call is always code. There is no model anywhere in here.

``plan_queue`` is the dry run: the same order and the same checks, but
nothing is written and nothing is dialed.

With ``max_parallel`` above 1, debts are worked on in threads, each with its
own database connection from ``connect``; the row lock keeps them safe.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psycopg

from compliance import Decision, allow, block, check_before_call, may_call
from compliance.audit import record
from harness.db import connect as connect_db
from harness.db import load_call_facts
from harness.precall import check_and_record
from phone.client import CallError, PhoneClient

# AgentPhone refusal -> call_attempt.outcome
REFUSED_OUTCOME = {
    "throttled": "throttled",
    "concurrency": "concurrency_limit",
    "busy": "number_busy",
    "no_funds": "no_funds",
    "other": "failed",
}

log = logging.getLogger(__name__)

# Outcomes that say something is wrong for every call, not just this one (no
# funds, too many calls, our number busy, or no answer from AgentPhone). The
# queue stops on these, so the rest of the debts don't each lose an attempt
# from their 7-in-7 allowance to a call that never rang.
STOP_OUTCOMES = frozenset(
    {"throttled", "concurrency_limit", "number_busy", "no_funds", "placed_unknown"}
)

LOCK_DEBT_SQL = "select id::text, status from public.debt where account_number = %s for update"
STATUS_SQL = "select status from public.debt where account_number = %s"

PHONE_ID_SQL = """
    select ph.id::text
    from public.debt d
    join public.phone ph on ph.debtor_id = d.debtor_id
    where d.id = %s and ph.e164 = %s
"""

INSERT_ATTEMPT_SQL = """
    insert into public.call_attempt (debt_id, phone_id, placed_at, outcome)
    values (%s, %s, %s, 'placing')
    returning id::text
"""

MARK_SQL = """
    update public.call_attempt set outcome = %s, agentphone_call_id = %s
    where id = %s
"""

# Last attempt per account, for the queue order. Never-called debts sort first.
ORDER_SQL = """
    select d.account_number, max(a.placed_at)
    from public.debt d
    left join public.call_attempt a on a.debt_id = d.id
    where d.account_number = any(%s)
    group by d.account_number
"""

ACTIVE_SQL = """
    select d.account_number
    from public.debt d
    join public.debtor p on p.id = d.debtor_id
    where d.status = 'active' and p.archived_at is null
    order by 1
"""


@dataclass(frozen=True)
class DialOutcome:
    account_number: str
    decisions: list[Decision]
    attempt_id: str | None = None
    outcome: str | None = None  # call_attempt.outcome after dialing; None if blocked
    call_id: str | None = None
    to_number: str | None = None

    @property
    def blocked(self) -> bool:
        return not may_call(self.decisions)

    @property
    def reasons(self) -> list[str]:
        return [f"{d.rule_id}: {d.reason}" for d in self.decisions if not d.allow]


@dataclass
class DialResult:
    outcomes: list[DialOutcome] = field(default_factory=list)
    stopped: str | None = None  # why the queue stopped early, if it did
    not_tried: list[str] = field(default_factory=list)  # accounts left undialed

    @property
    def placed(self) -> list[DialOutcome]:
        return [o for o in self.outcomes if o.outcome == "placed"]

    @property
    def blocked(self) -> list[DialOutcome]:
        return [o for o in self.outcomes if o.blocked]


def active_accounts(conn: psycopg.Connection) -> list[str]:
    return [r[0] for r in conn.execute(ACTIVE_SQL).fetchall()]


def queue_order(conn: psycopg.Connection, debtors: Sequence[str]) -> list[str]:
    """``debtors`` without repeats, never-called first, then oldest last attempt first."""
    wanted = list(dict.fromkeys(debtors))
    last: dict[str, datetime | None] = dict(conn.execute(ORDER_SQL, (wanted,)).fetchall())

    def key(account: str) -> tuple[int, str, str]:
        t = last.get(account)
        return (0 if t is None else 1, t.isoformat() if t else "", account)

    return sorted(wanted, key=key)


def check_debt_status(status: str | None) -> Decision:
    """Only an ``active`` debt may be called; disputed, paused or closed ones may not."""
    rule = "debt_status"
    if status is None:
        return block(rule, "no such debt", {})
    if status != "active":
        return block(rule, f"the debt is {status}, not active", {"status": status})
    return allow(rule, "the debt is active", {"status": status})


@dataclass(frozen=True)
class Reservation:
    decisions: list[Decision]
    attempt_id: str | None = None
    to_number: str | None = None


def reserve(conn: psycopg.Connection, account_number: str, when: datetime) -> Reservation:
    """Lock the debt, run and record every check, and insert the attempt if all allow.

    Runs in the caller's transaction and commits nothing; ``reserve_attempt``
    wraps it in its own.
    """
    locked = conn.execute(LOCK_DEBT_SQL, (account_number,)).fetchone()
    decisions = check_and_record(conn, account_number, when)
    if locked is not None:  # no debt at all is already blocked by the checks
        debt_id, status = locked
        decision = check_debt_status(status)
        record(conn, decision, kind="gate", debt_id=debt_id)
        decisions = [*decisions, decision]
    if not may_call(decisions):
        return Reservation(decisions)

    facts = load_call_facts(account_number, conn)
    assert facts is not None and facts.phone is not None  # the checks above passed
    to_number = facts.phone.e164
    row = conn.execute(PHONE_ID_SQL, (facts.debt_id, to_number)).fetchone()
    if row is None:  # the phone vanished between the checks and here
        gone = block("precall", "phone record not found when reserving the call", {})
        record(conn, gone, kind="gate", debt_id=facts.debt_id)
        return Reservation([*decisions, gone])
    inserted = conn.execute(INSERT_ATTEMPT_SQL, (facts.debt_id, row[0], when)).fetchone()
    assert inserted is not None
    return Reservation(decisions, attempt_id=inserted[0], to_number=to_number)


def _require_idle(conn: psycopg.Connection) -> None:
    # Inside an open transaction, conn.transaction() only makes a savepoint, so
    # the attempt would not be committed before dialing.
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("conn must not be inside a transaction")


def reserve_attempt(conn: psycopg.Connection, account_number: str, when: datetime) -> Reservation:
    """``reserve`` in one transaction of its own, committed before any dialing."""
    _require_idle(conn)
    with conn.transaction():
        return reserve(conn, account_number, when)


def _mark(conn: psycopg.Connection, attempt_id: str, outcome: str, call_id: str | None) -> None:
    with conn.transaction():
        conn.execute(MARK_SQL, (outcome, call_id, attempt_id))


def dial_one(
    conn: psycopg.Connection,
    account_number: str,
    when: datetime | None,
    phone: PhoneClient,
) -> DialOutcome:
    """Check, record, then dial ``account_number`` at most once.

    ``when=None`` means the moment this debt is checked, so in a long live run
    each debt is checked right before its own call. ``conn`` must not be inside
    a transaction, so the attempt is committed before the phone rings.
    """
    r = reserve_attempt(conn, account_number, when or datetime.now(UTC))
    if r.attempt_id is None or r.to_number is None:
        return DialOutcome(account_number, r.decisions)

    def done(outcome: str, call_id: str | None = None) -> DialOutcome:
        return DialOutcome(
            account_number,
            r.decisions,
            attempt_id=r.attempt_id,
            outcome=outcome,
            call_id=call_id,
            to_number=r.to_number,
        )

    # TODO(recording consent): no recording-consent decision exists yet, so we
    # leave AgentPhone's default. Once one does, pass disable_recording from it.
    try:
        call_id = phone.make_call(r.to_number, disable_recording=None)
    except CallError as exc:
        outcome = REFUSED_OUTCOME[exc.kind]
        _mark(conn, r.attempt_id, outcome, None)
        return done(outcome)
    except Exception:
        # Timeout or a lost reply: the call may have rung. Never retry.
        _mark(conn, r.attempt_id, "placed_unknown", None)
        return done("placed_unknown")
    try:
        _mark(conn, r.attempt_id, "placed", call_id)
    except Exception:
        # The call is ringing but we couldn't save its id. Say it loudly, so a
        # person can attach it to the attempt (still "placing", so it counts).
        log.error("call %s placed for attempt %s, but saving its id failed", call_id, r.attempt_id)
        raise
    return done("placed", call_id)


def dial_queue(
    debtors: Sequence[str],
    when: datetime | None,
    phone: PhoneClient,
    conn: psycopg.Connection,
    *,
    max_parallel: int = 1,
    connect: Callable[[], psycopg.Connection] | None = None,
) -> DialResult:
    """Call each of ``debtors`` that every check allows, once, in queue order.

    Pass ``when=None`` for a live run: each debt is then checked at the moment
    it comes up, not at the time the run started.
    """
    if max_parallel < 1:
        raise ValueError("max_parallel must be at least 1")
    _require_idle(conn)
    order = queue_order(conn, debtors)
    conn.commit()  # end that read, so each debt gets a transaction of its own

    result = DialResult()
    stop = threading.Event()
    lock = threading.Lock()

    def finish(outcome: DialOutcome) -> None:
        with lock:
            result.outcomes.append(outcome)
            if outcome.outcome in STOP_OUTCOMES and result.stopped is None:
                result.stopped = f"{outcome.account_number} came back {outcome.outcome}"
                stop.set()

    if max_parallel == 1:
        for account in order:
            if stop.is_set():
                break
            finish(dial_one(conn, account, when, phone))
    else:
        opener = connect or connect_db

        def work(account: str) -> None:
            if stop.is_set():
                return
            try:
                with opener() as own:
                    finish(dial_one(own, account, when, phone))
            except BaseException:
                stop.set()  # a crash in one worker stops the others from dialing
                raise

        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            for future in [pool.submit(work, a) for a in order]:
                future.result()

    done = {o.account_number for o in result.outcomes}
    result.not_tried = [a for a in order if a not in done]
    result.outcomes.sort(key=lambda o: order.index(o.account_number))
    return result


def plan_queue(debtors: Sequence[str], when: datetime, conn: psycopg.Connection) -> DialResult:
    """The dry run: who would be called and why the rest are blocked. Writes nothing."""
    order = queue_order(conn, debtors)
    outcomes = []
    for account in order:
        facts = load_call_facts(account, conn)
        decisions = check_before_call(facts, when)
        if facts is not None:
            row = conn.execute(STATUS_SQL, (account,)).fetchone()
            decisions = [*decisions, check_debt_status(row[0] if row else None)]
        to_number = facts.phone.e164 if facts and facts.phone else None
        outcome = "would_call" if may_call(decisions) else None
        outcomes.append(
            DialOutcome(
                account,
                decisions,
                outcome=outcome,
                to_number=to_number,
            )
        )
    return DialResult(outcomes)
