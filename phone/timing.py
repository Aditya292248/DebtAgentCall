"""How long each live caller turn takes on our side, recorded without slowing it.

A ``TurnTimer`` is started when the webhook request arrives (only if the app was
given a ``record_timing`` writer) and kept in a context variable, so the call path
only calls small helpers here: ``span`` around a step, ``set_kind``, ``set_turn``,
and ``attach`` to time model calls and tools. Every helper does nothing when no
timer is running. All clocks are ``time.perf_counter()`` (monotonic).

What is stored (see ``TurnRecord``): numbers, flags and ids. Never text: no
transcript, no phone number, no name, no model output. ``tools`` holds tool names
and their time only.

The row is written after the reply has been handed to the server: the webhook
attaches ``timer.write`` as a background task of the response. A failed write is
logged and changes nothing about the call. No query runs in the hot path.

Not measured here: AgentPhone's speech-to-text and text-to-speech, the network to
and from AgentPhone, and the database (not counted separately). See docs/latency.md.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

KINDS = ("fixed", "model", "stop", "blocked", "failure", "replay")


@dataclass(frozen=True)
class TurnRecord:
    """One turn's timing, as stored in ``public.turn_timing``. Times are milliseconds."""

    call_id: str
    kind: str
    total_ms: float
    turn_seq: int | None = None
    stage: str | None = None
    verified_ms: float | None = None  # request received to signature checked
    triage_ms: float | None = None  # stop triggers plus the stage step
    first_line_ms: float | None = None  # request received to first line handed over
    model_ms: float = 0.0
    model_calls: int = 0
    model_first_ms: float | None = None  # first model call: wait for its first output
    tool_ms: float = 0.0
    tool_calls: int = 0
    tools: dict[str, dict[str, float]] = field(default_factory=dict)  # name -> count, ms
    hold_line: bool = False
    streamed: bool = False


RecordTiming = Callable[[TurnRecord], None]


class TurnTimer:
    def __init__(self, write: RecordTiming) -> None:
        self._write = write
        self._lock = threading.Lock()  # for the tool and model sums only, held for an add
        self.t0 = time.perf_counter()
        self.call_id: str | None = None
        self.turn_seq: int | None = None
        self.stage: str | None = None
        self.kind: str | None = None
        self.verified_at: float | None = None
        self.triage = 0.0
        self.first_line_at: float | None = None
        self.end_at: float | None = None
        self.model = 0.0
        self.model_calls = 0
        self.model_first: float | None = None
        self.tools: dict[str, list[float]] = {}  # name -> [count, seconds]
        self.hold = False
        self.streamed = False

    # --- called from the call path ---------------------------------------------------

    def mark_verified(self) -> None:
        self.verified_at = time.perf_counter()

    def first_line(self) -> None:
        if self.first_line_at is None:
            self.first_line_at = time.perf_counter()

    def finish(self) -> None:
        """The last line has been handed to the server."""
        if self.end_at is None:
            self.end_at = time.perf_counter()

    def model_call(self, seconds: float, first_output: float) -> None:
        with self._lock:
            self.model += seconds
            self.model_calls += 1
            if self.model_first is None:
                self.model_first = first_output

    def tool_call(self, name: str, seconds: float) -> None:
        with self._lock:
            entry = self.tools.setdefault(name, [0, 0.0])
            entry[0] += 1
            entry[1] += seconds

    # --- turning it into a row --------------------------------------------------------

    def record(self) -> TurnRecord | None:
        """The row for this turn, or None if the turn never learned its call id."""
        if self.call_id is None:
            return None
        end = self.end_at if self.end_at is not None else time.perf_counter()
        kind = self.kind or ("model" if self.model_calls else "fixed")
        return TurnRecord(
            call_id=self.call_id,
            kind=kind,
            total_ms=_ms(end - self.t0),
            turn_seq=self.turn_seq,
            stage=self.stage,
            verified_ms=None if self.verified_at is None else _ms(self.verified_at - self.t0),
            triage_ms=_ms(self.triage),
            first_line_ms=None if self.first_line_at is None else _ms(self.first_line_at - self.t0),
            model_ms=_ms(self.model),
            model_calls=self.model_calls,
            model_first_ms=None if self.model_first is None else _ms(self.model_first),
            tool_ms=_ms(sum(s for _, s in self.tools.values())),
            tool_calls=int(sum(n for n, _ in self.tools.values())),
            tools={k: {"count": int(n), "ms": _ms(s)} for k, (n, s) in self.tools.items()},
            hold_line=self.hold,
            streamed=self.streamed,
        )

    def write(self) -> None:
        """Run after the reply is sent. Never raises: a lost row is logged, nothing more."""
        try:
            row = self.record()
            if row is not None:
                self._write(row)
        except Exception:
            log.exception("could not record the timing of a turn; the call is not affected")


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 3)


_current: ContextVar[TurnTimer | None] = ContextVar("turn_timer", default=None)


def start(write: RecordTiming | None) -> TurnTimer | None:
    """Start timing a request. With no writer, timing is off and this is None."""
    if write is None:
        return None
    timer = TurnTimer(write)
    _current.set(timer)
    return timer


def current() -> TurnTimer | None:
    return _current.get()


@contextmanager
def span() -> Iterator[None]:
    """Add the time spent inside the block to the stop-trigger and stage-step total."""
    timer = _current.get()
    if timer is None:
        yield
        return
    began = time.perf_counter()
    try:
        yield
    finally:
        timer.triage += time.perf_counter() - began


def set_kind(kind: str) -> None:
    if (timer := _current.get()) is not None:
        timer.kind = kind


def set_turn(call_id: str | None = None, seq: int | None = None, stage: str | None = None) -> None:
    if (timer := _current.get()) is not None:
        if call_id is not None:
            timer.call_id = call_id
        if seq is not None:
            timer.turn_seq = seq
        if stage is not None:
            timer.stage = stage


def attach(agent: Any) -> None:
    """Have the agent report its model calls and tool runs to this turn's timer."""
    if (timer := _current.get()) is not None:
        agent.on_model_call = timer.model_call
        agent.on_tool_time = timer.tool_call


INSERT_SQL = (
    "insert into public.turn_timing (call_id, turn_seq, stage, kind, total_ms, verified_ms,"
    " triage_ms, first_line_ms, model_ms, model_calls, model_first_ms, tool_ms, tool_calls,"
    " tools, hold_line, streamed) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
    " %s, %s, %s, %s)"
)


def record_in_db(row: TurnRecord) -> None:
    """The default writer: one insert, committed on its own connection."""
    from psycopg.types.json import Jsonb

    from harness.db import connect

    with connect() as conn:
        conn.execute(
            INSERT_SQL,
            (
                row.call_id, row.turn_seq, row.stage, row.kind, row.total_ms, row.verified_ms,
                row.triage_ms, row.first_line_ms, row.model_ms, row.model_calls,
                row.model_first_ms, row.tool_ms, row.tool_calls, Jsonb(row.tools),
                row.hold_line, row.streamed,
            ),
        )  # fmt: skip
