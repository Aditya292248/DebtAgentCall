"""The fake provider: a second implementation of the seam, with no network.

A seam you have only one implementation of is just indirection. ``fake`` is the
second one, so tests and ``verify`` run deterministic and offline.

    fake()                                  # always "ok"
    fake(scripted=["a", "b"])               # "a", then "b", then "b" forever
    fake(scripted=lambda msgs: "PONG")      # compute a reply from the messages

A script entry can also be a full ``LLMResponse``, which is how a test makes the
"model" ask for tools:

    fake(scripted=[tool_reply(tool_call("lookup_account", name="Sam Doe")), "done"])

Streaming. ``FakeProvider.stream`` is what ``model.chat_stream`` uses. A plain
entry comes back as one piece; a ``streamed(...)`` entry comes back piece by
piece, and a number between pieces is seconds to wait first, so a test can
play a slow model:

    fake(scripted=[streamed("One moment. ", 3.0, "Here it is.")])
    fake(scripted=[streamed("Let me check. ", tool_calls=[tool_call("lookup_account")])])

Either way the call is recorded in ``.calls`` and ``.kwargs`` (with
``stream=True``), and ``.streamed`` counts the streamed ones.

Copied from codingHarness; streaming added here.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

from model.provider import LLMResponse, Provider


@dataclass(frozen=True)
class Streamed:
    """A scripted reply sent in pieces: text, or a float of seconds to wait."""

    pieces: tuple[str | float, ...]
    tool_calls: list[dict] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(p for p in self.pieces if isinstance(p, str))

    def response(self) -> LLMResponse:
        finish = "tool_calls" if self.tool_calls else "stop"
        return LLMResponse(
            content=self.text, tool_calls=list(self.tool_calls), finish_reason=finish
        )


def streamed(*pieces: str | float, tool_calls: list[dict] | None = None) -> Streamed:
    """A reply streamed in ``pieces``; numbers are pauses in seconds."""
    return Streamed(tuple(pieces), list(tool_calls or []))


Reply = str | LLMResponse | Streamed
Script = Callable[[list[dict]], Reply] | Sequence[Reply] | None


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
        self.streamed = 0  # how many of those calls were streamed

    def _next(self, messages: list[dict], kwargs: dict) -> Reply:
        turn = len(self.calls)
        self.calls.append(messages)
        self.kwargs.append(kwargs)
        if callable(self.scripted):
            return self.scripted(messages)
        if self.scripted:
            return self.scripted[min(turn, len(self.scripted) - 1)]
        return self.default

    def __call__(self, messages: list[dict], **kwargs: object) -> LLMResponse:
        reply = self._next(messages, kwargs)
        if isinstance(reply, Streamed):
            return reply.response()
        if isinstance(reply, LLMResponse):
            return reply
        return LLMResponse(content=reply, finish_reason="stop")

    def stream(self, messages: list[dict], **kwargs: object) -> Iterator[str | LLMResponse]:
        """The scripted reply in pieces, then the whole ``LLMResponse``."""
        self.streamed += 1
        reply = self._next(messages, {**kwargs, "stream": True})
        if isinstance(reply, Streamed):
            for piece in reply.pieces:
                if isinstance(piece, str):
                    if piece:
                        yield piece
                else:
                    time.sleep(piece)
            yield reply.response()
            return
        resp = reply if isinstance(reply, LLMResponse) else LLMResponse(reply, finish_reason="stop")
        if resp.content:
            yield resp.content
        yield resp


def fake(*, scripted: Script = None, default: str = "ok") -> Provider:
    return Provider(
        base_url="fake://local",
        model="fake",
        responder=FakeProvider(scripted=scripted, default=default),
    )
