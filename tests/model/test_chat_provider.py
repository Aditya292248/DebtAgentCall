"""The model seam.

What it checks: ``chat()`` sends messages to the model and returns its reply, routed
through a swappable ``Provider``. Runs against the fake provider, so it is offline
and deterministic; the live-model version is ``uv run accept part-01``.
"""

from __future__ import annotations

import pytest

from model import FakeProvider, LLMResponse, Provider, chat, fake


def test_chat_returns_model_content():
    reply = chat([{"role": "user", "content": "hi"}], provider=fake(default="hello there"))
    assert reply.content == "hello there"


def test_chat_forwards_messages_unchanged():
    provider = fake()
    messages = [{"role": "user", "content": "greet Sam Doe"}]
    chat(messages, provider=provider)
    responder = provider.responder
    assert isinstance(responder, FakeProvider)
    assert responder.calls == [messages]


def test_fake_script_list_then_repeats_last():
    provider = fake(scripted=["a", "b"])
    msgs = [{"role": "user", "content": "x"}]
    assert [chat(msgs, provider=provider).content for _ in range(3)] == ["a", "b", "b"]


def test_fake_script_callable_sees_messages():
    provider = fake(scripted=lambda msgs: msgs[-1]["content"].upper())
    assert chat([{"role": "user", "content": "pong"}], provider=provider).content == "PONG"


def test_chat_dispatches_to_responder_not_http():
    seen: dict = {}

    def responder(messages, **kwargs):
        seen["kwargs"] = kwargs
        return LLMResponse(content="from responder")

    # Unroutable base_url: if chat() tried HTTP, this would fail.
    p = Provider(base_url="http://invalid.invalid/v1", model="m", responder=responder)
    assert chat([{"role": "user", "content": "hi"}], provider=p, max_tokens=7).content == (
        "from responder"
    )
    assert seen["kwargs"]["max_tokens"] == 7


def test_from_env_reads_env_vars(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://env-host/v1")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    monkeypatch.setenv("LLM_API_KEY", "env-key")
    p = Provider.from_env()
    assert (p.base_url, p.model, p.api_key, p.responder) == (
        "http://env-host/v1",
        "env-model",
        "env-key",
        None,
    )


# --- negative case -----------------------------------------------------------


@pytest.mark.parametrize("base_url", ["", "   ", "localhost:1234/v1"])
def test_bad_base_url_fails_fast_with_clear_error(base_url: str):
    # No responder, so chat() takes the HTTP path; it must refuse before any request.
    p = Provider(base_url=base_url, model="m")
    with pytest.raises(ValueError, match="LLM_BASE_URL must start with http:// or https://"):
        chat([{"role": "user", "content": "hi"}], provider=p, timeout=1.0)
