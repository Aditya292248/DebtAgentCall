"""`uv run audit-verify` — replay the audit log and check every hash.

Exits 0 when the chain is whole, and 1 with each problem printed when it isn't
(or when the database can't be reached).
"""

from __future__ import annotations

import sys

import psycopg

from compliance.audit import verify_chain


def run(conn: psycopg.Connection) -> int:
    """Check the chain on ``conn``, print the result, and return the exit code."""
    count_row = conn.execute("select count(*) from public.audit_event").fetchone()
    count = count_row[0] if count_row else 0
    problems = verify_chain(conn)
    if problems:
        print(f"audit chain BROKEN: {len(problems)} problem(s) in {count} rows")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"audit chain OK: {count} rows, every hash matches")
    return 0


def main(argv: list[str] | None = None) -> int:
    from harness.db import connect

    try:
        with connect() as conn:
            return run(conn)
    except psycopg.Error as exc:
        print(f"audit-verify could not read the audit log: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
