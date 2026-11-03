"""The agent keeps the conversation.

What it checks: ``Agent`` keeps the conversation in ``self.messages`` and sends the
whole thing on every call, so the model can answer from earlier turns. Runs against
the fake provider, so it is offline and deterministic; the live-model version is
``uv run accept part-02``.
"""

from __future__ import annotations

import pytest

from harness import Agent
from model import FakeProvider, LLMResponse, Provider, fake


def test_messages_start_empty():
    assert Agent(provider=fake()).messages == []


def test_history_grows_user_assistant_user_assistant():
    agent = Agent(provider=fake(scripted=["Hello, Sam Doe.", "Your name is Sam Doe."]))
    assert agent.send("My name is Sam Doe.") == "Hello, Sam Doe."
    assert agent.send("What is my name?") == "Your name is Sam Doe."
    assert agent.messages == [
        {"role": "user", "content": "My name is Sam Doe."},
        {"role": "assistant", "content": "Hello, Sam Doe."},
        {"role": "user", "content": "What is my name?"},
        {"role": "assistant", "content": "Your name is Sam Doe."},
    ]


def test_second_call_sends_the_first_exchange(no_instructions):
    provider = fake(scripted=["r1", "r2"])
    agent = Agent(provider=provider, system_prompt_path=no_instructions)
    agent.send("u1")
    agent.send("u2")
    responder = provider.responder
    assert isinstance(responder, FakeProvider)
    # The first call's snapshot must not grow when later turns are appended.
    assert responder.calls == [
        [{"role": "user", "content": "u1"}],
        [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "r1"},
            {"role": "user", "content": "u2"},
        ],
    ]


def test_model_can_answer_from_earlier_turn():
    # A "model" that can only know the name if an earlier turn is in its input.
    def remembers(msgs: list[dict]) -> str:
        said = [m["content"] for m in msgs if m["role"] == "user"]
        name = next((s.removeprefix("my name is ") for s in said if s.startswith("my name")), None)
        return name or "I don't know"

    agent = Agent(provider=fake(scripted=remembers))
    agent.send("my name is Sam Doe")
    assert agent.send("what is my name?") == "Sam Doe"


def test_separate_agents_do_not_share_history():
    a, b = Agent(provider=fake()), Agent(provider=fake())
    a.send("hello")
    assert b.messages == []


# --- negative case -----------------------------------------------------------


def test_failed_model_call_leaves_history_unchanged():
    replies = iter([LLMResponse(content="Hello, Sam Doe.")])

    def responder(messages, **kwargs):
        try:
            return next(replies)
        except StopIteration:
            raise ConnectionError("model server went away") from None

    agent = Agent(provider=Provider(base_url="http://unused/v1", model="m", responder=responder))
    agent.send("My name is Sam Doe.")
    before = list(agent.messages)

    with pytest.raises(ConnectionError, match="model server went away"):
        agent.send("What is my name?")
    # The failed turn is not left half-recorded: no user message without a reply.
    assert agent.messages == before
