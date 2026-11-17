"""Test numbers: the owner's own phone skips calling hours and the call limit, nothing else.

What it checks:
  - The negative case: with ``TEST_NUMBERS`` unset, or for a number that is not on
    the list, a 04:00 call is still blocked by ``calling_hours``.
  - A listed number outside hours passes ``calling_hours`` with a reason that says
    it was skipped, and the evidence keeps what the rule would have said.
  - The negative case for the limit: a number not on the list with 7 attempts in
    7 days is still blocked by ``call_frequency``, as is a recent conversation.
  - A listed number with 7 recent attempts, or a conversation, passes
    ``call_frequency`` with the skip reason; missing call history is never skipped.
  - A listed number is still blocked by consent (missing or revoked) and by a
    cease, attorney, bankruptcy or deceased flag. Only the two named rules skip.
  - A listed number inside hours is not marked as skipped.
  - A malformed entry is ignored, never a wildcard; an empty list changes nothing.
  - Against the local database (``db``): ``check_and_record`` writes one audit row
    per skipped rule, ``dial_one`` dials a listed number once at 04:00, dials one
    with 7 recent attempts and still records the attempt, still refuses a listed
    number whose consent was revoked, and ``plan_queue``, ``next_allowed_call``
    and ``/api/precall`` all agree.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from compliance import Attempt, CallFacts, Flag, PhoneConsent, check_before_call, may_call
from compliance.testnumbers import EXEMPT_RULES, EXEMPTION_REASON, parse_test_numbers

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


def seven_attempts(when=NIGHT):
    return [Attempt("d1", when - timedelta(hours=i + 1)) for i in range(7)]


def test_a_number_not_on_the_list_with_seven_attempts_is_blocked_by_call_frequency(monkeypatch):
    monkeypatch.setenv("TEST_NUMBERS", OTHER)
    decisions = check_before_call(facts(attempts=seven_attempts(DAY)), DAY)
    freq = by_rule(decisions)["call_frequency"]
    assert not freq.allow and "7 calls in the last 7 days" in freq.reason
    assert not may_call(decisions)


def test_a_listed_number_with_seven_recent_attempts_skips_call_frequency():
    decisions = check_before_call(
        facts(attempts=seven_attempts(DAY)), DAY, test_numbers=frozenset({PHONE})
    )
    freq = by_rule(decisions)["call_frequency"]
    assert freq.allow and freq.reason == EXEMPTION_REASON
    assert freq.evidence["exemption"] == "test_number"
    assert "7 calls in the last 7 days" in freq.evidence["would_have_blocked"]
    assert may_call(decisions)


def test_a_listed_number_skips_the_pause_after_a_conversation():
    talked = [Attempt("d1", DAY - timedelta(days=2), True, True)]
    decisions = check_before_call(facts(attempts=talked), DAY, test_numbers=frozenset({PHONE}))
    assert by_rule(decisions)["call_frequency"].reason == EXEMPTION_REASON and may_call(decisions)
    unlisted = check_before_call(facts(attempts=talked), DAY, test_numbers=frozenset())
    assert not by_rule(unlisted)["call_frequency"].allow


def test_only_the_two_named_rules_can_be_skipped():
    assert EXEMPT_RULES == {"calling_hours", "call_frequency"}


def test_missing_call_history_is_never_skipped():
    decisions = check_before_call(
        CallFacts("d1", "90012", consent(), None, []), DAY, test_numbers=frozenset({PHONE})
    )
    assert not by_rule(decisions)["call_frequency"].allow and not may_call(decisions)


@pytest.mark.parametrize("kind", ["cease", "attorney", "bankruptcy", "deceased", "dnc"])
def test_a_listed_number_with_a_stop_flag_is_still_blocked(kind):
    flag = Flag(kind, set_at=datetime(2026, 10, 1, tzinfo=UTC))
    f = CallFacts("d1", "90012", consent(), seven_attempts(NIGHT), [flag])
    decisions = check_before_call(f, NIGHT, test_numbers=frozenset({PHONE}))
    by = by_rule(decisions)
    assert by["calling_hours"].allow and by["call_frequency"].allow  # both skipped
    assert len([d for d in decisions if not d.allow]) == 1 and not may_call(decisions)


def test_a_listed_number_with_revoked_consent_and_seven_attempts_is_still_blocked():
    phone = consent(revoked_at=datetime(2026, 10, 1, tzinfo=UTC))
    decisions = check_before_call(
        facts(phone, seven_attempts(NIGHT)), NIGHT, test_numbers=frozenset({PHONE})
    )
    assert [d.rule_id for d in decisions if not d.allow] == ["tcpa_consent"]


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


CASEY, CASEY_PHONE = "ACC-1004", "+16175550104"  # seeded: 7 attempts in the 7 days before SEED_DAY
JORDAN, JORDAN_PHONE = "ACC-1003", "+13125550103"  # seeded: a conversation on 2026-11-02
SEED_DAY = datetime(2026, 11, 5, 19, 0, tzinfo=UTC)  # 14:00 in New York


def _skips(conn, rule: str) -> int:
    return conn.execute(
        "select count(*) from public.audit_event where kind = 'test_number_exemption'"
        " and rule_id = %s",
        (rule,),
    ).fetchone()[0]


@pytest.mark.db
def test_a_debtor_not_on_the_list_is_still_blocked_by_call_frequency(db, monkeypatch):
    from harness.precall import check_and_record

    monkeypatch.setenv("TEST_NUMBERS", PHONE)  # someone else's number is listed
    decisions = check_and_record(db, CASEY, SEED_DAY)
    assert [d.rule_id for d in decisions if not d.allow] == ["call_frequency"]
    db.rollback()


@pytest.mark.db
def test_the_frequency_skip_is_audited_and_the_attempt_is_still_recorded(db, monkeypatch):
    from dialer.service import dial_one
    from harness.precall import check_and_record
    from phone.client import FakePhoneClient

    monkeypatch.setenv("TEST_NUMBERS", CASEY_PHONE)
    before = _skips(db, "call_frequency")
    assert may_call(check_and_record(db, CASEY, SEED_DAY))
    assert _skips(db, "call_frequency") == before + 1
    db.rollback()

    count = (
        "select count(*) from public.call_attempt ca join public.debt d on d.id = ca.debt_id"
        " where d.account_number = %s"
    )
    attempts = db.execute(count, (CASEY,)).fetchone()[0]
    db.rollback()
    phone = FakePhoneClient()
    outcome = dial_one(db, CASEY, SEED_DAY, phone)
    try:
        assert outcome.attempt_id is not None and len(phone.calls) == 1
        assert db.execute(count, (CASEY,)).fetchone()[0] == attempts + 1
    finally:
        db.execute("delete from public.call_attempt where id = %s", (outcome.attempt_id,))
        db.commit()


@pytest.mark.db
def test_next_allowed_call_and_precall_agree_with_the_dialer(db, monkeypatch):
    from fastapi.testclient import TestClient

    from dialer.service import plan_queue
    from harness import operator_reads as reads
    from harness.api import app

    client = TestClient(app)

    def api(account: str) -> dict:
        body = client.get("/api/precall", params={"at": SEED_DAY.isoformat()}).json()
        return next(d for d in body["debts"] if d["account_number"] == account)

    for account, number in ((CASEY, CASEY_PHONE), (JORDAN, JORDAN_PHONE)):
        monkeypatch.delenv("TEST_NUMBERS", raising=False)
        assert api(account)["may_call"] is False
        assert reads.next_allowed_call(db, account, SEED_DAY)["allowed_now"] is False
        db.rollback()

        monkeypatch.setenv("TEST_NUMBERS", number)
        (planned,) = plan_queue([account], SEED_DAY, db).outcomes
        assert planned.outcome == "would_call"
        row = api(account)
        assert row["may_call"] is True
        assert any(
            d["rule_id"] == "call_frequency" and d["reason"] == EXEMPTION_REASON
            for d in row["decisions"]
        )
        result = reads.next_allowed_call(db, account, SEED_DAY)
        assert result["allowed_now"] is True and result["next_allowed"] == SEED_DAY.isoformat()
        db.rollback()


@pytest.mark.db
def test_a_listed_number_with_a_cease_flag_is_still_blocked(db, monkeypatch):
    from harness.precall import check_and_record

    row = db.execute(
        "select p.e164 from public.phone p join public.debt d on d.debtor_id = p.debtor_id"
        " where d.account_number = 'ACC-1007'"
    ).fetchone()
    monkeypatch.setenv("TEST_NUMBERS", row[0])
    decisions = check_and_record(db, "ACC-1007", NIGHT)
    assert not may_call(decisions)
    assert any(d.rule_id == "cease" and not d.allow for d in decisions)
    db.rollback()
