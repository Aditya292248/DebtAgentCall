"""The fake provider: a second implementation of the seam, with no network.

A seam you have only one implementation of is just indirection. ``fake`` is the
second one, so tests and ``verify`` run deterministic and offline.

    fake()                                  # always "ok"
    fake(scripted=["a", "b"])               # "a", then "b", then "b" forever
    fake(scripted=lambda msgs: "PONG")      # compute a reply from the messages

Copied from codingHarness unchanged.
"""

from __future__ import annotations

from collections.abc import Callable

from model.provider import LLMResponse, Provider

Script = Callable[[list[dict]], str] | list[str] | None


class FakeProvider:
    def __init__(self, *, scripted: Script = None, default: str = "ok") -> None:
        self.scripted = scripted
        self.default = default
        self.calls: list[list[dict]] = []  # every messages list it was sent

    def __call__(self, messages: list[dict], **_kwargs: object) -> LLMResponse:
        turn = len(self.calls)
        self.calls.append(messages)
        if callable(self.scripted):
            content = self.scripted(messages)
        elif self.scripted:
            content = self.scripted[min(turn, len(self.scripted) - 1)]
        else:
            content = self.default
        return LLMResponse(content=content, finish_reason="stop")


def fake(*, scripted: Script = None, default: str = "ok") -> Provider:
    return Provider(
        base_url="fake://local",
        model="fake",
        responder=FakeProvider(scripted=scripted, default=default),
    )
