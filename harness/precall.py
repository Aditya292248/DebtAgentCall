"""Run the pre-call checks for one debt and write every decision to the audit log.

``check_and_record(conn, account_number, when)`` loads the facts, runs
``compliance.check_before_call`` and records each decision it returns, allowed
and blocked alike, in ``audit_event``. It returns the decisions, so the caller
still uses ``may_call`` to decide.

The checks themselves stay plain functions with no database; this is the one
place that joins them to it. Nothing is committed here: the caller's
transaction decides, so a later step can commit the audit rows together with
the call it places.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import psycopg

from compliance import Decision, check_before_call
from compliance.audit import record
from harness.db import load_call_facts


def check_and_record(
    conn: psycopg.Connection,
    account_number: str,
    when: datetime,
    *,
    call_id: str | None = None,
) -> list[Decision]:
    """Every pre-call decision for ``account_number`` at ``when``, each one recorded."""
    facts = load_call_facts(account_number, conn)
    decisions = check_before_call(facts, when)
    if facts is None:
        # No debt row means no debt_id to log, so keep the account number we were
        # asked about; otherwise the block couldn't be traced back to anything.
        decisions = [
            replace(d, evidence={**d.evidence, "account_number": account_number}) for d in decisions
        ]
    debt_id = facts.debt_id if facts else None
    for d in decisions:
        record(conn, d, kind="gate", call_id=call_id, debt_id=debt_id)
    return decisions
