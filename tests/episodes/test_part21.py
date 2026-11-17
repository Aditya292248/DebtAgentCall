"""Call tools and approvals: the compliance gate first, then the approver.

What it checks:
  - ``lookup_account`` before the person is verified returns ``error: blocked``
    and no account data, in GREETING, VERIFY, THIRD_PARTY and a failed-verify WRAP
  - once verified and disclosed it returns this call's debt, and it takes no
    account id or name from the model
  - a plan inside the policy is approved; one outside it is saved as pending,
    with the fixed "a specialist will confirm" line, and is not approved
  - arguments the approver can't parse are always denied, and audited
  - after ``log_dispute`` offers and callbacks return ``error: blocked by dispute``,
    on later turns too (a fresh context that reads the store), and even when
    saving the dispute or writing its audit row failed
  - 52 weekly, 26 biweekly and 12 monthly installments all count as 12 months
  - a plan always adds up to exactly the balance, never a cent more
  - a dispute before verification is saved for review, does not mark the debt,
    and its result says nothing about a debt; a dispute is not forever: once
    the debt's status is cleared, a later call can offer plans again
  - a callback outside calling hours, or after a cease flag, is blocked
  - every gate and approver decision is written to the audit log
  - against the real local database (``db``): the rows and audit rows are written
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from compliance import Contact, Flag
from compliance.decision import Decision
from harness.call_state import CallState, Debtor, Stage
from harness.call_tools import (
    BLOCKED_BY_DISPUTE,
    DISPUTE_LOGGED,
    DISPUTE_NOT_SAVED,
    DISPUTE_NOTED,
    PENDING_PLAN,
    CallContext,
    CallTools,
    MemoryCallStore,
    PlanPolicy,
    PolicyApprover,
    call_tools,
)
from harness.tools import run_tool

# Sam as supabase/seed.sql has them: $1,250.00 with Northwind Furniture, in Los Angeles.
SAM = Debtor(
    debtor_id="00000000-0000-4000-8000-000000000001",
    debt_id="00000000-0000-4000-8000-00000000a001",
    first_name="Sam",
    last_name="Doe",
    dob=date(1985, 4, 12),
    zip="90012",
    creditor="Northwind Furniture",
    balance_cents=125000,
)
NOW = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)  # 10:00 in Los Angeles


def verified(state: CallState) -> CallState:
    return replace(state, stage=Stage.DISCUSS, verified=True, disclosed=True)


NO_FLAGS: list[Flag] = []


def make_ctx(
    *, stage_fn=verified, flags: list[Flag] | None = NO_FLAGS
) -> tuple[CallContext, list[Decision], MemoryCallStore]:
    audited: list[Decision] = []
    store = MemoryCallStore()
    ctx = CallContext(
        debtor=SAM,
        state=stage_fn(CallState.new("call-part21", SAM)),
        store=store,
        audit=lambda d, call_id, debt_id: audited.append(d),
        contact=Contact(zip=SAM.zip, phone_e164="+12135550101"),
        flags=flags,
        clock=lambda: NOW,
    )
    return ctx, audited, store


def call(ctx: CallContext, tool: str, **args: str) -> str:
    return run_tool(call_tools(ctx), tool, json.dumps(args))


# --- lookup_account -----------------------------------------------------------


@pytest.mark.parametrize(
    "state_fn",
    [
        lambda s: s,  # GREETING
        lambda s: replace(s, stage=Stage.VERIFY),
        lambda s: replace(s, stage=Stage.THIRD_PARTY),
        lambda s: replace(s, stage=Stage.WRAP),  # verification failed
        lambda s: replace(s, stage=Stage.DISCUSS),  # stage moved but not verified
    ],
)
def test_lookup_before_verification_is_blocked_and_leaks_nothing(state_fn) -> None:
    ctx, audited, _ = make_ctx(stage_fn=state_fn)
    result = call(ctx, "lookup_account")
    assert result.startswith("error: blocked")
    for leak in ("1250", "1,250", "125000", "Northwind", "balance", SAM.debt_id):
        assert leak not in result
    assert [d.allow for d in audited] == [False]


def test_lookup_after_verification_returns_this_calls_debt() -> None:
    ctx, audited, _ = make_ctx()
    account = json.loads(call(ctx, "lookup_account"))
    assert account == {
        "creditor": "Northwind Furniture",
        "balance": "1250.00",
        "currency": "USD",
        "disputed": False,
    }
    assert [d.allow for d in audited] == [True, True]  # gate, then approver


def test_lookup_takes_no_id_or_name_from_the_model() -> None:
    ctx, _, _ = make_ctx()
    assert "unexpected argument" in call(ctx, "lookup_account", name="Jordan Lee")
    assert (
        call_tools(ctx).get("lookup_account").schema()["function"]["parameters"]["properties"] == {}
    )


# --- offer_payment_plan -------------------------------------------------------


def test_offer_inside_policy_is_auto_approved() -> None:
    ctx, _, store = make_ctx()
    result = call(
        ctx, "offer_payment_plan", down_cents="25000", installments="10", frequency="monthly"
    )
    assert result.startswith("approved: ")
    assert json.loads(result.removeprefix("approved: "))["installment_cents"] == 10000
    assert [r["status"] for r in store.rows] == ["approved"]


@pytest.mark.parametrize(
    ("down", "count", "freq"),
    [("5000", "10", "monthly"), ("25000", "18", "monthly"), ("25000", "60", "weekly")],
)
def test_offer_outside_policy_is_pending_not_approved(down: str, count: str, freq: str) -> None:
    ctx, audited, store = make_ctx()
    result = call(ctx, "offer_payment_plan", down_cents=down, installments=count, frequency=freq)
    assert result == PENDING_PLAN
    assert "approved" not in result
    assert [r["status"] for r in store.rows] == ["pending"]
    gate, approver = audited
    assert gate.allow and not approver.allow
    assert "outside plan policy" in approver.reason


def test_policy_is_read_from_the_context() -> None:
    ctx, _, store = make_ctx()
    ctx.policy = PlanPolicy(min_down_pct=50, max_months=3)
    assert (
        call(ctx, "offer_payment_plan", down_cents="25000", installments="2", frequency="monthly")
        == PENDING_PLAN
    )


# --- the approver -------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("offer_payment_plan", "{not json"),
        ("offer_payment_plan", "[1, 2]"),
        ("offer_payment_plan", {"down_cents": "lots", "installments": "3", "frequency": "monthly"}),
        ("offer_payment_plan", {"down_cents": "25000", "installments": "3", "frequency": "daily"}),
        (
            "offer_payment_plan",
            {"down_cents": "25000", "installments": "0", "frequency": "monthly"},
        ),
        ("offer_payment_plan", {"down_cents": "25000"}),
        ("schedule_callback", {"datetime": "tomorrow-ish", "tz": "America/Los_Angeles"}),
        ("schedule_callback", {"datetime": "2026-11-17T10:00", "tz": "Mars/Olympus"}),
        ("lookup_account", {"account_id": "ACC-1003"}),
        ("log_dispute", "not json"),
        ("delete_everything", {}),
    ],
)
def test_unparseable_args_are_denied_and_audited(name: str, args) -> None:
    ctx, audited, _ = make_ctx()
    assert PolicyApprover(ctx).approve(name, args) is False
    (decision,) = audited
    assert not decision.allow


def test_unparseable_offer_through_the_tool_writes_nothing() -> None:
    ctx, audited, store = make_ctx()
    result = call(
        ctx, "offer_payment_plan", down_cents="lots", installments="3", frequency="monthly"
    )
    assert result.startswith("error:")
    assert store.rows == []
    assert [d.allow for d in audited] == [True, False]


# --- log_dispute --------------------------------------------------------------


def test_offers_and_callbacks_are_blocked_after_a_dispute() -> None:
    ctx, _, store = make_ctx()
    tools = call_tools(ctx)
    assert run_tool(tools, "log_dispute", {"reason": "I already paid this"}) == DISPUTE_LOGGED
    assert ctx.disputed
    offer = {"down_cents": "25000", "installments": "10", "frequency": "monthly"}
    assert run_tool(tools, "offer_payment_plan", offer) == BLOCKED_BY_DISPUTE
    callback = {"datetime": "2026-11-17T10:00", "tz": "America/Los_Angeles"}
    assert run_tool(tools, "schedule_callback", callback) == BLOCKED_BY_DISPUTE
    assert [r["kind"] for r in store.rows] == ["dispute"]


@pytest.mark.parametrize("stage", [Stage.GREETING, Stage.VERIFY, Stage.THIRD_PARTY])
def test_dispute_before_verification_is_noted_without_naming_a_debt(stage: Stage) -> None:
    ctx, _, store = make_ctx(stage_fn=lambda s: replace(s, stage=stage))
    result = call(ctx, "log_dispute", reason="he doesn't owe anything")
    assert result == DISPUTE_NOTED
    for word in ("debt", "collect", "balance", "owe", SAM.creditor):
        assert word not in result.lower()
    assert store.rows[0]["kind"] == "dispute" and store.rows[0]["verified"] is False
    assert store.disputed_debts == set()  # a stranger can't mark the real debt disputed
    assert ctx.disputed  # but nothing more is offered on this call


def test_unverified_dispute_does_not_block_a_later_call() -> None:
    first, _, store = make_ctx(stage_fn=lambda s: replace(s, stage=Stage.THIRD_PARTY))
    call(first, "log_dispute", reason="not his")
    later, _, _ = make_ctx()
    later.store = store
    later.state = replace(later.state, call_id="call-part21-later")
    assert call(later, "offer_payment_plan", **OFFER).startswith("approved: ")


def test_a_cleared_dispute_no_longer_blocks_a_later_call() -> None:
    first, _, store = make_ctx()
    call(first, "log_dispute", reason="I already paid this")
    later, _, _ = make_ctx()
    later.store = store
    later.state = replace(later.state, call_id="call-part21-later")
    assert call(later, "offer_payment_plan", **OFFER) == BLOCKED_BY_DISPUTE
    store.disputed_debts.clear()  # a person reviewed it and set the debt back to active
    assert call(later, "offer_payment_plan", **OFFER).startswith("approved: ")


@pytest.mark.parametrize(
    ("down", "count"), [("12500", "7"), ("12501", "3"), ("125000", "1"), ("99999", "52")]
)
def test_plan_adds_up_to_exactly_the_balance(down: str, count: str) -> None:
    ctx, _, store = make_ctx()
    freq = "weekly" if count == "52" else "monthly"
    result = call(ctx, "offer_payment_plan", down_cents=down, installments=count, frequency=freq)
    plan = json.loads(result.removeprefix("approved: "))
    paid = plan["down_cents"] + plan["installment_cents"] * (plan["installments"] - 1)
    assert paid + plan["last_installment_cents"] == plan["total_cents"] == SAM.balance_cents
    assert 0 <= plan["last_installment_cents"] - plan["installment_cents"] < plan["installments"]


OFFER = {"down_cents": "25000", "installments": "10", "frequency": "monthly"}


def _boom(*_: object) -> None:
    raise RuntimeError("database down")


def test_offers_stay_blocked_when_saving_the_dispute_fails() -> None:
    ctx, _, store = make_ctx()
    store.save_dispute = _boom  # type: ignore[method-assign]
    tools = call_tools(ctx)
    result = run_tool(tools, "log_dispute", {"reason": "not mine"})
    assert result == DISPUTE_NOT_SAVED.format(exc=repr(RuntimeError("database down")))
    assert ctx.disputed
    assert run_tool(tools, "offer_payment_plan", OFFER) == BLOCKED_BY_DISPUTE
    assert store.rows == []


def test_dispute_is_saved_even_when_the_audit_write_fails() -> None:
    ctx, _, store = make_ctx()
    ctx.audit = lambda *_: _boom()
    result = call(ctx, "log_dispute", reason="not mine")
    assert result.startswith(DISPUTE_LOGGED) and "audit failed" in result
    assert [r["kind"] for r in store.rows] == ["dispute"]
    assert ctx.disputed


def test_dispute_blocks_offers_on_a_later_turn() -> None:
    first, _, store = make_ctx()
    assert call(first, "log_dispute", reason="I already paid this") == DISPUTE_LOGGED
    later, _, _ = make_ctx()  # the next caller turn builds a new context, same call id
    later.store = store
    assert not later.disputed
    assert call(later, "offer_payment_plan", **OFFER) == BLOCKED_BY_DISPUTE
    assert json.loads(call(later, "lookup_account"))["disputed"] is True


def test_offers_are_blocked_when_the_store_cant_say_if_disputed() -> None:
    ctx, _, store = make_ctx()
    store.is_disputed = _boom  # type: ignore[method-assign]
    assert call(ctx, "offer_payment_plan", **OFFER) == BLOCKED_BY_DISPUTE


@pytest.mark.parametrize(
    ("count", "freq", "approved"),
    [
        ("52", "weekly", True),
        ("26", "biweekly", True),
        ("12", "monthly", True),
        ("53", "weekly", False),
        ("27", "biweekly", False),
        ("13", "monthly", False),
    ],
)
def test_twelve_months_in_every_frequency(count: str, freq: str, approved: bool) -> None:
    ctx, _, _ = make_ctx()
    result = call(ctx, "offer_payment_plan", down_cents="25000", installments=count, frequency=freq)
    assert result.startswith("approved: ") is approved


# --- schedule_callback --------------------------------------------------------


def test_callback_inside_calling_hours_is_booked() -> None:
    ctx, _, store = make_ctx()
    result = call(ctx, "schedule_callback", datetime="2026-11-17T10:30", tz="America/Los_Angeles")
    assert result.startswith("ok: callback booked")
    assert store.rows[0]["kind"] == "callback"


@pytest.mark.parametrize(
    "local", ["2026-11-17T07:30", "2026-11-17T21:00", "2026-11-16T09:00", "2027-02-01T10:00"]
)
def test_callback_outside_hours_or_window_is_blocked(local: str) -> None:
    ctx, _, store = make_ctx()
    result = call(ctx, "schedule_callback", datetime=local, tz="America/Los_Angeles")
    assert result.startswith("error: blocked")
    assert store.rows == []


def test_callback_in_callers_zone_still_checks_the_debtors_zone() -> None:
    # 10:00 in New York is 07:00 in Los Angeles, where Sam is.
    ctx, _, _ = make_ctx()
    result = call(ctx, "schedule_callback", datetime="2026-11-17T10:00", tz="America/New_York")
    assert result.startswith("error: blocked (calling hours")


@pytest.mark.parametrize("flags", [[Flag("cease")], None])
def test_callback_and_offer_blocked_after_cease_or_unknown_flags(flags) -> None:
    ctx, _, store = make_ctx(flags=flags)
    assert call(
        ctx, "schedule_callback", datetime="2026-11-17T10:30", tz="America/Los_Angeles"
    ).startswith("error: blocked (cease")
    assert call(
        ctx, "offer_payment_plan", down_cents="25000", installments="10", frequency="monthly"
    ).startswith("error: blocked (cease")
    assert store.rows == []


# --- escalate and end ----------------------------------------------------------


def test_escalate_and_end_call() -> None:
    ctx, audited, store = make_ctx()
    assert call(ctx, "escalate_to_human", reason="wants a manager").startswith("ok:")
    assert call(ctx, "end_call", reason="caller is done") == "ok: ending the call"
    assert ctx.state.stage == Stage.WRAP
    assert [r["kind"] for r in store.rows] == ["escalation"]
    assert len(audited) == 4 and all(d.allow for d in audited)


def test_tool_names_and_never_raises() -> None:
    ctx, _, _ = make_ctx()
    tools = CallTools(ctx).registry()
    assert tools.names() == [
        "lookup_account",
        "offer_payment_plan",
        "schedule_callback",
        "log_dispute",
        "escalate_to_human",
        "end_call",
    ]
    assert run_tool(tools, "offer_payment_plan", "{broken").startswith("error:")


# --- the real database ---------------------------------------------------------


@pytest.mark.db
def test_rows_and_audit_are_written_to_the_database(scratch_sam: Debtor) -> None:
    from harness.call_tools import PgCallStore, audit_to_conn, load_plan_policy
    from harness.db import connect

    # Its own copy of Sam, not the seed row, so a used database can't change the result.
    sam = scratch_sam
    call_id = f"test-part21-{uuid.uuid4()}"
    with connect() as conn:
        try:
            ctx = CallContext(
                debtor=sam,
                state=verified(CallState.new(call_id, sam)),
                store=PgCallStore(conn),
                audit=audit_to_conn(conn),
                contact=Contact(zip=sam.zip, phone_e164="+12135550101"),
                flags=[],
                policy=load_plan_policy(conn, sam.creditor),
                clock=lambda: NOW,
            )
            assert ctx.policy == PlanPolicy()
            tools = call_tools(ctx)
            offer = {"down_cents": "5000", "installments": "10", "frequency": "monthly"}
            assert run_tool(tools, "offer_payment_plan", offer) == PENDING_PLAN
            assert run_tool(tools, "log_dispute", {"reason": "already paid"}) == DISPUTE_LOGGED
            status = conn.execute(
                "select status from public.payment_plan where call_id = %s", (call_id,)
            ).fetchall()
            assert status == [("pending",)]
            kinds = conn.execute(
                "select decision from public.audit_event where call_id = %s order by seq",
                (call_id,),
            ).fetchall()
            assert kinds == [("allow",), ("block",), ("allow",), ("allow",)]
            debt = conn.execute(
                "select status from public.debt where id = %s", (sam.debt_id,)
            ).fetchone()
            assert debt == ("disputed",)
            # The next turn's context starts with disputed=False and reads the database.
            later = replace(ctx, disputed=False)
            assert run_tool(call_tools(later), "offer_payment_plan", OFFER) == BLOCKED_BY_DISPUTE
            # The dispute was committed by the tool itself, before it said "recorded".
            with connect() as other:
                seen = other.execute(
                    "select verified from public.dispute where call_id = %s", (call_id,)
                ).fetchall()
            assert seen == [(True,)]
        finally:
            # The tool committed; deleting scratch_sam takes the plan and dispute with it.
            conn.rollback()
