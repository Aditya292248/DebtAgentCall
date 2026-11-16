"""Test numbers: the owner's own phone may be called outside hours, nothing else changes.

What it checks:
  - The negative case: with ``TEST_NUMBERS`` unset, or for a number that is not on
    the list, a 04:00 call is still blocked by ``calling_hours``.
  - A listed number outside hours passes ``calling_hours`` with a reason that says
    it was skipped, and the evidence keeps what the rule would have said.
  - A listed number is still blocked by consent (missing or revoked) and by the
    7-in-7 limit. The exemption touches no other rule.
  - A listed number inside hours is not marked as skipped.
  - A malformed entry is ignored, never a wildcard; an empty list changes nothing.
  - Against the local database (``db``): ``check_and_record`` writes the skip to
    the audit log, ``dial_one`` dials a listed number once at 04:00 and still
    refuses a listed number whose consent was revoked, and ``plan_queue`` agrees.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from compliance import Attempt, CallFacts, PhoneConsent, check_before_call, may_call
from compliance.testnumbers import EXEMPTION_REASON, parse_test_numbers

NIGHT = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)  # 04:00 in Los Angeles
DAY = datetime(2026, 11, 5, 19, 0, tzinfo=UTC)  # 11:00 in Los Angeles
PHONE = "+12135550101"  # synthetic: the seeded Sam Doe number
OTHER = "+12135550199"
SAM, RILEY = "ACC-1001", "ACC-1005"
RILEY_PHONE = "+14045550105"  # consent revoked in the seed


def consent(e164: str = PHONE, **kw) -> PhoneConsent:
    kw.setdefault("consent_at", datetime(2025, 1, 15, tzinfo=UTC))
    return PhoneConsent(e164=e164, **kw)


def facts(phone: PhoneConsent | None = None, attempts=None) -> CallFacts:
    return CallFacts(
        debt_id="d1",
        zip="90012",
        phone=phone or consent(),
        attempts=[] if attempts is None else attempts,
        flags=[],
    )


def by_rule(decisions) -> dict:
    return {d.rule_id: d for d in decisions}


@pytest.fixture(autouse=True)
def no_ambient_test_numbers(monkeypatch):
    """Neither the shell nor a developer's .env may leak a list into these tests."""
    monkeypatch.delenv("TEST_NUMBERS", raising=False)
    monkeypatch.setattr("model.provider._load_dotenv", lambda *a, **k: None)


# --- the negative case: without the list, 04:00 is still blocked -----------------


def test_unset_list_still_blocks_a_4am_call():
    decisions = check_before_call(facts(), NIGHT)
    hours = by_rule(decisions)["calling_hours"]
    assert not hours.allow and "outside 08:00-21:00" in hours.reason
    assert not may_call(decisions)


def test_a_number_not_on_the_list_is_still_blocked_at_4am(monkeypatch):
    monkeypatch.setenv("TEST_NUMBERS", OTHER)
    hours = by_rule(check_before_call(facts(), NIGHT))["calling_hours"]
    assert not hours.allow and "outside 08:00-21:00" in hours.reason


def test_an_empty_list_changes_nothing(monkeypatch):
    baseline = check_before_call(facts(), NIGHT)
    for raw in ("", " , ,"):
        monkeypatch.setenv("TEST_NUMBERS", raw)
        assert check_before_call(facts(), NIGHT) == baseline
    assert check_before_call(facts(), NIGHT, test_numbers=frozenset()) == baseline


# --- a listed number skips the hours rule, and only that ---------------------------


def test_a_listed_number_outside_hours_skips_only_the_hours_rule(monkeypatch):
    monkeypatch.setenv("TEST_NUMBERS", f"{OTHER}, {PHONE}")
    decisions = check_before_call(facts(), NIGHT)
    hours = by_rule(decisions)["calling_hours"]
    assert hours.allow and hours.reason == EXEMPTION_REASON
    assert hours.evidence["exemption"] == "test_number"
    assert "outside 08:00-21:00" in hours.evidence["would_have_blocked"]
    assert may_call(decisions)
    assert [d.rule_id for d in decisions] == [d.rule_id for d in check_before_call(facts(), DAY)]


def test_a_listed_number_inside_hours_is_not_marked_as_skipped():
    decisions = check_before_call(facts(), DAY, test_numbers=frozenset({PHONE}))
    hours = by_rule(decisions)["calling_hours"]
    assert hours.allow and "skipped" not in hours.reason and "exemption" not in hours.evidence


def test_a_listed_number_with_revoked_consent_is_still_blocked():
    phone = consent(revoked_at=datetime(2026, 10, 1, tzinfo=UTC))
    decisions = check_before_call(facts(phone), NIGHT, test_numbers=frozenset({PHONE}))
    by = by_rule(decisions)
    assert by["calling_hours"].allow
    assert not by["tcpa_consent"].allow
    assert not may_call(decisions)


def test_a_listed_number_with_no_consent_is_still_blocked():
    decisions = check_before_call(
        facts(consent(consent_at=None)), NIGHT, test_numbers=frozenset({PHONE})
    )
    assert not by_rule(decisions)["tcpa_consent"].allow and not may_call(decisions)


def test_a_listed_number_is_still_held_to_seven_calls_in_seven_days():
    recent = [Attempt("d1", NIGHT - timedelta(hours=i + 1)) for i in range(7)]
    decisions = check_before_call(facts(attempts=recent), NIGHT, test_numbers=frozenset({PHONE}))
    assert not by_rule(decisions)["call_frequency"].allow and not may_call(decisions)


# --- parsing: bad entries are ignored, never wildcards -----------------------------


def test_malformed_entries_are_ignored_and_logged(caplog):
    with caplog.at_level(logging.WARNING):
        parsed = parse_test_numbers(f"*, +1, 213555, +1213*, abc, {PHONE}, +0123456789")
    assert parsed == {PHONE}
    assert caplog.text.count("ignoring entry") == 6


def test_a_wildcard_or_prefix_entry_matches_nothing(monkeypatch):
    for raw in ("*", "+1", "+1213", "+1213555*"):
        monkeypatch.setenv("TEST_NUMBERS", raw)
        hours = by_rule(check_before_call(facts(), NIGHT))["calling_hours"]
        assert not hours.allow


def test_entries_are_normalized():
    assert parse_test_numbers(" +1 (213) 555-0101 ,+1.213.555.0101") == {PHONE}


# --- against the real local database ------------------------------------------------


@pytest.fixture
def db():
    from harness.db import connect

    with connect() as c:
        yield c
        c.rollback()


def _audit_count(conn, kind: str) -> int:
    row = conn.execute(
        "select count(*) from public.audit_event where kind = %s and reason = %s",
        (kind, EXEMPTION_REASON),
    ).fetchone()
    return row[0]


@pytest.mark.db
def test_the_skip_is_written_to_the_audit_log(db, monkeypatch):
    from harness.precall import check_and_record

    assert not may_call(check_and_record(db, SAM, NIGHT))  # unset: blocked
    monkeypatch.setenv("TEST_NUMBERS", PHONE)
    before = _audit_count(db, "test_number_exemption")
    decisions = check_and_record(db, SAM, NIGHT)
    assert by_rule(decisions)["calling_hours"].reason == EXEMPTION_REASON
    assert _audit_count(db, "test_number_exemption") == before + 1
    assert _audit_count(db, "gate") >= 1
    db.rollback()


@pytest.mark.db
def test_the_dry_run_and_the_dialer_agree(db, monkeypatch):
    from dialer.service import dial_one, plan_queue
    from phone.client import FakePhoneClient

    phone = FakePhoneClient()
    blocked = dial_one(db, SAM, NIGHT, phone)  # unset: nothing placed
    assert blocked.attempt_id is None and phone.calls == []

    monkeypatch.setenv("TEST_NUMBERS", PHONE)
    (planned,) = plan_queue([SAM], NIGHT, db).outcomes
    assert planned.outcome == "would_call"
    db.rollback()  # dial_one wants a connection that is not in a transaction
    outcome = dial_one(db, SAM, NIGHT, phone)
    try:
        assert outcome.attempt_id is not None and len(phone.calls) == 1
    finally:
        db.execute("delete from public.call_attempt where id = %s", (outcome.attempt_id,))
        db.commit()


@pytest.mark.db
def test_a_listed_number_with_revoked_consent_is_not_dialed(db, monkeypatch):
    from dialer.service import dial_one
    from phone.client import FakePhoneClient

    monkeypatch.setenv("TEST_NUMBERS", RILEY_PHONE)
    phone = FakePhoneClient()
    outcome = dial_one(db, RILEY, DAY, phone)
    assert outcome.attempt_id is None and phone.calls == []
    assert any(d.rule_id == "tcpa_consent" and not d.allow for d in outcome.decisions)
