"""``model/`` — everything about talking to a model.

This is the model seam: the one place the rest of the code goes through to call a
model and get a reply. Callers use three names from here: ``chat``, ``chat_stream``
(the same call, streamed) and ``Provider``.
Everything behind them (the HTTP backend, the fake) can change without touching
the callers.

    from model import chat
    reply = chat([{"role": "user", "content": "Say hello."}])
    print(reply.content)

Adapted from codingHarness, keeping only what a single model call needs.
"""

from __future__ import annotations

from model.client import chat, chat_stream
from model.fake import FakeProvider, Streamed, fake, streamed, tool_call, tool_reply
from model.openai_compatible import complete_openai, stream_openai
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
    "Streamed",
    "chat",
    "chat_stream",
    "complete_openai",
    "fake",
    "stream_openai",
    "streamed",
    "tool_call",
    "tool_reply",
]
