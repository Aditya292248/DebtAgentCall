"""`uv run demo part-NN` — run a part's on-camera demonstration."""

from __future__ import annotations

import sys

from tasks.checks import DEMOS


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in DEMOS:
        print(f"usage: demo <part>   (known: {', '.join(sorted(DEMOS)) or 'none'})")
        return 2
    DEMOS[args[0]]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
