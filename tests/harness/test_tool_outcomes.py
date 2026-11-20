"""Tool outcomes: every tool call ends in one known outcome, and the model is told which.

What it checks: a tool that raises after it may have written is ``unknown`` and is
never run again; a request whose result was never saved (a crash) comes back as
``unknown``; a slow tool is ``timeout`` if it only reads and ``unknown`` otherwise;
after an ``unknown`` the rest of the batch is ``not_started`` and never runs; a gate
block is ``denied``. Also the text the model reads for each outcome, that
``run_tool`` still returns the text it always did, and that a live call fails closed
on an ``unknown`` outcome. Runs against the fake provider, offline.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from compliance.decision import Decision
from compliance.hours import Contact
from harness import Agent
from harness.call_state import CallState, Debtor, Stage
from harness.call_tools import CallContext, MemoryCallStore, call_tools
from harness.memory import JsonlSession, unanswered_tool_calls
from harness.tool_outcome import (
    INTERRUPTED,
    Denied,
    ToolError,
    ToolOutcome,
    ToolResult,
    outcome_of,
    tool_body,
    tool_message,
)
from harness.tools import Tool, ToolRegistry, run_tool, run_tool_result
from model import fake, tool_call, tool_reply
from phone.live_tools import LiveTools

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


def _agent(tools: list[Tool], script, no_instructions: Path, **kw) -> Agent:
    return Agent(
        provider=fake(scripted=script),
        system_prompt_path=no_instructions,
        tools=ToolRegistry(tools),
        **kw,
    )


def _results(agent: Agent) -> dict[str, str]:
    return {m["tool_call_id"]: m["content"] for m in agent.messages if m["role"] == "tool"}


# --- (a) a tool that fails after writing is unknown, and is not called again --------


def test_a_tool_that_raises_after_writing_is_unknown_and_never_run_again(no_instructions: Path):
    rows: list[str] = []

    def save_note(text: str) -> str:
        rows.append(text)  # the row is written...
        raise ConnectionError("connection dropped before the commit was confirmed")

    tool = Tool("save_note", "Save a note.", {"text": "The note."}, save_note)
    # A model that asks again every time it sees a failure.
    agent = _agent(
        [tool],
        lambda msgs: tool_reply(tool_call("save_note", text="hello")),
        no_instructions,
        max_steps=1,
    )

    agent.run("save it")

    assert rows == ["hello"]  # called once, not retried by the harness
    first = _results(agent)["call_1"]
    assert outcome_of(first) == "unknown"
    assert "not known whether this tool ran" in first


def test_the_same_failure_in_a_pure_read_is_a_plain_error():
    boom = Tool("read", "Read.", {}, lambda: 1 / 0, safe_to_retry=True)
    assert run_tool_result(ToolRegistry([boom]), "read", "{}").outcome is ToolOutcome.ERROR


def test_a_tool_can_say_it_failed_cleanly():
    def fn() -> str:
        raise ToolError("no such row")

    result = run_tool_result(ToolRegistry([Tool("t", "d", {}, fn)]), "t", "{}")
    assert result.outcome is ToolOutcome.ERROR


# --- (b) a crash between the request and its saved result ---------------------------


def test_a_crash_between_request_and_result_is_unknown_and_the_model_is_told(
    tmp_path: Path, no_instructions: Path
):
    session = JsonlSession.for_call(tmp_path, "call_crash")
    session.append({"role": "user", "content": "Save my note."})
    request = tool_call("save_note", "call_9", text="hi")
    session.append({"role": "assistant", "content": "", "tool_calls": [request]})
    # The process died here: no result was saved.

    provider = fake()
    agent = Agent(provider=provider, system_prompt_path=no_instructions, session=session)

    result = agent.messages[-1]
    assert result["tool_call_id"] == "call_9"
    assert result["content"] == tool_message(INTERRUPTED)
    assert outcome_of(result["content"]) == "unknown"
    assert "Do not repeat the action" in result["content"]
    assert unanswered_tool_calls(JsonlSession(session.path).load()) == []  # saved too

    agent.run("Did it save?")
    sent = provider.responder.calls[0]
    assert any("Do not repeat the action" in str(m.get("content")) for m in sent)


# --- (c) a slow tool --------------------------------------------------------------


def test_a_slow_tool_is_timeout_if_safe_to_retry_and_unknown_otherwise():
    def slow() -> str:
        time.sleep(0.5)
        return "late"

    tools = ToolRegistry(
        [
            Tool("read_slow", "d", {}, slow, safe_to_retry=True, timeout_seconds=0.05),
            Tool("write_slow", "d", {}, slow, timeout_seconds=0.05),
        ]
    )
    assert run_tool_result(tools, "read_slow", "{}").outcome is ToolOutcome.TIMEOUT
    assert run_tool_result(tools, "write_slow", "{}").outcome is ToolOutcome.UNKNOWN


def test_a_tool_inside_its_time_limit_succeeds():
    tools = ToolRegistry([Tool("t", "d", {}, lambda: "fine", timeout_seconds=5)])
    assert run_tool_result(tools, "t", "{}") == ToolResult(ToolOutcome.SUCCESS, "fine")


# --- (d) after unknown, the rest of the batch is not started ------------------------


def test_after_unknown_the_rest_of_the_batch_is_not_started_and_never_runs(
    no_instructions: Path,
):
    ran: list[str] = []

    def first() -> str:
        ran.append("first")
        raise RuntimeError("died mid-write")

    def second() -> str:
        ran.append("second")
        return "ok"

    agent = _agent(
        [Tool("first", "d", {}, first), Tool("second", "d", {}, second)],
        [tool_reply(tool_call("first", "a"), tool_call("second", "b")), "I could not tell."],
        no_instructions,
    )
    seen: list[tuple[str, str, ToolOutcome]] = []
    agent.on_tool_outcome = lambda cid, name, r: seen.append((cid, name, r.outcome))

    assert agent.run("go") == "I could not tell."

    assert ran == ["first"]
    results = _results(agent)
    assert outcome_of(results["a"]) == "unknown"
    assert outcome_of(results["b"]) == "not_started"
    assert "did not run" in results["b"]
    assert seen == [("a", "first", ToolOutcome.UNKNOWN), ("b", "second", ToolOutcome.NOT_STARTED)]


def test_an_aborted_turn_answers_every_call_aborted_and_runs_nothing(no_instructions: Path):
    ran: list[str] = []
    tool = Tool("t", "d", {}, lambda: ran.append("x") or "ok")
    agent = _agent(
        [tool], [tool_reply(tool_call("t", "a"), tool_call("t", "b")), "done"], no_instructions
    )
    agent.send("go")
    agent.abort()

    agent._run_tools(agent.messages[-1]["tool_calls"])

    assert ran == []
    assert [outcome_of(m["content"]) for m in agent.messages if m["role"] == "tool"] == [
        "aborted",
        "aborted",
    ]


# --- (e) a gate block is denied ---------------------------------------------------


def _ctx(audited: list[Decision]) -> CallContext:
    # Still in VERIFY, so the gate blocks lookup_account.
    state = CallState.new("call-outcomes", SAM)
    return CallContext(
        debtor=SAM,
        state=CallState(**{**state.__dict__, "stage": Stage.VERIFY}),
        store=MemoryCallStore(),
        audit=lambda d, call_id, debt_id: audited.append(d),
        contact=Contact(zip=SAM.zip, phone_e164="+12135550101"),
        flags=[],
        clock=lambda: datetime(2026, 11, 16, 18, 0, tzinfo=UTC),
    )


def test_a_gate_block_is_denied_and_audited_as_such():
    audited: list[Decision] = []
    tools = call_tools(_ctx(audited))

    result = run_tool_result(tools, "lookup_account", "{}")

    assert result.outcome is ToolOutcome.DENIED
    assert result.text.startswith("error: blocked")
    assert any(d.evidence.get("outcome") == "denied" for d in audited if not d.allow)
    message = tool_message(result)
    assert message.startswith("outcome: denied\n")
    assert "will not change that" in message


def test_call_tools_mark_only_the_read_as_safe_to_retry():
    marks = {t.name: t.safe_to_retry for t in call_tools(_ctx([]))}
    assert marks == {
        "lookup_account": True,
        "offer_payment_plan": False,
        "schedule_callback": False,
        "log_dispute": False,
        "escalate_to_human": False,
        "end_call": False,
    }


# --- what the model reads -----------------------------------------------------------


def test_success_and_error_carry_only_the_tools_own_text():
    assert tool_message(ToolResult(ToolOutcome.SUCCESS, '{"a": 1}')) == 'outcome: success\n{"a": 1}'
    assert tool_message(ToolResult(ToolOutcome.ERROR, "error: bad")) == "outcome: error\nerror: bad"


@pytest.mark.parametrize(
    ("outcome", "phrases"),
    [
        (ToolOutcome.UNKNOWN, ["not known whether", "Do not repeat", "safe to retry", "read tool"]),
        (ToolOutcome.TIMEOUT, ["did not finish in time", "safe to try once more"]),
        (ToolOutcome.DENIED, ["not allowed", "will not change that"]),
        (ToolOutcome.ABORTED, ["did not run", "cancelled"]),
        (ToolOutcome.NOT_STARTED, ["did not run", "earlier tool call"]),
    ],
)
def test_the_text_for_each_other_outcome(outcome: ToolOutcome, phrases: list[str]):
    message = tool_message(ToolResult(outcome, "detail"))
    assert message.splitlines()[0] == f"outcome: {outcome.value}"
    assert message.endswith("detail")
    for phrase in phrases:
        assert phrase in message


def test_tool_body_and_outcome_of_round_trip():
    message = tool_message(ToolResult(ToolOutcome.SUCCESS, '{"a": 1}'))
    assert json.loads(tool_body(message)) == {"a": 1}
    assert outcome_of(message) == "success"
    assert outcome_of("plain old text") is None
    assert tool_body("plain old text") == "plain old text"


# --- run_tool keeps its old text ---------------------------------------------------


def test_run_tool_still_returns_the_same_text():
    tools = ToolRegistry(
        [
            Tool("ok", "d", {"x": "x"}, lambda x: x.upper()),
            Tool("boom", "d", {}, lambda: 1 / 0),
            Tool("deny", "d", {}, lambda: Denied("error: blocked (dispute)")),
            Tool("num", "d", {}, lambda: 5),
        ]
    )
    assert run_tool(tools, "ok", '{"x": "a"}') == "A"
    assert (
        run_tool(tools, "boom", "{}") == "error: boom failed: ZeroDivisionError('division by zero')"
    )
    assert run_tool(tools, "deny", "{}") == "error: blocked (dispute)"
    assert run_tool(tools, "num", "{}") == "error: num returned int, not a string"
    assert run_tool(tools, "nope", "{}").startswith("error: unknown tool 'nope'")
    assert run_tool(tools, "ok", "{}") == "error: ok is missing argument(s): x"
    assert run_tool_result(tools, "nope", "{}").outcome is ToolOutcome.ERROR


# --- a live call fails closed on unknown ---------------------------------------------


def test_a_live_call_fails_closed_when_a_tool_outcome_is_unknown():
    def never_opened(debtor: Debtor):
        raise AssertionError("not opened")

    live = LiveTools(
        SAM, CallState.new("call-live", SAM), "+12135550101", open_backend=never_opened
    )
    live.on_outcome("c1", "log_dispute", ToolResult(ToolOutcome.SUCCESS, "ok"))
    assert live.failure is None
    live.on_outcome("c2", "offer_payment_plan", ToolResult(ToolOutcome.UNKNOWN, "timed out"))
    assert live.failure is not None
    assert "unknown outcome" in live.failure
