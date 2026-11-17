"""The HTTP backend: one POST to an OpenAI-compatible ``/chat/completions``.

Works with any server that speaks the OpenAI chat format: LM Studio, Ollama,
OpenRouter, OpenAI, vLLM, and so on. A base URL that does not start with
``http://`` or ``https://`` is rejected before any request, with a clear message,
instead of a confusing httpx error.

``stream_openai`` is the same call streamed (``"stream": true``).

Adapted from codingHarness.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx

from model.provider import LLMResponse, Provider


def _base_url(provider: Provider) -> str:
    base_url = provider.base_url.strip()
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(
            f"LLM_BASE_URL must start with http:// or https://, got {provider.base_url!r}"
        )
    return base_url.rstrip("/")


def _payload(
    provider: Provider,
    messages: list[dict],
    model: str | None,
    tools: list | None,
    temperature: float,
    max_tokens: int,
) -> dict:
    _base_url(provider)  # a bad URL fails before anything is built or sent
    payload: dict = {
        "model": model or provider.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
    return payload


def complete_openai(
    provider: Provider,
    messages: list[dict],
    *,
    model: str | None = None,
    tools: list | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    timeout: float = 180.0,
) -> LLMResponse:
    payload = _payload(provider, messages, model, tools, temperature, max_tokens)
    resp = httpx.post(
        f"{_base_url(provider)}/chat/completions",
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
        tool_calls=msg.get("tool_calls") or [],
        usage=data.get("usage") or {},
        finish_reason=choice.get("finish_reason"),
        raw=data,
    )


def stream_openai(
    provider: Provider,
    messages: list[dict],
    *,
    model: str | None = None,
    tools: list | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    timeout: float = 180.0,
    client: httpx.Client | None = None,
) -> Iterator[str | LLMResponse]:
    """The same call with ``"stream": true``: server-sent events, one ``data:`` line
    per delta, ending with ``data: [DONE]``.

    Yields each piece of reply text as it arrives, then the whole ``LLMResponse``.
    Tool calls come in pieces keyed by ``index`` (id and name first, then the
    arguments a few characters at a time); they are put back together here.
    """
    payload = _payload(provider, messages, model, tools, temperature, max_tokens)
    payload["stream"] = True
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict] = {}
    finish: str | None = None
    usage: dict = {}
    http = client or httpx.Client(timeout=timeout)
    try:
        with http.stream(
            "POST",
            f"{_base_url(provider)}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {provider.api_key}"},
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue  # blank lines, comments, "event:" lines
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                event = json.loads(data)
                usage = event.get("usage") or usage
                for choice in event.get("choices") or []:
                    finish = choice.get("finish_reason") or finish
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                    for piece in delta.get("tool_calls") or []:
                        _add_tool_piece(calls, piece)
                    if text := delta.get("content"):
                        content.append(text)
                        yield text
    finally:
        if client is None:
            http.close()
    yield LLMResponse(
        content="".join(content),
        reasoning="".join(reasoning) or None,
        tool_calls=[calls[i] for i in sorted(calls)],
        usage=usage,
        finish_reason=finish,
    )


def _add_tool_piece(calls: dict[int, dict], piece: dict) -> None:
    """Merge one streamed tool-call delta into the call it belongs to."""
    call = calls.setdefault(
        piece.get("index", len(calls)),
        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
    )
    if piece.get("id"):
        call["id"] = piece["id"]
    fn = piece.get("function") or {}
    if fn.get("name"):
        call["function"]["name"] += fn["name"]
    if fn.get("arguments"):
        call["function"]["arguments"] += fn["arguments"]
