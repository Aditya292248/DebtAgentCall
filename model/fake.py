"""The fake provider: a second implementation of the seam, with no network.

A seam you have only one implementation of is just indirection. ``fake`` is the
second one, so tests and ``verify`` run deterministic and offline.

    fake()                                  # always "ok"
    fake(scripted=["a", "b"])               # "a", then "b", then "b" forever
    fake(scripted=lambda msgs: "PONG")      # compute a reply from the messages

A script entry can also be a full ``LLMResponse``, which is how a test makes the
"model" ask for tools:

    fake(scripted=[tool_reply(tool_call("lookup_account", name="Sam Doe")), "done"])

Copied from codingHarness.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from model.provider import LLMResponse, Provider

Reply = str | LLMResponse
Script = Callable[[list[dict]], Reply] | list[Reply] | None


def tool_call(name: str, call_id: str = "call_1", /, **arguments: object) -> dict:
    """One tool request, in the OpenAI shape a real model returns.

    ``name`` and ``call_id`` are positional-only, so a tool can take an argument
    called ``name`` too: ``tool_call("lookup_account", name="Sam Doe")``.
    """
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def tool_reply(*calls: dict) -> LLMResponse:
    """A model reply that asks for ``calls`` instead of answering."""
    return LLMResponse(content="", tool_calls=list(calls), finish_reason="tool_calls")


class FakeProvider:
    def __init__(self, *, scripted: Script = None, default: str = "ok") -> None:
        self.scripted = scripted
        self.default = default
        self.calls: list[list[dict]] = []  # every messages list it was sent
        self.kwargs: list[dict] = []  # and the other arguments (tools, model, ...)

    def __call__(self, messages: list[dict], **kwargs: object) -> LLMResponse:
        turn = len(self.calls)
        self.calls.append(messages)
        self.kwargs.append(kwargs)
        reply: Reply
        if callable(self.scripted):
            reply = self.scripted(messages)
        elif self.scripted:
            reply = self.scripted[min(turn, len(self.scripted) - 1)]
        else:
            reply = self.default
        if isinstance(reply, LLMResponse):
            return reply
        return LLMResponse(content=reply, finish_reason="stop")


def fake(*, scripted: Script = None, default: str = "ok") -> Provider:
    return Provider(
        base_url="fake://local",
        model="fake",
        responder=FakeProvider(scripted=scripted, default=default),
    )
