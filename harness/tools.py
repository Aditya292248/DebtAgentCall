"""Tools: what the model may ask the harness to do, and the code that does it.

Each ``Tool`` has two halves:
  - a JSON schema (``schema()``), sent to the model so it knows the tool exists
  - a Python function, which the harness runs when the model asks for it

The model never runs anything. It replies with a request ("call lookup_account
with name=Sam Doe"); ``run_tool`` checks the request, runs it, and returns a
string. Every failure (unknown tool, broken JSON, missing or wrong arguments, an
exception inside the tool) comes back as an ``error: ...`` string, so the model
can read what went wrong instead of the loop crashing. ``run_tool_result`` also
says how the call ended (``harness/tool_outcome.py``): a tool that raises, or
outlives its time limit, and is not ``safe_to_retry`` is ``unknown``, never an
automatic retry.

A ``ToolRegistry`` is the set of tools one agent may use. ``default_tools()``
holds the one tool so far, ``lookup_account``, which reads made-up accounts from
Postgres through ``harness.db.find_account``.

Adapted from codingHarness.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from harness.db import find_account
from harness.tool_outcome import Denied, ToolError, ToolOutcome, ToolResult


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: dict[str, str]  # argument name -> description; all are required strings
    fn: Callable[..., str]  # fn(**arguments) -> result text
    # Repeating it is harmless: only pure reads. Anything that writes or dials stays False,
    # so a crash or timeout in the middle of it is "unknown" and never run again.
    safe_to_retry: bool = False
    timeout_seconds: float | None = None  # stop waiting after this long; None: no limit

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        p: {"type": "string", "description": d} for p, d in self.params.items()
                    },
                    "required": list(self.params),
                    "additionalProperties": False,
                },
            },
        }


class ToolRegistry:
    """The tools one agent may use, by name, in the order they were registered."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> Tool:
        """Add a tool. Two tools with one name would leave the model guessing: error."""
        if tool.name in self._tools:
            raise ValueError(f"a tool named {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(list(self._tools.values()))

    def __len__(self) -> int:
        return len(self._tools)

    def schemas(self) -> list[dict]:
        """The tool list in the shape ``chat(tools=...)`` sends to the model."""
        return [t.schema() for t in self]


def run_tool(registry: ToolRegistry, name: str, arguments: str | dict | None) -> str:
    """Run one tool call from the model. Always returns a string, never raises.

    The text is the same as before outcomes existed; ``run_tool_result`` adds which
    outcome it was.
    """
    return run_tool_result(registry, name, arguments).text


class _TimedOut(Exception):
    pass


def _error(text: str) -> ToolResult:
    return ToolResult(ToolOutcome.ERROR, text)


def _call(tool: Tool, args: dict) -> tuple[bool, object]:
    """Run ``tool.fn``: ``(True, result)``, or ``(False, exception)``.

    A tool with a time limit runs in a worker thread and is not waited for past it,
    which gives ``(False, _TimedOut())``. The thread may still finish, and write.
    """
    if tool.timeout_seconds is None:
        try:
            return True, tool.fn(**args)
        except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the loop
            return False, exc
    box: list[tuple[bool, object]] = []

    def work() -> None:
        try:
            box.append((True, tool.fn(**args)))
        except Exception as exc:  # noqa: BLE001
            box.append((False, exc))

    worker = threading.Thread(target=work, name=f"tool-{tool.name}", daemon=True)
    worker.start()
    worker.join(tool.timeout_seconds)
    return box[0] if box else (False, _TimedOut())


def run_tool_result(registry: ToolRegistry, name: str, arguments: str | dict | None) -> ToolResult:
    """Run one tool call and say how it ended. Never raises, and never runs a tool twice."""
    tool = registry.get(name)
    if tool is None:
        return _error(f"error: unknown tool {name!r}; available: {', '.join(registry.names())}")

    # Models send arguments as a JSON string; a few servers already decode it.
    if isinstance(arguments, dict):
        args = arguments
    else:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            return _error(f"error: arguments for {name} are not valid JSON ({exc})")
        if not isinstance(args, dict):
            return _error(f"error: arguments for {name} must be a JSON object")

    missing = [p for p in tool.params if p not in args]
    if missing:
        return _error(f"error: {name} is missing argument(s): {', '.join(missing)}")
    extra = [a for a in args if a not in tool.params]
    if extra:
        return _error(f"error: {name} got unexpected argument(s): {', '.join(extra)}")
    wrong = [a for a, v in args.items() if not isinstance(v, str)]
    if wrong:
        return _error(f"error: {name} argument(s) must be strings: {', '.join(wrong)}")

    ok, result = _call(tool, args)
    if not ok:
        if isinstance(result, _TimedOut):
            text = f"error: {name} did not finish within {tool.timeout_seconds} seconds"
            return ToolResult(
                ToolOutcome.TIMEOUT if tool.safe_to_retry else ToolOutcome.UNKNOWN, text
            )
        text = f"error: {name} failed: {result!r}"
        if isinstance(result, ToolError):
            return _error(text)
        # It raised, so it may have written first. Only a pure read is known to be harmless.
        return ToolResult(ToolOutcome.ERROR if tool.safe_to_retry else ToolOutcome.UNKNOWN, text)
    # The result goes straight into a tool message, which must be text.
    if not isinstance(result, str):
        return _error(f"error: {name} returned {type(result).__name__}, not a string")
    if isinstance(result, Denied):
        return ToolResult(ToolOutcome.DENIED, str(result))
    if result.startswith("error:"):
        return _error(result)
    return ToolResult(ToolOutcome.SUCCESS, result)


# ----------------------------------------------------------------------------
# lookup_account: the first tool
# ----------------------------------------------------------------------------


def lookup_account(name: str, find: Callable[[str], dict | None] | None = None) -> str:
    """The account for ``name`` as JSON, or an ``error: no account found`` line.

    ``find`` defaults to the database; tests that run offline pass their own.
    """
    account = (find or find_account)(name)
    if account is None:
        return f"error: no account found for {name!r}"
    return json.dumps(account)


LOOKUP_ACCOUNT = Tool(
    name="lookup_account",
    description=(
        "Look up a person's account by their full name. Returns the account id, "
        "creditor, balance and currency, or says that no account was found."
    ),
    params={"name": "The person's full name, for example 'Sam Doe'."},
    fn=lambda name: lookup_account(name),
    safe_to_retry=True,
)


def default_tools() -> ToolRegistry:
    """A fresh registry with the tools every agent gets unless told otherwise."""
    return ToolRegistry([LOOKUP_ACCOUNT])
