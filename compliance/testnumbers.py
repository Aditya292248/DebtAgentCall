"""The owner's test-number list: real test calls outside hours and past the call limit.

``TEST_NUMBERS`` is a comma-separated list of E.164 numbers in the environment
or ``.env``. It is empty by default, and then nothing here changes anything.

A number on the list skips two rules and nothing else: ``calling_hours`` and
``call_frequency`` (Reg F's 7-in-7 limit and the 7-day pause after a
conversation), the two the owner's repeated test calls would otherwise hit. The
other pre-call checks (consent, cease, DNC, attorney, bankruptcy, deceased, debt
status) still run and still block. The dialer still records every attempt.

``check_before_call`` is the only
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
# The only rules a listed number may skip. Everything else always applies.
EXEMPT_RULES = frozenset({"calling_hours", "call_frequency"})

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


def exempt_for_test_number(
    decision: Decision, e164: str | None, test_numbers: frozenset[str]
) -> Decision:
    """``decision``, unless it blocks a listed number on a rule in ``EXEMPT_RULES``.

    Then an allow saying it was skipped, with what the rule would have said. This
    is the one place the exemption is decided, for every rule.
    """
    if (
        decision.allow
        or decision.rule_id not in EXEMPT_RULES
        or e164 is None
        or e164 not in test_numbers
    ):
        return decision
    evidence = {
        **decision.evidence,
        EXEMPTION_KEY: "test_number",
        "would_have_blocked": decision.reason,
    }
    return allow(decision.rule_id, EXEMPTION_REASON, evidence)


def was_exempted(decision: Decision) -> bool:
    return decision.evidence.get(EXEMPTION_KEY) == "test_number"
