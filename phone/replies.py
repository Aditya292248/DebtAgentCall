"""The replies we send back to AgentPhone during a call.

AgentPhone speaks the ``text`` to the caller. ``hangup`` ends the call after
speaking it, and ``action: transfer`` hands the call to the agent's
``transferNumber``.

A reply can also be streamed: ``Content-Type: application/x-ndjson``, one JSON
object per line. AgentPhone starts speaking the first line while we are still
working. Every line but the last says ``"interim": true``; the last one has no
``interim`` and closes the turn. Per the AgentPhone docs (docs.agentphone.ai,
Calls and Webhooks guides, read 2026-11-16) the same fields as a single reply
apply, and later lines must still arrive within the webhook's read timeout
(30 s by default). The docs show a final line with ``text`` only; a final line
carrying ``hangup`` or ``action``, or with empty ``text``, is not shown there.

``SentenceBuffer`` turns the model's text pieces into whole sentences, so each
one can be checked before it is sent.
"""

from __future__ import annotations

import json

from compliance import split_sentences

NDJSON = "application/x-ndjson"


def say(text: str) -> dict:
    """Speak ``text`` and wait for the caller."""
    return {"text": text}


def say_and_hangup(text: str) -> dict:
    """Speak ``text``, then end the call."""
    return {"text": text, "hangup": True}


def transfer() -> dict:
    """Hand the call to a person at the agent's transfer number."""
    return {"action": "transfer"}


def interim(text: str) -> dict:
    """One streamed line that is spoken while the turn stays open."""
    return {"text": text, "interim": True}


def ndjson_line(chunk: dict) -> bytes:
    """A chunk as one NDJSON line."""
    return json.dumps(chunk, separators=(",", ":")).encode() + b"\n"


class SentenceBuffer:
    """Collects streamed text and hands back each sentence once it is complete.

    A sentence is complete once ``compliance.split_sentences`` would cut after
    it whatever came next: "Thanks. " is complete (the space shows it), but
    "$1,250." waits, since the next piece may make it "$1,250.50", and so does
    "Mr." before "Doe". ``flush()`` gives what is left at the end of the reply.
    """

    _NEXT = "\x00"  # stands for "more text follows", to ask the splitter where it cuts

    def __init__(self) -> None:
        self._text = ""

    def feed(self, piece: str) -> list[str]:
        self._text += piece
        pieces = split_sentences(self._text + self._NEXT)
        done, rest = pieces[:-1], pieces[-1].removesuffix(self._NEXT)
        if not done:
            return []
        self._text = self._text[self._text.rindex(rest) :] if rest else ""
        return done

    def flush(self) -> list[str]:
        pieces, self._text = split_sentences(self._text), ""
        return pieces
