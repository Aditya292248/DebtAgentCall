"""Tool outcomes: every tool call ends in exactly one known outcome, and the model is told which.

    success      the tool ran and returned a result
    error        the tool ran and failed cleanly: bad arguments, an unknown tool, a
                 result that is not text, or an ``error: ...`` result of its own
    timeout      a tool that is safe to retry did not finish in its time limit
    denied       not allowed: a compliance gate block, or the approver said no
    aborted      the turn was cancelled before this tool started, so it never ran
    not_started  an earlier call in the same batch ended ``unknown`` or ``aborted``,
                 so this one was deliberately not run
    unknown      the harness cannot tell whether it ran: it crashed or restarted
                 between the request and the saved result, an exception came out of
                 a tool that may have written, or a tool with side effects timed
                 out. Never retried automatically.

What the model reads (``tool_message``) is always the same shape: one line,
``outcome: <name>``, then the text. ``success`` and ``error`` carry the tool's own
text; ``unknown``, ``denied``, ``timeout``, ``aborted`` and ``not_started`` begin with
one fixed sentence saying what that means and what to do, then any detail.
``tool_body`` takes the line back off, for code that reads a tool's JSON.

A tool marks a clean failure by raising ``ToolError`` (outcome ``error``) and a
refusal by returning ``Denied(text)`` (outcome ``denied``). Any other exception
leaves the state unclear, so it is ``unknown`` unless the tool is safe to retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ToolOutcome(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    DENIED = "denied"
    ABORTED = "aborted"
    NOT_STARTED = "not_started"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolResult:
    outcome: ToolOutcome
    text: str  # the tool's own text, or the detail of why it did not run


class ToolError(Exception):
    """Raised by a tool that failed cleanly: it changed nothing, or says what it changed."""


class Denied(str):
    """A tool result that is a refusal (gate block, approver said no): outcome ``denied``."""


OUTCOME_LINE = "outcome: "

_MEANING = {
    ToolOutcome.UNKNOWN: (
        "It is not known whether this tool ran. Do not repeat the action unless the tool is "
        "marked safe to retry (read-only lookups are). Check state with a read tool, or tell "
        "the person instead."
    ),
    ToolOutcome.TIMEOUT: (
        "This tool did not finish in time. It only reads, so it is safe to try once more."
    ),
    ToolOutcome.DENIED: "This was not allowed. Retrying will not change that.",
    ToolOutcome.ABORTED: "This tool did not run: the turn was cancelled before it started.",
    ToolOutcome.NOT_STARTED: (
        "This tool did not run: an earlier tool call in the same batch ended unknown or was "
        "cancelled, so this one was held back."
    ),
}

# The unknown result for a request whose answer was never saved (a crash or restart).
INTERRUPTED = ToolResult(
    ToolOutcome.UNKNOWN, "the call stopped before this tool finished; it may have run"
)


def tool_message(result: ToolResult) -> str:
    """The text of the tool message the model reads."""
    lines = [f"{OUTCOME_LINE}{result.outcome.value}"]
    if meaning := _MEANING.get(result.outcome):
        lines.append(meaning)
    if result.text:
        lines.append(result.text)
    return "\n".join(lines)


def tool_body(content: str | None) -> str:
    """``content`` without its ``outcome:`` line, so a JSON result can be parsed."""
    text = content or ""
    if text.startswith(OUTCOME_LINE):
        _, _, text = text.partition("\n")
    return text


def outcome_of(content: str | None) -> str | None:
    """The outcome named on the first line of a tool message, or None for old messages."""
    first = (content or "").split("\n", 1)[0]
    return first[len(OUTCOME_LINE) :] if first.startswith(OUTCOME_LINE) else None
