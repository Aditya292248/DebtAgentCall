"""Per-turn timing: what is recorded, and that recording never touches the call.

The webhook is driven with the signed fake caller's helpers, a fake model and an
in-memory tool store, and each turn's timing row goes to a list (or, for the
database test, to ``public.turn_timing`` through ``record_in_db``).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from model.fake import streamed
from phone import signed_headers
from phone.timing import TurnRecord, record_in_db
from phone.webhook import create_app
from sim.agentphone_sim import post_timed
from tasks.latency_sim import NUMBER, SECRET, build_app

DISTINCT_CALLER = "My marmalade zanzibar quokka sentence"
DISTINCT_MODEL = "Certainly, the velvet harpsichord option is open."
TO_DISCUSS = ["Hello?", "Yes, this is Sam Doe speaking.", "April 12, 1985, and my ZIP is 90012."]
MORE = "Can you tell me more about this?"
PLAN = "Could we set up a payment plan with a small down payment?"


def signed(call_id: str, text: str, webhook_id: str | None = None) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "event": "agent.message",
            "channel": "voice",
            "data": {"callId": call_id, "transcript": text, "to": NUMBER},
        }
    ).encode()
    wid = webhook_id or f"wh_{uuid.uuid4().hex}"
    return body, signed_headers(body, SECRET, time.time(), wid, "agent.message")


def turn(app: Any, call_id: str, text: str) -> list[tuple[float, dict]]:
    status, chunks = post_timed(app, *signed(call_id, text))
    assert status == 200
    return chunks


def reply_texts(chunks: list[tuple[float, dict]]) -> list[str]:
    return [c.get("text", "") for _, c in chunks]


def to_discuss(app: Any, call_id: str) -> None:
    for line in TO_DISCUSS:
        turn(app, call_id, line)


def say_model(text: str):  # type: ignore[no-untyped-def]
    return lambda messages: streamed(text)


# --- (e) fixed lines record no model time, model turns record a model call -------------


def test_a_fixed_line_turn_has_no_model_time_and_a_model_turn_has_a_call(tmp_path: Path) -> None:
    rows: list[TurnRecord] = []
    app = build_app(rows.append, tmp_path)
    to_discuss(app, "call_kinds")
    turn(app, "call_kinds", MORE)
    fixed, model = rows[0], rows[-1]
    assert [r.call_id for r in rows] == ["call_kinds"] * 4
    assert (fixed.kind, fixed.model_calls, fixed.model_ms, fixed.tool_calls) == ("fixed", 0, 0, 0)
    assert fixed.stage == "GREETING" and fixed.turn_seq == 1
    assert model.kind == "model" and model.model_calls >= 1 and model.model_ms > 0
    assert model.stage == "DISCUSS" and model.turn_seq == 4


def test_a_tool_turn_counts_the_tool_by_name(tmp_path: Path) -> None:
    rows: list[TurnRecord] = []
    app = build_app(rows.append, tmp_path)
    to_discuss(app, "call_tool")
    turn(app, "call_tool", PLAN)
    row = rows[-1]
    assert row.kind == "model" and row.tool_calls == 1 and row.model_calls == 2
    assert set(row.tools) == {"offer_payment_plan"}
    assert row.tools["offer_payment_plan"]["count"] == 1
    assert row.tool_ms == pytest.approx(row.tools["offer_payment_plan"]["ms"])


def test_the_times_are_ordered_the_way_a_turn_runs(tmp_path: Path) -> None:
    rows: list[TurnRecord] = []
    app = build_app(rows.append, tmp_path)
    to_discuss(app, "call_order")
    turn(app, "call_order", MORE)
    r = rows[-1]
    assert r.verified_ms is not None and r.first_line_ms is not None
    assert 0 < r.verified_ms < r.first_line_ms <= r.total_ms
    assert r.triage_ms is not None and r.triage_ms < r.total_ms
    assert r.model_ms <= r.total_ms and r.hold_line is False


def test_a_stop_turn_a_blocked_turn_and_a_hold_turn_are_marked(tmp_path: Path) -> None:
    rows: list[TurnRecord] = []
    app = build_app(rows.append, tmp_path)
    to_discuss(app, "call_stop")
    turn(app, "call_stop", "Stop calling me.")
    assert rows[-1].kind == "stop" and rows[-1].model_calls == 0

    rows.clear()
    blocked = build_app(rows.append, tmp_path, say_model("We will sue you. Please call back."))
    to_discuss(blocked, "call_blocked")
    turn(blocked, "call_blocked", "What happens next?")
    assert rows[-1].kind == "blocked"

    rows.clear()
    slow = build_app(rows.append, tmp_path, lambda m: streamed(0.3, "Sure."), hold_after=0.05)
    to_discuss(slow, "call_hold")
    chunks = turn(slow, "call_hold", "What happens next?")
    assert reply_texts(chunks)[0].startswith("One moment")
    assert rows[-1].hold_line is True and rows[-1].streamed is True
    assert rows[-1].first_line_ms is not None and rows[-1].first_line_ms >= 50
    assert rows[-1].substantive_ms is not None
    assert rows[-1].substantive_ms > rows[-1].first_line_ms + 100


def test_a_retry_with_the_same_webhook_id_makes_no_second_row(tmp_path: Path) -> None:
    rows: list[TurnRecord] = []
    app = build_app(rows.append, tmp_path)
    to_discuss(app, "call_replay")
    body, headers = signed("call_replay", MORE)
    post_timed(app, body, headers)
    post_timed(app, body, headers)  # answered from the saved reply, no model, no new row
    assert len(rows) == 4 and rows[-1].kind == "model"


# --- (a) a failed write never changes the reply, and the call goes on -------------------


def run_call(app: Any, call_id: str) -> list[list[str]]:
    to_discuss(app, call_id)
    return [
        reply_texts(turn(app, call_id, MORE)),
        reply_texts(turn(app, call_id, PLAN)),
        reply_texts(turn(app, call_id, "Stop calling me.")),
    ]


def test_a_failed_timing_write_never_changes_the_reply(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(row: TurnRecord) -> None:
        raise RuntimeError("database went away")

    plain = run_call(build_app(None, tmp_path / "a"), "call_same")
    with caplog.at_level(logging.ERROR):
        failing = run_call(build_app(broken, tmp_path / "b"), "call_same")
    assert failing == plain  # same words, same hangup, and the call went on to the end
    assert "could not record the timing" in caplog.text


def test_a_slow_timing_write_does_not_delay_the_reply(tmp_path: Path) -> None:
    def slow(row: TurnRecord) -> None:
        time.sleep(0.4)

    app = build_app(slow, tmp_path)
    to_discuss(app, "call_slow_write")
    chunks = turn(app, "call_slow_write", MORE)
    assert chunks[-1][0] < 0.3  # the reply left long before the 0.4 s write finished


# --- (c) the write happens after the reply has been handed over -------------------------


@pytest.mark.parametrize("text", [MORE, "Hello?"])  # a streamed-capable turn, a fixed line
def test_the_timing_write_runs_after_the_reply_is_sent(tmp_path: Path, text: str) -> None:
    events: list[str] = []
    app = build_app(lambda row: events.append("write"), tmp_path)

    async def spy(scope, receive, send):  # type: ignore[no-untyped-def]
        async def send2(message: dict) -> None:
            if message["type"] == "http.response.body" and not message.get("more_body"):
                events.append("reply sent")
            await send(message)

        await app(scope, receive, send2)

    call_id = f"call_after_{abs(hash(text))}"
    if text == MORE:
        to_discuss(app, call_id)
    events.clear()
    status, _ = post_timed(spy, *signed(call_id, text))
    assert status == 200
    assert events == ["reply sent", "write"]


# --- (b) no text is stored -------------------------------------------------------------


def test_the_row_has_only_the_expected_numeric_and_id_fields() -> None:
    assert {f.name for f in dataclasses.fields(TurnRecord)} == {
        "call_id", "kind", "total_ms", "turn_seq", "stage", "verified_ms", "triage_ms",
        "first_line_ms", "model_ms", "model_calls", "model_first_ms", "tool_ms", "tool_calls",
        "tools", "hold_line", "streamed", "substantive_ms",
    }  # fmt: skip


def test_no_caller_or_model_text_or_phone_number_reaches_the_row(tmp_path: Path) -> None:
    rows: list[TurnRecord] = []
    app = build_app(rows.append, tmp_path, say_model(DISTINCT_MODEL))
    to_discuss(app, "call_text")
    turn(app, "call_text", DISTINCT_CALLER)
    turn(app, "call_text", "Stop calling me, my number is 213 555 0101.")
    stored = json.dumps([dataclasses.asdict(r) for r in rows])
    for secret in (DISTINCT_CALLER, DISTINCT_MODEL, "marmalade", "harpsichord", "Sam", "Doe",
                   "555", "1985", "90012"):  # fmt: skip
        assert secret not in stored
    assert any(r.kind == "model" for r in rows) and any(r.kind == "stop" for r in rows)


@pytest.mark.db
def test_the_stored_database_row_holds_no_text() -> None:
    from harness.db import connect

    call_id = f"call_timing_{uuid.uuid4().hex[:12]}"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app = build_app(record_in_db, Path(tmp), say_model(DISTINCT_MODEL))
            to_discuss(app, call_id)
            turn(app, call_id, DISTINCT_CALLER)
        with connect() as conn:
            stored = conn.execute(
                "select row_to_json(t)::text from public.turn_timing t where call_id = %s",
                (call_id,),
            ).fetchall()
        assert len(stored) == 4
        blob = " ".join(r[0] for r in stored)
        for secret in (DISTINCT_CALLER, DISTINCT_MODEL, "marmalade", "harpsichord", "555"):
            assert secret not in blob
    finally:
        with connect() as conn:
            conn.execute("delete from public.turn_timing where call_id = %s", (call_id,))


def test_timing_is_off_unless_a_writer_is_given(tmp_path: Path) -> None:
    """The default app records nothing, so every older test runs exactly as before."""
    app = build_app(None, tmp_path)
    assert reply_texts(turn(app, "call_off", "Hello?"))[0]
    assert create_app(secret=SECRET) is not None


def test_busy_verified_caller_uses_the_streaming_model(tmp_path: Path) -> None:
    rows: list[TurnRecord] = []
    app = build_app(rows.append, tmp_path, say_model("When would suit you?"))
    to_discuss(app, "call_busy")
    chunks = turn(app, "call_busy", "The shop is opening and I need to go.")
    assert "".join(reply_texts(chunks)) == "When would suit you?"
    assert rows[-1].kind == "model" and rows[-1].model_calls == 1
    assert rows[-1].substantive_ms is not None
