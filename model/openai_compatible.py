"""The HTTP backend: one POST to an OpenAI-compatible ``/chat/completions``.

Works with any server that speaks the OpenAI chat format: LM Studio, Ollama,
OpenRouter, OpenAI, vLLM, and so on. A base URL that does not start with
``http://`` or ``https://`` is rejected before any request, with a clear message,
instead of a confusing httpx error.

Adapted from codingHarness.
"""

from __future__ import annotations

import httpx

from model.provider import LLMResponse, Provider


def complete_openai(
    provider: Provider,
    messages: list[dict],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    timeout: float = 180.0,
) -> LLMResponse:
    base_url = provider.base_url.strip()
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(
            f"LLM_BASE_URL must start with http:// or https://, got {provider.base_url!r}"
        )

    payload: dict = {
        "model": model or provider.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    resp = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {provider.api_key}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    choice = data["choices"][0]
    msg = choice.get("message") or {}
    return LLMResponse(
        content=msg.get("content") or "",
        # Reasoning models (Gemma, DeepSeek, ...) put their thinking here.
        reasoning=msg.get("reasoning_content"),
        usage=data.get("usage") or {},
        finish_reason=choice.get("finish_reason"),
        raw=data,
    )
