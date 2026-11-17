"""What the webhook does when a call ends (``agent.call_ended``).

What it checks, with the real local database, the signed webhook, a fake model and
a saved call state per call:
  - a bad signature changes nothing and reaches neither the model nor the reconciler
  - a call where the caller never spoke is NOT recorded as a conversation, so it does
    not start the 7-day pause; a verified conversation is, and it does
  - the same end event twice changes nothing the second time (one audit row, one summary)
  - a call we did not place is answered 200 and audited, never an error
  - the outcome comes from the saved state and ``disconnectionReason``; an end that
    cannot be explained is ``ended_unknown`` (no conversation) with a blocked audit row
  - the saved state is marked ended and its stage closed
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compliance.attempts import check_call_frequency
from harness.call_state import CallState, Debtor, Stage
from harness.db import connect, load_attempts
from harness.memory import JsonlSession
from model import FakeProvider, Provider
from phone import signed_headers
from phone.call_end import EndedCall, derive
from phone.webhook import WEBHOOK_PATH, create_app

pytestmark = pytest.mark.db

SECRET = "whsec_test_only"
NOW = 1_793_000_000.0


class World:
    """An app on the real database, with JSONL sessions and a counting fake model."""

    def __init__(self, sam: Debtor, folder: Path) -> None:
        self.sam = sam
        self.folder = folder
        self.model = FakeProvider(default="ok")
        provider = Provider(base_url="fake://local", model="fake", responder=self.model)
        self.open = partial(JsonlSession.for_call, folder)
        self.client = TestClient(
            create_app(
                provider=provider,
                secret=SECRET,
                open_session=self.open,
                clock=lambda: NOW,
                find_debtor=lambda phone: None,
            )
        )
        self.n = 0

    def place(self, **state: object) -> str:
        """An attempt for Sam's debt with a call id, and (optionally) a saved state."""
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        with connect() as conn:
            conn.execute(
                "insert into public.call_attempt (debt_id, placed_at, outcome, agentphone_call_id)"
                " values (%s, %s, 'placed', %s)",
                (self.sam.debt_id, datetime(2026, 11, 10, 15, tzinfo=UTC), call_id),
            )
        if state:
            base = CallState.new(call_id, self.sam)
            from dataclasses import replace

            self.open(call_id).put_state(replace(base, **state).to_dict())  # type: ignore[arg-type]
        return call_id

    def end(
        self,
        call_id: str,
        *,
        reason: str = "user_hangup",
        said: list[str] | None = None,
        headers: dict | None = None,
        webhook_id: str | None = None,
    ):
        transcript = [{"role": "agent", "content": "Hi, may I speak with Sam?"}]
        transcript += [{"role": "user", "content": line} for line in (said or [])]
        body = json.dumps(
            {
                "event": "agent.call_ended",
                "channel": "voice",
                "timestamp": "2026-11-10T15:05:00Z",
                "agentId": "agent_test",
                "data": {
                    "callId": call_id,
                    "startedAt": "2026-11-10T15:00:00Z",
                    "endedAt": "2026-11-10T15:05:00Z",
                    "durationSeconds": 300,
                    "disconnectionReason": reason,
                    "transcript": transcript,
                },
            }
        ).encode()
        self.n += 1
        hdrs = headers or signed_headers(
            body, SECRET, NOW, webhook_id or f"wh_{call_id}_{self.n}", "agent.call_ended"
        )
        return self.client.post(WEBHOOK_PATH, content=body, headers=hdrs)

    def attempt(self, call_id: str) -> dict:
        with connect() as conn:
            row = conn.execute(
                "select outcome, connected, conversation_with_debtor, ended_at, end_source"
                " from public.call_attempt where agentphone_call_id = %s",
                (call_id,),
            ).fetchone()
        assert row is not None
        keys = ("outcome", "connected", "conversation", "ended_at", "end_source")
        return dict(zip(keys, row, strict=True))

    def audit(self, call_id: str) -> list[tuple]:
        with connect() as conn:
            return conn.execute(
                "select kind, rule_id, decision from public.audit_event where call_id = %s"
                " order by seq",
                (call_id,),
            ).fetchall()

    def state(self, call_id: str) -> dict:
        raw = self.open(call_id).last_state()
        assert raw is not None
        return raw


@pytest.fixture
def world(scratch_sam: Debtor, tmp_path: Path) -> World:
    return World(scratch_sam, tmp_path / "sessions")


def _may_call_again(world: World) -> bool:
    with connect() as conn:
        attempts = load_attempts(conn, world.sam.debt_id)
    when = datetime(2026, 11, 11, 15, tzinfo=UTC)
    return check_call_frequency(attempts, world.sam.debt_id, when).allow


# --- the negative cases ------------------------------------------------------


def test_bad_signature_changes_nothing_and_calls_neither_model_nor_reconciler(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dialer.reconcile

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("the reconciler must not run from a webhook")

    monkeypatch.setattr(dialer.reconcile, "reconcile", boom)
    call_id = world.place(stage=Stage.DISCUSS, verified=True, disclosed=True, turns=4)
    good = world.end(call_id, said=["yes"], webhook_id="wh_probe")  # a signed one, for contrast
    assert good.status_code == 200 and world.attempt(call_id)["outcome"] == "answered"

    model_calls = len(world.model.calls)  # the control call wrote its narrative
    other = world.place(stage=Stage.DISCUSS, verified=True, disclosed=True, turns=4)
    forged = {"Content-Type": "application/json", "X-Webhook-Signature": "v1,nope"}
    resp = world.end(other, said=["yes"], headers=forged)
    assert resp.status_code == 401
    after = world.attempt(other)
    assert after["outcome"] == "placed" and after["ended_at"] is None
    assert len(world.model.calls) == model_calls
    assert world.audit(other) == []


def test_call_where_the_caller_never_spoke_is_not_a_conversation(world: World) -> None:
    call_id = world.place()  # rang, nobody talked, no state saved
    assert world.end(call_id, reason="user_hangup", said=[]).status_code == 200
    got = world.attempt(call_id)
    assert got["outcome"] == "no_answer"
    assert got["connected"] is False and got["conversation"] is False
    assert _may_call_again(world)  # no 7-day pause was started


def test_greeting_only_call_is_not_a_conversation_even_if_the_caller_spoke(world: World) -> None:
    call_id = world.place(stage=Stage.VERIFY, turns=2)  # spoke, never verified
    world.end(call_id, said=["hello", "who is this"])
    got = world.attempt(call_id)
    assert got["outcome"] == "abandoned" and got["conversation"] is False
    assert _may_call_again(world)


def test_verified_conversation_is_recorded_and_starts_the_pause(world: World) -> None:
    call_id = world.place(stage=Stage.DISCUSS, verified=True, disclosed=True, turns=6)
    world.end(call_id, said=["yes this is Sam", "ok"])
    got = world.attempt(call_id)
    assert got["outcome"] == "answered" and got["conversation"] is True
    assert got["connected"] is True and got["end_source"] == "webhook"
    assert not _may_call_again(world)


def test_verified_state_without_any_caller_speech_is_not_a_conversation() -> None:
    state = CallState(
        call_id="c", debtor_id="d", debt_id="b", stage=Stage.DISCUSS, verified=True, turns=0
    )
    verdict = derive(state, EndedCall("c", reason="user_hangup", caller_spoke=False))
    assert verdict.conversation is False


# --- idempotent, unknown calls, outcomes --------------------------------------


def test_the_same_end_event_twice_changes_nothing(world: World) -> None:
    call_id = world.place(stage=Stage.DISCUSS, verified=True, disclosed=True, turns=5)
    world.end(call_id, said=["yes"], webhook_id="wh_a")
    first = world.attempt(call_id)
    audit_rows = world.audit(call_id)
    # A second delivery (a new webhook id, so the in-memory dedupe doesn't catch it).
    resp = world.end(call_id, reason="agent_hangup", said=["yes"], webhook_id="wh_b")
    assert resp.status_code == 200
    assert world.attempt(call_id) == first
    assert world.audit(call_id) == audit_rows
    with connect() as conn:
        (n,) = conn.execute(
            "select count(*) from public.call_summary where call_id = %s", (call_id,)
        ).fetchone()  # type: ignore[misc]
    assert n == 1


def test_unknown_call_id_is_acknowledged_and_audited(world: World) -> None:
    resp = world.end("call_we_never_placed", said=["hi"])
    assert resp.status_code == 200 and resp.json() == {}
    assert ("call_ended", "call_end_unknown_call", "allow") in world.audit("call_we_never_placed")


def test_state_is_marked_ended_and_the_stage_closed(world: World) -> None:
    call_id = world.place(stage=Stage.DISCUSS, verified=True, disclosed=True, turns=5)
    world.end(call_id, said=["yes"])
    state = world.state(call_id)
    assert state["ended"] is True and state["stage"] == "WRAP"
    assert state["verified"] is True  # only the stage moved


def test_a_stopped_call_stays_stopped_and_is_ended_by_stop(world: World) -> None:
    call_id = world.place(stage=Stage.STOPPED, verified=False, turns=3)
    world.end(call_id, said=["stop calling me"])
    got = world.attempt(call_id)
    assert got["outcome"] == "ended_by_stop" and got["conversation"] is False
    assert world.state(call_id)["stage"] == "STOPPED"


@pytest.mark.parametrize(
    ("state", "reason", "said", "outcome"),
    [
        ({"stage": Stage.WRAP, "turns": 3, "verify_attempts": 2}, "agent_hangup", ["x"], "refused"),
        ({"stage": Stage.THIRD_PARTY, "turns": 2}, "agent_hangup", ["not here"], "third_party"),
        ({}, "voicemail_reached", [], "voicemail"),
        ({}, "dial_busy", [], "busy"),
    ],
)
def test_outcome_comes_from_state_and_reason(world, state, reason, said, outcome) -> None:
    call_id = world.place(**state)
    world.end(call_id, reason=reason, said=said)
    got = world.attempt(call_id)
    assert got["outcome"] == outcome and got["conversation"] is False


def test_unexplained_end_is_unknown_audited_and_never_a_conversation(world: World) -> None:
    call_id = world.place()
    world.end(call_id, reason="something_new", said=[])
    got = world.attempt(call_id)
    assert got["outcome"] == "ended_unknown" and got["conversation"] is False
    assert ("call_ended", "call_end_unknown_outcome", "block") in world.audit(call_id)


def test_unreadable_state_changes_nothing(world: World) -> None:
    call_id = world.place(stage=Stage.DISCUSS, verified=True, turns=3)

    def broken(_call_id: str):
        raise OSError("sessions are down")

    app = create_app(
        secret=SECRET, open_session=broken, clock=lambda: NOW, find_debtor=lambda p: None
    )
    world.client = TestClient(app)
    assert world.end(call_id, said=["yes"]).status_code == 200  # never an error loop
    got = world.attempt(call_id)
    assert got["outcome"] == "placed" and got["ended_at"] is None  # the reconciler can retry
