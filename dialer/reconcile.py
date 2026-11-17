"""``uv run reconcile``: close the attempts whose call never reported its end.

    uv run reconcile --dry-run          show what would change, write nothing
    uv run reconcile                    apply it
    uv run reconcile --minutes 60       only attempts placed more than an hour ago

``agent.call_ended`` is sent once and never retried, so a restart, a dropped
request or a failed write leaves an attempt with no end recorded. This finds
attempts placed more than ``--minutes`` (default 30) ago that have an AgentPhone
call id and no ``ended_at``, reads each call back with ``GET /v1/calls/{id}``
(reads may be retried; a dial never is), and applies the same update as the
webhook (``phone.call_end.record_call_end``).

An attempt is left untouched, and reported, when AgentPhone cannot answer for the
call, or says it has not ended yet. A call ended with nothing in the lookup that
says whether the caller spoke is recorded as no conversation unless the saved
call state shows the person was verified.

This is a standalone command: nothing runs it by itself, so schedule it (cron,
a task runner) if you want stale attempts closed without someone remembering.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg

from harness import call_summary
from harness.memory import PostgresSession, Session
from model.provider import _load_dotenv
from phone.call_end import EndedCall, EndResult, parse_time, record_call_end
from phone.client import CallLookupError, CallReader

log = logging.getLogger(__name__)

DEFAULT_MINUTES = 30

STALE_SQL = """
    select id::text, agentphone_call_id, placed_at from public.call_attempt
    where agentphone_call_id is not null and ended_at is null and placed_at < %s
    order by placed_at
"""


@dataclass(frozen=True)
class Finding:
    attempt_id: str
    call_id: str
    action: str  # recorded | would_record | still_live | lookup_failed | duplicate | unknown_call
    detail: str = ""


def stale_attempts(conn: psycopg.Connection, older_than: timedelta, now: datetime) -> list[tuple]:
    return conn.execute(STALE_SQL, (now - older_than,)).fetchall()


def reconcile(
    phone: CallReader,
    *,
    connect: Callable[[], psycopg.Connection],
    open_session: Callable[[str], Session] = PostgresSession,
    narrator: call_summary.Narrator | None = None,
    older_than: timedelta = timedelta(minutes=DEFAULT_MINUTES),
    dry_run: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[Finding]:
    """Close every stale attempt AgentPhone says has ended. Safe to run again."""
    with connect() as conn:
        stale = stale_attempts(conn, older_than, now())
    findings: list[Finding] = []
    for attempt_id, call_id, _placed in stale:
        try:
            info = phone.get_call(call_id)
        except CallLookupError as exc:
            findings.append(Finding(attempt_id, call_id, "lookup_failed", str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - any failure leaves the attempt alone
            findings.append(Finding(attempt_id, call_id, "lookup_failed", repr(exc)))
            continue
        if info.ended_at is None:
            findings.append(Finding(attempt_id, call_id, "still_live", f"status {info.status}"))
            continue
        ended = EndedCall(
            call_id=call_id,
            reason=info.disconnection_reason,
            ended_at=parse_time(info.ended_at),
            caller_spoke=True if info.caller_turns else None,  # empty: cannot tell
            source="reconciler",
        )
        try:
            result = record_call_end(
                ended,
                connect=connect,
                open_session=open_session,
                narrator=narrator,
                dry_run=dry_run,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - e.g. the saved state could not be read
            log.exception("call %s: reconcile failed", call_id)
            findings.append(Finding(attempt_id, call_id, "lookup_failed", f"not applied: {exc!r}"))
            continue
        findings.append(_finding(attempt_id, call_id, result))
    return findings


def _finding(attempt_id: str, call_id: str, result: EndResult) -> Finding:
    action = {"recorded": "recorded", "dry_run": "would_record"}.get(result.status, result.status)
    detail = ""
    if result.outcome:
        detail = (
            f"outcome {result.outcome}, connected {result.connected}, "
            f"conversation {result.conversation}"
        )
    return Finding(attempt_id, call_id, action, detail)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reconcile", description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print what would change")
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES, help="minimum age")
    args = parser.parse_args(argv)
    if args.minutes < 1:
        print("--minutes must be at least 1", file=sys.stderr)
        return 2

    _load_dotenv()
    from harness.db import connect
    from phone.client import AgentPhoneClient

    try:
        phone = AgentPhoneClient.from_env()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    findings = reconcile(
        phone,
        connect=connect,
        narrator=None if args.dry_run else call_summary.model_narrator(),
        older_than=timedelta(minutes=args.minutes),
        dry_run=args.dry_run,
    )
    for f in findings:
        print(f"  {f.action:<13} {f.call_id}  {f.detail}")
    failed = [f for f in findings if f.action == "lookup_failed"]
    print(f"{len(findings)} stale attempts, {len(failed)} left untouched after an error")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
