"""`uv run latency-report` — how long live caller turns took on our side.

Reads ``public.turn_timing`` (written by the webhook after each reply, numbers only)
and prints, for all turns and by kind of turn, the count and the p50, p95 and max of
the time to the first substantive line and of the whole turn, the share of turns that used the
hold line, and the time spent in the model and in tools.

Percentiles use linear interpolation between the nearest ranks (the same as
``statistics.quantiles(method="inclusive")``). A group with fewer than 20 turns has
a p95 that is little more than its max; read it as such.

  --since 2h    only turns from the last 2 hours (s, m, h or d)
  --call ID     only this call, and its full id is shown
  --json        the same numbers as JSON

Call ids are shown as a short prefix unless ``--call`` is given. Server time only:
telephony transcription and playback are outside this measurement.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

PREFIX = 10
EMPTY = "no turns recorded yet"
COLUMNS = (
    "call_id, turn_seq, stage, kind, total_ms, first_line_ms, model_ms, model_calls, "
    "tool_ms, tool_calls, hold_line, substantive_ms"
)
GROUPS: tuple[tuple[str, Callable[[dict], bool]], ...] = (
    ("all turns", lambda r: True),
    ("fixed line (no model)", lambda r: r["kind"] == "fixed"),
    ("model turn", lambda r: r["kind"] == "model"),
    ("model turn with tools", lambda r: r["kind"] == "model" and r["tool_calls"] > 0),
)


def percentile(values: Sequence[float], p: float) -> float | None:
    """The p-th percentile (0 to 100) by linear interpolation, or None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * p / 100
    low, high = math.floor(pos), math.ceil(pos)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def parse_since(text: str) -> timedelta:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", text.strip())
    if not match:
        raise ValueError(f"--since wants a number and s, m, h or d (like 2h), not {text!r}")
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[match[2]]
    return timedelta(**{unit: float(match[1])})


def _stats(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values) if values else None,
    }


def summarize(rows: list[dict]) -> dict[str, Any]:
    """The numbers the report shows, from rows as dicts (times in milliseconds)."""
    groups = []
    for name, keep in GROUPS:
        part = [r for r in rows if keep(r)]
        firsts = [r["first_line_ms"] for r in part if r["first_line_ms"] is not None]
        groups.append(
            {
                "group": name,
                "turns": len(part),
                "first_line_ms": _stats(firsts),
                "substantive_ms": _stats(
                    [r["substantive_ms"] for r in part if r.get("substantive_ms") is not None]
                ),
                "total_ms": _stats([r["total_ms"] for r in part]),
                "hold_line_share": (sum(1 for r in part if r["hold_line"]) / len(part))
                if part
                else None,
                "model_ms": _stats([r["model_ms"] for r in part if r["model_calls"] > 0]),
                "model_calls": sum(r["model_calls"] for r in part),
                "tool_ms": _stats([r["tool_ms"] for r in part if r["tool_calls"] > 0]),
                "tool_calls": sum(r["tool_calls"] for r in part),
            }
        )
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    return {"turns": len(rows), "kinds": kinds, "groups": groups}


def slowest(rows: list[dict], full_ids: bool, n: int = 3) -> list[dict]:
    top = sorted(rows, key=lambda r: r["total_ms"], reverse=True)[:n]
    return [
        {
            "call": r["call_id"] if full_ids else r["call_id"][:PREFIX],
            "turn": r["turn_seq"],
            "kind": r["kind"],
            "stage": r["stage"],
            "total_ms": r["total_ms"],
        }
        for r in top
    ]


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def render(summary: dict[str, Any], worst: list[dict]) -> str:
    if summary["turns"] == 0:
        return EMPTY
    lines = [f"{summary['turns']} turns (milliseconds, our server only)"]
    lines.append("  by kind: " + ", ".join(f"{k} {v}" for k, v in sorted(summary["kinds"].items())))
    head = (
        f"{'':<24}{'turns':>6} | {'first answer':^23} | {'whole turn':^23} | {'hold':>5} | "
        f"{'model p50/p95':>14} | {'tools p50/p95':>14}"
    )
    sub = (
        f"{'':<24}{'':>6} | {'p50':>7}{'p95':>8}{'max':>8} | {'p50':>7}{'p95':>8}{'max':>8} | "
        f"{'share':>5} | {'':>14} | {'':>14}"
    )
    lines += ["", head, sub, "-" * len(head)]
    for g in summary["groups"]:
        f, t = g["substantive_ms"], g["total_ms"]
        share = "-" if g["hold_line_share"] is None else f"{g['hold_line_share'] * 100:.0f}%"
        model = f"{_fmt(g['model_ms']['p50'])}/{_fmt(g['model_ms']['p95'])}"
        tools = f"{_fmt(g['tool_ms']['p50'])}/{_fmt(g['tool_ms']['p95'])}"
        lines.append(
            f"{g['group']:<24}{g['turns']:>6} | {_fmt(f['p50']):>7}{_fmt(f['p95']):>8}"
            f"{_fmt(f['max']):>8} | {_fmt(t['p50']):>7}{_fmt(t['p95']):>8}{_fmt(t['max']):>8} | "
            f"{share:>5} | {model:>14} | {tools:>14}"
        )
    lines += ["", "slowest turns:"]
    for w in worst:
        lines.append(
            f"  call {w['call']} turn {w['turn']} {w['kind']} ({w['stage']}): {w['total_ms']:.1f}"
        )
    return "\n".join(lines)


def fetch(conn: Any, since: timedelta | None, call: str | None) -> list[dict]:
    """The rows, oldest first. A call id matches whole, or as the start of one."""
    where: list[str] = []
    params: list[Any] = []
    if since is not None:
        where.append("created_at >= %s")
        params.append(datetime.now(UTC) - since)
    if call is not None:
        where.append("call_id like %s")
        params.append(call.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    sql = f"select {COLUMNS} from public.turn_timing"
    if where:
        sql += " where " + " and ".join(where)
    sql += " order by created_at"
    cur = conn.execute(sql, params)
    names = [c.name for c in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def main(argv: list[str] | None = None, connect: Callable[[], Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report how long live caller turns took.")
    parser.add_argument("--since", help="only turns from the last N s/m/h/d, e.g. 2h")
    parser.add_argument("--call", help="only this call id (shown in full)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)
    try:
        since = parse_since(args.since) if args.since else None
    except ValueError as exc:
        parser.error(str(exc))
    if connect is None:
        from harness.db import connect as db_connect

        connect = db_connect
    try:
        with connect() as conn:
            rows = fetch(conn, since, args.call)
    except Exception as exc:  # noqa: BLE001 - a person is reading this, so say what is wrong
        print(f"could not read turn_timing: {str(exc).strip().splitlines()[0]}", file=sys.stderr)
        return 1
    summary = summarize(rows)
    worst = slowest(rows, full_ids=args.call is not None)
    if args.json:
        print(json.dumps({**summary, "slowest": worst}, indent=2))
    else:
        print(render(summary, worst))
    return 0


if __name__ == "__main__":
    sys.exit(main())
