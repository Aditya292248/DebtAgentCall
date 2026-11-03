"""Tools: what the model may ask the harness to do, and the code that does it.

Each ``Tool`` has two halves:
  - a JSON schema (``schema()``), sent to the model so it knows the tool exists
  - a Python function, which the harness runs when the model asks for it

The model never runs anything. It replies with a request ("call lookup_account
with name=Sam Doe"); ``run_tool`` checks the request, runs it, and returns a
string. Every failure (unknown tool, broken JSON, missing or wrong arguments, an
exception inside the tool) comes back as an ``error: ...`` string, so the model
can read what went wrong and try again instead of the loop crashing.

A ``ToolRegistry`` is the set of tools one agent may use. ``default_tools()``
holds the one tool so far, ``lookup_account``, which reads made-up accounts from
``data/accounts.json``.

Adapted from codingHarness.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ACCOUNTS_PATH = Path(__file__).resolve().parent.parent / "data" / "accounts.json"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: dict[str, str]  # argument name -> description; all are required strings
    fn: Callable[..., str]  # fn(**arguments) -> result text

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
    """Run one tool call from the model. Always returns a string, never raises."""
    tool = registry.get(name)
    if tool is None:
        return f"error: unknown tool {name!r}; available: {', '.join(registry.names())}"

    # Models send arguments as a JSON string; a few servers already decode it.
    if isinstance(arguments, dict):
        args = arguments
    else:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            return f"error: arguments for {name} are not valid JSON ({exc})"
        if not isinstance(args, dict):
            return f"error: arguments for {name} must be a JSON object"

    missing = [p for p in tool.params if p not in args]
    if missing:
        return f"error: {name} is missing argument(s): {', '.join(missing)}"
    extra = [a for a in args if a not in tool.params]
    if extra:
        return f"error: {name} got unexpected argument(s): {', '.join(extra)}"
    wrong = [a for a, v in args.items() if not isinstance(v, str)]
    if wrong:
        return f"error: {name} argument(s) must be strings: {', '.join(wrong)}"

    try:
        result = tool.fn(**args)
    except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the loop
        return f"error: {name} failed: {exc!r}"
    # The result goes straight into a tool message, which must be text.
    if not isinstance(result, str):
        return f"error: {name} returned {type(result).__name__}, not a string"
    return result


# ----------------------------------------------------------------------------
# lookup_account: the first tool
# ----------------------------------------------------------------------------


def lookup_account(name: str, path: str | Path = ACCOUNTS_PATH) -> str:
    """The account for ``name`` as JSON, or a plain "no account" line. Case-insensitive."""
    accounts = json.loads(Path(path).read_text(encoding="utf-8"))
    wanted = name.strip().lower()
    for account in accounts:
        if account["name"].lower() == wanted:
            return json.dumps(account)
    return f"no account found for {name!r}"


LOOKUP_ACCOUNT = Tool(
    name="lookup_account",
    description=(
        "Look up a person's account by their full name. Returns the account id, "
        "creditor, balance and currency, or says that no account was found."
    ),
    params={"name": "The person's full name, for example 'Sam Doe'."},
    fn=lambda name: lookup_account(name),
)


def default_tools() -> ToolRegistry:
    """A fresh registry with the tools every agent gets unless told otherwise."""
    return ToolRegistry([LOOKUP_ACCOUNT])
