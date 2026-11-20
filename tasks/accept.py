"""`uv run accept part-NN [--demo-db]` — prove a part's capability against the real model.

From part-06 on the checks use the sample people; see `tasks/demo.py` for
what happens on an empty database and for `--demo-db`.
"""

from __future__ import annotations

import sys

from tasks.checks import ACCEPTANCE
from tasks.dbsetup import prepare_part


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    use_demo_db = "--demo-db" in args
    args = [a for a in args if a != "--demo-db"]
    if len(args) != 1 or args[0] not in ACCEPTANCE:
        print(
            f"usage: accept <part> [--demo-db]   (known: {', '.join(sorted(ACCEPTANCE)) or 'none'})"
        )
        return 2
    part = args[0]
    if (code := prepare_part(part, use_demo_db)) is not None:
        return code
    ok = ACCEPTANCE[part]()
    print(f"\naccept {part}: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
