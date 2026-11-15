"""Compaction: when a long call nears the context limit, summarize the middle.

Models pay most attention to the start and the end of their context, so we keep:
  - the head: the first ``keep_head`` messages, i.e. how the call opened
  - the tail: the last ``keep_tail`` messages, i.e. what is being said right now
and replace everything in between with one summary message the model writes.
The system prompt (and later, the stage instructions) is not in the history at
all: it is added fresh on every model call, so compaction never touches it.

    keep_head=2, keep_tail=3:
    [hello] [hi] [a] [b] [c] [d] [e] [f] [g]  ->  [hello] [hi] [summary of a..d] [e] [f] [g]

The cut points can move a message or two so the history stays valid:
  - the head grows forward until it ends on a finished assistant turn (not a user
    message, not a tool request still waiting for its results), because the summary
    goes in next as a user message and two user messages in a row break some chat
    templates (Gemma's among them)
  - the tail grows backward until it starts on an assistant message, so a tool
    result is never cut off from the request it answers
A second compaction finds the first summary in its middle and folds it in.

Compliance never depends on this. Whether the person was verified, whether the
debt collector disclosure was said, and whether they asked us to stop are kept
by code outside the messages (``CallState``, the flags tables), not in what the
model remembers. A summary can drop or garble anything, so nothing may ever read
those facts back out of the history, before or after a compaction. The summary
message says so too, so the model doesn't treat it as a record of them.

Token counts here are estimates: about 4 characters per token, which is close for
English and low for numbers and JSON. The agent prefers the exact count the
server reports for its last call and only estimates what came after.

Adapted from codingHarness.
"""

from __future__ import annotations

import json
from collections.abc import Callable

CHARS_PER_TOKEN = 4
PER_MESSAGE_TOKENS = 4  # role markers and separators each message costs
CLIP_CHARS = 2_000  # per message, when rendering the middle for the summarizer

SUMMARY_HEADER = (
    "[Summary of the earlier part of this call, written to save space. It is not a "
    "record of identity verification, the disclosure, or stop requests: the system "
    "tracks those separately.]"
)

SUMMARIZE_PROMPT = (
    "You compress the transcript of a phone call between a debt collection agent and "
    "a caller so the agent can keep going with less context. Write a concise summary "
    "of the transcript you are given. Keep: what the caller said about themselves and "
    "their situation, questions they asked and the answers given, amounts and dates "
    "mentioned exactly as said, tool results, anything the agent promised, and "
    "anything still unresolved. If the transcript starts with an earlier summary, keep "
    "everything it says that still matters. Do not add anything that was not said. "
    "Drop small talk. Write plain prose or bullets, no preamble."
)


def estimate_tokens(
    messages: list[dict],
    tools: list[dict] | None = None,
    count: Callable[[str], int] | None = None,
) -> int:
    """Token count for a payload, including tool schemas if they're sent.

    ``count`` is the model's tokenizer (``text -> tokens``) if there is one. If
    it's missing or fails, fall back to about 4 characters per token.
    """
    texts = [m.get("content") or "" for m in messages]
    texts += [json.dumps(m["tool_calls"]) for m in messages if m.get("tool_calls")]
    if tools:
        texts.append(json.dumps(tools))
    text = "\n".join(texts)

    overhead = PER_MESSAGE_TOKENS * len(messages)
    if count is not None:
        try:
            return overhead + count(text)
        except Exception:  # noqa: BLE001 - a flaky tokenizer must not stop the call
            pass
    return overhead + len(text) // CHARS_PER_TOKEN


def split(messages: list[dict], keep_head: int, keep_tail: int) -> tuple[int, int] | None:
    """``(head_end, tail_start)``: keep ``[:head_end]`` and ``[tail_start:]``,
    summarize the middle. None if, after adjusting the cuts, there is no middle."""
    if keep_head < 1 or keep_tail < 1:
        raise ValueError("keep at least one message at each end")
    n = len(messages)

    def head_can_end_at(h: int) -> bool:
        last, nxt = messages[h - 1], messages[h]
        return last["role"] != "user" and not last.get("tool_calls") and nxt["role"] != "tool"

    head_end = min(keep_head, n)
    while head_end < n and not head_can_end_at(head_end):
        head_end += 1

    tail_start = max(head_end, n - keep_tail)
    while tail_start > head_end and messages[tail_start]["role"] != "assistant":
        tail_start -= 1

    return (head_end, tail_start) if tail_start > head_end else None


def render(messages: list[dict]) -> str:
    """The middle as plain text for the summarizer, each message clipped."""

    def clip(text: str) -> str:
        return text if len(text) <= CLIP_CHARS else text[:CLIP_CHARS] + " [...]"

    lines: list[str] = []
    for m in messages:
        if m["role"] == "tool":
            lines.append(f"[tool result] {clip(m.get('content') or '')}")
            continue
        if m.get("content"):
            lines.append(f"[{m['role']}] {clip(m['content'])}")
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            lines.append(f"[{m['role']} called {fn.get('name')}] {clip(str(fn.get('arguments')))}")
    return "\n".join(lines)


def compact_messages(
    messages: list[dict],
    summarize: Callable[[list[dict]], str],
    keep_head: int,
    keep_tail: int,
) -> list[dict]:
    """Return a new history: head, one summary message, tail. Unchanged if too short.

    If ``summarize`` raises, the error goes to the caller and ``messages`` is untouched.
    """
    cut = split(messages, keep_head, keep_tail)
    if cut is None:
        return list(messages)
    head_end, tail_start = cut
    summary = summarize(messages[head_end:tail_start]).strip() or "(nothing worth keeping)"
    note = {"role": "user", "content": f"{SUMMARY_HEADER}\n{summary}"}
    return [*messages[:head_end], note, *messages[tail_start:]]
