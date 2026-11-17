"""AgentPhoneClient reads the real ``agentphone`` SDK's errors.

The SDK raises ``AgentPhoneError(status, message)`` with the code in ``.status``.
A refusal (402, 409, 429, other 4xx) must become a ``CallError``, so the dialer
records that no call was placed; a 5xx stays a raw error ("may have rung").
Only ``calls.make`` is replaced; nothing goes over the network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agentphone import AgentPhoneError, AuthenticationError, RateLimitError

from phone.client import AgentPhoneClient, CallError, CallLookupError


def client_raising(exc: Exception) -> AgentPhoneClient:
    def make(*_args: object, **_kwargs: object) -> None:
        raise exc

    client = AgentPhoneClient(api_key="test-key", agent_id="agent-1", from_number_id="num-1")
    client._sdk = SimpleNamespace(calls=SimpleNamespace(make=make))
    return client


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (AgentPhoneError(402, "no funds"), "no_funds"),
        (AgentPhoneError(409, "number busy"), "busy"),
        (RateLimitError(429, "slow down"), "concurrency"),
        (AuthenticationError(401, "bad key"), "other"),
        (AgentPhoneError(400, "invalid number"), "other"),
    ],
)
def test_an_sdk_refusal_is_a_call_error(exc: Exception, kind: str) -> None:
    with pytest.raises(CallError) as caught:
        client_raising(exc).make_call("+12135550101")
    assert caught.value.kind == kind


def test_an_sdk_5xx_is_raised_as_it_is() -> None:
    exc = AgentPhoneError(503, "unavailable")
    with pytest.raises(AgentPhoneError) as caught:
        client_raising(exc).make_call("+12135550101")
    assert caught.value is exc


# --- reading a call back (safe to repeat, unlike a dial) ---------------------------


def client_reading(*answers: object) -> tuple[AgentPhoneClient, list[int]]:
    """A client whose ``calls.get`` gives each answer in turn: an exception is raised."""
    seen: list[int] = []

    def get(call_id: str) -> object:
        seen.append(1)
        answer = answers[min(len(seen), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = AgentPhoneClient(api_key="test-key", agent_id="agent-1", from_number_id="num-1")
    client._sdk = SimpleNamespace(calls=SimpleNamespace(get=get))
    return client, seen


def _call() -> SimpleNamespace:
    return SimpleNamespace(
        id="call_1",
        status="completed",
        ended_at="2026-11-10T17:05:00Z",
        duration_seconds=61,
        transcripts=[SimpleNamespace(transcript="hello"), SimpleNamespace(transcript="")],
    )


def test_get_call_retries_a_5xx_then_answers() -> None:
    client, seen = client_reading(AgentPhoneError(503, "unavailable"), _call())
    info = client.get_call("call_1", sleep=lambda _s: None)
    assert len(seen) == 2
    assert info.ended_at == "2026-11-10T17:05:00Z" and info.caller_turns == ("hello",)


def test_get_call_gives_up_after_three_tries_with_a_lookup_error() -> None:
    client, seen = client_reading(AgentPhoneError(503, "unavailable"))
    with pytest.raises(CallLookupError):
        client.get_call("call_1", sleep=lambda _s: None)
    assert len(seen) == 3


def test_get_call_does_not_retry_a_4xx() -> None:
    client, seen = client_reading(AgentPhoneError(404, "no such call"))
    with pytest.raises(CallLookupError):
        client.get_call("call_1", sleep=lambda _s: None)
    assert len(seen) == 1
