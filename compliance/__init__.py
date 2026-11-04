"""``compliance/`` — the legal rules, as plain Python checks.

Each rule is a function the harness calls before it acts. The model can't talk
its way past one, because the check never reads the model's words: it looks at
the facts (the time, the debtor's location) and returns a ``Decision``.

Nothing here is wired into the agent yet.
"""

from __future__ import annotations

from compliance.decision import Decision, allow, block
from compliance.hours import Contact, check_calling_hours
from compliance.timezones import zones_for

__all__ = ["Contact", "Decision", "allow", "block", "check_calling_hours", "zones_for"]
