"""``model/`` — everything about talking to a model.

This is the model seam: the one place the rest of the code goes through to call a
model and get a reply. Callers use two names from here, ``chat`` and ``Provider``.
Everything behind them (the HTTP backend, the fake) can change without touching
the callers.

    from model import chat
    reply = chat([{"role": "user", "content": "Say hello."}])
    print(reply.content)

Adapted from codingHarness, keeping only what a single model call needs.
"""

from __future__ import annotations

from model.client import chat
from model.fake import FakeProvider, fake, tool_call, tool_reply
from model.openai_compatible import complete_openai
from model.provider import (
    DEFAULT_API_KEY,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    LLMResponse,
    Provider,
)

__all__ = [
    "DEFAULT_API_KEY",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "FakeProvider",
    "LLMResponse",
    "Provider",
    "chat",
    "complete_openai",
    "fake",
    "tool_call",
    "tool_reply",
]
