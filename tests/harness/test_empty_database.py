"""The API and the operator chat on a database with no people.

What it checks: ``/api/precall`` and the audit check answer with empty results
instead of errors, the operator's read tools return empty lists and totals of
zero with a note that there are no people yet, and finding someone says the
same instead of "no match". Each test uses an empty ``_test`` database.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from harness.api import app
from harness.memory import JsonlSession
from harness.operator import Operator
from model.fake import fake
from phone.client import FakePhoneClient

pytestmark = pytest.mark.db


def make_operator(tmp_path) -> Operator:
    return Operator(
        JsonlSession.for_call(tmp_path / "chats", "empty"),
        provider=fake(default="ok"),
        phone=FakePhoneClient(),
    )


def test_precall_returns_an_empty_list_not_an_error(empty_database):
    res = TestClient(app).get("/api/precall")
    assert res.status_code == 200
    assert res.json()["debts"] == []


def test_the_audit_check_answers_on_an_empty_database(empty_database):
    assert TestClient(app).get("/api/audit/verify").status_code == 200


def test_list_debtors_is_empty_and_says_why(empty_database, tmp_path):
    for filters in ("", "ready", "blocked", "status:active"):
        result = json.loads(make_operator(tmp_path).list_debtors("balance", filters, ""))
        assert result["debtors"] == [] and result["count"] == 0
        assert "no people yet" in result["note"].lower()


def test_the_portfolio_summary_is_zero_and_says_why(empty_database, tmp_path):
    result = json.loads(make_operator(tmp_path).portfolio_summary(""))
    assert result["debts"] == {}
    assert "no people yet" in result["note"].lower()
    assert "nan" not in json.dumps(result).lower()


def test_finding_someone_says_there_are_no_people_yet(empty_database, tmp_path):
    reply = make_operator(tmp_path).find_debtor("Sam")
    assert reply.startswith("error:") and "no people yet" in reply.lower()
