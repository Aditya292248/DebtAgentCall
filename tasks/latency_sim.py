"""A simulated run for the latency numbers: server overhead only.

The signed fake caller (``sim/``) plays Sam Doe against the real webhook app, with
the project's fake model (no network, no waiting), an in-memory tool store and JSONL
sessions in a temporary folder. So what is timed is our own code: signature check,
dedupe, stop triggers, the stage machine, the agent loop and tool gate, the sentence
filter, the state save. It says nothing about a real model or a real network.

``simulate`` returns the rows the webhook recorded. ``paired_overhead`` runs the same
turns with timing off and on, interleaved, and compares the time the reply took to
leave the app. Used by ``uv run demo latency`` and for docs/latency.md.
"""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness import Agent
from harness.call_state import Debtor
from harness.call_tools import MemoryCallStore
from harness.memory import JsonlSession
from harness.tools import ToolRegistry
from model import FakeProvider, Provider
from model.fake import streamed, tool_call, tool_reply
from phone import signed_headers
from phone.live_tools import ToolBackend
from phone.timing import RecordTiming, TurnRecord
from phone.webhook import create_app, debt_as_found
from sim.agentphone_sim import Script, post_timed, run_script

SECRET = "whsec_latency_sim"
NUMBER = "+12135550101"
SAM = Debtor(
    debtor_id="00000000-0000-4000-8000-000000000001",
    debt_id="00000000-0000-4000-8000-00000000a001",
    first_name="Sam",
    last_name="Doe",
    dob=date(1985, 4, 12),
    zip="90012",
    creditor="Northwind Furniture",
    balance_cents=125000,
)
# Three fixed-line turns (greeting, identity, disclosure), then model turns: two plain
# answers, one that asks for a payment plan (a tool runs), and a goodbye.
LINES = [
    "Hello?",
    "Yes, this is Sam Doe speaking.",
    "April 12, 1985, and my ZIP is 90012.",
    "Can you tell me a little more about this?",
    "What options would I have?",
    "Could we set up a payment plan with a small down payment?",
    "Okay, thank you.",
]
PLAN = tool_call("offer_payment_plan", down_cents="25000", installments="10", frequency="monthly")


def _model(messages: list[dict]):  # type: ignore[no-untyped-def]
    last = messages[-1]
    if last.get("role") == "tool":
        return streamed("Thanks, I have noted that for review. ", "Is there anything else?")
    if "payment plan" in str(last.get("content", "")):
        return tool_reply(PLAN)
    return streamed("Sure, I can help with that. ", "What works best for you?")


def build_app(
    record_timing: RecordTiming | None,
    folder: Path,
    scripted: Any = _model,
    hold_after: float = 2.5,
) -> FastAPI:
    provider = Provider(
        base_url="fake://local", model="fake", responder=FakeProvider(scripted=scripted)
    )
    return create_app(
        secret=SECRET,
        agent_factory=lambda s: Agent(provider=provider, session=s, tools=ToolRegistry([])),
        open_session=lambda cid: JsonlSession.for_call(folder, cid),
        find_debtor={NUMBER: SAM}.get,
        audit=lambda d, call_id, debt_id: None,
        record_stop=lambda stop: None,
        open_backend=lambda debtor: ToolBackend(
            store=MemoryCallStore(), audit=lambda d, call_id, debt_id: None, flags=[]
        ),
        supervisor_number="",
        read_debt=debt_as_found,
        on_call_ended=lambda ended: None,  # type: ignore[arg-type,return-value]
        record_timing=record_timing,
        hold_after=hold_after,
    )


def simulate(calls: int) -> list[TurnRecord]:
    """Play ``calls`` simulated calls and return every turn's recorded timing."""
    rows: list[TurnRecord] = []
    with tempfile.TemporaryDirectory() as tmp:
        client = TestClient(build_app(rows.append, Path(tmp)))
        script = Script("latency", LINES, to_number=NUMBER)
        for _ in range(calls):
            run_script(script, client, SECRET)
    return rows


def _turn(app: FastAPI, call_id: str, text: str) -> float:
    """Seconds until the last line of the reply left the app."""
    body = json.dumps(
        {
            "event": "agent.message",
            "channel": "voice",
            "data": {"callId": call_id, "transcript": text, "to": NUMBER},
        }
    ).encode()
    headers = signed_headers(body, SECRET, time.time(), f"wh_{uuid.uuid4().hex}", "agent.message")
    status, chunks = post_timed(app, body, headers)
    assert status == 200
    return chunks[-1][0]


def paired_overhead(calls: int) -> tuple[list[float], list[float]]:
    """Seconds for the reply to leave the app, per turn: (timing off, timing on).

    Two apps share one folder; each simulated call is played on both, in alternating
    order, so drift in the machine's speed hits both alike. The "on" app records rows
    to a list (no database), which is the cost of the timer itself.
    """
    off: list[float] = []
    on: list[float] = []
    sink: list[TurnRecord] = []
    with tempfile.TemporaryDirectory() as tmp:
        apps = {"off": build_app(None, Path(tmp)), "on": build_app(sink.append, Path(tmp))}
        for n in range(calls):
            order = ("off", "on") if n % 2 == 0 else ("on", "off")
            for name in order:
                call_id = f"call_ovh_{name}_{uuid.uuid4().hex[:10]}"
                for line in LINES:
                    took = _turn(apps[name], call_id, line)
                    (on if name == "on" else off).append(took)
    return off, on
