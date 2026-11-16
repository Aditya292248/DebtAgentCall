"""The owner's test-number list: the one way to place a real test call outside calling hours.

``TEST_NUMBERS`` is a comma-separated list of E.164 numbers in the environment
or ``.env``. It is empty by default, and then nothing here changes anything.

A number on the list skips the calling-hours rule and nothing else; the other
pre-call checks still run and still block. ``check_before_call`` is the only
place that applies it, so the dialer, the operator agent and ``/api/precall``
all agree. It is meant for the repo owner's own phone.

An entry must be a full E.164 number (``+`` then 8 to 15 digits; spaces, dashes,
dots and brackets are ignored). Anything else is ignored and logged, never read
as a wildcard: there is no ``*`` and no prefix matching.
"""

from __future__ import annotations

import logging
import os
import re

from compliance.decision import Decision, allow

log = logging.getLogger(__name__)

ENV_NAME = "TEST_NUMBERS"
EXEMPTION_REASON = "skipped: test number (TEST_NUMBERS)"
EXEMPTION_KEY = "exemption"

_E164 = re.compile(r"\+[1-9]\d{7,14}")
_SEPARATORS = re.compile(r"[\s\-.()]")


def parse_test_numbers(raw: str | None) -> frozenset[str]:
    """The valid E.164 numbers in ``raw``; every bad entry is logged and dropped."""
    numbers: set[str] = set()
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        candidate = _SEPARATORS.sub("", entry)
        if _E164.fullmatch(candidate):
            numbers.add(candidate)
        else:
            log.warning(
                "TEST_NUMBERS: ignoring entry that is not a full E.164 number (%d chars)",
                len(entry),
            )
    return frozenset(numbers)


def configured_test_numbers() -> frozenset[str]:
    """The list from the environment, with ``.env`` as a fallback. Empty when unset."""
    from model.provider import _load_dotenv

    _load_dotenv()
    return parse_test_numbers(os.environ.get(ENV_NAME))


def exempt_calling_hours(
    hours: Decision, e164: str | None, test_numbers: frozenset[str]
) -> Decision:
    """``hours``, unless it blocks a listed number: then an allow saying it was skipped."""
    if hours.allow or e164 is None or e164 not in test_numbers:
        return hours
    evidence = {
        **hours.evidence,
        EXEMPTION_KEY: "test_number",
        "would_have_blocked": hours.reason,
    }
    return allow(hours.rule_id, EXEMPTION_REASON, evidence)


def was_exempted(decision: Decision) -> bool:
    return decision.evidence.get(EXEMPTION_KEY) == "test_number"
