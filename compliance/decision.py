"""The answer every compliance gate gives: allowed or blocked, and why.

``Decision`` is frozen, so once a gate has decided, its fields can't be
reassigned and nothing later can quietly flip ``allow``. ``rule_id`` names the
rule, ``reason`` is one short line a person can read, and ``evidence`` is a
record of the facts the gate looked at, for the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Decision:
    allow: bool
    rule_id: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


def allow(rule_id: str, reason: str, evidence: dict[str, Any] | None = None) -> Decision:
    return Decision(allow=True, rule_id=rule_id, reason=reason, evidence=dict(evidence or {}))


def block(rule_id: str, reason: str, evidence: dict[str, Any] | None = None) -> Decision:
    return Decision(allow=False, rule_id=rule_id, reason=reason, evidence=dict(evidence or {}))
