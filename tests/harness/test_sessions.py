"""Sessions: each call keeps its own saved history between webhook turns.

What it checks: a session saves messages and gives them back, in a JSONL file
and in the ``call_message`` table alike; through the webhook, turn 2 of a call
sends the model turn 1's messages too. And the refusals: two calls never see
each other's messages; a half-written last JSONL line is dropped (and cut off
before the next write) while a broken middle line raises; a turn that died after
a tool request gets a placeholder result on the next load; a failed turn is
taken back out of the saved history too. The Postgres tests are marked ``db``
and skip without a local Supabase.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness import Agent
from harness.memory import (
    INTERRUPTED,
    JsonlSession,
    PostgresSession,
    Session,
    unanswered_tool_calls,
)
from harness.tools import ToolRegistry
from model import FakeProvider, LLMResponse, Provider
from model.fake import tool_call, tool_reply
from phone import signed_headers
from phone.webhook import WEBHOOK_PATH, create_app

SECRET = "whsec_test_only"
NOW = 1_793_000_000.0

OpenSession = Callable[[str], Session]


@pytest.fixture(params=["jsonl", pytest.param("postgres", marks=pytest.mark.db)])
def open_session(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[OpenSession]:
    """``open_session(call_id)`` for each backend. Postgres rows are deleted afterwards."""
    if request.param == "jsonl":
        yield partial(JsonlSession.for_call, tmp_path / "sessions")
        return
    from harness.db import connect

    prefix = f"test_{uuid.uuid4().hex[:8]}_"
    yield lambda call_id: PostgresSession.for_call(prefix + call_id)
    with connect() as conn:
        conn.execute("delete from public.call_message where call_id like %s", (prefix + "%",))


def _echo(messages: list[dict]) -> str:
    return f"heard {messages[-1]['content']}"


def _provider(responder: FakeProvider) -> Provider:
    return Provider(base_url="fake://local", model="fake", responder=responder)


def _client(responder: FakeProvider, open_session: OpenSession, no_instructions) -> TestClient:
    provider = _provider(responder)
    app = create_app(
        secret=SECRET,
        agent_factory=lambda session: Agent(
            provider=provider, system_prompt_path=no_instructions, session=session
        ),
        open_session=open_session,
        clock=lambda: NOW,
        stages=False,  # the bare webhook; call stages are tested in test_call_stages.py
    )
    return TestClient(app)


def _say(client: TestClient, call_id: str, text: str) -> str:
    body = json.dumps(
        {
            "event": "agent.message",
            "channel": "voice",
            "data": {"callId": call_id, "transcript": text, "direction": "outbound"},
        }
    ).encode()
    headers = signed_headers(body, SECRET, NOW, f"wh_{uuid.uuid4().hex}", "agent.message")
    resp = client.post(WEBHOOK_PATH, content=body, headers=headers)
    assert resp.status_code == 200
    return resp.json()["text"]


# --- the session itself -----------------------------------------------------


def test_session_round_trip(open_session: OpenSession) -> None:
    session = open_session("call_rt")
    first = {"role": "user", "content": "Hello?"}
    second = {"role": "assistant", "content": "Hi, may I speak with Sam Doe?"}
    session.append(first)
    session.append(second)
    assert session.load() == [first, second]
    assert session.header()["type"] == "session"
    assert session.header()["id"].endswith("call_rt")

    # Opening the same call again finds everything, and adds no second header.
    again = open_session("call_rt")
    assert again.load() == [first, second]
    again.replace([second])
    assert open_session("call_rt").load() == [second]


def test_jsonl_file_is_one_record_per_line(tmp_path: Path) -> None:
    session = JsonlSession.for_call(tmp_path, "call_1")
    session.append({"role": "user", "content": "Hi"})
    session.replace([])
    kinds = [json.loads(line)["type"] for line in session.path.read_text().splitlines()]
    assert kinds == ["session", "message", "compaction"]


# --- through the webhook ----------------------------------------------------


def test_turn_two_sends_the_model_turn_one(open_session, no_instructions) -> None:
    responder = FakeProvider(scripted=_echo)
    client = _client(responder, open_session, no_instructions)
    assert _say(client, "call_a", "This is Sam Doe.") == "heard This is Sam Doe."
    assert _say(client, "call_a", "How much do I owe?") == "heard How much do I owe?"
    assert responder.calls[1] == [
        {"role": "user", "content": "This is Sam Doe."},
        {"role": "assistant", "content": "heard This is Sam Doe."},
        {"role": "user", "content": "How much do I owe?"},
    ]


def test_two_calls_never_see_each_other(open_session, no_instructions) -> None:
    responder = FakeProvider(scripted=_echo)
    client = _client(responder, open_session, no_instructions)
    _say(client, "call_a", "This is Sam Doe, my balance is private.")
    _say(client, "call_b", "Hello?")
    _say(client, "call_a", "Still there?")
    _say(client, "call_b", "Who is this?")
    sent_for_b = [m["content"] for m in responder.calls[1] + responder.calls[3]]
    assert not any("Sam Doe" in text or "Still there" in text for text in sent_for_b)
    assert responder.calls[3] == [
        {"role": "user", "content": "Hello?"},
        {"role": "assistant", "content": "heard Hello?"},
        {"role": "user", "content": "Who is this?"},
    ]
    assert len(open_session("call_a").load()) == 4
    assert len(open_session("call_b").load()) == 4


def test_unsafe_call_id_stays_in_the_sessions_folder(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    session = JsonlSession.for_call(root, "../../escape")
    assert session.path.parent == root
    assert not (tmp_path / "escape.jsonl").exists()


# --- damage and crashes -----------------------------------------------------


def test_half_written_last_line_is_dropped_and_cut_off(tmp_path: Path) -> None:
    session = JsonlSession.for_call(tmp_path, "call_1")
    session.append({"role": "user", "content": "Hello?"})
    with session.path.open("a", encoding="utf-8") as f:
        f.write('{"type": "message", "message": {"role": "assis')  # died mid-write
    assert session.load() == [{"role": "user", "content": "Hello?"}]

    # The next write trims the broken piece first, so it never ends up mid-file.
    session.append({"role": "assistant", "content": "Hi"})
    assert session.load() == [
        {"role": "user", "content": "Hello?"},
        {"role": "assistant", "content": "Hi"},
    ]


def test_broken_middle_line_raises(tmp_path: Path) -> None:
    session = JsonlSession.for_call(tmp_path, "call_1")
    with session.path.open("a", encoding="utf-8") as f:
        f.write("not json\n")
    session.append({"role": "user", "content": "Hello?"})
    with pytest.raises(ValueError, match="corrupt session line"):
        session.load()


def test_turn_that_died_after_a_tool_call_gets_a_placeholder(open_session, no_instructions) -> None:
    session = open_session("call_crash")
    session.append({"role": "user", "content": "How much do I owe?"})
    request = tool_call("lookup_account", "call_9", name="Sam Doe")
    # Saved the model's tool request, then the process died before the result.
    session.append({"role": "assistant", "content": "", "tool_calls": [request]})

    responder = FakeProvider(scripted=_echo)
    agent = Agent(
        provider=_provider(responder),
        system_prompt_path=no_instructions,
        session=open_session("call_crash"),
    )
    placeholder = {"role": "tool", "tool_call_id": "call_9", "content": INTERRUPTED}
    assert agent.messages[-1] == placeholder
    assert open_session("call_crash").load()[-1] == placeholder  # saved, not just in memory

    agent.run("Hello?")
    assert placeholder in responder.calls[0]
    assert unanswered_tool_calls(open_session("call_crash").load()) == []


def test_failed_turn_is_taken_back_out_of_the_session(open_session, no_instructions) -> None:
    def tool_then_down(messages: list[dict]) -> LLMResponse:
        # The first call asks for a tool; the model is down for the next one.
        if len(responder.calls) == 1:
            return tool_reply(tool_call("lookup_account", name="Sam Doe"))
        raise RuntimeError("model is down")

    session = open_session("call_fail")
    session.append({"role": "user", "content": "Hello?"})
    session.append({"role": "assistant", "content": "Hi."})
    before = session.load()

    responder = FakeProvider(scripted=tool_then_down)
    agent = Agent(
        provider=_provider(responder),
        system_prompt_path=no_instructions,
        tools=ToolRegistry(),  # no tools, so nothing touches the database
        session=session,
    )
    with pytest.raises(RuntimeError):
        agent.run("How much do I owe?")
    assert agent.messages == before
    assert open_session("call_fail").load() == before
