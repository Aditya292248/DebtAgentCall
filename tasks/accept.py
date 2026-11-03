"""`uv run accept part-NN` — prove a part's capability against the real model."""

from __future__ import annotations

import sys

from tasks.checks import ACCEPTANCE


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in ACCEPTANCE:
        print(f"usage: accept <part>   (known: {', '.join(sorted(ACCEPTANCE)) or 'none'})")
        return 2
    part = args[0]
    ok = ACCEPTANCE[part]()
    print(f"\naccept {part}: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
