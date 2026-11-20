"""Subagents: delegate and fan out, used to review calls afterwards.

What it checks: ``run_subagent`` runs a task on a fresh agent (its own prompt,
an empty history) and returns only its reply; ``fan_out`` runs several in
parallel and keeps task order; the ``delegate`` and ``fan_out`` tools do the
same for the model. Negative cases: a subagent never gets ``delegate`` or
``fan_out``, a subagent that raises becomes an ``error:`` string, more than
``MAX_FAN_OUT`` tasks is refused, and the live-call webhook never offers either
tool. ``review_transcript`` flags a threat. All offline, with the fake model.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness import Agent
from harness.memory import JsonlSession
from harness.review import REVIEW_AREAS, Finding, parse_verdict, review_transcript
from harness.subagents import (
    LIVE_CALL_EXCLUDED,
    MAX_FAN_OUT,
    SUBAGENT_PROMPT,
    delegate_tool,
    fan_out,
    fan_out_tool,
    run_subagent,
)
from harness.tools import Tool, ToolRegistry, default_tools, run_tool
from model import FakeProvider, Provider, fake
from model.fake import tool_call, tool_reply
from phone import signed_headers
from phone.webhook import WEBHOOK_PATH, create_app


def _responder(provider: Provider) -> FakeProvider:
    assert isinstance(provider.responder, FakeProvider)
    return provider.responder


def task_of(messages: list[dict]) -> str:
    """The task text a subagent was started with."""
    first = next(m for m in messages if m["role"] == "user")["content"]
    return first.removeprefix("Your task:\n").split("\n\n")[0]


def _echo_tool() -> Tool:
    return Tool("echo", "Echo text back.", {"text": "Anything."}, lambda text: text)


# --- run_subagent ------------------------------------------------------------


def test_run_subagent_runs_a_fresh_agent_and_returns_its_reply() -> None:
    provider = fake(scripted=lambda msgs: f"report for {task_of(msgs)}")
    out = run_subagent("summarize the call", provider=provider, tools=ToolRegistry())
    assert out == "report for summarize the call"
    sent = _responder(provider).calls[0]
    assert sent[0] == {"role": "system", "content": SUBAGENT_PROMPT}  # its own prompt
    assert [m["role"] for m in sent] == ["system", "user"]  # nothing from anyone else


def test_run_subagent_can_use_its_tools() -> None:
    provider = fake(scripted=[tool_reply(tool_call("echo", text="hi")), "done"])
    out = run_subagent("say hi", provider=provider, tools=ToolRegistry([_echo_tool()]))
    assert out == "done"
    assert _responder(provider).calls[1][-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "outcome: success\nhi",
    }


def test_subagent_that_raises_returns_an_error_instead_of_crashing() -> None:
    def broken(_msgs: list[dict]) -> str:
        raise ConnectionError("model is down")

    out = run_subagent("anything", provider=fake(scripted=broken), tools=ToolRegistry())
    assert out.startswith("error: subagent failed: ConnectionError")


def test_blank_task_is_an_error_not_a_model_call() -> None:
    provider = fake()
    assert run_subagent("   ", provider=provider).startswith("error:")
    assert _responder(provider).calls == []


def test_subagents_never_get_delegate_or_fan_out() -> None:
    provider = fake()
    tools = ToolRegistry([_echo_tool()])
    tools.register(delegate_tool(provider=provider))
    tools.register(fan_out_tool(provider=provider))
    run_subagent("x", provider=provider, tools=tools)
    offered = {t["function"]["name"] for t in _responder(provider).kwargs[0]["tools"]}
    assert offered == {"echo"}
    assert {"delegate", "fan_out"} <= set(tools.names())  # the caller's registry is untouched


# --- fan_out -----------------------------------------------------------------


def test_fan_out_keeps_task_order() -> None:
    def model(msgs: list[dict]) -> str:
        task = task_of(msgs)
        time.sleep(0.05 if task == "first" else 0)  # finish out of order
        return f"report for {task}"

    reports = fan_out(["first", "second", "third"], provider=fake(scripted=model))
    assert reports == ["report for first", "report for second", "report for third"]


def test_fan_out_really_runs_in_parallel() -> None:
    barrier = threading.Barrier(3, timeout=5)

    def model(_msgs: list[dict]) -> str:
        barrier.wait()  # only passes if all three subagents are calling at once
        return "ok"

    assert fan_out(["a", "b", "c"], provider=fake(scripted=model)) == ["ok", "ok", "ok"]


def test_fan_out_one_failure_does_not_stop_the_others() -> None:
    def model(msgs: list[dict]) -> str:
        if task_of(msgs) == "bad":
            raise RuntimeError("nope")
        return "ok"

    reports = fan_out(["good", "bad", "good too"], provider=fake(scripted=model))
    assert reports[0] == "ok" and reports[2] == "ok"
    assert reports[1].startswith("error: subagent failed: RuntimeError")


def test_fan_out_refuses_more_than_max_fan_out_tasks() -> None:
    provider = fake()
    with pytest.raises(ValueError, match="at most"):
        fan_out([f"t{i}" for i in range(MAX_FAN_OUT + 1)], provider=provider)
    assert _responder(provider).calls == []  # refused before any subagent ran
    assert len(fan_out([f"t{i}" for i in range(MAX_FAN_OUT)], provider=provider)) == MAX_FAN_OUT


def test_fan_out_drops_blank_tasks() -> None:
    assert fan_out(["", "  "], provider=fake()) == []


# --- the tools ---------------------------------------------------------------


def test_delegate_tool_runs_a_subagent() -> None:
    provider = fake(scripted=lambda msgs: f"did {task_of(msgs)}")
    tools = ToolRegistry([delegate_tool(provider=provider, tools=ToolRegistry())])
    assert run_tool(tools, "delegate", {"task": "check the amounts"}) == "did check the amounts"


def test_fan_out_tool_takes_a_json_list_or_lines() -> None:
    provider = fake(scripted=lambda msgs: f"did {task_of(msgs)}")
    tools = ToolRegistry([fan_out_tool(provider=provider, tools=ToolRegistry())])
    out = run_tool(tools, "fan_out", {"tasks": json.dumps(["one", "two"])})
    assert out == "## subtask 1: one\ndid one\n\n## subtask 2: two\ndid two"
    assert run_tool(tools, "fan_out", {"tasks": "one\ntwo"}) == out


def test_fan_out_tool_refuses_too_many_tasks_with_an_error() -> None:
    provider = fake()
    tools = ToolRegistry([fan_out_tool(provider=provider)])
    many = json.dumps([f"t{i}" for i in range(MAX_FAN_OUT + 1)])
    assert run_tool(tools, "fan_out", {"tasks": many}).startswith("error: fan_out takes at most")
    assert run_tool(tools, "fan_out", {"tasks": " \n "}).startswith("error:")
    assert _responder(provider).calls == []


# --- live calls never get them -----------------------------------------------


def test_live_call_tools_have_no_delegate_or_fan_out(tmp_path: Path) -> None:
    assert not set(LIVE_CALL_EXCLUDED) & set(default_tools().names())
    assert not set(LIVE_CALL_EXCLUDED) & set(Agent(provider=fake()).tools.names())

    # The webhook as it builds its agent today, with no agent_factory given.
    provider = fake(default="Hello.")
    secret = "whsec_test_only"
    app = create_app(
        provider=provider,
        secret=secret,
        open_session=partial(JsonlSession.for_call, tmp_path),
        stages=False,
    )
    body = json.dumps(
        {"event": "agent.message", "channel": "voice", "data": {"callId": "c1", "transcript": "Hi"}}
    ).encode()
    headers = signed_headers(body, secret, time.time(), f"wh_{uuid.uuid4().hex}", "agent.message")
    assert TestClient(app).post(WEBHOOK_PATH, content=body, headers=headers).status_code == 200

    sent = _responder(provider).kwargs
    assert sent, "the webhook never called the model"
    offered = {t["function"]["name"] for k in sent for t in (k.get("tools") or [])}
    assert not offered & set(LIVE_CALL_EXCLUDED)


# --- review after the call ---------------------------------------------------

SAM = {
    "account_id": "ACC-1001",
    "name": "Sam Doe",
    "creditor": "Acme Bank",
    "balance": "1250.00",
    "currency": "USD",
}
THREAT_CALL = [
    ("Hello?", "Hi, may I speak with Sam Doe?"),
    ("This is Sam.", "Thanks, Sam. I'm a debt collector; what you say will be used for that."),
    ("How much?", "You owe Acme Bank $1,250.00."),
    ("I can't pay.", "If you don't pay, we will have you arrested."),
]


def _scripted_reviewer(msgs: list[dict]) -> str:
    area = task_of(msgs).removeprefix("Area: ").split("\n")[0]
    if area == "threats":
        return 'FLAG: the agent said "we will have you arrested"'
    return f"OK: {area} fine"


def test_review_transcript_flags_a_threat() -> None:
    provider = fake(scripted=_scripted_reviewer)
    findings = review_transcript(THREAT_CALL, SAM, provider=provider)
    assert [f.area for f in findings] == list(REVIEW_AREAS)
    flagged = [f for f in findings if not f.ok]
    assert flagged == [Finding("threats", False, 'the agent said "we will have you arrested"')]
    # One reviewer per area, each sent the transcript and the account, with no tools.
    calls = _responder(provider).calls
    assert len(calls) == len(REVIEW_AREAS)
    assert all("arrested" in c[-1]["content"] and "1250.00" in c[-1]["content"] for c in calls)
    assert all(k.get("tools") is None for k in _responder(provider).kwargs)


def test_reviewer_without_a_clear_verdict_is_not_ok() -> None:
    assert parse_verdict("amounts", "OK: matches") == Finding("amounts", True, "matches")
    assert parse_verdict("amounts", "**FLAG**: said $1,500").ok is False
    unclear = parse_verdict("amounts", "error: subagent failed: TimeoutError()")
    assert unclear.ok is False and unclear.note.startswith("no clear verdict")
