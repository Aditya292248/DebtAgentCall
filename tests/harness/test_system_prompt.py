"""The system prompt.

What it checks: ``load_system_prompt()`` reads the prompt from a file, and ``Agent``
sends it as the first, ``system``, message of every call, ahead of the history.
Runs against the fake provider, so it is offline and deterministic; the live-model
version is ``uv run accept part-03``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import Agent
from harness.agent import DEFAULT_SYSTEM_PROMPT_PATH
from harness.instructions import load_system_prompt
from model import FakeProvider, fake


def _calls(agent: Agent) -> list[list[dict]]:
    responder = agent.provider.responder if agent.provider else None
    assert isinstance(responder, FakeProvider)
    return responder.calls


@pytest.fixture
def prompt_file(tmp_path: Path) -> Path:
    f = tmp_path / "system.md"
    f.write_text("  You are a polite phone agent.  \n", encoding="utf-8")
    return f


def test_load_reads_and_strips_the_file(prompt_file: Path):
    assert load_system_prompt(prompt_file) == "You are a polite phone agent."


def test_repo_ships_a_default_prompt():
    assert load_system_prompt(DEFAULT_SYSTEM_PROMPT_PATH)


def test_agent_uses_the_default_prompt_file():
    agent = Agent(provider=fake())
    agent.send("hi")
    first = _calls(agent)[0][0]
    assert first == {"role": "system", "content": load_system_prompt(DEFAULT_SYSTEM_PROMPT_PATH)}


def test_system_prompt_comes_before_history(prompt_file: Path):
    agent = Agent(provider=fake(scripted=["r1", "r2"]), system_prompt_path=prompt_file)
    agent.send("u1")
    agent.send("u2")
    assert _calls(agent)[1] == [
        {"role": "system", "content": "You are a polite phone agent."},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "r1"},
        {"role": "user", "content": "u2"},
    ]


def test_system_prompt_is_not_stored_in_history(prompt_file: Path):
    agent = Agent(provider=fake(), system_prompt_path=prompt_file)
    agent.send("hi")
    assert [m["role"] for m in agent.messages] == ["user", "assistant"]


def test_edits_to_the_file_apply_on_the_next_turn(prompt_file: Path):
    agent = Agent(provider=fake(), system_prompt_path=prompt_file)
    agent.send("a")
    prompt_file.write_text("v2", encoding="utf-8")
    agent.send("b")
    assert [call[0]["content"] for call in _calls(agent)] == [
        "You are a polite phone agent.",
        "v2",
    ]


# --- negative case -----------------------------------------------------------


@pytest.mark.parametrize("content", [None, "", "   \n"])
def test_missing_or_blank_prompt_sends_no_system_message(tmp_path: Path, content: str | None):
    f = tmp_path / "system.md"
    if content is not None:
        f.write_text(content, encoding="utf-8")
    agent = Agent(provider=fake(default="ok"), system_prompt_path=f)
    # No crash and no empty system message: the model gets the history only.
    assert agent.send("hi") == "ok"
    assert _calls(agent)[0] == [{"role": "user", "content": "hi"}]
