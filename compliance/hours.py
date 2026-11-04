"""Calling hours: no calls before 8am or from 9pm on, in the debtor's local time.

FDCPA 805(a)(1) and Reg F 1006.6(b)(1)(i) treat a call outside 8am-9pm local
time as inconvenient, and so is any time the debtor has told us is bad for them.

``check_calling_hours(contact, when)`` decides one call:
  - It finds every zone the debtor might be in (``zones_for``). If there are
    none, we don't know their local time, so the call is blocked.
  - The call is allowed only if ``when`` is from 08:00 up to (not including)
    21:00 in EVERY one of those zones.
  - It is also blocked if ``when`` falls inside any window the debtor said is
    inconvenient, checked in every zone.

``when`` can be now or a time in the future, so a later tool that books a
callback can ask the same question. It must be timezone-aware: a naive time
like "9:05pm" means nothing until we know whose 9:05pm it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from compliance.decision import Decision, allow, block
from compliance.timezones import zones_for

RULE_ID = "calling_hours"
EARLIEST = time(8, 0)  # allowed from 08:00
LATEST = time(21, 0)  # blocked from 21:00


@dataclass(frozen=True)
class Contact:
    zip: str | None
    phone_e164: str | None
    # Local-time windows the debtor said are bad, as (start, end). The start is
    # inside the window and the end is not. A window like (22:00, 07:00) wraps
    # past midnight.
    inconvenient: list[tuple[time, time]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # A bad window must fail loudly here, not be skipped or crash mid-check.
        for start, end in self.inconvenient:
            if start.tzinfo is not None or end.tzinfo is not None:
                raise ValueError("inconvenient windows are local clock times, without a time zone")
            if start == end:
                raise ValueError(f"inconvenient window {start:%H:%M}-{end:%H:%M} is empty")


def _in_window(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # wraps past midnight


def check_calling_hours(contact: Contact, when: datetime) -> Decision:
    """Allow the call only if ``when`` is a convenient time in every zone the debtor may be in."""
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("when must be timezone-aware, for example datetime.now(timezone.utc)")

    zones = zones_for(contact.zip, contact.phone_e164)
    evidence: dict = {
        "when_utc": when.astimezone(ZoneInfo("UTC")).isoformat(),
        "zip": contact.zip,
        "phone_e164": contact.phone_e164,
        "zones": sorted(zones) if zones else [],
    }
    if not zones:
        return block(RULE_ID, "no known time zone for this ZIP or phone number", evidence)

    local = {z: when.astimezone(ZoneInfo(z)) for z in sorted(zones)}
    evidence["local_times"] = {z: t.strftime("%Y-%m-%d %H:%M %Z") for z, t in local.items()}

    for zone, t in local.items():
        clock = t.time().replace(tzinfo=None)
        if not EARLIEST <= clock < LATEST:
            return block(RULE_ID, f"outside 08:00-21:00: {t:%H:%M} {zone}", evidence)
        for start, end in contact.inconvenient:
            if _in_window(clock, start, end):
                return block(
                    RULE_ID,
                    f"debtor said {start:%H:%M}-{end:%H:%M} is inconvenient: {t:%H:%M} {zone}",
                    evidence,
                )

    shown = ", ".join(f"{t:%H:%M} {z}" for z, t in local.items())
    return allow(RULE_ID, f"within 08:00-21:00: {shown}", evidence)
