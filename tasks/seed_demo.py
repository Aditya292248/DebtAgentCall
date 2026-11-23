"""`uv run seed-demo` — load the made-up people into the database DATABASE_URL names.

    uv run seed-demo            load them (does nothing if they are already there)
    uv run seed-demo --check    say whether the demo rows are present; changes nothing
    uv run seed-demo --force    load them even though the database holds other people

The database a fresh `supabase start` makes is empty on purpose. This is the only
way sample people get in, and it refuses a database that already holds anyone who
is not a demo person, so it cannot be run by accident against real data.
"""

from __future__ import annotations

import argparse
import sys

from tasks.dbsetup import (
    DEMO_DEBTOR_IDS,
    UnsafeDatabase,
    demo_debtor_count,
    load_demo_seed,
    other_debtor_count,
    require_local_server,
)

TOTAL = len(DEMO_DEBTOR_IDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seed-demo", description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report whether demo rows are present")
    parser.add_argument(
        "--force", action="store_true", help="load even if the database has non-demo people"
    )
    args = parser.parse_args(argv)

    from harness.db import connect, database_url

    try:
        if not args.force and not args.check:
            try:
                require_local_server(database_url())
            except UnsafeDatabase:
                print(
                    "refusing to load demo data into a database on another machine; "
                    "it may be a hosted, real one. Pass --force if you are sure.",
                    file=sys.stderr,
                )
                return 2
        with connect() as conn:
            demo, others = demo_debtor_count(conn), other_debtor_count(conn)
            if args.check:
                state = "present" if demo == TOTAL else "partly present" if demo else "absent"
                print(f"demo rows: {state} ({demo} of {TOTAL} demo people)")
                print(f"other people in this database: {others}")
                return 0 if demo == TOTAL else 1
            if others and not args.force:
                print(
                    f"refusing to load demo data: this database already holds {others} "
                    "person(s) who are not demo people, and it may be your real data.\n"
                    "Point DATABASE_URL at a throwaway database, or pass --force if you are sure.",
                    file=sys.stderr,
                )
                return 2
            print(f"demo data: {load_demo_seed(conn)}")
    except UnsafeDatabase as exc:
        print(f"seed-demo: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - one readable line instead of a traceback
        print(
            f"seed-demo: could not use the database ({exc}); is `npx supabase start` running?",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
