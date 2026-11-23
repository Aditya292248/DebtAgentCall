"""`uv run demo part-NN [--demo-db]` — run a part's on-camera demonstration.

From part-06 on the demos use the sample people. A fresh database has none, so
the demo says so instead of failing; `--demo-db` runs it on a throwaway
`debtagentcall_demo` database that it creates and fills itself.
"""

from __future__ import annotations

import sys

from tasks.checks import DEMOS
from tasks.dbsetup import prepare_part


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    use_demo_db = "--demo-db" in args
    args = [a for a in args if a != "--demo-db"]
    if len(args) != 1 or args[0] not in DEMOS:
        print(f"usage: demo <part> [--demo-db]   (known: {', '.join(sorted(DEMOS)) or 'none'})")
        return 2
    if (code := prepare_part(args[0], use_demo_db)) is not None:
        return code
    DEMOS[args[0]]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
