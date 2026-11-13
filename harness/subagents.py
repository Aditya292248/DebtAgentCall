"""Subagents: hand a self-contained task to a fresh agent and get back its reply.

A subagent is just a new ``Agent``: same model and tools as whoever started it,
but its own short system prompt (``instructions/subagent.md``) and an empty
history. It works on the task, and only its final reply comes back.

    run_subagent(task)            one subagent, one task
    fan_out([task, task, ...])    one subagent per task, in parallel
    delegate_tool(...)            the model's way to call run_subagent
    fan_out_tool(...)             the model's way to call fan_out

Where they are used: only after a call, never during one. A subagent is a whole
extra model conversation, far too slow for a person waiting on the phone. So
the tools for live calls must never contain ``delegate`` or ``fan_out``;
``LIVE_CALL_EXCLUDED`` names them, and a test checks the webhook's tools.

Rules that keep this safe:
  - subagents never get ``delegate`` or ``fan_out`` themselves, so a subagent
    can't start subagents that start subagents forever
  - a subagent that raises becomes an ``error: subagent failed: ...`` result;
    it never takes the caller or the other subagents down with it
  - more than ``MAX_FAN_OUT`` tasks is refused, not cut short, so no task is
    silently skipped

Tools in this repo have no approval step yet, so there is no approver to share
between parallel subagents (codingHarness puts a lock around its approver).

Adapted from codingHarness.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from harness.instructions import load_system_prompt
from harness.tools import Tool, ToolRegistry

SUBAGENT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "instructions" / "subagent.md"
SUBAGENT_PROMPT = load_system_prompt(SUBAGENT_PROMPT_PATH)

MAX_FAN_OUT = 8  # subagents per fan_out call
MAX_PARALLEL = 4  # running at once
SUBAGENT_TOOLS_EXCLUDED = ("delegate", "fan_out")  # no subagents of subagents
LIVE_CALL_EXCLUDED = ("delegate", "fan_out")  # too slow for a live call


def delegate_prompt(task: str) -> str:
    """The message a subagent starts from: the task, and how to answer."""
    return (
        f"Your task:\n{task}\n\n"
        "You can't ask the other agent questions, so make reasonable assumptions and "
        "mention them in your reply."
    )


def without(tools: ToolRegistry, names: tuple[str, ...]) -> ToolRegistry:
    """A new registry with every tool except ``names``; ``tools`` is left as it was."""
    return ToolRegistry(t for t in tools if t.name not in names)


def _subagent_tools(tools: ToolRegistry | None) -> ToolRegistry | None:
    # None means the Agent's default tools, which never include delegate or fan_out.
    return None if tools is None else without(tools, SUBAGENT_TOOLS_EXCLUDED)


def run_subagent(task: str, **agent_options: object) -> str:
    """Run ``task`` on a fresh agent and return its final reply. Never raises."""
    from harness.agent import Agent  # lazy, so agent.py may import this module later

    if not task.strip():
        return "error: a subagent needs a task"
    options = dict(agent_options)
    options["tools"] = _subagent_tools(options.get("tools"))  # type: ignore[arg-type]
    options.setdefault("system_prompt_path", SUBAGENT_PROMPT_PATH)
    try:
        agent = Agent(**options)  # type: ignore[arg-type]
        return agent.run(delegate_prompt(task))
    except Exception as exc:  # noqa: BLE001 - one subagent's crash is its own result
        return f"error: subagent failed: {exc!r}"


def _clean(tasks: list[str]) -> list[str]:
    """Drop blank tasks."""
    return [t.strip() for t in tasks if t.strip()]


def fan_out(
    tasks: list[str], max_parallel: int = MAX_PARALLEL, **agent_options: object
) -> list[str]:
    """One subagent per task, up to ``max_parallel`` at once. Replies in task order.

    Raises ``ValueError`` for more than ``MAX_FAN_OUT`` tasks or ``max_parallel < 1``.
    """
    if max_parallel < 1:
        raise ValueError("max_parallel must be at least 1")
    tasks = _clean(tasks)
    if len(tasks) > MAX_FAN_OUT:
        raise ValueError(f"fan_out takes at most {MAX_FAN_OUT} tasks, got {len(tasks)}")
    if not tasks:
        return []
    with ThreadPoolExecutor(max_workers=min(max_parallel, len(tasks))) as pool:
        return list(pool.map(lambda task: run_subagent(task, **agent_options), tasks))


def delegate_tool(**agent_options: object) -> Tool:
    """The ``delegate`` tool: hand one self-contained subtask to a fresh subagent."""

    def delegate(task: str) -> str:
        return run_subagent(task, **agent_options)

    return Tool(
        name="delegate",
        description=(
            "Hand one self-contained subtask to a fresh subagent and get back its reply. "
            "The subagent has your tools but none of this conversation, so describe the "
            "task fully: what to do and what to report back."
        ),
        params={"task": "The full, self-contained task for the subagent."},
        fn=delegate,
    )


def _parse_tasks(text: str) -> list[str]:
    """Tasks as a JSON list of strings, or else one task per line."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return _clean(text.splitlines())
    if isinstance(parsed, list) and all(isinstance(t, str) for t in parsed):
        return _clean(parsed)
    return _clean([text])


def fan_out_tool(**agent_options: object) -> Tool:
    """The ``fan_out`` tool: run independent subtasks on parallel subagents."""

    def fan(tasks: str) -> str:
        todo = _parse_tasks(tasks)
        if not todo:
            return "error: fan_out needs at least one non-empty task"
        if len(todo) > MAX_FAN_OUT:
            return f"error: fan_out takes at most {MAX_FAN_OUT} tasks, got {len(todo)}"
        replies = fan_out(todo, **agent_options)  # type: ignore[arg-type]
        return "\n\n".join(
            f"## subtask {i}: {task}\n{reply}"
            for i, (task, reply) in enumerate(zip(todo, replies, strict=True), start=1)
        )

    return Tool(
        name="fan_out",
        description=(
            f"Run independent subtasks at the same time, one fresh subagent each (at most "
            f"{MAX_FAN_OUT}). Each subagent sees only its own task, so make every task "
            "self-contained. Returns every subagent's reply."
        ),
        params={"tasks": "A JSON list of subtasks, or one subtask per line."},
        fn=fan,
    )
