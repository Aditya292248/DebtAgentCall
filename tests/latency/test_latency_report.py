"""`uv run latency-report`: the numbers, the filters and the empty case."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest

from phone.timing import TurnRecord, record_in_db
from tasks import latency_report as report


def row(total: float, *, kind: str = "model", **kw) -> dict:  # type: ignore[no-untyped-def]
    base = {
        "call_id": "call_abcdefghijklmnop", "turn_seq": 1, "stage": "DISCUSS", "kind": kind,
        "total_ms": total, "first_line_ms": total, "model_ms": 0.0,
        "model_calls": 0, "tool_ms": 0.0, "tool_calls": 0, "hold_line": False,
    }  # fmt: skip
    return {**base, **kw}


def test_p50_and_p95_on_a_known_set() -> None:
    rows = [row(float(n)) for n in range(1, 101)]  # 1..100 ms
    everyone = report.summarize(rows)["groups"][0]
    assert everyone["turns"] == 100
    assert everyone["total_ms"]["p50"] == pytest.approx(50.5)
    assert everyone["total_ms"]["p95"] == pytest.approx(95.05)
    assert everyone["total_ms"]["max"] == 100.0
    assert report.percentile([5.0], 95) == 5.0 and report.percentile([], 50) is None


def test_groups_split_by_kind_and_tools_and_count_the_hold_line() -> None:
    rows = [
        row(4.0, kind="fixed"),
        row(40.0, model_ms=30.0, model_calls=1),
        row(90.0, model_ms=40.0, model_calls=2, tool_ms=20.0, tool_calls=1, hold_line=True),
        row(10.0, kind="stop"),
    ]
    g = {x["group"]: x for x in report.summarize(rows)["groups"]}
    assert g["all turns"]["turns"] == 4 and g["all turns"]["hold_line_share"] == 0.25
    assert g["fixed line (no model)"]["turns"] == 1
    assert g["model turn"]["turns"] == 2 and g["model turn"]["model_calls"] == 3
    assert g["model turn with tools"]["turns"] == 1
    assert g["model turn with tools"]["tool_ms"]["p50"] == 20.0
    assert g["model turn with tools"]["hold_line_share"] == 1.0


def test_an_empty_table_prints_the_empty_message(capsys: pytest.CaptureFixture[str]) -> None:
    class Empty:
        description = [type("C", (), {"name": "call_id"})()]

        def execute(self, sql, params=()):  # type: ignore[no-untyped-def]
            return self

        def fetchall(self) -> list:
            return []

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
            return None

    assert report.main([], connect=Empty) == 0
    assert capsys.readouterr().out.strip() == "no turns recorded yet"


def test_since_parses_and_rejects() -> None:
    assert report.parse_since("2h") == timedelta(hours=2)
    assert report.parse_since("90s") == timedelta(seconds=90)
    with pytest.raises(ValueError):
        report.parse_since("soon")


def test_call_ids_are_cut_to_a_prefix_unless_asked_for() -> None:
    rows = [row(5.0)]
    assert report.slowest(rows, full_ids=False)[0]["call"] == "call_abcde"
    assert report.slowest(rows, full_ids=True)[0]["call"] == "call_abcdefghijklmnop"


@pytest.fixture
def stored_calls() -> Iterator[tuple[str, str]]:
    from harness.db import connect

    tag = uuid.uuid4().hex[:10]
    mine, other = f"call_rep_{tag}_a", f"call_rep_{tag}_b"
    try:
        for n in range(1, 101):
            record_in_db(
                TurnRecord(
                    mine,
                    "model",
                    float(n),
                    turn_seq=n,
                    first_line_ms=float(n) / 2,
                    model_ms=1.0,
                    model_calls=1,
                )  # fmt: skip
            )
        record_in_db(TurnRecord(other, "fixed", 3.0, turn_seq=1, first_line_ms=3.0))
        yield mine, other
    finally:
        with connect() as conn:
            conn.execute(
                "delete from public.turn_timing where call_id like %s", (f"call_rep_{tag}%",)
            )


@pytest.mark.db
def test_the_report_reads_the_table(
    stored_calls: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    mine, other = stored_calls
    assert report.main(["--call", mine, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    everyone = out["groups"][0]
    assert everyone["turns"] == 100 and everyone["total_ms"]["p50"] == pytest.approx(50.5)
    assert everyone["total_ms"]["p95"] == pytest.approx(95.05, abs=0.01)
    assert out["slowest"][0]["call"] == mine  # shown in full when --call is given

    assert report.main(["--call", mine, "--since", "1h"]) == 0
    assert "100 turns" in capsys.readouterr().out
    assert report.main(["--call", "call_rep_nothing_like_this"]) == 0
    assert capsys.readouterr().out.strip() == "no turns recorded yet"
    assert report.main(["--call", other, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["kinds"] == {"fixed": 1}


@pytest.mark.db
def test_without_call_only_a_short_prefix_is_printed(
    stored_calls: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    mine, _ = stored_calls
    assert report.main(["--since", "5m"]) == 0
    out = capsys.readouterr().out
    assert mine not in out and mine[:10] in out
