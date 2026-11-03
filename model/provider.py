"""Provider config — where model calls go.

A ``Provider`` is plain data: base_url + model + api_key for any OpenAI-compatible
``/chat/completions`` endpoint (LM Studio, Ollama, OpenRouter, OpenAI, ...).

It may also carry a ``responder``: a callable that returns an ``LLMResponse``
directly. When set, ``chat()`` calls it instead of the network. That one optional
field is what lets the fake provider exist without any special-casing upstream.

``Provider.from_env()`` reads ``LLM_BASE_URL``, ``LLM_MODEL`` and ``LLM_API_KEY``,
falling back to a local ``.env`` file and then to LM Studio's defaults.

Adapted from codingHarness.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE_URL = "http://localhost:1234/v1"  # LM Studio's default server
DEFAULT_MODEL = "google/gemma-3-12b"
DEFAULT_API_KEY = "lm-studio"


@dataclass
class LLMResponse:
    """What one model call returns, normalised across providers."""

    content: str
    reasoning: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    finish_reason: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class Provider:
    base_url: str
    model: str
    api_key: str = "x"
    responder: Callable[..., LLMResponse] | None = None

    @classmethod
    def from_env(cls) -> Provider:
        """Build from LLM_BASE_URL / LLM_MODEL / LLM_API_KEY (``.env`` is a fallback)."""
        _load_dotenv()
        return cls(
            base_url=os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL),
            model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
            api_key=os.environ.get("LLM_API_KEY", DEFAULT_API_KEY),
        )


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Tiny .env reader so we don't need python-dotenv. Real env vars win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
