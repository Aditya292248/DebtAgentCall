"""Call tools on live calls: the gate and the approver on every tool the model calls.

What it checks, through the signed simulator and the webhook with stages on:
  - before verification, a model that calls ``lookup_account`` anyway gets
    ``error: blocked`` and no balance reaches the caller (the negative case:
    before this change the tool did not exist on the call at all)
  - a model that says "you're verified" unlocks nothing: the server's state
    decides, so the lookup is still blocked and the stage stays VERIFY
  - a verified caller: a plan inside the policy is approved, saved, and its
    amounts may be spoken; one outside it is saved as pending, not approved
  - ``end_call`` hangs up after the reply and leaves the call in WRAP
  - ``escalate_to_human`` saves a request; it transfers only when a
    supervisor number is configured
  - a failed save fails closed: the model's "approved" is never spoken, the
    call hangs up with a fixed line, and an error is logged
  - only the six call tools are offered (no subagents, no fan-out), and a turn
    without tools never opens the backend
  - a model that says "approved" for a plan code saved as pending has that
    sentence dropped, and the caller hears the fixed pending line
  - the same webhook delivered twice, even to a fresh app (a restart), gets
    the first reply back without running the model or saving a second row
  - a retry of a turn that saved a row and then crashed is not run again: it
    hangs up with the fixed line, and only one row exists
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness import Agent
from harness.call_state import Debtor, Stage
from harness.call_tools import ESCALATED, PENDING_PLAN, MemoryCallStore
from harness.memory import JsonlSession
from harness.stages import VERIFY_ASK
from harness.subagents import delegate_tool, fan_out_tool
from harness.tool_outcome import tool_body
from harness.tools import ToolRegistry
from model import FakeProvider, Provider
from model.fake import tool_call, tool_reply
from model.provider import LLMResponse
from phone import signed_headers
from phone.live_tools import COMMITMENT_RULE, PENDING_LINE, ToolBackend
from phone.webhook import CANT_CONTINUE, SAFE_LINE, WEBHOOK_PATH, create_app, debt_as_found
from sim.agentphone_sim import Script, reply_of, run_script

SECRET = "whsec_test_only"
SAM_PHONE = "+12135550101"
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
TO_VERIFY = ["Hello?", "Yes, speaking."]
TO_DISCUSS = [*TO_VERIFY, "4/12/1985, 90012"]
CALL_TOOLS = {
    "lookup_account",
    "offer_payment_plan",
    "schedule_callback",
    "log_dispute",
    "escalate_to_human",
    "end_call",
}
LEAKS = ("1,250", "1250", "125000", "Northwind")


class BrokenStore(MemoryCallStore):
    def save_plan(self, ctx, plan, status) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("database went away")


class Call:
    """The webhook in-process with a fake model and an in-memory tool backend."""

    def __init__(  # type: ignore[no-untyped-def]
        self, tmp_path: Path, scripted, *, store=None, supervisor: str = "", open_backend=None
    ) -> None:
        self.responder = FakeProvider(scripted=scripted)
        self.store = store if store is not None else MemoryCallStore()
        self.audited: list = []  # call tool decisions
        self.blocked: list = []  # dropped sentences
        self.opened = 0
        self.folder = tmp_path / "sessions"
        provider = Provider(base_url="fake://local", model="fake", responder=self.responder)
        # The agent comes with subagent tools, to show the webhook replaces them.
        tools = ToolRegistry([delegate_tool(provider=provider), fan_out_tool(provider=provider)])
        app = create_app(
            secret=SECRET,
            agent_factory=lambda s: Agent(provider=provider, session=s, tools=tools),
            open_session=lambda cid: JsonlSession.for_call(self.folder, cid),
            find_debtor={SAM_PHONE: SAM}.get,
            read_debt=debt_as_found,
            audit=lambda d, call_id, debt_id: self.blocked.append(d),
            record_stop=lambda stop: None,
            open_backend=open_backend or self._open,
            supervisor_number=supervisor,
        )
        self.client = TestClient(app)

    def _open(self, debtor: Debtor) -> ToolBackend:
        self.opened += 1
        return ToolBackend(
            store=self.store,
            audit=lambda d, call_id, debt_id: self.audited.append(d),
            flags=[],
        )

    def turn(self, call_id: str, text: str, webhook_id: str | None = None) -> dict:
        body = json.dumps(
            {
                "event": "agent.message",
                "channel": "voice",
                "data": {"callId": call_id, "transcript": text, "to": SAM_PHONE},
            }
        ).encode()
        webhook_id = webhook_id or f"wh_{uuid.uuid4().hex}"
        headers = signed_headers(body, SECRET, time.time(), webhook_id, "agent.message")
        resp = self.client.post(WEBHOOK_PATH, content=body, headers=headers)
        assert resp.status_code == 200
        return reply_of(resp)

    def records(self) -> list[dict]:
        (path,) = self.folder.glob("*.jsonl")
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def states(self) -> list[dict]:
        return [r["state"] for r in self.records() if r["type"] == "state"]

    def tool_results(self) -> list[str]:
        return [
            tool_body(r["message"]["content"])
            for r in self.records()
            if r["type"] == "message" and r["message"]["role"] == "tool"
        ]


def lookup_then_echo(messages: list[dict]):  # type: ignore[no-untyped-def]
    """A model that calls lookup_account and then reads out whatever it got."""
    last = messages[-1]
    if last["role"] == "tool":
        return f"Here is what I found. {last['content']}"
    return tool_reply(tool_call("lookup_account"))


# --- negative: no account before verification ---------------------------------


def test_lookup_before_verification_is_blocked_and_no_balance_is_spoken(tmp_path: Path) -> None:
    call = Call(tmp_path, lookup_then_echo)
    script = Script("why_verify", [*TO_VERIFY, "Why do you need that? How much do I owe?"])
    turns = run_script(script, call.client, SECRET)

    assert turns[1][1] == VERIFY_ASK
    (result,) = call.tool_results()
    assert "error: blocked" in result
    spoken = turns[2][1]
    for leak in LEAKS:
        assert leak not in spoken and leak not in result
    assert "$" not in spoken
    # The gate said no, and it was audited; the approver never saw it.
    assert [(d.rule_id, d.allow) for d in call.audited] == [("call_tool_gate", False)]
    assert call.responder.kwargs[0].get("tools") is None  # not offered before verification
    assert call.states()[-1]["stage"] == Stage.VERIFY


def test_model_saying_verified_unlocks_nothing(tmp_path: Path) -> None:
    claims = LLMResponse(
        content="Great, you're verified now.",
        tool_calls=[tool_call("lookup_account")],
        finish_reason="tool_calls",
    )
    call = Call(tmp_path, [claims, "Your balance is $1,250.00 with Northwind Furniture."])
    for line in TO_VERIFY:
        call.turn("call_claim", line)
    reply = call.turn("call_claim", "Why? I already told you who I am.")  # a question: the model

    assert "error: blocked" in call.tool_results()[0]
    for leak in LEAKS:
        assert leak not in reply["text"]
    state = call.states()[-1]
    assert (state["stage"], state["verified"], state["disclosed"]) == ("VERIFY", False, False)
    assert [d.rule_id for d in call.blocked] == ["third_party_disclosure"]


# --- a verified caller --------------------------------------------------------


def to_discuss(call: Call, call_id: str) -> None:
    for line in TO_DISCUSS:
        call.turn(call_id, line)
    assert call.states()[-1]["stage"] == Stage.DISCUSS


def test_plan_inside_policy_is_approved_and_saved_before_it_is_said(tmp_path: Path) -> None:
    said = "Your plan is approved: $250.00 today, then ten monthly payments of $100.00."
    plan = tool_call(
        "offer_payment_plan", down_cents="25000", installments="10", frequency="monthly"
    )
    call = Call(tmp_path, [tool_reply(plan), said])
    to_discuss(call, "call_ok")
    reply = call.turn("call_ok", "Can I put 250 down and pay the rest over ten months?")

    assert reply == {"text": said}
    (row,) = call.store.rows
    assert (row["kind"], row["status"], row["installment_cents"]) == (
        "payment_plan",
        "approved",
        10000,
    )
    assert call.tool_results()[0].startswith("approved: ")
    assert [(d.rule_id, d.allow) for d in call.audited] == [
        ("call_tool_gate", True),
        ("call_tool_approver", True),
    ]
    offered = {t["function"]["name"] for t in call.responder.kwargs[0]["tools"]}
    assert offered == CALL_TOOLS  # no delegate, no fan_out on a live call
    assert call.blocked == []


def test_plan_outside_policy_is_pending_not_approved(tmp_path: Path) -> None:
    plan = tool_call(
        "offer_payment_plan", down_cents="5000", installments="18", frequency="monthly"
    )
    call = Call(tmp_path, [tool_reply(plan), "A specialist will confirm that plan with you."])
    to_discuss(call, "call_pending")
    reply = call.turn("call_pending", "Can I do fifty down and eighteen months?")

    assert reply == {"text": "A specialist will confirm that plan with you."}
    assert [r["status"] for r in call.store.rows] == ["pending"]
    assert call.tool_results() == [PENDING_PLAN]
    approver = [d for d in call.audited if d.rule_id == "call_tool_approver"]
    assert [d.allow for d in approver] == [False]
    assert "outside plan policy" in approver[0].reason


def test_end_call_hangs_up(tmp_path: Path) -> None:
    end = tool_call("end_call", reason="caller is done")
    call = Call(tmp_path, [tool_reply(end), "Thank you for your time. Goodbye."])
    to_discuss(call, "call_end")
    reply = call.turn("call_end", "That's all for today.")

    assert reply == {"text": "Thank you for your time. Goodbye.", "hangup": True}
    assert call.states()[-1]["stage"] == Stage.WRAP


def test_escalation_without_a_supervisor_saves_a_request_and_stays_on(tmp_path: Path) -> None:
    ask = tool_call("escalate_to_human", reason="wants a manager")
    call = Call(tmp_path, [tool_reply(ask), "A specialist will follow up with you."])
    to_discuss(call, "call_esc")
    reply = call.turn("call_esc", "I want to talk to a manager.")

    assert reply == {"text": "A specialist will follow up with you."}
    assert [r["kind"] for r in call.store.rows] == ["escalation"]
    assert call.tool_results() == [ESCALATED]


def test_escalation_with_a_supervisor_transfers(tmp_path: Path) -> None:
    ask = tool_call("escalate_to_human", reason="wants a manager")
    call = Call(tmp_path, [tool_reply(ask), "Let me connect you."], supervisor="+15555550199")
    to_discuss(call, "call_xfer")
    reply = call.turn("call_xfer", "I want to talk to a manager.")

    assert reply == {"action": "transfer"}
    assert [r["kind"] for r in call.store.rows] == ["escalation"]


# --- fail closed --------------------------------------------------------------


def test_failed_save_is_never_promised(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    plan = tool_call(
        "offer_payment_plan", down_cents="25000", installments="10", frequency="monthly"
    )
    call = Call(tmp_path, [tool_reply(plan), "Your plan is all set up."], store=BrokenStore())
    to_discuss(call, "call_broken")
    with caplog.at_level(logging.ERROR, logger="phone.webhook"):
        reply = call.turn("call_broken", "Can I put 250 down and pay over ten months?")

    assert reply == {"text": CANT_CONTINUE, "hangup": True}
    assert call.store.rows == []
    assert "database went away" in caplog.text
    history = JsonlSession.for_call(call.folder, "call_broken").load()
    assert history[-1] == {"role": "assistant", "content": CANT_CONTINUE}
    assert call.states()[-1]["stage"] == Stage.WRAP


def test_backend_opening_failure_fails_closed(tmp_path: Path) -> None:
    def refuse(debtor: Debtor) -> ToolBackend:
        raise OSError("no database")

    call = Call(tmp_path, lookup_then_echo, open_backend=refuse)
    to_discuss(call, "call_nodb")
    reply = call.turn("call_nodb", "How much do I owe?")
    assert reply == {"text": CANT_CONTINUE, "hangup": True}
    assert call.tool_results()[0].startswith("error: the call tools are unavailable")


def test_turn_without_tools_never_opens_the_backend(tmp_path: Path) -> None:
    call = Call(tmp_path, ["Happy to help with anything else."])
    to_discuss(call, "call_plain")
    assert call.turn("call_plain", "Thanks.") == {"text": "Happy to help with anything else."}
    assert call.opened == 0


# --- the reply matches what code decided ----------------------------------------


def test_pending_plan_spoken_as_approved_is_replaced(tmp_path: Path) -> None:
    plan = tool_call(
        "offer_payment_plan", down_cents="5000", installments="18", frequency="monthly"
    )
    call = Call(tmp_path, [tool_reply(plan), "Great news, your plan is approved."])
    to_discuss(call, "call_lie")
    reply = call.turn("call_lie", "Can I do fifty down and eighteen months?")

    assert reply == {"text": PENDING_LINE}
    assert [r["status"] for r in call.store.rows] == ["pending"]
    assert [(d.rule_id, d.evidence["sentence"]) for d in call.blocked] == [
        (COMMITMENT_RULE, "Great news, your plan is approved.")
    ]
    saved = JsonlSession.for_call(call.folder, "call_lie").load()
    assert saved[-1] == {"role": "assistant", "content": PENDING_LINE}


def test_approval_claimed_without_any_tool_is_dropped(tmp_path: Path) -> None:
    call = Call(tmp_path, ["Your plan is all set. Anything else?"])
    to_discuss(call, "call_claim_plan")
    # The first sentence is blocked, so the turn stops there with the safe line.
    assert call.turn("call_claim_plan", "So are we good?") == {"text": SAFE_LINE}
    assert [d.rule_id for d in call.blocked] == [COMMITMENT_RULE]


# --- retries ------------------------------------------------------------------


def test_same_webhook_twice_saves_one_row_even_after_a_restart(tmp_path: Path) -> None:
    plan = tool_call(
        "offer_payment_plan", down_cents="25000", installments="10", frequency="monthly"
    )
    said = "Your plan is approved: $250.00 today, then ten monthly payments of $100.00."
    call = Call(tmp_path, [tool_reply(plan), said])
    to_discuss(call, "call_retry")
    first = call.turn("call_retry", "Ten months, 250 down.", webhook_id="wh_plan_1")
    model_calls = len(call.responder.calls)

    # A new app on the same sessions and store: the in-memory list of ids is gone.
    restarted = Call(tmp_path, [tool_reply(plan), said], store=call.store)
    again = restarted.turn("call_retry", "Ten months, 250 down.", webhook_id="wh_plan_1")

    assert again == first == {"text": said}
    assert len(call.store.rows) == 1
    assert len(call.responder.calls) == model_calls
    assert restarted.responder.calls == []


def test_retry_after_a_crash_past_a_saved_row_is_not_run_again(tmp_path: Path) -> None:
    plan = tool_call(
        "offer_payment_plan", down_cents="25000", installments="10", frequency="monthly"
    )

    def save_then_die(messages: list[dict]):  # type: ignore[no-untyped-def]
        if messages[-1]["role"] == "tool":
            raise TimeoutError("model timed out")
        return tool_reply(plan)

    call = Call(tmp_path, save_then_die)
    to_discuss(call, "call_crash")
    with pytest.raises(TimeoutError):
        call.turn("call_crash", "Ten months, 250 down.", webhook_id="wh_crash")
    assert [r["status"] for r in call.store.rows] == ["approved"]

    restarted = Call(tmp_path, save_then_die, store=call.store)
    reply = restarted.turn("call_crash", "Ten months, 250 down.", webhook_id="wh_crash")
    assert reply == {"text": CANT_CONTINUE, "hangup": True}
    assert len(call.store.rows) == 1
    assert restarted.responder.calls == []


def test_model_failure_before_any_save_can_be_retried(tmp_path: Path) -> None:
    attempts: list[int] = []

    def flaky(messages: list[dict]) -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("model timed out")
        return "Happy to help."

    call = Call(tmp_path, flaky)
    to_discuss(call, "call_flaky")
    with pytest.raises(TimeoutError):
        call.turn("call_flaky", "Hi again.", webhook_id="wh_flaky")
    assert call.turn("call_flaky", "Hi again.", webhook_id="wh_flaky") == {"text": "Happy to help."}


def test_ten_month_request_survives_fragments_and_cannot_save_four_months(tmp_path: Path) -> None:
    wrong = tool_call(
        "offer_payment_plan", down_cents="25000", installments="4", frequency="monthly"
    )
    call = Call(
        tmp_path,
        [
            "What schedule suits you?",
            "How much upfront?",
            tool_reply(wrong),
            "Let me check the requested terms.",
        ],
    )
    to_discuss(call, "call_preference")
    assert (
        call.turn("call_preference", "Can I spread this out?")["text"] == "What schedule suits you?"
    )
    assert call.turn("call_preference", "Over ten months please.")["text"] == "How much upfront?"
    call.turn("call_preference", "I can do 250 down.")
    assert not call.store.rows
    assert "caller requested 10 months" in call.tool_results()[-1]
