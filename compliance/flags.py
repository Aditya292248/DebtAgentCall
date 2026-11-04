"""Flags that stop calls: stop requests, do-not-call, lawyers, bankruptcy, death.

A ``Flag`` is a fact someone recorded about a debtor. One with no ``debt_id``
covers all of their debts; one with a ``debt_id`` covers only that debt. A flag
whose ``data`` has an ``"e164"`` is about that one number only.

One check per rule, each blocking if a matching flag applies to this call:
  - ``check_cease``      ``cease`` or ``opt_out_phone``: they told us to stop
                         (FDCPA 805(c)), or to stop using this number.
  - ``check_dnc``        ``dnc``: the number is on a do-not-call list.
  - ``check_attorney``   ``attorney``: they have a lawyer for this debt, so we
                         talk to the lawyer, not them (FDCPA 805(a)(2)).
  - ``check_bankruptcy`` ``bankruptcy``: the automatic stay stops collection.
  - ``check_deceased``   ``deceased``: never call a dead person's number to
                         collect; estates are handled another way.

``flags`` is ``None`` when they weren't loaded. Then every check blocks, since
"no flags found" and "didn't look" are not the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from compliance.decision import Decision, allow, block


@dataclass(frozen=True)
class Flag:
    kind: str
    debt_id: str | None = None  # None: covers every debt of the debtor
    data: dict[str, Any] = field(default_factory=dict)
    set_at: datetime | None = None
    source: str | None = None


def _applies(flag: Flag, debt_id: str, phone_e164: str | None) -> bool:
    if flag.debt_id is not None and flag.debt_id != debt_id:
        return False
    number = flag.data.get("e164")
    # A flag for one number applies to that number. If we don't know which
    # number we'd dial, it applies too: we can't show it's a different one.
    return number is None or phone_e164 is None or number == phone_e164


def _check(
    rule_id: str,
    kinds: tuple[str, ...],
    flags: list[Flag] | None,
    debt_id: str,
    phone_e164: str | None,
    reasons: dict[str, str],
) -> Decision:
    if flags is None:
        return block(rule_id, "flags not loaded, so we can't tell", {"debt_id": debt_id})
    hits = [f for f in flags if f.kind in kinds and _applies(f, debt_id, phone_e164)]
    evidence = {
        "debt_id": debt_id,
        "phone_e164": phone_e164,
        "flags": [
            {
                "kind": f.kind,
                "debt_id": f.debt_id,
                "data": dict(f.data),
                "set_at": f.set_at.isoformat() if f.set_at else None,
                "source": f.source,
            }
            for f in hits
        ],
    }
    if hits:
        first = hits[0]
        when = f" on {first.set_at:%Y-%m-%d}" if first.set_at else ""
        return block(rule_id, f"{reasons[first.kind]}{when}", evidence)
    return allow(rule_id, f"no {' or '.join(kinds)} flag", evidence)


def check_cease(flags: list[Flag] | None, debt_id: str, phone_e164: str | None) -> Decision:
    """Block if the debtor asked us to stop, or to stop calling this number."""
    return _check(
        "cease",
        ("cease", "opt_out_phone"),
        flags,
        debt_id,
        phone_e164,
        {"cease": "debtor asked us to stop", "opt_out_phone": "debtor opted this number out"},
    )


def check_dnc(flags: list[Flag] | None, debt_id: str, phone_e164: str | None) -> Decision:
    """Block if the number is on a do-not-call list."""
    return _check(
        "dnc", ("dnc",), flags, debt_id, phone_e164, {"dnc": "number is on a do-not-call list"}
    )


def check_attorney(flags: list[Flag] | None, debt_id: str, phone_e164: str | None) -> Decision:
    """Block if a lawyer represents the debtor on this debt."""
    return _check(
        "attorney",
        ("attorney",),
        flags,
        debt_id,
        phone_e164,
        {"attorney": "debtor has a lawyer for this debt; contact the lawyer"},
    )


def check_bankruptcy(flags: list[Flag] | None, debt_id: str, phone_e164: str | None) -> Decision:
    """Block if the debtor filed for bankruptcy."""
    return _check(
        "bankruptcy",
        ("bankruptcy",),
        flags,
        debt_id,
        phone_e164,
        {"bankruptcy": "debtor filed for bankruptcy"},
    )


def check_deceased(flags: list[Flag] | None, debt_id: str, phone_e164: str | None) -> Decision:
    """Block if the debtor has died."""
    return _check(
        "deceased",
        ("deceased",),
        flags,
        debt_id,
        phone_e164,
        {"deceased": "debtor is recorded as deceased"},
    )
