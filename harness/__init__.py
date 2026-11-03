"""``harness/`` — the agent that holds a conversation with the model.

Callers use one name from here, ``Agent``. It keeps the conversation and sends it
to the model through ``model.chat`` on every turn.
"""

from __future__ import annotations

from harness.agent import Agent

__all__ = ["Agent"]
