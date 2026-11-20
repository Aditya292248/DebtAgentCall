"""Add, archive and restore a person from the web UI's API.

What it checks, against the real local database with made-up people only:
  - A person added without consent is saved but blocked by the consent rule
    (never allowed); with a consent source and date every check allows, and one
    of the two alone is refused.
  - An archived person is blocked by the pre-call checks (``archived``), can't
    be placed by the dialer, and leaves the dialer's queue.
  - Archiving deletes nothing: the debtor, debt, call attempts and audit rows
    stay, and the audit chain still verifies. Restore brings the person back
    and is audited.
  - A duplicate phone or account number is 409 and writes nothing, and the phone
    message names only the existing account number.
  - A bad phone, ZIP, state, date of birth, amount, account number or half a
    consent record is 422 with the field named, and nothing is written.
  - No response holds a whole phone number, and there is no delete endpoint.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from compliance import check_before_call, may_call
from dialer.service import ACTIVE_SQL, dial_one, plan_queue
from harness.api import app
from harness.db import connect, load_call_facts
from harness.people import check_person
from harness.precall import check_and_record
from phone.client import FakePhoneClient

WHEN = datetime(2026, 11, 5, 19, 0, tzinfo=UTC)  # 11:00 in Los Angeles
client = TestClient(app)

CONSENT = {"consent_source": "signed paper form, made up for tests", "consent_date": "2026-10-01"}


@pytest.fixture
def created() -> Iterator[list[str]]:
    """Account numbers this test made; their debtors are deleted afterwards (audit rows stay)."""
    accounts: list[str] = []
    yield accounts
    with connect() as conn:
        conn.execute(
            "delete from public.debtor where id in (select debtor_id from public.debt"
            " where account_number = any(%s))",
            (accounts,),
        )


def person(created: list[str], **over) -> dict:
    tag = uuid.uuid4().hex[:10].upper()
    body = {
        "first_name": "Casey",
        "last_name": f"Example-{tag}",
        "dob": "1985-04-12",
        "address": "100 Example Street, Los Angeles",
        "zip": "90012",
        "state": "CA",
        "phone": "+1213" + str(uuid.uuid4().int)[:7],
        "line_type": "cell",
        "creditor": "Northwind Furniture",
        "account_number": f"PM-{tag}",
        "balance_cents": 125000,
        "consent_source": "",
        "consent_date": "",
    }
    body.update(over)
    created.append(body["account_number"])
    return body


def rules(account: str) -> dict[str, bool]:
    with connect() as conn:
        facts = load_call_facts(account, conn)
    return {d.rule_id: d.allow for d in check_before_call(facts, WHEN)}


def counts(account: str) -> dict[str, int]:
    with connect() as conn:
        row = conn.execute(
            "select (select count(*) from public.debtor p join public.debt d on d.debtor_id = p.id"
            " where d.account_number = %(a)s),"
            " (select count(*) from public.phone ph join public.debt d"
            " on d.debtor_id = ph.debtor_id"
            " where d.account_number = %(a)s),"
            " (select count(*) from public.debt where account_number = %(a)s),"
            " (select count(*) from public.audit_event where kind = 'person'"
            "  and evidence->>'account_number' = %(a)s)",
            {"a": account},
        ).fetchone()
    return dict(zip(("debtor", "phone", "debt", "audit"), row, strict=True))


def attempt_count(account: str) -> int:
    with connect() as conn:
        return conn.execute(
            "select count(*) from public.call_attempt a join public.debt d on d.id = a.debt_id"
            " where d.account_number = %s",
            (account,),
        ).fetchone()[0]


def audit_since(seq: int, account: str) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "select rule_id from public.audit_event where seq > %s and kind = 'person'"
            " and evidence->>'account_number' = %s order by seq",
            (seq, account),
        ).fetchall()
    return [r[0] for r in rows]


def last_seq() -> int:
    with connect() as conn:
        return conn.execute("select coalesce(max(seq), 0) from public.audit_event").fetchone()[0]


def add_callable(created: list[str]) -> dict:
    body = person(created, **CONSENT)
    assert client.post("/api/people", json=body).status_code == 201
    return body


# --- consent ---------------------------------------------------------------------


@pytest.mark.db
def test_a_person_added_without_consent_is_blocked_by_the_consent_rule(created):
    body = person(created)
    res = client.post("/api/people", json=body)
    assert res.status_code == 201
    assert res.json()["consent_recorded"] is False
    assert rules(body["account_number"])["tcpa_consent"] is False
    with connect() as conn:
        decisions = check_and_record(conn, body["account_number"], WHEN)
        row = conn.execute(
            "select consent_source, consent_at from public.phone where e164 = %s", (body["phone"],)
        ).fetchone()
    assert not may_call(decisions)
    assert row == (None, None)  # nothing was filled in for the person


@pytest.mark.db
def test_a_person_added_with_consent_passes_every_check(created):
    body = add_callable(created)
    assert all(rules(body["account_number"]).values())
    with connect() as conn:
        source, at = conn.execute(
            "select consent_source, consent_at from public.phone where e164 = %s", (body["phone"],)
        ).fetchone()
    assert source == CONSENT["consent_source"] and at.date().isoformat() == "2026-10-01"


@pytest.mark.db
@pytest.mark.parametrize("drop", ["consent_source", "consent_date"])
def test_half_a_consent_record_is_refused_and_nothing_is_saved(created, drop):
    body = person(created, **CONSENT)
    body[drop] = ""
    before = counts(body["account_number"])
    res = client.post("/api/people", json=body)
    assert res.status_code == 422
    assert drop in res.json()["detail"]["problems"]
    assert counts(body["account_number"]) == before


# --- archive ---------------------------------------------------------------------


@pytest.mark.db
def test_an_archived_person_is_blocked_by_the_checks_and_cannot_be_dialed(created):
    account = add_callable(created)["account_number"]
    assert rules(account)["archived"] is True
    assert client.post(f"/api/people/{account}/archive").status_code == 200

    decided = rules(account)
    assert decided["archived"] is False and not all(decided.values())

    phone = FakePhoneClient()
    with connect() as conn:
        outcome = dial_one(conn, account, WHEN, phone)
        planned = plan_queue([account], WHEN, conn).outcomes[0]
        queue = [r[0] for r in conn.execute(ACTIVE_SQL).fetchall()]
    assert phone.calls == [] and outcome.blocked and outcome.attempt_id is None
    assert any(d.rule_id == "archived" and not d.allow for d in outcome.decisions)
    assert planned.outcome is None
    assert account not in queue

    client.post(f"/api/people/{account}/restore")
    with connect() as conn:
        assert account in [r[0] for r in conn.execute(ACTIVE_SQL).fetchall()]


@pytest.mark.db
def test_archiving_deletes_nothing_and_the_audit_chain_still_verifies(created):
    account = add_callable(created)["account_number"]
    with connect() as conn:  # a call attempt, so there is history to keep
        conn.execute(
            "insert into public.call_attempt (debt_id, phone_id, placed_at, outcome)"
            " select d.id, ph.id, now(), 'answered' from public.debt d"
            " join public.phone ph on ph.debtor_id = d.debtor_id where d.account_number = %s",
            (account,),
        )
    before, attempts, seq = counts(account), attempt_count(account), last_seq()
    assert attempts == 1

    assert client.post(f"/api/people/{account}/archive").status_code == 200

    after = counts(account)
    assert (after["debtor"], after["phone"], after["debt"]) == (1, 1, 1)
    assert after["audit"] > before["audit"]
    assert attempt_count(account) == attempts
    with connect() as conn:
        archived = conn.execute(
            "select p.archived_at is not null from public.debtor p join public.debt d"
            " on d.debtor_id = p.id where d.account_number = %s",
            (account,),
        ).fetchone()[0]
    assert archived
    assert audit_since(seq, account) == ["person_archived"]
    assert client.get("/api/audit/verify").json()["ok"] is True


@pytest.mark.db
def test_restore_brings_the_person_back_and_is_audited(created):
    account = add_callable(created)["account_number"]
    seq = last_seq()
    client.post(f"/api/people/{account}/archive")
    res = client.post(f"/api/people/{account}/restore")
    assert res.status_code == 200 and res.json()["archived"] is False
    assert all(rules(account).values())  # consent and history untouched
    assert audit_since(seq, account) == ["person_archived", "person_restored"]
    assert client.get("/api/audit/verify").json()["ok"] is True


@pytest.mark.db
def test_archiving_twice_or_restoring_a_live_person_is_409_and_unknown_is_404(created):
    account = add_callable(created)["account_number"]
    assert client.post(f"/api/people/{account}/restore").status_code == 409
    assert client.post(f"/api/people/{account}/archive").status_code == 200
    assert client.post(f"/api/people/{account}/archive").status_code == 409
    assert client.post("/api/people/NOPE-0/archive").status_code == 404


def test_there_is_no_delete_endpoint():
    assert client.delete("/api/people/ACC-1001").status_code in (404, 405)
    deletes = [r.path for r in app.routes if "DELETE" in (getattr(r, "methods", None) or ())]
    assert deletes == []


# --- duplicates and bad input ----------------------------------------------------


@pytest.mark.db
def test_a_duplicate_phone_is_409_naming_only_the_account_and_writes_nothing(created):
    first = person(created)
    assert client.post("/api/people", json=first).status_code == 201
    second = person(created, phone=first["phone"])
    before = counts(second["account_number"])
    res = client.post("/api/people", json=second)
    assert res.status_code == 409
    message = res.json()["detail"]["message"]
    assert first["account_number"] in message
    assert first["last_name"] not in message and first["phone"] not in message
    assert counts(second["account_number"]) == before


@pytest.mark.db
def test_a_duplicate_account_number_is_409_and_writes_nothing(created):
    first = person(created)
    assert client.post("/api/people", json=first).status_code == 201
    second = person(created, account_number=first["account_number"])
    with connect() as conn:
        debtors = conn.execute("select count(*) from public.debtor").fetchone()[0]
    res = client.post("/api/people", json=second)
    assert res.status_code == 409
    assert res.json()["detail"]["field"] == "account_number"
    with connect() as conn:
        assert conn.execute("select count(*) from public.debtor").fetchone()[0] == debtors


@pytest.mark.db
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("phone", "2135550123"),
        ("phone", "+0123456"),
        ("phone", "+12"),
        ("zip", "9001"),
        ("state", "ZZ"),
        ("state", "ca"),
        ("dob", "2999-01-01"),
        ("dob", "1985-02-30"),
        ("balance_cents", 0),
        ("balance_cents", -5),
        ("balance_cents", 12.5),
        ("balance_cents", "1250.00"),
        ("account_number", ""),
        ("account_number", "has space"),
        ("first_name", " "),
        ("consent_date", "2999-01-01"),
    ],
)
def test_invalid_fields_are_rejected_with_the_field_named_and_nothing_written(
    created, field, value
):
    body = person(created, **CONSENT)
    body[field] = value
    before = counts(body["account_number"])
    res = client.post("/api/people", json=body)
    assert res.status_code == 422
    assert field in res.json()["detail"]["problems"]
    assert counts(body["account_number"]) == before


def test_unknown_fields_are_refused_and_check_person_accepts_a_good_person():
    assert check_person({"nope": 1}) == {"fields": "unknown fields: nope"}
    assert check_person(person([], **CONSENT)) == {}


# --- masking ---------------------------------------------------------------------


@pytest.mark.db
def test_no_response_holds_a_whole_phone_number(created):
    body = add_callable(created)
    account = body["account_number"]
    responses = [
        client.post(f"/api/people/{account}/archive"),
        client.post(f"/api/people/{account}/restore"),
        client.post("/api/people", json=person(created, phone=body["phone"])),
    ]
    for res in responses:
        assert body["phone"] not in res.text and body["phone"][2:] not in res.text
    assert responses[0].json()["phone"] == "***-***-" + body["phone"][-4:]


@pytest.mark.db
def test_the_operator_chat_tools_leave_an_archived_person_out(created):
    from harness.operator_reads import list_debtors

    account = add_callable(created)["account_number"]

    def listed() -> list[str]:
        with connect() as conn:
            out = list_debtors(conn, "balance", "", 1000, WHEN)
        return [d["account"] if "account" in d else str(d) for d in out["debtors"]]

    assert any(account in x for x in listed())
    client.post(f"/api/people/{account}/archive")
    assert not any(account in x for x in listed())
