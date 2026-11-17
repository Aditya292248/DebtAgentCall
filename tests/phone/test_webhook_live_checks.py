"""Live checks: stop triggers and the sentence filter on every real call turn.

What it checks, through the signed simulator and the webhook with stages on:
  - "stop calling me" ends the call: the model is never called, the ``cease``
    flag is handed to the stop recorder, the reply hangs up, the call is
    STOPPED (the negative case: before this change the model answered)
  - every stop trigger kind does the same in DISCUSS, with its own fixed
    close and flag; a lawyer's flag covers this debt only
  - before verification a dispute leaves the debt alone and gets a neutral
    goodbye, and a wrong number in the same breath wins over it
  - saving the stop can fail (or the session can) and the call still ends
    with the fixed close: fail closed, and an error is logged
  - a later turn on a STOPPED call gets the fixed close, still no model
  - a model scripted to say the balance before verification has that sentence
    dropped, and an ``utterance_blocked`` decision is recorded
  - a failed audit write never lets the blocked sentence through
  - ``record_stop_in_db`` writes the flag, revokes the number's consent (or
    marks a disputed debt ``disputed``, so the dialer skips it) and
    adds a ``stop_trigger`` audit row (``db`` test)
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compliance import SpeechState, check_sentence
from compliance.triggers import CLOSES
from harness import Agent
from harness.call_state import Debtor, Stage
from harness.memory import JsonlSession
from harness.stages import GREETING, STOPPED_CLOSE
from model import FakeProvider, Provider
from phone import signed_headers
from phone.webhook import (
    SAFE_LINE,
    UNVERIFIED_DISPUTE_CLOSE,
    WEBHOOK_PATH,
    Stop,
    create_app,
    debt_as_found,
    record_stop_in_db,
)
from sim.agentphone_sim import load_script, reply_of, run_script

SECRET = "whsec_test_only"
SAM_PHONE = "+12135550101"

# The same person as supabase/seed.sql, so these tests need no database.
SAM = Debtor(
    debtor_id="00000000-0000-4000-8000-000000000001",
    debt_id="00000000-0000-4000-8000-00000000a001",
    first_name="Sam",
    last_name="Doe",
    dob=date(1985, 4, 12),
    zip="90012",
    creditor="Northwind Furniture",
    balance_cents=125000,
)
BALANCE = "Your balance with Northwind Furniture is $1,250.00."
LEAKY = f"Sure. {BALANCE} Anything else?"
TO_DISCUSS = ["Hello?", "Yes, speaking.", "4/12/1985, 90012"]


class Call:
    """The webhook in-process: fake model, fake debtor, lists for audit rows and stops."""

    def __init__(
        self, tmp_path: Path, reply: str = LEAKY, record_stop=None, audit=None, open_session=None
    ) -> None:
        self.responder = FakeProvider(default=reply)
        self.audited: list = []
        self.stops: list[Stop] = []
        self.folder = tmp_path / "sessions"
        provider = Provider(base_url="fake://local", model="fake", responder=self.responder)
        app = create_app(
            secret=SECRET,
            agent_factory=lambda session: Agent(provider=provider, session=session),
            open_session=open_session or (lambda cid: JsonlSession.for_call(self.folder, cid)),
            find_debtor={SAM_PHONE: SAM}.get,
            read_debt=debt_as_found,
            audit=audit or (lambda decision, call_id, debt_id: self.audited.append(decision)),
            record_stop=record_stop or self.stops.append,
        )
        self.client = TestClient(app)

    def turn(self, call_id: str, text: str) -> dict:
        body = json.dumps(
            {
                "event": "agent.message",
                "channel": "voice",
                "data": {
                    "callId": call_id,
                    "transcript": text,
                    "to": SAM_PHONE,
                    "direction": "outbound",
                },
            }
        ).encode()
        headers = signed_headers(
            body, SECRET, time.time(), f"wh_{uuid.uuid4().hex}", "agent.message"
        )
        resp = self.client.post(WEBHOOK_PATH, content=body, headers=headers)
        assert resp.status_code == 200
        return reply_of(resp)

    def stages(self) -> list[Stage]:
        (path,) = self.folder.glob("*.jsonl")
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [Stage(r["state"]["stage"]) for r in records if r["type"] == "state"]


# --- negative: "stop calling me" never reaches the model ------------------------


def test_stop_calling_hangs_up_without_the_model_and_saves_the_flag(tmp_path: Path) -> None:
    call = Call(tmp_path)
    assert call.turn("call_stop", "Hello?") == {"text": GREETING.format(full_name="Sam Doe")}
    reply = call.turn("call_stop", "Stop calling me.")

    assert reply == {"text": CLOSES["cease"], "hangup": True}
    assert call.responder.calls == []
    (stop,) = call.stops
    assert stop.trigger.kind == "cease"
    assert stop.phone == SAM_PHONE
    assert stop.stage == Stage.GREETING
    flag = stop.flag()
    assert flag is not None
    assert (flag.kind, flag.debt_id, flag.source) == ("cease", None, "live_call")
    assert flag.data == {"call_id": "call_stop", "matched": "Stop calling"}
    assert call.stages()[-1] == Stage.STOPPED


def test_stop_calling_through_the_simulator(tmp_path: Path) -> None:
    call = Call(tmp_path)
    turns = run_script(load_script("sam_stop_calling"), call.client, SECRET)
    # The simulator stops at the hangup, so the third line is never sent.
    assert [reply for _, reply in turns] == [GREETING.format(full_name="Sam Doe"), CLOSES["cease"]]
    assert call.responder.calls == []
    assert [s.trigger.flag for s in call.stops] == ["cease"]


@pytest.mark.parametrize(
    ("said", "kind", "flag_kind", "this_debt_only"),
    [
        ("I have a lawyer handling this.", "attorney", "attorney", True),
        ("I filed for bankruptcy last month.", "bankruptcy", "bankruptcy", False),
        ("Please stop calling me.", "cease", "cease", False),
        ("That's not my debt.", "dispute", None, False),
        ("Sorry, wrong person.", "wrong_number", None, False),
    ],
)
def test_each_stop_trigger_in_discuss_ends_the_call(
    tmp_path: Path, said: str, kind: str, flag_kind: str | None, this_debt_only: bool
) -> None:
    call = Call(tmp_path)
    for line in TO_DISCUSS:
        call.turn("call_d", line)
    assert call.stages()[-1] == Stage.DISCUSS

    reply = call.turn("call_d", said)
    assert reply == {"text": CLOSES[kind], "hangup": True}
    assert call.responder.calls == []  # the model never saw it
    (stop,) = call.stops
    assert stop.trigger.kind == kind
    assert stop.stage == Stage.DISCUSS
    flag = stop.flag()
    if flag_kind is None:
        assert flag is None
    else:
        assert flag is not None and flag.kind == flag_kind
        assert flag.debt_id == (SAM.debt_id if this_debt_only else None)
    assert stop.verified is True
    decision = stop.decision()
    assert not decision.allow and decision.rule_id == kind
    assert decision.evidence["stage"] == "DISCUSS"
    assert decision.evidence["verified"] is True
    assert call.stages()[-1] == Stage.STOPPED


def test_a_stopped_call_stays_stopped(tmp_path: Path) -> None:
    call = Call(tmp_path)
    call.turn("call_s", "Hello?")
    call.turn("call_s", "I'm talking to my attorney about this.")
    assert call.turn("call_s", "How much was it again?") == {"text": STOPPED_CLOSE, "hangup": True}
    assert call.responder.calls == []


@pytest.mark.parametrize(
    "said",
    [
        "Sorry, this number isn't mine anymore, Sam had it before.",
        "I already paid it off, who is this?",
    ],
)
def test_a_dispute_before_verification_changes_nothing_and_hints_at_nothing(
    tmp_path: Path, said: str
) -> None:
    call = Call(tmp_path)
    call.turn("call_u", "Hello?")
    reply = call.turn("call_u", said)
    assert reply == {"text": UNVERIFIED_DISPUTE_CLOSE, "hangup": True}
    assert "dispute" not in reply["text"].lower()
    # The strictest filter (nobody verified) lets it through: it reveals nothing.
    assert check_sentence(UNVERIFIED_DISPUTE_CLOSE, SpeechState(creditor=SAM.creditor)).allow
    assert call.responder.calls == []
    (stop,) = call.stops
    assert stop.trigger.kind == "dispute" and stop.verified is False
    assert not stop.marks_disputed  # the debt stays as it is
    decision = stop.decision()
    assert decision.evidence["verified"] is False and decision.evidence["escalate"] is True
    assert call.stages()[-1] == Stage.STOPPED


def test_before_verification_a_wrong_number_wins_over_a_dispute(tmp_path: Path) -> None:
    call = Call(tmp_path)
    call.turn("call_w", "Hello?")
    reply = call.turn("call_w", "That phone wasn't mine, wrong number.")
    assert reply == {"text": CLOSES["wrong_number"], "hangup": True}
    assert [s.trigger.kind for s in call.stops] == ["wrong_number"]


# --- fail closed: a failed write still ends the call ----------------------------


def test_a_failed_save_still_says_the_close_and_hangs_up(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(stop: Stop) -> None:
        raise RuntimeError("database is down")

    call = Call(tmp_path, record_stop=broken)
    call.turn("call_f", "Hello?")
    with caplog.at_level(logging.ERROR, logger="phone.webhook"):
        reply = call.turn("call_f", "Don't call me again.")
    assert reply == {"text": CLOSES["cease"], "hangup": True}
    assert call.responder.calls == []
    assert "NOT saved" in caplog.text and "cease" in caplog.text
    assert call.stages()[-1] == Stage.STOPPED


def test_a_broken_session_still_says_the_close_and_hangs_up(tmp_path: Path) -> None:
    def no_session(call_id: str):  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    call = Call(tmp_path, open_session=no_session)
    assert call.turn("call_b", "Take me off your list.") == {
        "text": CLOSES["cease"],
        "hangup": True,
    }
    assert call.responder.calls == []
    (stop,) = call.stops  # still saved, with the stage unknown
    assert stop.stage is None


# --- the sentence filter on the model's reply -----------------------------------


def test_balance_before_verification_is_not_spoken(tmp_path: Path) -> None:
    call = Call(tmp_path, reply=f"{BALANCE} May I ask who I'm speaking with?")
    call.turn("call_v", "Hello?")
    reply = call.turn("call_v", "Who is this?")  # GREETING: the model answers
    assert reply == {"text": SAFE_LINE}  # the first sentence is blocked: the turn stops there
    assert len(call.responder.calls) == 1
    assert [(d.rule_id, d.evidence["sentence"]) for d in call.audited] == [
        ("third_party_disclosure", BALANCE)
    ]
    assert call.stages()[-1] == Stage.GREETING


def test_a_reply_that_is_all_debt_talk_becomes_the_safe_line(tmp_path: Path) -> None:
    call = Call(tmp_path, reply=BALANCE)
    call.turn("call_x", "Hello?")
    call.turn("call_x", "Yes, speaking.")
    assert call.turn("call_x", "Why do you need that?") == {"text": SAFE_LINE}  # VERIFY


def test_a_failed_audit_write_never_lets_the_sentence_through(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(decision, call_id, debt_id) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("database is down")

    call = Call(tmp_path, reply=f"{BALANCE} Who am I speaking with?", audit=broken)
    call.turn("call_a", "Hello?")
    with caplog.at_level(logging.ERROR, logger="phone.webhook"):
        reply = call.turn("call_a", "Who is this?")
    assert reply == {"text": SAFE_LINE}
    assert "not audited" in caplog.text


# --- the database writer ---------------------------------------------------------


@pytest.fixture
def throwaway_debtor():  # type: ignore[no-untyped-def]
    """A made-up debtor, debt and phone in the local database, deleted afterwards."""
    from harness.db import connect

    tag = uuid.uuid4().hex[:8]
    phone = f"+1999{int(tag, 16) % 10_000_000:07d}"
    with connect() as conn:
        debtor_id = conn.execute(
            "insert into public.debtor (first_name, last_name, dob, address, zip, state) "
            "values ('Test', 'Stopper', '1990-01-01', '1 Test St', '90012', 'CA') "
            "returning id::text"
        ).fetchone()[0]
        debt_id = conn.execute(
            "insert into public.debt (debtor_id, account_number, creditor, balance_cents) "
            "values (%s, %s, 'Test Creditor', 100) returning id::text",
            (debtor_id, f"TEST-{tag}"),
        ).fetchone()[0]
        conn.execute(
            "insert into public.phone (debtor_id, e164, line_type, consent_source, consent_at) "
            "values (%s, %s, 'cell', 'test', now())",
            (debtor_id, phone),
        )
    debtor = Debtor(debtor_id, debt_id, "Test", "Stopper", date(1990, 1, 1), "90012", "T", 100)
    try:
        yield debtor, phone, f"test_{tag}_stop"
    finally:
        with connect() as conn:  # flags, phone and debt go with the debtor
            conn.execute("delete from public.debtor where id = %s", (debtor_id,))


def _written(debtor: Debtor, call_id: str) -> tuple:
    from harness.db import connect

    with connect() as conn:
        flags = conn.execute(
            "select kind, debt_id, source, data->>'call_id' from public.flags where debtor_id = %s",
            (debtor.debtor_id,),
        ).fetchall()
        revoked = conn.execute(
            "select revoked_at is not null from public.phone where debtor_id = %s",
            (debtor.debtor_id,),
        ).fetchone()[0]
        status = conn.execute(
            "select status from public.debt where id = %s", (debtor.debt_id,)
        ).fetchone()[0]
        audit = conn.execute(
            "select kind, rule_id, decision, debt_id::text from public.audit_event "
            "where call_id = %s",
            (call_id,),
        ).fetchall()
    return flags, revoked, status, audit


@pytest.mark.db
def test_record_stop_in_db_writes_flag_consent_and_audit_row(throwaway_debtor) -> None:  # type: ignore[no-untyped-def]
    from compliance.triggers import detect

    debtor, phone, call_id = throwaway_debtor
    (trigger,) = detect("stop calling me")
    record_stop_in_db(Stop(trigger, call_id, debtor, phone, Stage.DISCUSS))
    flags, revoked, status, audit = _written(debtor, call_id)
    assert flags == [("cease", None, "live_call", call_id)]
    assert revoked is True
    assert status == "active"
    assert audit == [("stop_trigger", "cease", "block", debtor.debt_id)]


@pytest.mark.db
def test_a_dispute_marks_the_debt_disputed_so_the_dialer_stops(throwaway_debtor) -> None:  # type: ignore[no-untyped-def]
    from compliance.triggers import detect
    from dialer.service import check_debt_status
    from harness.db import connect

    debtor, phone, call_id = throwaway_debtor
    (trigger,) = detect("That's not my debt.")
    record_stop_in_db(Stop(trigger, call_id, debtor, phone, Stage.DISCUSS, verified=True))
    flags, revoked, status, audit = _written(debtor, call_id)
    assert flags == []
    assert revoked is False
    assert status == "disputed"
    assert not check_debt_status(status).allow  # the next campaign run skips it
    with connect() as conn:
        disputes = conn.execute(
            "select verified from public.dispute where call_id = %s", (call_id,)
        ).fetchall()
    assert disputes == [(True,)]
    assert audit == [("stop_trigger", "dispute", "block", debtor.debt_id)]


class _RecordingConn:
    """Stands in for one psycopg connection: keeps every statement, commits nothing."""

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.rowcount = 1

    def __enter__(self) -> _RecordingConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> _RecordingConn:
        self.sql.append(" ".join(sql.split()))
        return self

    def fetchone(self) -> tuple:
        return (1, "0" * 64)  # what audit.record reads back: seq, hash


@pytest.mark.parametrize(
    ("said", "verified", "expected"),
    [
        ("stop calling me", True, ["insert into public.flags", "update public.phone", "audit"]),
        ("That's not my debt.", True, ["into public.dispute", "set status = 'disputed'", "audit"]),
        ("That's not my debt.", False, ["into public.dispute", "audit_event"]),  # debt untouched
        ("I have a lawyer.", False, ["insert into public.flags", "audit_event"]),
        ("wrong number", False, ["audit_event"]),
    ],
)
def test_record_stop_in_db_writes_on_one_connection(
    monkeypatch: pytest.MonkeyPatch, said: str, verified: bool, expected: list[str]
) -> None:
    import harness.db
    from compliance.triggers import detect

    conn = _RecordingConn()
    monkeypatch.setattr(harness.db, "connect", lambda: conn)
    trigger = detect(said)[0]
    record_stop_in_db(Stop(trigger, "call_1", SAM, SAM_PHONE, Stage.DISCUSS, verified))
    assert len(conn.sql) == len(expected)
    for statement, wanted in zip(conn.sql, expected, strict=True):
        assert wanted in statement, statement
