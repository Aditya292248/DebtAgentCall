"""Placing an outbound call: one ``make_call``, never retried.

``PhoneClient`` is the one thing the dialer needs from a phone
provider: ``make_call(to_number, ...) -> call_id``.

``AgentPhoneClient`` wraps the AgentPhone SDK (``agentphone``, imported only
when a client is made, so tests never need it). It reads ``AGENTPHONE_API_KEY``,
``AGENTPHONE_AGENT_ID`` and ``AGENTPHONE_FROM_NUMBER_ID``. It never passes a
``system_prompt``, so the call runs in webhook mode and every word comes from
our server. It turns the SDK's own retries off: ``POST /v1/calls`` has no
idempotency key, so a retry after a lost reply could ring the person twice.

What can go wrong, as a small ``CallError(kind)``:

  ==============  ======================================  ===============
  kind            AgentPhone answer                        the call
  ==============  ======================================  ===============
  ``throttled``   429 with ``Retry-After``                 not placed
  ``concurrency`` 429 without it (too many live calls)    not placed
  ``busy``        409 (our number is busy or activating)  not placed
  ``no_funds``    402                                      not placed
  ``other``       any other 4xx                            not placed
  ==============  ======================================  ===============

Anything else (a timeout, a dropped connection, a 5xx) is *not* a
``CallError``: we don't know whether AgentPhone placed the call, so the error
is raised as it is and the runner marks the attempt ``placed_unknown``.

``FakePhoneClient`` is for tests: it records every call and can be told to
raise any of those errors, or a timeout.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

CallErrorKind = Literal["throttled", "concurrency", "busy", "no_funds", "other"]
CALL_ERROR_KINDS: tuple[CallErrorKind, ...] = (
    "throttled",
    "concurrency",
    "busy",
    "no_funds",
    "other",
)


class CallError(Exception):
    """AgentPhone refused the call, so it was not placed."""

    def __init__(self, kind: CallErrorKind, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind: CallErrorKind = kind
        self.detail = detail


class PhoneClient(Protocol):
    def make_call(
        self,
        to_number: str,
        *,
        disable_recording: bool | None = None,
        ring_timeout_seconds: int | None = None,
    ) -> str:
        """Place one call and return its call id. Raise ``CallError`` if it was refused."""
        ...


def call_error_for(status: int, headers: dict[str, str] | None = None) -> CallError | None:
    """The ``CallError`` for an HTTP refusal, or None when we can't tell if a call exists."""
    names = {k.lower() for k in (headers or {})}
    if status == 429:
        if "retry-after" in names:
            return CallError("throttled", "429 with Retry-After")
        return CallError("concurrency", "429, too many calls at once")
    if status == 409:
        return CallError("busy", "409, the number is busy or still activating")
    if status == 402:
        return CallError("no_funds", "402, the account has no funds")
    if 400 <= status < 500:
        return CallError("other", f"HTTP {status}")
    return None  # 5xx: the call may or may not have been created


def _status_and_headers(exc: Exception) -> tuple[int | None, dict[str, str]]:
    """Pull the HTTP status and headers out of an SDK error, whatever its class."""
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    headers = getattr(response, "headers", None) or {}
    return (status if isinstance(status, int) else None), dict(headers)


@dataclass
class AgentPhoneClient:
    api_key: str
    agent_id: str
    from_number_id: str
    _sdk: Any = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> AgentPhoneClient:
        missing = [
            name
            for name in ("AGENTPHONE_API_KEY", "AGENTPHONE_AGENT_ID", "AGENTPHONE_FROM_NUMBER_ID")
            if not os.environ.get(name)
        ]
        if missing:
            raise RuntimeError(f"set {', '.join(missing)} to place real calls")
        return cls(
            api_key=os.environ["AGENTPHONE_API_KEY"],
            agent_id=os.environ["AGENTPHONE_AGENT_ID"],
            from_number_id=os.environ["AGENTPHONE_FROM_NUMBER_ID"],
        )

    def _client(self) -> Any:
        if self._sdk is None:
            try:
                from agentphone import AgentPhone  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError("install the AgentPhone SDK: uv pip install agentphone") from exc
            try:
                self._sdk = AgentPhone(api_key=self.api_key, max_retries=0)
            except TypeError:
                # An SDK without the option. It must not retry on its own either.
                self._sdk = AgentPhone(api_key=self.api_key)
        return self._sdk

    def make_call(
        self,
        to_number: str,
        *,
        disable_recording: bool | None = None,
        ring_timeout_seconds: int | None = None,
    ) -> str:
        # No system_prompt: webhook mode. Optional fields are sent only when set;
        # the REST names are disableRecording and ringTimeoutSeconds.
        extra: dict[str, Any] = {}
        if disable_recording is not None:
            extra["disableRecording"] = disable_recording
        if ring_timeout_seconds is not None:
            extra["ringTimeoutSeconds"] = ring_timeout_seconds
        kwargs: dict[str, Any] = {"from_number_id": self.from_number_id}
        if extra:
            kwargs["extra_body"] = extra
        try:
            call = self._client().calls.make(self.agent_id, to_number, **kwargs)
        except Exception as exc:
            status, headers = _status_and_headers(exc)
            error = call_error_for(status, headers) if status is not None else None
            if error is not None:
                raise error from exc
            raise  # timeout, network or 5xx: unknown whether the call exists
        call_id = getattr(call, "id", None) or (call.get("id") if isinstance(call, dict) else None)
        if not call_id:
            raise RuntimeError("AgentPhone answered without a call id")
        return str(call_id)


@dataclass
class FakePhoneClient:
    """Records each ``make_call``. Queue errors with ``fail_next``; calls succeed otherwise."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    errors: list[BaseException] = field(default_factory=list)
    on_call: Any = None  # optional hook(to_number), run before answering
    _ids: Iterator[int] = field(default_factory=lambda: iter(range(1, 1_000_000)))

    def fail_next(self, error: CallErrorKind | BaseException) -> None:
        self.errors.append(CallError(error) if isinstance(error, str) else error)

    def make_call(
        self,
        to_number: str,
        *,
        disable_recording: bool | None = None,
        ring_timeout_seconds: int | None = None,
    ) -> str:
        self.calls.append(
            {
                "to_number": to_number,
                "disable_recording": disable_recording,
                "ring_timeout_seconds": ring_timeout_seconds,
            }
        )
        if self.on_call is not None:
            self.on_call(to_number)
        if self.errors:
            raise self.errors.pop(0)
        return f"fake-call-{next(self._ids)}"
