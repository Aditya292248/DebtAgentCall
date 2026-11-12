"""The replies we send back to AgentPhone during a call.

AgentPhone speaks the ``text`` to the caller. ``hangup`` ends the call after
speaking it, and ``action: transfer`` hands the call to the agent's
``transferNumber``. Streamed (NDJSON) replies come later.
"""

from __future__ import annotations


def say(text: str) -> dict:
    """Speak ``text`` and wait for the caller."""
    return {"text": text}


def say_and_hangup(text: str) -> dict:
    """Speak ``text``, then end the call."""
    return {"text": text, "hangup": True}


def transfer() -> dict:
    """Hand the call to a person at the agent's transfer number."""
    return {"action": "transfer"}
