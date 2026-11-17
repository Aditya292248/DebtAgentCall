"""``chat()`` — the single entry point for every model call (``chat_stream()``
for the same call, streamed).

It picks the provider (explicit, else from env) and dispatches: to the provider's
``responder`` if it has one, otherwise over HTTP. Callers never branch on which.

Adapted from codingHarness.
"""

from __future__ import annotations

from collections.abc import Iterator

from model.openai_compatible import complete_openai, stream_openai
from model.provider import LLMResponse, Provider


def chat(
    messages: list[dict],
    *,
    model: str | None = None,
    tools: list | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    timeout: float = 180.0,
    provider: Provider | None = None,
) -> LLMResponse:
    # max_tokens is roomy because reasoning models burn tokens thinking before
    # they emit any visible content; too low and you get an empty reply.
    provider = provider or Provider.from_env()
    if provider.responder is not None:
        return provider.responder(
            messages,
            model=model,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    return complete_openai(
        provider,
        messages,
        model=model,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def chat_stream(
    messages: list[dict],
    *,
    model: str | None = None,
    tools: list | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    timeout: float = 180.0,
    provider: Provider | None = None,
) -> Iterator[str | LLMResponse]:
    """Like ``chat``, but streamed: each ``str`` is new reply text as it arrives,
    and the last item is the whole ``LLMResponse`` (content and tool calls).

    A responder with a ``stream`` method streams; one without it is called once
    and its reply comes back as a single piece.
    """
    provider = provider or Provider.from_env()
    responder = provider.responder
    if responder is not None:
        kwargs = {
            "model": model,
            "tools": tools,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        stream = getattr(responder, "stream", None)
        if callable(stream):
            yield from stream(messages, **kwargs)
            return
        resp = responder(messages, **kwargs)
        if resp.content:
            yield resp.content
        yield resp
        return
    yield from stream_openai(
        provider,
        messages,
        model=model,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
