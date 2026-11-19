"""The live call view's API: one call's conversation, state, tools and compliance events.

What it checks, with the real local database and made-up calls:
  - the list gives each call's debtor, account, stage and whether it ended
  - one call's conversation is in order, caller and agent, with the stage, the
    verified flag, the tools it ran (arguments and result), a plan waiting for a
    person, the sentence the filter blocked and a stop trigger from the audit log
  - an ended call carries its recorded outcome and summary
  - never a full phone number or a secret, in the list or the call, even when the
    saved text and the debtor's own phone row have one (the negative case)
  - an unknown or odd call id is a 404 that names no other call
  - the routes have no write methods: POST, PUT, PATCH and DELETE are refused
  - one call's transcript, tools and events never include another call's
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from compliance.audit import record
from compliance.decision import block
from harness.api import app
from harness.call_state import CallState, Debtor, Stage
from harness.call_view import scrub_text
from harness.db import connect
from harness.memory import PostgresSession
from model.fake import tool_call

client = TestClient(app)

FULL_NUMBER = "+14045550187"
SECRET = "sk-live-AbCdEf1234567890"


def _call_id() -> str:
    return f"callview_{uuid.uuid4().hex[:12]}"


def _seed_call(debtor: Debtor, call_id: str, *, words: str = "Hello, who is this?") -> None:
    """A made-up call in progress: greeting, verify, two tool calls, one blocked sentence."""
    session = PostgresSession.for_call(call_id)
    session.append({"role": "user", "content": words})
    session.append({"role": "assistant", "content": "Hi, am I speaking with Sam?"})
    session.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                tool_call("lookup_account", "t1", name="Sam Doe", api_key=SECRET),
                tool_call("offer_payment_plan", "t2", down_cents="5000"),
            ],
        }
    )
    session.append(
        {"role": "tool", "tool_call_id": "t1", "content": f"account on file {FULL_NUMBER}"}
    )
    session.put_state(
        CallState.new(call_id, debtor).to_dict()
        | {"stage": Stage.DISCUSS.value, "verified": True, "disclosed": True, "turns": 3}
    )
    with connect() as conn:
        conn.execute(
            "insert into public.call_attempt (debt_id, placed_at, outcome, agentphone_call_id)"
            " values (%s, %s, 'placed', %s)",
            (debtor.debt_id, datetime.now(UTC), call_id),
        )
        conn.execute(
            "insert into public.payment_plan (call_id, debt_id, down_cents, installments,"
            " frequency, installment_cents, last_installment_cents, status)"
            " values (%s, %s, 5000, 6, 'monthly', 20000, 20000, 'pending')",
            (call_id, debtor.debt_id),
        )
        record(
            conn,
            block("utterance_threat", "sounds like a threat", {"sentence": "We will sue you."}),
            kind="utterance_blocked",
            call_id=call_id,
            debt_id=debtor.debt_id,
        )
        record(
            conn,
            block("stop_trigger", "caller asked to stop", {"phone": FULL_NUMBER}),
            kind="stop_trigger",
            call_id=call_id,
            debt_id=debtor.debt_id,
        )


@pytest.fixture
def sam_with_phone(scratch_sam: Debtor) -> Debtor:
    with connect() as conn:
        conn.execute(
            "insert into public.phone (debtor_id, e164, line_type) values (%s, %s, 'cell')",
            (scratch_sam.debtor_id, FULL_NUMBER),
        )
    return scratch_sam


@pytest.mark.db
def test_one_call_has_its_conversation_state_tools_and_compliance_events(sam_with_phone):
    call_id = _call_id()
    _seed_call(sam_with_phone, call_id)
    body = client.get(f"/api/calls/{call_id}").json()

    assert [(m["speaker"], m["text"]) for m in body["messages"]] == [
        ("caller", "Hello, who is this?"),
        ("agent", "Hi, am I speaking with Sam?"),
    ]
    assert body["state"] == {"stage": "discuss", "verified": True, "disclosed": True, "turns": 3}
    assert body["ended"] is False and body["summary"] is None
    assert body["debtor_name"] == f"Sam {sam_with_phone.last_name}"
    tools = {t["name"]: t for t in body["tool_calls"]}
    assert tools["lookup_account"]["arguments"]["name"] == "Sam Doe"
    assert tools["lookup_account"]["result"].startswith("account on file")
    assert tools["offer_payment_plan"]["result"] is None  # no answer saved yet
    assert [p["installments"] for p in body["pending_plans"]] == [6]
    blocked = next(e for e in body["audit_events"] if e["kind"] == "utterance_blocked")
    assert blocked["decision"] == "block" and blocked["sentence"] == "We will sue you."
    assert any(e["kind"] == "stop_trigger" for e in body["audit_events"])


@pytest.mark.db
def test_the_list_names_the_debtor_account_stage_and_whether_it_ended(sam_with_phone):
    call_id = _call_id()
    _seed_call(sam_with_phone, call_id)
    calls = client.get("/api/calls").json()["calls"]
    mine = next(c for c in calls if c["call_id"] == call_id)
    assert mine["stage"] == "discuss" and mine["ended"] is False and mine["outcome"] == "placed"
    assert mine["debtor_name"] == f"Sam {sam_with_phone.last_name}"
    assert mine["account_number"].startswith("TEST-")
    assert len(calls) <= 20


@pytest.mark.db
def test_an_ended_call_shows_its_recorded_outcome_and_summary(sam_with_phone):
    call_id = _call_id()
    _seed_call(sam_with_phone, call_id)
    with connect() as conn:
        conn.execute(
            "update public.call_attempt set outcome = 'answered', ended_at = now()"
            " where agentphone_call_id = %s",
            (call_id,),
        )
        conn.execute(
            "insert into public.call_summary"
            " (call_id, debt_id, outcome, facts, facts_text, narrative)"
            " values (%s, %s, 'answered', %s, 'Call outcome: answered.', 'A short talk.')",
            (call_id, sam_with_phone.debt_id, Jsonb({})),
        )
    body = client.get(f"/api/calls/{call_id}").json()
    assert body["ended"] is True and body["outcome"] == "answered"
    assert body["summary"]["facts"] == "Call outcome: answered."
    assert body["summary"]["narrative"] == "A short talk."
    listed = next(c for c in client.get("/api/calls").json()["calls"] if c["call_id"] == call_id)
    assert listed["ended"] is True and listed["outcome"] == "answered"


@pytest.mark.db
def test_no_full_phone_number_or_secret_is_ever_returned(sam_with_phone):
    call_id = _call_id()
    _seed_call(sam_with_phone, call_id, words=f"Call me back on {FULL_NUMBER} please")
    texts = [
        client.get(f"/api/calls/{call_id}").text,
        client.get("/api/calls").text,
    ]
    for text in texts:
        assert FULL_NUMBER not in text
        assert "4045550187" not in text.replace("-", "").replace(" ", "")
        assert SECRET not in text
    detail = json.loads(texts[0])
    assert "***-0187" in detail["messages"][0]["text"]  # masked, still readable
    assert detail["tool_calls"][0]["arguments"]["api_key"] == "[hidden]"


def test_scrub_masks_long_digit_runs_and_keeps_amounts_and_accounts():
    assert scrub_text("call 404-555-0187 or (404) 555 0187") == "call ***-0187 or ***-0187"
    assert scrub_text("owes $1,250.00 on ACC-1001, 90012") == "owes $1,250.00 on ACC-1001, 90012"
    assert scrub_text("Bearer abc.def.ghi and sk-live-AbCdEf123456") == "[hidden] and [hidden]"


@pytest.mark.db
def test_an_unknown_call_is_a_404_that_names_no_other_call(sam_with_phone):
    real = _call_id()
    _seed_call(sam_with_phone, real)
    for bad in (_call_id(), "has spaces", "x" * 300, "a;drop table x"):
        res = client.get(f"/api/calls/{bad}")
        assert res.status_code == 404
        assert real not in res.text and sam_with_phone.last_name not in res.text


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize("path", ["/api/calls", "/api/calls/some_call"])
def test_the_call_routes_have_no_write_methods(method, path):
    res = getattr(client, method)(path)
    assert res.status_code == 405  # refused before anything runs, so no database is needed


@pytest.mark.db
def test_a_calls_transcript_never_includes_another_calls_messages(sam_with_phone):
    first, second = _call_id(), _call_id()
    _seed_call(sam_with_phone, first, words="first call words")
    _seed_call(sam_with_phone, second, words="second call words")
    one = client.get(f"/api/calls/{first}").json()
    two = client.get(f"/api/calls/{second}").json()
    assert [m["text"] for m in one["messages"]][0] == "first call words"
    assert "second call words" not in json.dumps(one)
    assert "first call words" not in json.dumps(two)
    assert {e["seq"] for e in one["audit_events"]}.isdisjoint(e["seq"] for e in two["audit_events"])
