"""`uv run verify` — the offline gate. Format, lint, types, tests, smoke import.

Never touches a real model, so it is safe to run anywhere (CI included).
"""

from __future__ import annotations

import importlib
import subprocess
import sys

SMOKE_MODULES = [
    "model",
    "harness",
    "compliance",
    "compliance.audit",
    "compliance.utterance",
    "harness.memory",
    "harness.precall",
    "tasks.checks",
    "tasks.audit_verify",
    "phone.webhook",
    "sim.agentphone_sim",
    "tasks.serve",
]


def _run(cmd: list[str]) -> int:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    failed: list[str] = []

    if _run(["ruff", "format", "--check", "."]) != 0:
        failed.append("ruff-format")
    if _run(["ruff", "check", "."]) != 0:
        failed.append("ruff-check")
    if _run(["mypy", "model", "harness", "compliance", "phone", "sim", "tasks"]) != 0:
        failed.append("mypy")
    if _run(["pytest", "-q"]) != 0:
        failed.append("pytest")

    print("\n$ smoke import", flush=True)
    try:
        for name in SMOKE_MODULES:
            importlib.import_module(name)
        print("smoke import OK")
    except Exception as exc:  # noqa: BLE001
        print(f"smoke import FAILED: {exc!r}")
        failed.append("smoke-import")

    if failed:
        print(f"\nverify FAILED: {', '.join(failed)}")
        return 1
    print("\nverify OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
