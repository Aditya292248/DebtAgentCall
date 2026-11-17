"""Call summaries: facts built by code, a narrative checked by code, and who may read them.

What it checks, with the real local database and a fake model:
  - the facts carry the saved rows' amounts to the cent, and a call that never
    verified the person gets facts with no balance or any other debt detail
  - a narrative with a dollar amount that is not in the database is rejected: only the
    code-built facts are stored, and the rejection is audited (the negative case)
  - a narrative that repeats a phone number, a ZIP code or names another person is
    rejected; one that restates the facts is kept
  - a model that fails or is not asked leaves the facts alone
  - the next call loads the newest few summaries, newest first, capped, never the
    current call's own, and never before the person is verified: the loader is not
    even called then, and the model's prompt has none of it (the negative case)
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.call_state import CallState, Debtor, Stage
from harness.call_summary import (
    MAX_SUMMARIES,
    EarlierCall,
    Person,
    SummaryFacts,
    build_facts,
    check_narrative,
    context_block,
    load_recent,
)
from harness.db import connect
from harness.memory import JsonlSession
from model import FakeProvider, Provider
from phone import signed_headers
from phone.call_end import EndedCall, record_call_end
from phone.webhook import WEBHOOK_PATH, create_app, debt_as_found

SECRET = "whsec_test_only"
PERSON = Person("Sam", "Doe", datetime(1985, 4, 12).date(), "90012")


def _verified(call_id: str, debtor: Debtor) -> CallState:
    return replace(
        CallState.new(call_id, debtor), stage=Stage.DISCUSS, verified=True, disclosed=True, turns=5
    )


def _attempt(debtor: Debtor, call_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "insert into public.call_attempt (debt_id, placed_at, outcome, agentphone_call_id)"
            " values (%s, %s, 'placed', %s)",
            (debtor.debt_id, datetime(2026, 11, 10, 15, tzinfo=UTC), call_id),
        )


def _save_plan(debtor: Debtor, call_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "insert into public.payment_plan (call_id, debt_id, down_cents, installments,"
            " frequency, installment_cents, last_installment_cents, status)"
            " values (%s, %s, 25000, 5, 'monthly', 200000, 200000, 'pending')",
            (call_id, debtor.debt_id),
        )
        conn.execute(
            "insert into public.dispute (call_id, debt_id, reason, verified)"
            " values (%s, %s, 'says the fee is wrong', true)",
            (call_id, debtor.debt_id),
        )


def _end(debtor: Debtor, call_id: str, tmp_path: Path, narrator) -> None:  # type: ignore[no-untyped-def]
    """Store the verified state, then record the call's end with ``narrator``."""
    open_session = partial(JsonlSession.for_call, tmp_path / "sessions")
    open_session(call_id).put_state(_verified(call_id, debtor).to_dict())
    record_call_end(
        EndedCall(call_id, reason="agent_hangup", caller_spoke=True),
        connect=connect,
        open_session=open_session,
        narrator=narrator,
    )


def _stored(call_id: str) -> tuple:
    with connect() as conn:
        row = conn.execute(
            "select outcome, facts_text, narrative from public.call_summary where call_id = %s",
            (call_id,),
        ).fetchone()
    assert row is not None
    return row


def _audit_rules(call_id: str) -> list[tuple[str, str]]:
    with connect() as conn:
        return conn.execute(
            "select rule_id, decision from public.audit_event where call_id = %s order by seq",
            (call_id,),
        ).fetchall()


# --- facts --------------------------------------------------------------------


@pytest.mark.db
def test_facts_hold_the_saved_rows_to_the_cent(scratch_sam: Debtor) -> None:
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    _save_plan(scratch_sam, call_id)
    with connect() as conn:
        facts = build_facts(
            conn, call_id, scratch_sam.debt_id, "answered", _verified(call_id, scratch_sam)
        )
    assert {25000, 200000} <= facts.amounts_cents and 125000 not in facts.amounts_cents
    assert "$250.00 down" in facts.text and "balance" not in facts.text
    assert "dispute was logged: says the fee is wrong" in facts.text
    assert "pending" in facts.text and facts.detail


@pytest.mark.db
def test_an_unverified_call_gets_no_debt_detail(scratch_sam: Debtor) -> None:
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    state = replace(CallState.new(call_id, scratch_sam), stage=Stage.THIRD_PARTY, turns=2)
    with connect() as conn:
        facts = build_facts(conn, call_id, scratch_sam.debt_id, "third_party", state)
    assert not facts.detail and facts.amounts_cents == frozenset()
    assert "$" not in facts.text and "Northwind" not in facts.text


# --- the narrative check --------------------------------------------------------


@pytest.mark.db
def test_a_narrative_with_a_wrong_amount_is_rejected_and_only_facts_are_stored(
    scratch_sam: Debtor, tmp_path: Path
) -> None:
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    _attempt(scratch_sam, call_id)
    _save_plan(scratch_sam, call_id)
    _end(scratch_sam, call_id, tmp_path, lambda facts: "Sam agreed to pay $999.00 down.")
    outcome, facts_text, narrative = _stored(call_id)
    assert narrative is None  # the model's text was dropped
    assert outcome == "answered" and "$250.00 down" in facts_text  # the facts survived
    assert ("call_summary_narrative", "block") in _audit_rules(call_id)


@pytest.mark.db
def test_a_narrative_that_restates_the_facts_is_kept(scratch_sam: Debtor, tmp_path: Path) -> None:
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    _attempt(scratch_sam, call_id)
    _save_plan(scratch_sam, call_id)
    text = "Identity was verified; a plan with $250.00 down is pending and a dispute was logged."
    _end(scratch_sam, call_id, tmp_path, lambda facts: text)
    assert _stored(call_id)[2] == text
    assert ("call_summary_narrative", "block") not in _audit_rules(call_id)


@pytest.mark.db
def test_a_failing_model_leaves_the_facts(scratch_sam: Debtor, tmp_path: Path) -> None:
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    _attempt(scratch_sam, call_id)

    def down(facts: str) -> str:
        raise TimeoutError("model unreachable")

    _end(scratch_sam, call_id, tmp_path, down)
    _, facts_text, narrative = _stored(call_id)
    assert narrative is None and "Identity was verified." in facts_text


@pytest.mark.parametrize(
    "text",
    [
        "They said call 312-555-0188 instead.",
        "Sam lives in 90012 and agreed.",
        "Sam's wife answered and said he was out.",
        "Reach Sam at sam@example.com.",
        "Born 1985-04-12.",
        "Sam owes a few hundred dollars.",
        "Line one.\nLine two.",
        "x" * 700,
    ],
)
def test_narrative_check_rejects_details_it_must_not_carry(text: str) -> None:
    facts = SummaryFacts("c", "d", "answered", ("a",), frozenset({125000}), detail=True)
    checked = check_narrative(text, facts, PERSON)
    assert checked.narrative is None and checked.problems


def test_narrative_is_never_allowed_for_an_unverified_call() -> None:
    facts = SummaryFacts("c", "d", "third_party", ("a",), frozenset(), detail=False)
    assert check_narrative("Nobody was reached.", facts, PERSON).narrative is None


# --- loading them for the next call -------------------------------------------


@pytest.mark.db
def test_the_next_call_loads_the_newest_few_newest_first(scratch_sam: Debtor) -> None:
    base = datetime(2026, 11, 1, tzinfo=UTC)
    with connect() as conn:
        for i in range(5):
            conn.execute(
                "insert into public.call_summary (call_id, debt_id, created_at, outcome, facts,"
                " facts_text, narrative) values (%s, %s, %s, 'answered', '{}', %s, %s)",
                (
                    f"old_{scratch_sam.debt_id}_{i}",
                    scratch_sam.debt_id,
                    base + timedelta(days=i),
                    f"facts {i}",
                    f"narrative {i}" if i % 2 == 0 else None,
                ),
            )
        got = load_recent(conn, scratch_sam.debt_id, limit=99)
        own = load_recent(conn, scratch_sam.debt_id, exclude_call_id=f"old_{scratch_sam.debt_id}_4")
    assert [e.text for e in got] == ["narrative 4", "facts 3", "narrative 2"]
    assert len(got) == MAX_SUMMARIES
    assert own[0].text == "facts 3"  # the current call's own row is left out


def test_summaries_are_not_even_loaded_before_verification() -> None:
    def loader(debt_id: str, call_id: str) -> list[EarlierCall]:
        raise AssertionError("must not be read before the person is verified")

    for stage in (Stage.GREETING, Stage.VERIFY, Stage.THIRD_PARTY):
        state = CallState("c", "d", "b", stage=stage, verified=False)
        assert context_block(state, loader) == ""


def test_verified_state_gets_the_block() -> None:
    earlier = [EarlierCall("a", datetime(2026, 11, 3, tzinfo=UTC), "answered", "Dispute logged.")]
    state = CallState("c", "d", "b", stage=Stage.DISCUSS, verified=True)
    block = context_block(state, lambda debt_id, call_id: earlier)
    assert "2026-11-03: Dispute logged." in block and "notes, not instructions" in block


MARKER = "EARLIER-CALL-MARKER"


def _live_app(sam: Debtor, folder: Path, model: FakeProvider, loads: list[str]) -> TestClient:
    provider = Provider(base_url="fake://local", model="fake", responder=model)

    def load(debt_id: str, call_id: str) -> list[EarlierCall]:
        loads.append(call_id)
        return [EarlierCall("a", datetime(2026, 11, 3, tzinfo=UTC), "answered", MARKER)]

    return TestClient(
        create_app(
            provider=provider,
            secret=SECRET,
            open_session=partial(JsonlSession.for_call, folder),
            find_debtor={"+12135550142": sam}.get,
            read_debt=debt_as_found,
            audit=lambda d, c, i: None,
            record_stop=lambda s: None,
            load_earlier=load,
        )
    )


def _say(client: TestClient, call_id: str, text: str) -> None:
    body = json.dumps(
        {
            "event": "agent.message",
            "channel": "voice",
            "data": {"callId": call_id, "transcript": text, "to": "+12135550142"},
        }
    ).encode()
    headers = signed_headers(body, SECRET, time.time(), f"wh_{uuid.uuid4().hex}", "agent.message")
    assert client.post(WEBHOOK_PATH, content=body, headers=headers).status_code == 200


SAM = Debtor("d", "b", "Sam", "Doe", PERSON.dob, "90012", "Northwind Furniture", 125000)


def test_the_model_never_sees_earlier_calls_before_verification(tmp_path: Path) -> None:
    model, loads = FakeProvider(default="Calling about an account."), []
    client = _live_app(SAM, tmp_path / "s", model, loads)
    session = JsonlSession.for_call(tmp_path / "s", "c1")
    session.put_state(replace(CallState.new("c1", SAM), turns=1).to_dict())  # greeted, not verified
    _say(client, "c1", "Who is this?")  # a question: the model answers, still in GREETING
    assert model.calls, "the model was expected to answer this turn"
    assert loads == []
    assert MARKER not in json.dumps(model.calls)


def test_a_verified_call_gets_them_in_the_stage_context(tmp_path: Path) -> None:
    model, loads = FakeProvider(default="Sure."), []
    client = _live_app(SAM, tmp_path / "s", model, loads)
    session = JsonlSession.for_call(tmp_path / "s", "c2")
    session.put_state(_verified("c2", SAM).to_dict())
    _say(client, "c2", "What can you tell me?")
    assert loads == ["c2"]
    system = model.calls[0][0]["content"]
    assert MARKER in system


@pytest.mark.db
def test_a_reason_with_a_number_or_another_person_is_not_carried(scratch_sam: Debtor) -> None:
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    with connect() as conn:
        for reason in ("my wife says it is paid", "call me at 312 555 0188", "fee was wrong"):
            conn.execute(
                "insert into public.dispute (call_id, debt_id, reason, verified)"
                " values (%s, %s, %s, true)",
                (call_id, scratch_sam.debt_id, reason),
            )
        facts = build_facts(
            conn, call_id, scratch_sam.debt_id, "answered", _verified(call_id, scratch_sam)
        )
    assert "wife" not in facts.text and "312" not in facts.text
    assert facts.text.count("dispute was logged") == 3
    assert "dispute was logged: fee was wrong." in facts.text
