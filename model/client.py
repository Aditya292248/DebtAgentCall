"""``chat()`` — the single entry point for every model call.

It picks the provider (explicit, else from env) and dispatches: to the provider's
``responder`` if it has one, otherwise over HTTP. Callers never branch on which.

Adapted from codingHarness.
"""

from __future__ import annotations

from model.openai_compatible import complete_openai
from model.provider import LLMResponse, Provider


def chat(
    messages: list[dict],
    *,
    model: str | None = None,
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
            temperature=temperature,
            max_tokens=max_tokens,
        )
    return complete_openai(
        provider,
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
