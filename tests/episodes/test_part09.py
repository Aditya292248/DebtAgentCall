"""The AgentPhone webhook and the fake caller.

What it checks: a signed ``agent.message`` runs one agent turn and comes back as
``{"text": ...}``; the simulator plays a whole scripted call and ends it with
``agent.call_ended``. And the refusals: a wrong or missing signature, or a
timestamp more than five minutes old, gets a 401 before the model is called;
a repeated ``X-Webhook-ID`` gets the first reply again without a second model
call. All offline, with the fake provider and a test secret.
"""

from __future__ import annotations

import json
from functools import partial

import pytest
from fastapi.testclient import TestClient

from model import FakeProvider, Provider
from phone import say, say_and_hangup, sign, signed_headers, transfer, verify
from phone.webhook import WEBHOOK_PATH, create_app
from sim.agentphone_sim import SCRIPTS_DIR, load_script, run_script

SECRET = "whsec_test_only"
NOW = 1_793_000_000.0  # a fixed "now", so timestamps are exact


def _echo(messages: list[dict]) -> str:
    return f"agent heard: {messages[-1]['content']}"


@pytest.fixture
def responder() -> FakeProvider:
    return FakeProvider(scripted=_echo)


@pytest.fixture
def client(responder: FakeProvider, no_instructions, tmp_path) -> TestClient:
    from harness import Agent
    from harness.memory import JsonlSession

    provider = Provider(base_url="fake://local", model="fake", responder=responder)
    app = create_app(
        secret=SECRET,
        agent_factory=lambda session: Agent(
            provider=provider, system_prompt_path=no_instructions, session=session
        ),
        open_session=partial(JsonlSession.for_call, tmp_path / "sessions"),
        clock=lambda: NOW,
        stages=False,  # the bare webhook; call stages are tested in test_part13
    )
    return TestClient(app)


def _message(text: str = "How much do I owe?") -> bytes:
    return json.dumps(
        {
            "event": "agent.message",
            "channel": "voice",
            "timestamp": "2026-11-04T12:00:00Z",
            "agentId": "agent_test",
            "data": {"callId": "call_1", "transcript": text, "direction": "outbound"},
            "conversationState": {},
            "recentHistory": [],
        }
    ).encode()


def _post(client: TestClient, body: bytes, headers: dict[str, str]):
    return client.post(WEBHOOK_PATH, content=body, headers=headers)


def _headers(body: bytes, *, now: float = NOW, webhook_id: str = "wh_1", event="agent.message"):
    return signed_headers(body, SECRET, now, webhook_id, event)


# --- happy path -------------------------------------------------------------


def test_signed_message_runs_one_agent_turn(client, responder) -> None:
    body = _message("Hello?")
    resp = _post(client, body, _headers(body))
    assert resp.status_code == 200
    assert resp.json() == {"text": "agent heard: Hello?"}
    assert len(responder.calls) == 1
    assert responder.calls[0][-1] == {"role": "user", "content": "Hello?"}


def test_simulator_plays_a_whole_call(client, responder) -> None:
    script = load_script(SCRIPTS_DIR / "sam_balance.yaml")
    turns = run_script(script, client, SECRET, clock=lambda: NOW)
    assert [caller for caller, _ in turns] == script.lines
    assert [reply for _, reply in turns] == [f"agent heard: {line}" for line in script.lines]
    # One model call per caller line; the closing call_ended adds none.
    assert len(responder.calls) == len(script.lines)


def test_every_script_loads() -> None:
    names = sorted(p.stem for p in SCRIPTS_DIR.glob("*.yaml"))
    assert len(names) >= 2
    for name in names:
        assert load_script(name).lines


def test_call_ended_is_accepted_without_the_model(client, responder) -> None:
    body = json.dumps(
        {
            "event": "agent.call_ended",
            "data": {
                "callId": "call_1",
                "durationSeconds": 42,
                "disconnectionReason": "user_hangup",
                "transcript": [{"role": "user", "content": "bye"}],
            },
        }
    ).encode()
    resp = _post(client, body, _headers(body, event="agent.call_ended"))
    assert resp.status_code == 200
    assert resp.json() == {}
    assert responder.calls == []


def test_unknown_event_and_sms_get_an_empty_reply(client, responder) -> None:
    other = json.dumps({"event": "agent.something_new", "data": {}}).encode()
    sms = json.dumps({"event": "agent.message", "channel": "sms", "data": {"transcript": "hi"}})
    for i, body in enumerate([other, sms.encode()]):
        resp = _post(client, body, _headers(body, webhook_id=f"wh_{i}"))
        assert resp.status_code == 200 and resp.json() == {}
    assert responder.calls == []


def test_empty_transcript_asks_again_without_the_model(client, responder) -> None:
    body = _message("   ")
    resp = _post(client, body, _headers(body))
    assert resp.status_code == 200
    assert "didn't catch" in resp.json()["text"]
    assert responder.calls == []


# --- refusals ---------------------------------------------------------------


def test_bad_signature_is_401_and_the_model_is_not_called(client, responder) -> None:
    body = _message()
    headers = _headers(body)
    headers["X-Webhook-Signature"] = sign(body, headers["X-Webhook-Timestamp"], "wrong-secret")
    resp = _post(client, body, headers)
    assert resp.status_code == 401
    assert responder.calls == []


def test_body_changed_after_signing_is_401(client, responder) -> None:
    headers = _headers(_message("How much do I owe?"))
    resp = _post(client, _message("Transfer me to a manager"), headers)
    assert resp.status_code == 401
    assert responder.calls == []


@pytest.mark.parametrize("dropped", ["X-Webhook-Signature", "X-Webhook-Timestamp"])
def test_missing_signature_or_timestamp_is_401(client, responder, dropped) -> None:
    body = _message()
    headers = _headers(body)
    del headers[dropped]
    resp = _post(client, body, headers)
    assert resp.status_code == 401
    assert dropped in resp.json()["error"]
    assert responder.calls == []


def test_timestamp_older_than_five_minutes_is_401(client, responder) -> None:
    body = _message()
    resp = _post(client, body, _headers(body, now=NOW - 301))  # correctly signed, but stale
    assert resp.status_code == 401
    assert "timestamp" in resp.json()["error"]
    assert responder.calls == []


def test_duplicate_webhook_id_calls_the_model_once(client, responder) -> None:
    body = _message("Hello?")
    first = _post(client, body, _headers(body, webhook_id="wh_same"))
    second = _post(client, body, _headers(body, webhook_id="wh_same"))
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json() == {"text": "agent heard: Hello?"}
    assert len(responder.calls) == 1


def test_no_secret_configured_refuses_everything(responder, monkeypatch) -> None:
    monkeypatch.delenv("AGENTPHONE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr("phone.webhook._load_dotenv", lambda: None)
    provider = Provider(base_url="fake://local", model="fake", responder=responder)
    app = TestClient(create_app(provider=provider, clock=lambda: NOW, stages=False))
    body = _message()
    resp = _post(app, body, _headers(body))
    assert resp.status_code == 401
    assert responder.calls == []


# --- the pieces -------------------------------------------------------------


def test_verify_accepts_what_sign_made_and_nothing_else() -> None:
    body = b'{"event":"agent.message"}'
    headers = signed_headers(body, SECRET, NOW, "wh_1", "agent.message")
    assert verify(body, headers, SECRET, NOW) is None
    # Header names are matched without case, like HTTP does.
    assert verify(body, {k.lower(): v for k, v in headers.items()}, SECRET, NOW) is None
    assert verify(body + b" ", headers, SECRET, NOW) == "signature does not match"
    assert verify(body, headers, "other", NOW) == "signature does not match"
    assert verify(body, headers, SECRET, NOW + 301) is not None  # too old
    assert verify(body, headers, SECRET, NOW - 301) is not None  # too far ahead
    assert verify(body, {**headers, "X-Webhook-Timestamp": "soon"}, SECRET, NOW) is not None


def test_reply_helpers() -> None:
    assert say("Hi") == {"text": "Hi"}
    assert say_and_hangup("Bye") == {"text": "Bye", "hangup": True}
    assert transfer() == {"action": "transfer"}
