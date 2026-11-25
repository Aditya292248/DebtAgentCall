"""``uv run dialer``: place the queued calls, or show who would be called.

    uv run dialer --dry-run                  every active debt, checked now
    uv run dialer --dry-run --when 2026-11-05T19:00:00+00:00 <account> <account>
    uv run dialer --confirm                  places real calls

With no accounts in the database there is nothing to dial: it says so and exits 0.
A fresh database is empty; add people in the app, or load sample people into a
throwaway database with ``uv run seed-demo``.

``--dry-run`` writes nothing and dials nobody: it prints who would be called
and why each of the others is blocked.

Without ``--dry-run`` it refuses to start unless ``AGENTPHONE_API_KEY`` is set
*and* ``--confirm`` is passed, so a typo can't ring real phones. ``--when`` is
for dry runs only: a real call rings now, so it is checked and logged at now.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from dialer.service import DialResult, active_accounts, dial_queue, plan_queue
from model.provider import _load_dotenv

NOTHING_TO_DIAL = (
    "nothing to dial: there are no active accounts in this database yet.\n"
    "Add people in the app first. (To try the dialer on made-up people, point "
    "DATABASE_URL at a throwaway database and run `uv run seed-demo`.)"
)


def _parse_when(text: str | None) -> datetime:
    if text is None:
        return datetime.now(UTC)
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        raise SystemExit("--when needs a time zone, for example 2026-11-05T19:00:00+00:00")
    return when


def print_result(result: DialResult, *, dry_run: bool) -> None:
    for o in result.outcomes:
        if o.blocked:
            print(f"  blocked  {o.account_number}  " + "; ".join(o.reasons))
        elif dry_run:
            print(f"  call     {o.account_number}  {o.to_number}  (every check allows)")
        else:
            extra = f"  call id {o.call_id}" if o.call_id else ""
            print(f"  {o.outcome:<8} {o.account_number}  {o.to_number}{extra}")
    verb = "would call" if dry_run else "placed"
    count = len(result.outcomes) - len(result.blocked) if dry_run else len(result.placed)
    print(f"{verb} {count}, blocked {len(result.blocked)}")
    if result.stopped:
        print(f"stopped early: {result.stopped}", file=sys.stderr)
        print(f"not tried: {', '.join(result.not_tried) or 'none'}", file=sys.stderr)


def refusal(args: argparse.Namespace) -> str | None:
    """Why a real run must not start, or None if it may."""
    if args.dry_run:
        return None
    if args.when:
        # Real calls ring now, so they must be checked (and logged) at now.
        return "refusing to place real calls: --when only works with --dry-run"
    missing = []
    if not os.environ.get("AGENTPHONE_API_KEY"):
        missing.append("AGENTPHONE_API_KEY is not set")
    if not args.confirm:
        missing.append("--confirm was not passed")
    if missing:
        return "refusing to place real calls: " + " and ".join(missing) + " (try --dry-run)"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dialer", description=__doc__.splitlines()[0])
    parser.add_argument("accounts", nargs="*", help="account numbers (default: every active debt)")
    parser.add_argument("--dry-run", action="store_true", help="show who would be called")
    parser.add_argument("--confirm", action="store_true", help="really place the calls")
    parser.add_argument("--when", help="dry run only: ISO time with a zone (default: now)")
    parser.add_argument("--max-parallel", type=int, default=1)
    args = parser.parse_args(argv)

    _load_dotenv()
    if why := refusal(args):
        print(why, file=sys.stderr)
        return 2
    when = _parse_when(args.when)

    from harness.db import connect

    with connect() as conn:
        accounts = args.accounts or active_accounts(conn)
        conn.commit()
        if not accounts:
            print(NOTHING_TO_DIAL)
            return 0
        print(f"dialer at {when.isoformat()}, {len(accounts)} debts")
        if args.dry_run:
            result = plan_queue(accounts, when, conn)
            conn.rollback()
        else:
            from phone.client import AgentPhoneClient

            phone = AgentPhoneClient.from_env()
            # when=None: each debt is checked at the moment it comes up.
            result = dial_queue(
                accounts, None, phone, conn, max_parallel=args.max_parallel, connect=connect
            )
    print_result(result, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
