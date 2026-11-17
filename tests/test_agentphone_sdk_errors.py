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

from phone.client import AgentPhoneClient, CallError


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
