"""Pre-call checks: call limits, consent, stop requests, do-not-call.

What it checks: each check in ``compliance/`` blocks the call it should (the 8th
call in 7 days, a call within 7 days of a conversation, no or revoked consent,
and each stopping flag), and ``check_before_call`` plus ``may_call`` refuse the
call if any one check blocks or any fact is missing. These run offline on
made-up facts. Tests marked ``db`` check that the new tables exist and that the
loaders in ``harness/db.py`` return the seeded rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from compliance import (
    Attempt,
    CallFacts,
    Flag,
    PhoneConsent,
    allow,
    block,
    check_before_call,
    check_call_frequency,
    check_cease,
    check_consent,
    check_dnc,
    may_call,
)

# 2026-11-05 14:00 in New York, 11:00 in Los Angeles: inside calling hours everywhere.
WHEN = datetime(2026, 11, 5, 19, 0, tzinfo=UTC)
DEBT = "debt-1"
OTHER_DEBT = "debt-2"
PHONE = "+12135550101"
GOOD_PHONE = PhoneConsent(
    e164=PHONE, consent_at=datetime(2025, 1, 15, tzinfo=UTC), consent_source="application"
)


def _calls(n: int, debt_id: str = DEBT, **kw) -> list[Attempt]:
    """``n`` unanswered calls, one a day, the last one an hour before ``WHEN``."""
    return [
        Attempt(debt_id=debt_id, placed_at=WHEN - timedelta(hours=1, days=i), **kw)
        for i in range(n)
    ]


def _facts(**overrides) -> CallFacts:
    base = dict(zip="90012", phone=GOOD_PHONE, attempts=[], flags=[])
    base.update(overrides)
    return CallFacts(debt_id=DEBT, **base)


# --- call frequency (Reg F 7-in-7) ---------------------------------------------


def test_the_seventh_call_after_six_is_allowed():
    d = check_call_frequency(_calls(6), DEBT, WHEN)
    assert d.allow
    assert d.rule_id == "call_frequency"
    assert d.evidence["attempts_in_window"] == 6


def test_the_eighth_call_in_seven_days_is_blocked():
    d = check_call_frequency(_calls(7), DEBT, WHEN)
    assert not d.allow
    assert d.rule_id == "call_frequency"
    assert "7 calls in the last 7 days" in d.reason


def test_unanswered_calls_count():
    no_answer = _calls(7, outcome="no_answer", connected=False)
    assert not check_call_frequency(no_answer, DEBT, WHEN).allow


def test_calls_older_than_seven_days_and_other_debts_dont_count():
    old = [Attempt(debt_id=DEBT, placed_at=WHEN - timedelta(days=7, minutes=1))]
    other = _calls(7, debt_id=OTHER_DEBT)
    d = check_call_frequency(_calls(6) + old + other, DEBT, WHEN)
    assert d.allow
    assert d.evidence["attempts_in_window"] == 6


def test_exactly_seven_days_ago_is_outside_the_window():
    edge = [Attempt(debt_id=DEBT, placed_at=WHEN - timedelta(days=7))]
    assert check_call_frequency(_calls(6) + edge, DEBT, WHEN).allow


def test_a_call_three_days_after_a_conversation_is_blocked():
    talked = Attempt(
        debt_id=DEBT,
        placed_at=WHEN - timedelta(days=3),
        connected=True,
        conversation_with_debtor=True,
        outcome="answered",
    )
    d = check_call_frequency([talked], DEBT, WHEN)
    assert not d.allow
    assert d.rule_id == "call_frequency"
    assert "talked with the debtor" in d.reason


def test_a_call_eight_days_after_a_conversation_is_allowed():
    talked = Attempt(
        debt_id=DEBT,
        placed_at=WHEN - timedelta(days=8),
        connected=True,
        conversation_with_debtor=True,
    )
    assert check_call_frequency([talked], DEBT, WHEN).allow


def test_no_call_history_is_blocked():
    d = check_call_frequency(None, DEBT, WHEN)
    assert not d.allow
    assert "not loaded" in d.reason


def test_naive_times_raise():
    with pytest.raises(ValueError, match="timezone-aware"):
        check_call_frequency([], DEBT, datetime(2026, 11, 5, 14, 0))
    naive = [Attempt(debt_id=DEBT, placed_at=datetime(2026, 11, 5, 9, 0))]
    with pytest.raises(ValueError, match="timezone-aware"):
        check_call_frequency(naive, DEBT, WHEN)


# --- TCPA consent ----------------------------------------------------------------


def test_consent_on_file_is_allowed():
    d = check_consent(GOOD_PHONE)
    assert d.allow
    assert d.rule_id == "tcpa_consent"


def test_no_consent_is_blocked():
    d = check_consent(PhoneConsent(e164=PHONE))
    assert not d.allow
    assert d.rule_id == "tcpa_consent"
    assert "no consent" in d.reason


def test_revoked_consent_is_blocked():
    revoked = PhoneConsent(
        e164=PHONE,
        consent_at=datetime(2025, 1, 15, tzinfo=UTC),
        revoked_at=datetime(2026, 10, 28, tzinfo=UTC),
    )
    d = check_consent(revoked)
    assert not d.allow
    assert d.rule_id == "tcpa_consent"
    assert "revoked on 2026-10-28" in d.reason


def test_no_phone_record_is_blocked():
    assert not check_consent(None).allow


# --- flags -----------------------------------------------------------------------

FLAG_CHECKS = [
    (check_cease, "cease", Flag(kind="cease", debt_id=DEBT)),
    (check_cease, "cease", Flag(kind="opt_out_phone", data={"e164": PHONE})),
    (check_dnc, "dnc", Flag(kind="dnc", data={"e164": PHONE})),
]


@pytest.mark.parametrize("check, rule_id, flag", FLAG_CHECKS)
def test_each_flag_blocks(check, rule_id, flag):
    d = check([flag], DEBT, PHONE)
    assert not d.allow
    assert d.rule_id == rule_id
    assert d.evidence["flags"][0]["kind"] == flag.kind


@pytest.mark.parametrize("check, rule_id, flag", FLAG_CHECKS)
def test_no_flags_allows(check, rule_id, flag):
    d = check([], DEBT, PHONE)
    assert d.allow
    assert d.rule_id == rule_id


@pytest.mark.parametrize("check, rule_id, flag", FLAG_CHECKS)
def test_flags_not_loaded_block(check, rule_id, flag):
    assert not check(None, DEBT, PHONE).allow


def test_a_flag_on_another_debt_does_not_block_this_one():
    assert check_cease([Flag(kind="cease", debt_id=OTHER_DEBT)], DEBT, PHONE).allow


def test_a_flag_on_another_number_does_not_block_this_one():
    other = Flag(kind="dnc", data={"e164": "+13125550103"})
    assert check_dnc([other], DEBT, PHONE).allow
    # But if we don't know which number we'd dial, it still blocks.
    assert not check_dnc([other], DEBT, None).allow


def test_a_flag_of_another_kind_does_not_block():
    assert check_cease([Flag(kind="dnc")], DEBT, PHONE).allow


# --- check_before_call and may_call ----------------------------------------------


def test_clean_facts_allow_the_call():
    decisions = check_before_call(_facts(), WHEN)
    assert [d.rule_id for d in decisions] == [
        "calling_hours",
        "call_frequency",
        "tcpa_consent",
        "cease",
        "dnc",
        "archived",
    ]
    assert all(d.allow for d in decisions)
    assert may_call(decisions)


@pytest.mark.parametrize("kind", ["attorney", "bankruptcy", "deceased"])
def test_removed_rule_flags_are_ignored_by_the_pre_call_checks(kind):
    """Old rows of a dropped kind are neither read nor a reason to block."""
    decisions = check_before_call(_facts(flags=[Flag(kind=kind)]), WHEN)
    assert [d.rule_id for d in decisions if not d.allow] == []
    assert may_call(decisions)


def test_one_block_among_many_allows_means_no_call():
    decisions = check_before_call(_facts(flags=[Flag(kind="dnc")]), WHEN)
    assert sum(not d.allow for d in decisions) == 1
    assert not may_call(decisions)


def test_may_call_needs_every_decision_to_allow():
    assert may_call([allow("a", "ok"), allow("b", "ok")])
    assert not may_call([allow("a", "ok"), block("b", "no"), allow("c", "ok")])
    assert not may_call([])


def test_calling_hours_is_one_of_the_checks():
    late = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)  # 22:00 in Los Angeles
    decisions = check_before_call(_facts(), late)
    blocked = [d.rule_id for d in decisions if not d.allow]
    assert blocked == ["calling_hours"]


@pytest.mark.parametrize(
    "flag, rule_id",
    [
        (Flag(kind="cease", debt_id=DEBT), "cease"),
        (Flag(kind="opt_out_phone", data={"e164": PHONE}), "cease"),
        (Flag(kind="dnc", data={"e164": PHONE}), "dnc"),
    ],
)
def test_a_stop_request_or_dnc_flag_still_blocks_the_call(flag, rule_id):
    decisions = check_before_call(_facts(flags=[flag]), WHEN)
    assert [d.rule_id for d in decisions if not d.allow] == [rule_id]
    assert not may_call(decisions)


@pytest.mark.parametrize(
    "overrides, blocked_rule",
    [
        ({"phone": None}, "tcpa_consent"),
        ({"attempts": None}, "call_frequency"),
        ({"flags": None}, "cease"),
        (
            {"zip": None, "phone": PhoneConsent(e164="+19995550000", consent_at=WHEN)},
            "calling_hours",
        ),
    ],
)
def test_missing_data_blocks(overrides, blocked_rule):
    decisions = check_before_call(_facts(**overrides), WHEN)
    assert blocked_rule in [d.rule_id for d in decisions if not d.allow]
    assert not may_call(decisions)


def test_no_facts_at_all_blocks():
    decisions = check_before_call(None, WHEN)
    assert not may_call(decisions)
    assert decisions[0].rule_id == "precall"


# --- against the real local database ----------------------------------------------


@pytest.mark.db
def test_the_new_tables_exist_with_row_level_security():
    from harness.db import connect

    with connect() as conn:
        rows = conn.execute(
            "select relname, relrowsecurity from pg_class "
            "where relnamespace = 'public'::regnamespace and relname = any(%s)",
            (["call_attempt", "flags"],),
        ).fetchall()
    assert dict(rows) == {"call_attempt": True, "flags": True}


@pytest.mark.db
def test_loaders_return_the_seeded_rows():
    from harness.db import load_call_facts

    alex = load_call_facts("ACC-1002")
    assert alex is not None and alex.attempts is not None
    assert len(alex.attempts) == 7  # one old, six recent
    assert alex.phone is not None and alex.phone.e164 == "+14155550102"

    casey = load_call_facts("ACC-1004")
    assert casey is not None and casey.attempts is not None
    assert len(casey.attempts) == 7
    assert all(a.placed_at.tzinfo is not None for a in casey.attempts)

    riley = load_call_facts("ACC-1005")
    assert riley is not None and riley.phone is not None
    assert riley.phone.revoked_at == datetime(2026, 10, 28, 18, 20, tzinfo=UTC)

    taylor = load_call_facts("ACC-1006")
    assert taylor is not None and taylor.phone is not None and taylor.phone.consent_at is None

    morgan = load_call_facts("ACC-1007")
    assert morgan is not None and morgan.flags is not None
    assert [(f.kind, f.debt_id) for f in morgan.flags] == [("cease", morgan.debt_id)]

    parker = load_call_facts("ACC-1008")
    assert parker is not None and parker.flags is not None
    assert [(f.kind, f.data) for f in parker.flags] == [("opt_out_phone", {"e164": "+13035550108"})]

    assert load_call_facts("ACC-9999") is None


SEEDED_EXPECTED = {
    # account: the rules that block it at 2026-11-05 14:00 New York time
    "ACC-1001": [],  # Sam: an old conversation no longer counts
    "ACC-1002": [],  # Alex: 6 recent calls, so the 7th is fine
    "ACC-1003": ["call_frequency"],  # Jordan: talked 3 days ago
    "ACC-1004": ["call_frequency"],  # Casey: 7 recent calls
    "ACC-1005": ["tcpa_consent"],  # Riley: revoked
    "ACC-1006": ["tcpa_consent"],  # Taylor: never gave consent
    "ACC-1007": ["cease"],  # Morgan: asked us to stop
    "ACC-1008": ["cease"],  # Parker: opted the number out
    "ACC-1009": ["dnc"],  # Avery
}


@pytest.mark.db
@pytest.mark.parametrize("account, blocked", SEEDED_EXPECTED.items())
def test_each_seeded_debtor_gets_the_expected_decisions(account, blocked):
    from harness.db import load_call_facts

    decisions = check_before_call(load_call_facts(account), WHEN)
    assert [d.rule_id for d in decisions if not d.allow] == blocked
    assert may_call(decisions) is (not blocked)
