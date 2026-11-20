"""Tools and the loop.

What it checks: the model can ask for ``lookup_account``; ``run()`` runs it, feeds
the result back as a ``tool`` message, and keeps calling the model until it answers
in plain text, or stops after ``max_steps``. Bad tool calls come back as ``error:``
strings instead of crashing. Runs against the fake provider, so it is offline and
deterministic; the live-model version is ``uv run accept part-04``.

``lookup_account`` reads Postgres now. These tests swap in the same made-up
accounts held in memory, so they still run without a database; the database
itself is tested in ``harness/test_account_lookup_db.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import Agent
from harness.tool_outcome import tool_body
from harness.tools import Tool, ToolRegistry, default_tools, lookup_account, run_tool
from model import FakeProvider, LLMResponse, fake, tool_call, tool_reply

ACCOUNTS = [
    {
        "account_id": "ACC-1001",
        "name": "Sam Doe",
        "creditor": "Northwind Furniture",
        "balance": "1250.00",
        "currency": "USD",
    },
    {
        "account_id": "ACC-1002",
        "name": "Alex Rivera",
        "creditor": "Bluebird Dental",
        "balance": "310.45",
        "currency": "USD",
    },
]


def _find_in_memory(name: str) -> dict | None:
    wanted = " ".join(name.split()).lower()
    return next((a for a in ACCOUNTS if a["name"].lower() == wanted), None)


@pytest.fixture(autouse=True)
def _accounts_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("harness.tools.find_account", _find_in_memory)


def _responder(agent: Agent) -> FakeProvider:
    r = agent.provider.responder if agent.provider else None
    assert isinstance(r, FakeProvider)
    return r


def _answer_from_tool_result(messages: list[dict]) -> str:
    """A scripted "model" that reads the last tool result and quotes the balance."""
    account = json.loads(tool_body(messages[-1]["content"]))
    return f"Your balance with {account['creditor']} is ${account['balance']}."


# --- schemas and the registry -------------------------------------------------


def test_lookup_account_schema():
    (schema,) = default_tools().schemas()
    fn = schema["function"]
    assert fn["name"] == "lookup_account"
    assert fn["parameters"]["required"] == ["name"]
    assert fn["parameters"]["additionalProperties"] is False
    json.dumps(schema)  # must be plain JSON to go over the wire


def test_registry_rejects_duplicate_names():
    tool = Tool(name="t", description="d", params={}, fn=lambda: "x")
    with pytest.raises(ValueError, match="already registered"):
        ToolRegistry([tool, tool])


def test_agent_sends_tool_schemas_with_every_call(no_instructions: Path):
    agent = Agent(provider=fake(), system_prompt_path=no_instructions)
    agent.send("hi")
    assert _responder(agent).kwargs[0]["tools"] == default_tools().schemas()


# --- lookup_account -----------------------------------------------------------


def test_lookup_account_finds_sam_doe_case_insensitively():
    account = json.loads(lookup_account("  sam DOE "))
    assert account["name"] == "Sam Doe"
    assert account["balance"] == "1250.00"


def test_lookup_account_unknown_name():
    assert lookup_account("Nobody Here") == "error: no account found for 'Nobody Here'"


def test_lookup_account_uses_the_finder_it_is_given():
    account = {"name": "Test Person", "balance": "1.00"}
    assert json.loads(lookup_account("Test Person", lambda name: account))["balance"] == "1.00"


# --- run_tool -----------------------------------------------------------------


def test_run_tool_accepts_string_or_decoded_arguments():
    tools = default_tools()
    from_json = run_tool(tools, "lookup_account", '{"name": "Sam Doe"}')
    from_dict = run_tool(tools, "lookup_account", {"name": "Sam Doe"})
    assert from_json == from_dict
    assert json.loads(from_json)["account_id"] == "ACC-1001"


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("delete_account", "{}", "error: unknown tool 'delete_account'; available: lookup_account"),
        ("lookup_account", "{not json", "error: arguments for lookup_account are not valid JSON"),
        ("lookup_account", '["Sam Doe"]', "error: arguments for lookup_account must be a JSON"),
        ("lookup_account", "{}", "error: lookup_account is missing argument(s): name"),
        ("lookup_account", '{"name": "a", "ssn": "1"}', "error: lookup_account got unexpected"),
        ("lookup_account", '{"name": 7}', "error: lookup_account argument(s) must be strings"),
    ],
)
def test_bad_tool_calls_become_error_strings(name, arguments, expected):
    assert run_tool(default_tools(), name, arguments).startswith(expected)


def test_a_tool_that_raises_becomes_an_error_string():
    def boom(name: str) -> str:
        raise RuntimeError("database down")

    tools = ToolRegistry(
        [Tool(name="lookup_account", description="d", params={"name": "n"}, fn=boom)]
    )
    result = run_tool(tools, "lookup_account", '{"name": "Sam Doe"}')
    assert result.startswith("error: lookup_account failed: RuntimeError('database down')")


def test_a_tool_that_returns_a_non_string_becomes_an_error_string():
    tools = ToolRegistry(
        [Tool(name="lookup_account", description="d", params={"name": "n"}, fn=lambda name: None)]
    )
    result = run_tool(tools, "lookup_account", '{"name": "Sam Doe"}')
    assert result == "error: lookup_account returned NoneType, not a string"


# --- the loop -----------------------------------------------------------------


def test_run_looks_up_the_account_and_answers_with_the_balance(no_instructions: Path):
    provider = fake(
        scripted=lambda msgs: (
            _answer_from_tool_result(msgs)
            if msgs[-1]["role"] == "tool"
            else tool_reply(tool_call("lookup_account", name="Sam Doe"))
        )
    )
    agent = Agent(provider=provider, system_prompt_path=no_instructions)

    reply = agent.run("What do I owe? I'm Sam Doe.")

    assert reply == "Your balance with Northwind Furniture is $1250.00."
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "tool", "assistant"]
    tool_msg = agent.messages[2]
    assert tool_msg["tool_call_id"] == "call_1"
    assert tool_msg["content"].startswith("outcome: success\n")
    assert json.loads(tool_body(tool_msg["content"]))["balance"] == "1250.00"
    assert len(_responder(agent).calls) == 2


def test_bad_calls_are_answered_and_the_model_can_recover(no_instructions: Path):
    provider = fake(
        scripted=[
            tool_reply(
                tool_call("transfer_money", "a"),
                tool_call("lookup_account", "b"),
            ),
            "Sorry, let me try again.",
        ]
    )
    agent = Agent(provider=provider, system_prompt_path=no_instructions)

    assert agent.run("hi") == "Sorry, let me try again."
    results = {m["tool_call_id"]: m["content"] for m in agent.messages if m["role"] == "tool"}
    assert results["a"].startswith("outcome: error\nerror: unknown tool 'transfer_money'")
    assert results["b"] == "outcome: error\nerror: lookup_account is missing argument(s): name"


def test_missing_tool_call_ids_are_filled_in(no_instructions: Path):
    call = {"type": "function", "function": {"name": "lookup_account", "arguments": "{}"}}
    provider = fake(scripted=[LLMResponse(content="", tool_calls=[call]), "done"])
    agent = Agent(provider=provider, system_prompt_path=no_instructions)
    agent.run("hi")
    assert agent.messages[1]["tool_calls"][0]["id"] == "call_0"
    assert agent.messages[2]["tool_call_id"] == "call_0"


def test_send_still_makes_one_call_and_runs_nothing(no_instructions: Path):
    provider = fake(scripted=[tool_reply(tool_call("lookup_account", name="Sam Doe"))])
    agent = Agent(provider=provider, system_prompt_path=no_instructions)
    assert agent.send("hi") == ""
    assert [m["role"] for m in agent.messages] == ["user", "assistant"]
    assert len(_responder(agent).calls) == 1


def test_max_steps_must_be_positive():
    with pytest.raises(ValueError, match="max_steps"):
        Agent(provider=fake(), max_steps=0)


def test_a_model_failure_mid_run_takes_the_whole_turn_back_out(no_instructions: Path):
    def model(msgs: list[dict]) -> LLMResponse:
        if msgs[-1]["role"] == "tool":
            raise RuntimeError("model timed out")
        return tool_reply(tool_call("lookup_account", name="Sam Doe"))

    agent = Agent(provider=fake(scripted=model), system_prompt_path=no_instructions)
    agent.messages = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "ok"},
    ]
    before = list(agent.messages)

    with pytest.raises(RuntimeError, match="timed out"):
        agent.run("What do I owe? I'm Sam Doe.")

    assert agent.messages == before
    assert len(_responder(agent).calls) == 2


# --- negative case -----------------------------------------------------------


def test_a_model_that_always_calls_a_tool_stops_at_max_steps(no_instructions: Path):
    provider = fake(scripted=lambda msgs: tool_reply(tool_call("lookup_account", name="Sam Doe")))
    agent = Agent(provider=provider, system_prompt_path=no_instructions, max_steps=3)

    reply = agent.run("loop forever")

    assert reply == "stopped: used all 3 steps without a final answer"
    assert len(_responder(agent).calls) == 3
    # Every tool call got a result, including the one on the last step.
    calls = [c["id"] for m in agent.messages for c in m.get("tool_calls", [])]
    results = [m["tool_call_id"] for m in agent.messages if m["role"] == "tool"]
    assert len(calls) == len(results) == 3
    assert agent.messages[-1]["role"] == "tool"
