"""The read-only web API behind the UI's pre-call badges and audit indicator.

What it checks: ``GET /api/precall`` gives the same decisions as
``check_before_call`` for the seeded debtors (a stop request blocks Morgan Patel, the
clock blocks Sam Doe at 9:30pm), a time without a zone is refused, the endpoint
writes nothing to the audit log, and ``GET /api/audit/verify`` reports the chain.
Everything here reads the real local database.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harness.api import app

AFTERNOON = "2026-11-05T19:00:00+00:00"  # 14:00 New York, 11:00 Los Angeles
EVENING_LA = "2026-11-06T05:30:00+00:00"  # 21:30 in Los Angeles

client = TestClient(app)


def _debt(body: dict, account: str) -> dict:
    return next(d for d in body["debts"] if d["account_number"] == account)


def _blocked(debt: dict) -> list[str]:
    return [d["rule_id"] for d in debt["decisions"] if not d["allow"]]


@pytest.mark.db
def test_a_debtor_with_a_stop_request_comes_back_blocked_by_cease():
    res = client.get("/api/precall", params={"at": AFTERNOON})
    assert res.status_code == 200
    morgan = _debt(res.json(), "ACC-1007")
    assert morgan["name"] == "Morgan Patel"
    assert morgan["may_call"] is False
    assert _blocked(morgan) == ["cease"]
    cease = next(d for d in morgan["decisions"] if d["rule_id"] == "cease")
    assert cease["reason"]


@pytest.mark.db
def test_sam_is_ready_in_the_afternoon_and_blocked_at_930pm():
    sam = _debt(client.get("/api/precall", params={"at": AFTERNOON}).json(), "ACC-1001")
    assert sam["may_call"] is True and _blocked(sam) == []
    sam = _debt(client.get("/api/precall", params={"at": EVENING_LA}).json(), "ACC-1001")
    assert sam["may_call"] is False and _blocked(sam) == ["calling_hours"]


@pytest.mark.db
def test_every_debt_gets_all_six_decisions():
    body = client.get("/api/precall", params={"at": AFTERNOON}).json()
    assert body["at"] == AFTERNOON
    assert all(len(d["decisions"]) == 6 for d in body["debts"])


@pytest.mark.db
def test_looking_at_the_badges_writes_nothing_to_the_audit_log():
    from harness.db import connect

    def count() -> int:
        with connect() as conn:
            row = conn.execute("select count(*) from public.audit_event").fetchone()
        return row[0] if row else 0

    before = count()
    client.get("/api/precall", params={"at": AFTERNOON})
    assert count() == before


def test_a_time_without_a_zone_is_refused():
    res = client.get("/api/precall", params={"at": "2026-11-05T21:30:00"})
    assert res.status_code == 400
    assert "time zone" in res.json()["detail"]


def test_a_time_that_is_not_a_time_is_refused():
    assert client.get("/api/precall", params={"at": "tonight"}).status_code == 400


def test_a_database_that_is_down_is_a_clear_503(monkeypatch):
    # Port 9 (discard) has nothing listening, so the connection is refused at once.
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:9/postgres")
    for path in ("/api/precall", "/api/audit/verify"):
        res = client.get(path)
        assert res.status_code == 503
        assert "database not reachable" in res.json()["detail"]


def test_another_database_error_is_a_500_that_says_why():
    import psycopg

    from harness import api

    def broken(*_args, **_kwargs):
        raise psycopg.errors.UndefinedTable('relation "public.flags" does not exist')

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(api, "connect", broken)
        res = TestClient(app, raise_server_exceptions=False).get("/api/precall")
    assert res.status_code == 500
    assert "UndefinedTable" in res.json()["detail"]


def test_acceptance_fails_without_a_database(monkeypatch):
    # Skipped db tests must not count as a pass: no database means no acceptance.
    from tasks.checks import _accept_part12

    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:9/postgres")
    assert _accept_part12() is False


@pytest.mark.db
def test_the_audit_verify_endpoint_reports_the_chain():
    body = client.get("/api/audit/verify").json()
    assert body["ok"] is True and body["problems"] == []
    assert body["rows"] >= 0
