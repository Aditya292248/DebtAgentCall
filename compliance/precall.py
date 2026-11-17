"""Every check we run before placing a call, in one place.

``CallFacts`` holds what the checks need to know about one debt: where the
debtor is, the phone we'd dial, the call history and the flags. Loading these
(``harness/db.py``) is kept apart from checking them, so the checks stay plain
functions that tests can run without a database.

``check_before_call(facts, when)`` runs every check and returns all of their
decisions, allowed and blocked alike, so a person (and later the audit log) can
see the whole picture. ``may_call(decisions)`` is True only if there is at least
one decision and every one allows.

Missing data blocks: no facts at all, no phone, no call history, or no flags
loaded each turn into a blocked decision, never a silent pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from compliance.attempts import Attempt, check_call_frequency
from compliance.consent import PhoneConsent, check_consent
from compliance.decision import Decision, block
from compliance.flags import (
    Flag,
    check_attorney,
    check_bankruptcy,
    check_cease,
    check_deceased,
    check_dnc,
)
from compliance.hours import Contact, check_calling_hours
from compliance.testnumbers import configured_test_numbers, exempt_for_test_number


@dataclass(frozen=True)
class CallFacts:
    debt_id: str
    zip: str | None
    phone: PhoneConsent | None
    attempts: list[Attempt] | None
    flags: list[Flag] | None


def check_before_call(
    facts: CallFacts | None,
    when: datetime,
    *,
    test_numbers: frozenset[str] | None = None,
) -> list[Decision]:
    """Every pre-call decision for this debt at ``when``.

    A number in ``test_numbers`` (default: the ``TEST_NUMBERS`` setting, empty if
    unset) skips the calling-hours and call-frequency rules only; see ``compliance/testnumbers.py``.
    """
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("when must be timezone-aware, for example datetime.now(timezone.utc)")
    if facts is None:
        return [block("precall", "no facts loaded for this debt", {})]

    e164 = facts.phone.e164 if facts.phone else None
    if test_numbers is None:
        test_numbers = configured_test_numbers()
    hours = check_calling_hours(Contact(zip=facts.zip, phone_e164=e164), when)
    frequency = check_call_frequency(facts.attempts, facts.debt_id, when)
    # No history means we can't count; that is missing data, so never exempt.
    frequency_numbers = test_numbers if facts.attempts is not None else frozenset()
    return [
        exempt_for_test_number(hours, e164, test_numbers),
        exempt_for_test_number(frequency, e164, frequency_numbers),
        check_consent(facts.phone),
        check_cease(facts.flags, facts.debt_id, e164),
        check_dnc(facts.flags, facts.debt_id, e164),
        check_attorney(facts.flags, facts.debt_id, e164),
        check_bankruptcy(facts.flags, facts.debt_id, e164),
        check_deceased(facts.flags, facts.debt_id, e164),
    ]


def may_call(decisions: list[Decision]) -> bool:
    """True only if there is at least one decision and all of them allow."""
    return bool(decisions) and all(d.allow for d in decisions)
