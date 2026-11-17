"""Compaction: long calls stay inside the model's context window.

What it checks: the agent compacts once the next call passes 80% of
``max_context_tokens`` and not before; the first ``keep_head`` and last
``keep_tail`` messages are kept word for word around one summary; the session
saves a ``compaction`` record, so the next webhook's agent loads the shorter
history; and a second compaction folds the first summary into the new one.
Negative cases: a tool request is never cut off from its result, whatever the
keep sizes; and a summarizer that raises leaves the history and the session
untouched while the caller still gets an answer. All offline, with the fake model.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from harness import Agent
from harness.agent import COMPACT_AT
from harness.compact import (
    SUMMARIZE_PROMPT,
    SUMMARY_HEADER,
    compact_messages,
    estimate_tokens,
    render,
    split,
)
from harness.memory import JsonlSession
from model import FakeProvider, Provider, fake


def _is_summarize_call(messages: list[dict]) -> bool:
    return bool(messages) and messages[0].get("content") == SUMMARIZE_PROMPT


def _model(summaries: list[str] | None = None, answer: str = "ok") -> Provider:
    """A fake model: summaries come from ``summaries`` in order, everything else gets ``answer``."""
    queue = list(summaries or [])

    def reply(messages: list[dict]) -> str:
        if _is_summarize_call(messages):
            return queue.pop(0) if queue else "a summary"
        return answer

    return fake(scripted=reply)


def _responder(provider: Provider) -> FakeProvider:
    assert isinstance(provider.responder, FakeProvider)
    return provider.responder


def _summarize_inputs(provider: Provider) -> list[str]:
    """What each summarizer call was asked to summarize."""
    return [c[1]["content"] for c in _responder(provider).calls if _is_summarize_call(c)]


def _convo(turns: int) -> list[dict]:
    """``turns`` caller/agent exchanges, long enough to cost real tokens."""
    messages: list[dict] = []
    for i in range(turns):
        messages.append({"role": "user", "content": f"caller line {i}: " + "words " * 40})
        messages.append({"role": "assistant", "content": f"agent line {i}"})
    return messages


def _with_tools() -> list[dict]:
    """A call with two tool rounds in it, so the cuts have pairs to land on."""

    def ask(call_id: str) -> dict:
        fn = {"name": "lookup_account", "arguments": json.dumps({"name": "Sam Doe"})}
        return {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "function": fn}]}

    return [
        {"role": "user", "content": "Hello?"},
        {"role": "assistant", "content": "Hi, may I speak with Sam Doe?"},
        {"role": "user", "content": "This is Sam. What do I owe?"},
        ask("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "balance 1240.00"},
        {"role": "assistant", "content": "Your balance is $1,240.00."},
        {"role": "user", "content": "Can you check again?"},
        ask("c2"),
        {"role": "tool", "tool_call_id": "c2", "content": "balance 1240.00"},
        {"role": "assistant", "content": "It is still $1,240.00."},
        {"role": "user", "content": "Okay, thanks."},
        {"role": "assistant", "content": "You're welcome."},
    ]


def _assert_pairs_intact(messages: list[dict]) -> None:
    """Every tool request is followed straight away by all its results, and every
    result follows the request it answers."""
    i = 0
    while i < len(messages):
        m = messages[i]
        if m["role"] == "tool":
            pytest.fail(f"tool result at {i} is cut off from its request")
        if m.get("tool_calls"):
            ids = [c["id"] for c in m["tool_calls"]]
            results = [r.get("tool_call_id") for r in messages[i + 1 : i + 1 + len(ids)]]
            assert results == ids, f"request at {i} lost its results"
            i += len(ids)
        i += 1


# --- when it happens ---------------------------------------------------------


def test_compacts_only_past_80_percent(no_instructions: Path) -> None:
    def agent_with_limit(limit: int) -> Agent:
        agent = Agent(provider=_model(), system_prompt_path=no_instructions)
        agent.max_context_tokens = limit
        agent.messages = _convo(8)
        return agent

    probe = agent_with_limit(10**6)
    size = probe.context_tokens() + estimate_tokens([{"role": "user", "content": "next"}])

    at_line = agent_with_limit(math.ceil(size / COMPACT_AT))  # size <= 80%: no compaction
    at_line.run("next")
    assert at_line.compactions == 0
    assert not _summarize_inputs(at_line.provider)  # type: ignore[arg-type]

    over = agent_with_limit(math.floor((size - 1) / COMPACT_AT))  # size > 80%: compact
    over.run("next")
    assert over.compactions == 1
    assert len(_summarize_inputs(over.provider)) == 1  # type: ignore[arg-type]
    assert sum(m["content"].startswith(SUMMARY_HEADER) for m in over.messages) == 1


def test_short_call_is_never_compacted(no_instructions: Path) -> None:
    provider = _model()
    agent = Agent(provider=provider, system_prompt_path=no_instructions, max_context_tokens=1)
    assert agent.run("Hello?") == "ok"
    assert agent.compact() is False
    assert agent.compactions == 0
    assert not _summarize_inputs(provider)


# --- what is kept -----------------------------------------------------------


def test_head_and_tail_are_kept_word_for_word(no_instructions: Path) -> None:
    original = _convo(6)
    provider = _model(["the caller asked about the balance"])
    agent = Agent(provider=provider, system_prompt_path=no_instructions, keep_head=2, keep_tail=3)
    agent.messages = list(original)

    assert agent.compact() is True
    assert agent.messages[:2] == original[:2]
    assert agent.messages[2] == {
        "role": "user",
        "content": f"{SUMMARY_HEADER}\nthe caller asked about the balance",
    }
    # The tail grows back from 3 to start on an assistant message.
    assert agent.messages[3:] == original[9:]
    # The summarizer saw exactly the middle.
    assert _summarize_inputs(provider) == [render(original[2:9])]


def test_summary_header_says_it_is_not_a_compliance_record() -> None:
    for word in ("verification", "disclosure", "stop requests"):
        assert word in SUMMARY_HEADER


@pytest.mark.parametrize("keep_tail", range(1, 10))
@pytest.mark.parametrize("keep_head", range(1, 10))
def test_a_tool_request_is_never_cut_from_its_result(keep_head: int, keep_tail: int) -> None:
    """Negative: whatever the keep sizes, no tool result is separated from its request."""
    original = _with_tools()
    compacted = compact_messages(original, lambda _: "summary", keep_head, keep_tail)
    _assert_pairs_intact(compacted)
    cut = split(original, keep_head, keep_tail)
    if cut is not None:
        head_end, tail_start = cut
        assert original[head_end - 1]["role"] != "user"  # the summary must not follow a user
        assert not original[head_end - 1].get("tool_calls")
        assert original[head_end]["role"] != "tool"
        assert original[tail_start]["role"] == "assistant"


def test_cut_that_would_land_inside_a_tool_round_moves(no_instructions: Path) -> None:
    """Negative: keep_head=4 ends on a tool request and keep_tail=4 starts on a result."""
    original = _with_tools()
    agent = Agent(provider=_model(), system_prompt_path=no_instructions, keep_head=4, keep_tail=4)
    agent.messages = list(original)
    assert agent.compact() is True
    _assert_pairs_intact(agent.messages)
    assert agent.messages[:5] == original[:5]  # head grew to take in the c1 result
    assert agent.messages[6:] == original[7:]  # tail grew back to the c2 request


# --- when the summarizer fails ----------------------------------------------


def test_failing_summarizer_leaves_history_and_answers(
    tmp_path: Path, no_instructions: Path
) -> None:
    """Negative: the summary call raises; nothing is lost and the caller still gets a reply."""

    def reply(messages: list[dict]) -> str:
        if _is_summarize_call(messages):
            raise RuntimeError("model overloaded")
        return "Sure, I can help with that."

    session = JsonlSession.for_call(tmp_path, "call_fail")
    agent = Agent(
        provider=fake(scripted=reply),
        system_prompt_path=no_instructions,
        session=session,
        max_context_tokens=50,
    )
    for message in _convo(5):
        agent._add(message)
    before = list(agent.messages)

    assert agent.run("Can I pay next week?") == "Sure, I can help with that."
    assert agent.compactions == 0
    assert agent.compact_error is not None and "model overloaded" in agent.compact_error
    assert agent.messages[: len(before)] == before
    assert agent.messages[len(before) :] == [
        {"role": "user", "content": "Can I pay next week?"},
        {"role": "assistant", "content": "Sure, I can help with that."},
    ]
    kinds = [json.loads(line)["type"] for line in session.path.read_text().splitlines()]
    assert "compaction" not in kinds
    assert JsonlSession.for_call(tmp_path, "call_fail").load() == agent.messages


# --- sessions and repeat compactions ----------------------------------------


def test_reload_after_compaction_gets_the_compacted_history(
    tmp_path: Path, no_instructions: Path
) -> None:
    provider = _model(["sam asked about the balance"])
    agent = Agent(
        provider=provider,
        system_prompt_path=no_instructions,
        session=JsonlSession.for_call(tmp_path, "call_long"),
        max_context_tokens=400,
    )
    for message in _convo(6):
        agent._add(message)
    agent.run("What was my balance again?")
    assert agent.compactions == 1

    session = JsonlSession.for_call(tmp_path, "call_long")
    kinds = [json.loads(line)["type"] for line in session.path.read_text().splitlines()]
    assert "compaction" in kinds
    reloaded = Agent(provider=provider, system_prompt_path=no_instructions, session=session)
    assert reloaded.messages == agent.messages
    assert any(m["content"].endswith("sam asked about the balance") for m in reloaded.messages)
    # The record keeps everything: the full old history is still in the file.
    assert "caller line 3" in session.path.read_text()


def test_second_compaction_folds_in_the_first_summary(no_instructions: Path) -> None:
    provider = _model(["FIRST: sam confirmed their name", "SECOND: sam set up a plan"])
    agent = Agent(provider=provider, system_prompt_path=no_instructions, keep_head=2, keep_tail=2)
    agent.messages = _convo(6)
    assert agent.compact() is True
    agent.messages += _convo(4)
    assert agent.compact() is True
    assert agent.compactions == 2

    second_input = _summarize_inputs(provider)[1]
    assert "FIRST: sam confirmed their name" in second_input
    summaries = [m for m in agent.messages if m["content"].startswith(SUMMARY_HEADER)]
    assert len(summaries) == 1
    assert summaries[0]["content"].endswith("SECOND: sam set up a plan")


def test_server_token_count_is_used_when_reported(no_instructions: Path) -> None:
    from model.provider import LLMResponse

    provider = fake(scripted=[LLMResponse(content="ok", usage={"prompt_tokens": 5000})])
    agent = Agent(provider=provider, system_prompt_path=no_instructions)
    agent.run("Hello?")
    extra = estimate_tokens(agent.messages[1:])  # the reply, added after the count
    assert agent.context_tokens() == 5000 + extra
