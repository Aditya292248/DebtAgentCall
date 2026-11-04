"""How often we may call: Reg F's 7-in-7 limit.

Reg F 1006.14(b)(2) presumes a collector is harassing someone if, for one debt,
it calls them:
  - more than 7 times within 7 days, or
  - within 7 days after having a phone conversation with them about that debt.

``check_call_frequency(attempts, debt_id, when)`` decides one call at ``when``:
  - It looks only at attempts for ``debt_id`` placed in the 7 days before
    ``when`` (from ``when`` minus 7 days, not included, up to ``when``).
  - It blocks if 7 or more of those are already there, because this call would
    be the 8th. Unanswered calls count.
  - It blocks if any of those was a conversation with the debtor.
  - It blocks if the call history wasn't loaded (``attempts`` is ``None``):
    without it we can't count.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from compliance.decision import Decision, allow, block

RULE_ID = "call_frequency"
MAX_ATTEMPTS = 7  # in any 7 days; the 8th is blocked
WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class Attempt:
    debt_id: str
    placed_at: datetime
    connected: bool = False
    conversation_with_debtor: bool = False
    outcome: str | None = None


def _aware(t: datetime, name: str) -> None:
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, for example datetime.now(UTC)")


def check_call_frequency(attempts: list[Attempt] | None, debt_id: str, when: datetime) -> Decision:
    """Allow the call only if it keeps this debt within Reg F's 7-in-7 limit."""
    _aware(when, "when")
    if attempts is None:
        return block(RULE_ID, "call history not loaded, so we can't count calls", {})

    start = when - WINDOW
    recent = []
    for a in attempts:
        _aware(a.placed_at, "placed_at")
        if a.debt_id == debt_id and start < a.placed_at <= when:
            recent.append(a)
    recent.sort(key=lambda a: a.placed_at)
    talks = [a for a in recent if a.conversation_with_debtor]

    evidence = {
        "debt_id": debt_id,
        "window_start_utc": start.astimezone(UTC).isoformat(),
        "attempts_in_window": len(recent),
        "placed_at": [a.placed_at.isoformat() for a in recent],
    }
    if talks:
        last = talks[-1].placed_at
        evidence["last_conversation"] = last.isoformat()
        return block(
            RULE_ID,
            f"talked with the debtor on {last:%Y-%m-%d}; no calls until {last + WINDOW:%Y-%m-%d}",
            evidence,
        )
    if len(recent) >= MAX_ATTEMPTS:
        return block(
            RULE_ID,
            f"{len(recent)} calls in the last 7 days; the limit is {MAX_ATTEMPTS}",
            evidence,
        )
    return allow(RULE_ID, f"{len(recent)} calls in the last 7 days, under {MAX_ATTEMPTS}", evidence)
