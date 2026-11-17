"""The reconciler: close the attempts whose call never reported its end.

What it checks, with the real local database and a fake AgentPhone call lookup:
  - a stale attempt (placed long ago, no end recorded) is closed from the lookup and
    the saved state, the same way the webhook does it
  - a recent attempt is left alone, and its call is not even looked up
  - an AgentPhone error, or a call that has not ended yet, leaves the attempt untouched
    and is reported
  - ``--dry-run`` prints what would change and writes nothing
  - running it twice changes nothing the second time
  - a lookup that cannot say whether the caller spoke never records a conversation
    unless the saved state shows a verified person
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest

from dialer.reconcile import main, reconcile
from harness.call_state import CallState, Debtor, Stage
from harness.db import connect
from harness.memory import JsonlSession
from phone.client import CallInfo, CallLookupError, FakePhoneClient

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)


class Setup:
    def __init__(self, sam: Debtor, folder: Path) -> None:
        self.sam = sam
        self.phone = FakePhoneClient()
        self.open = partial(JsonlSession.for_call, folder)

    def attempt(self, *, minutes_ago: int, state: dict | None = None) -> str:
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        with connect() as conn:
            conn.execute(
                "insert into public.call_attempt (debt_id, placed_at, outcome, agentphone_call_id)"
                " values (%s, %s, 'placed', %s)",
                (self.sam.debt_id, NOW - timedelta(minutes=minutes_ago), call_id),
            )
        if state is not None:
            base = CallState.new(call_id, self.sam)
            self.open(call_id).put_state(replace(base, **state).to_dict())  # type: ignore[arg-type]
        return call_id

    def ended(self, call_id: str, *, spoke: bool = True, reason: str | None = None) -> None:
        self.phone.infos[call_id] = CallInfo(
            call_id, "ended", "2026-11-10T17:05:00Z", 120, reason, ("hello",) if spoke else ()
        )

    def run(self, **kwargs: object):
        return reconcile(
            self.phone,
            connect=connect,
            open_session=self.open,
            now=lambda: NOW,
            **kwargs,  # type: ignore[arg-type]
        )

    def row(self, call_id: str) -> tuple:
        with connect() as conn:
            row = conn.execute(
                "select outcome, connected, conversation_with_debtor, ended_at, end_source"
                " from public.call_attempt where agentphone_call_id = %s",
                (call_id,),
            ).fetchone()
        assert row is not None
        return row


@pytest.fixture
def s(scratch_sam: Debtor, tmp_path: Path) -> Setup:
    return Setup(scratch_sam, tmp_path / "sessions")


VERIFIED = {"stage": Stage.DISCUSS, "verified": True, "disclosed": True, "turns": 6}


def test_a_stale_attempt_is_closed_from_the_lookup_and_a_recent_one_is_left_alone(s: Setup) -> None:
    stale = s.attempt(minutes_ago=90, state=VERIFIED)
    recent = s.attempt(minutes_ago=5, state=VERIFIED)
    s.ended(stale)
    s.ended(recent)
    findings = s.run()
    mine = {f.call_id: f for f in findings if f.call_id in (stale, recent)}
    assert mine[stale].action == "recorded" and recent not in mine
    outcome, connected, conversation, ended_at, source = s.row(stale)
    assert (outcome, connected, conversation, source) == ("answered", True, True, "reconciler")
    assert ended_at == datetime(2026, 11, 10, 17, 5, tzinfo=UTC)
    assert s.row(recent)[0] == "placed" and s.row(recent)[3] is None
    assert recent not in s.phone.looked_up


def test_an_agentphone_error_leaves_the_attempt_untouched_and_is_reported(s: Setup) -> None:
    call_id = s.attempt(minutes_ago=90, state=VERIFIED)  # the fake knows nothing of it
    (finding,) = [f for f in s.run() if f.call_id == call_id]
    assert finding.action == "lookup_failed"
    assert s.row(call_id)[0] == "placed" and s.row(call_id)[3] is None


def test_a_call_that_has_not_ended_is_left_alone(s: Setup) -> None:
    call_id = s.attempt(minutes_ago=90, state=VERIFIED)
    s.phone.infos[call_id] = CallInfo(call_id, "in-progress", None)
    (finding,) = [f for f in s.run() if f.call_id == call_id]
    assert finding.action == "still_live" and s.row(call_id)[3] is None


def test_dry_run_writes_nothing(s: Setup) -> None:
    call_id = s.attempt(minutes_ago=90, state=VERIFIED)
    s.ended(call_id)
    (finding,) = [f for f in s.run(dry_run=True) if f.call_id == call_id]
    assert finding.action == "would_record" and "answered" in finding.detail
    assert s.row(call_id)[0] == "placed" and s.row(call_id)[3] is None
    with connect() as conn:
        (n,) = conn.execute(
            "select count(*) from public.audit_event where call_id = %s", (call_id,)
        ).fetchone()  # type: ignore[misc]
    assert n == 0


def test_running_it_twice_changes_nothing_the_second_time(s: Setup) -> None:
    call_id = s.attempt(minutes_ago=90, state=VERIFIED)
    s.ended(call_id)
    s.run()
    first = s.row(call_id)
    again = [f for f in s.run() if f.call_id == call_id]
    assert again == []  # it has an end now, so it is no longer stale
    assert s.row(call_id) == first


def test_a_lookup_that_cannot_tell_if_anyone_spoke_is_not_a_conversation(s: Setup) -> None:
    call_id = s.attempt(minutes_ago=90)  # no state saved, no transcript in the lookup
    s.ended(call_id, spoke=False, reason=None)
    s.run()
    outcome, connected, conversation, *_ = s.row(call_id)
    assert outcome == "ended_unknown" and conversation is False and connected is False


def test_the_cli_refuses_without_agentphone_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("AGENTPHONE_API_KEY", "AGENTPHONE_AGENT_ID", "AGENTPHONE_FROM_NUMBER_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(Path(__file__).parent)  # no .env to fall back on
    assert main(["--dry-run"]) == 2
    assert "AGENTPHONE_API_KEY" in capsys.readouterr().err


def test_the_fake_lookup_error_type_is_what_the_reconciler_expects() -> None:
    with pytest.raises(CallLookupError):
        FakePhoneClient().get_call("nope")
