"""Streamed replies: the agent starts speaking after its first safe sentence.

What it checks, through the signed webhook with stages on and a streaming fake model:
  - the model streams a safe sentence, then a balance or a threat before
    verification: the first sentence goes out as an interim chunk before the
    model has finished, and the blocked one never appears in any chunk (the
    negative case: before this change the reply was one JSON object, sent only
    after the whole reply was written)
  - the blocked sentence is audited, the turn ends with the safe line, and the
    saved history holds only what was said
  - ``end_call``: the last chunk carries ``hangup`` and the call ends
  - a slow model: the fixed hold line goes out first, then the reply
  - time to the first chunk, measured: p50 under 0.7 s and p95 under 1.5 s
  - a tool mid-reply: its row is saved before a sentence after it is sent; a
    plan saved only as pending never streams "approved"; a failed save stops
    the stream with the fixed line and a hangup
  - fixed lines, stop triggers and a retried webhook are one plain JSON reply,
    and a bad signature is still refused before anything runs
  - the pieces: the sentence buffer, the fake's scripted streaming, the
    OpenAI-compatible SSE reader, and ``Agent.run_stream`` stopped mid-reply
"""

from __future__ import annotations

import json
import statistics
import time
import uuid
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from harness import Agent
from harness.call_state import Debtor, Stage
from harness.call_tools import MemoryCallStore
from harness.memory import JsonlSession
from harness.tools import ToolRegistry
from model import FakeProvider, Provider, chat_stream, stream_openai, streamed
from model.fake import tool_call
from model.provider import LLMResponse
from phone import signed_headers
from phone.live_tools import COMMITMENT_RULE, PENDING_LINE, ToolBackend
from phone.replies import SentenceBuffer
from phone.webhook import (
    CANT_CONTINUE,
    HOLD_LINE,
    SAFE_LINE,
    WEBHOOK_PATH,
    create_app,
    debt_as_found,
)
from sim.agentphone_sim import Script, combine, post_timed, reply_of, run_script

SECRET = "whsec_test_only"
SAM_PHONE = "+12135550101"
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
TO_DISCUSS = ["Hello?", "Yes, speaking.", "4/12/1985, 90012"]
LEAKS = ("1,250", "1250", "Northwind", "arrest")
SAFE_FIRST = "Thanks for taking my call. "
IN_POLICY = tool_call(
    "offer_payment_plan", down_cents="25000", installments="10", frequency="monthly"
)
OUTSIDE_POLICY = tool_call(
    "offer_payment_plan", down_cents="5000", installments="18", frequency="monthly"
)

Chunks = list[tuple[float, dict]]


class SlowStore(MemoryCallStore):
    """Takes ``delay`` seconds to save a plan, so the order of events shows in time."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    def save_plan(self, ctx, plan, status) -> None:  # type: ignore[no-untyped-def]
        time.sleep(self.delay)
        super().save_plan(ctx, plan, status)


class BrokenStore(MemoryCallStore):
    def save_plan(self, ctx, plan, status) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("database went away")


class Call:
    """The webhook in-process with a streaming fake model and an in-memory tool backend."""

    def __init__(  # type: ignore[no-untyped-def]
        self, tmp_path: Path, scripted, *, store=None, hold_after: float = 2.5
    ) -> None:
        self.responder = FakeProvider(scripted=scripted)
        self.store = store if store is not None else MemoryCallStore()
        self.blocked: list = []  # audited utterance_blocked decisions
        self.stops: list = []
        self.folder = tmp_path / "sessions"
        provider = Provider(base_url="fake://local", model="fake", responder=self.responder)
        self.app = create_app(
            secret=SECRET,
            agent_factory=lambda s: Agent(provider=provider, session=s, tools=ToolRegistry([])),
            open_session=lambda cid: JsonlSession.for_call(self.folder, cid),
            find_debtor={SAM_PHONE: SAM}.get,
            audit=lambda d, call_id, debt_id: self.blocked.append(d),
            record_stop=self.stops.append,
            open_backend=lambda debtor: ToolBackend(
                store=self.store, audit=lambda d, call_id, debt_id: None, flags=[]
            ),
            supervisor_number="",
            read_debt=debt_as_found,
            hold_after=hold_after,
        )

    def _signed(self, call_id: str, text: str, webhook_id: str | None) -> tuple[bytes, dict]:
        body = json.dumps(
            {
                "event": "agent.message",
                "channel": "voice",
                "data": {"callId": call_id, "transcript": text, "to": SAM_PHONE},
            }
        ).encode()
        wid = webhook_id or f"wh_{uuid.uuid4().hex}"
        return body, signed_headers(body, SECRET, time.time(), wid, "agent.message")

    def timed(self, call_id: str, text: str, webhook_id: str | None = None) -> Chunks:
        """One turn straight to the ASGI app, with the time each chunk left it."""
        status, chunks = post_timed(self.app, *self._signed(call_id, text, webhook_id))
        assert status == 200
        return chunks

    def client_turn(self, call_id: str, text: str, webhook_id: str | None = None):  # type: ignore[no-untyped-def]
        body, headers = self._signed(call_id, text, webhook_id)
        return TestClient(self.app).post(WEBHOOK_PATH, content=body, headers=headers)

    def to_discuss(self, call_id: str) -> None:
        for line in TO_DISCUSS:
            self.timed(call_id, line)

    def saved(self, call_id: str) -> list[dict]:
        return JsonlSession.for_call(self.folder, call_id).load()

    def stage(self, call_id: str) -> Stage:
        state = JsonlSession.for_call(self.folder, call_id).last_state() or {}
        return Stage(state["stage"])


def texts(chunks: Chunks) -> list[str]:
    return [c.get("text", "") for _, c in chunks]


def assert_stream_shape(chunks: Chunks) -> None:
    """Every chunk but the last is interim; the last one closes the turn."""
    assert len(chunks) >= 2
    assert all(c.get("interim") is True for _, c in chunks[:-1])
    assert "interim" not in chunks[-1][1]


# --- the negative case: a safe sentence, then one that must not be said ----------------


@pytest.mark.parametrize(
    ("bad", "rules"),
    [
        ("You owe $1,250.00 to Northwind Furniture. ", {"third_party_disclosure"}),
        ("If you don't pay we will have you arrested. ", {"threat"}),
    ],
)
def test_first_sentence_streams_and_the_blocked_one_never_does(
    tmp_path: Path, bad: str, rules: set[str]
) -> None:
    model = streamed(SAFE_FIRST, 0.4, bad, "Who am I speaking with?")
    call = Call(tmp_path, [model])
    call.timed("call_neg", "Hello?")  # GREETING's fixed line; no model
    chunks = call.timed("call_neg", "Who is this?")  # still GREETING: the model answers

    # The first sentence left before the model wrote the next one.
    assert chunks[0][1] == {"text": SAFE_FIRST.strip(), "interim": True}
    assert chunks[0][0] < 0.4
    assert_stream_shape(chunks)
    # The blocked sentence, and anything after it, is in no chunk at all.
    assert not any(leak in text for text in texts(chunks) for leak in LEAKS)
    assert "Who am I speaking with?" not in texts(chunks)
    assert chunks[-1][1] == {"text": SAFE_LINE}
    assert {d.rule_id for d in call.blocked} == rules and len(call.blocked) == 1
    assert call.saved("call_neg")[-1] == {
        "role": "assistant",
        "content": f"{SAFE_FIRST.strip()} {SAFE_LINE}",
    }
    assert call.stage("call_neg") == Stage.GREETING


def test_the_signed_simulator_hears_only_what_was_streamed(tmp_path: Path) -> None:
    model = streamed(SAFE_FIRST, "Your balance is $1,250.00. ", "Is this Sam?")
    call = Call(tmp_path, [model])
    replies: list[list[dict]] = []
    turns = run_script(
        Script("p25", ["Hello?", "Who is this?"]), TestClient(call.app), SECRET, replies=replies
    )
    assert turns[1][1] == f"{SAFE_FIRST.strip()} {SAFE_LINE}"
    assert replies[1][0] == {"text": SAFE_FIRST.strip(), "interim": True}
    assert not any(leak in json.dumps(replies) for leak in LEAKS)


# --- how a streamed turn ends -------------------------------------------------------------


def test_end_call_puts_hangup_on_the_last_chunk(tmp_path: Path) -> None:
    script = [
        streamed("Thanks for your time today. ", tool_calls=[tool_call("end_call", reason="done")]),
        streamed("Goodbye."),
    ]
    call = Call(tmp_path, script)
    call.to_discuss("call_end")
    chunks = call.timed("call_end", "That's all, thanks.")

    assert_stream_shape(chunks)
    assert texts(chunks)[:2] == ["Thanks for your time today.", "Goodbye."]
    assert chunks[-1][1].get("hangup") is True
    assert call.stage("call_end") == Stage.WRAP

    # The simulator stops at the hangup: "Hello?" is never sent.
    call2 = Call(tmp_path / "sim", script)
    turns = run_script(
        Script("p25", [*TO_DISCUSS, "That's all.", "Hello?"]), TestClient(call2.app), SECRET
    )
    assert [c for c, _ in turns][-1] == "That's all."


def test_a_slow_model_gets_the_hold_line_first(tmp_path: Path) -> None:
    call = Call(
        tmp_path, [streamed(0.6, "Here is what I can do. ", "Shall we go on?")], hold_after=0.2
    )
    call.to_discuss("call_slow")
    chunks = call.timed("call_slow", "What are my options?")

    assert chunks[0][1] == {"text": HOLD_LINE, "interim": True}
    assert 0.15 < chunks[0][0] < 0.5
    assert texts(chunks)[1:3] == ["Here is what I can do.", "Shall we go on?"]
    assert chunks[1][0] >= 0.55
    assert_stream_shape(chunks)


def test_a_quick_model_never_hears_the_hold_line(tmp_path: Path) -> None:
    call = Call(tmp_path, [streamed("Sure. ", "What works for you?")], hold_after=0.5)
    call.to_discuss("call_quick")
    chunks = call.timed("call_quick", "Can we talk options?")
    assert HOLD_LINE not in texts(chunks)
    assert texts(chunks)[0] == "Sure."


def test_time_to_first_chunk(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The model writes its first sentence at once, then takes 0.15 s for the rest:
    # what is measured is our side (signature, dedupe, triggers, state, filter).
    call = Call(tmp_path, [streamed("Sure, I can help with that. ", 0.15, "What works for you?")])
    call.to_discuss("call_t")
    firsts: list[float] = []
    for i in range(20):
        chunks = call.timed("call_t", f"Can we talk about option {i}?")
        assert chunks[0][1]["text"] == "Sure, I can help with that."
        assert chunks[-1][0] >= 0.15  # the reply was still being written
        firsts.append(chunks[0][0])
    p50 = statistics.median(firsts)
    p95 = statistics.quantiles(firsts, n=20)[-1]
    with capsys.disabled():
        print(f"\n  first chunk: p50 {p50 * 1000:.1f} ms, p95 {p95 * 1000:.1f} ms (20 turns)")
    assert p50 < 0.7 and p95 < 1.5


# --- tools mid-reply ----------------------------------------------------------------------


def test_a_plan_is_saved_before_the_sentence_that_promises_it(tmp_path: Path) -> None:
    script = [
        streamed("Let me set that up for you. ", tool_calls=[IN_POLICY]),
        streamed("Your plan is approved. ", "The first payment is $250.00 today."),
    ]
    call = Call(tmp_path, script, store=SlowStore(0.3))
    call.to_discuss("call_plan")
    chunks = call.timed("call_plan", "Can I put 250 down and pay over ten months?")

    assert texts(chunks)[:3] == [
        "Let me set that up for you.",
        "Your plan is approved.",
        "The first payment is $250.00 today.",
    ]
    assert chunks[0][0] < 0.3 <= chunks[1][0]  # spoken before the save; promised after it
    (row,) = call.store.rows
    assert (row["kind"], row["status"]) == ("payment_plan", "approved")
    assert call.blocked == []


def test_a_pending_plan_never_streams_approved(tmp_path: Path) -> None:
    script = [
        streamed("Let me check that. ", tool_calls=[OUTSIDE_POLICY]),
        streamed("Great news, your plan is approved. ", "Anything else?"),
    ]
    call = Call(tmp_path, script)
    call.to_discuss("call_pend")
    chunks = call.timed("call_pend", "Fifty down and eighteen months?")

    assert texts(chunks)[0] == "Let me check that."
    assert not any("approved" in t for t in texts(chunks))
    assert chunks[-1][1] == {"text": PENDING_LINE}
    assert [d.rule_id for d in call.blocked] == [COMMITMENT_RULE]
    assert [r["status"] for r in call.store.rows] == ["pending"]


def test_a_failed_save_stops_the_stream_and_hangs_up(tmp_path: Path) -> None:
    script = [
        streamed("Let me set that up. ", tool_calls=[IN_POLICY]),
        streamed("Your plan is approved."),
    ]
    call = Call(tmp_path, script, store=BrokenStore())
    call.to_discuss("call_broken")
    chunks = call.timed("call_broken", "Can we do a plan?")

    assert texts(chunks)[0] == "Let me set that up."
    assert not any("approved" in t for t in texts(chunks))
    assert chunks[-1][1] == {"text": CANT_CONTINUE, "hangup": True}


# --- what stays a single reply ------------------------------------------------------------


def test_fixed_lines_and_stop_triggers_are_one_json_reply(tmp_path: Path) -> None:
    call = Call(tmp_path, [streamed("Should never be said.")])
    greeting = call.client_turn("call_fixed", "Hello?")
    assert greeting.headers["content-type"].startswith("application/json")
    call.timed("call_fixed", "Yes, speaking.")
    call.timed("call_fixed", "4/12/1985, 90012")
    stop = call.client_turn("call_fixed", "Stop calling me.")
    assert stop.headers["content-type"].startswith("application/json")
    assert stop.json().get("hangup") is True
    assert call.responder.calls == []  # stop triggers run before the model
    assert [s.trigger.kind for s in call.stops] == ["cease"]


def test_a_retry_gets_the_whole_reply_without_the_model(tmp_path: Path) -> None:
    call = Call(tmp_path, [streamed("Sure. ", "What works for you?")])
    call.to_discuss("call_retry")
    first = call.timed("call_retry", "Options?", webhook_id="wh_same")
    again = call.client_turn("call_retry", "Options?", webhook_id="wh_same")
    assert again.headers["content-type"].startswith("application/json")
    assert again.json() == combine([c for _, c in first]) == {"text": "Sure. What works for you?"}
    assert len(call.responder.calls) == 1


def test_a_bad_signature_is_refused_before_anything_streams(tmp_path: Path) -> None:
    call = Call(tmp_path, [streamed("Should never be said.")])
    body, headers = call._signed("call_sig", "Hello?", None)
    status, chunks = post_timed(call.app, body + b" ", headers)
    assert status == 401 and "error" in chunks[0][1]
    assert call.responder.calls == []


# --- the pieces ---------------------------------------------------------------------------


def test_sentence_buffer_waits_for_a_whole_sentence() -> None:
    buf = SentenceBuffer()
    assert buf.feed("Your payment of $1,250.") == []
    assert buf.feed("50 is due. Mr.") == ["Your payment of $1,250.50 is due."]
    assert buf.feed(" Doe will call") == []
    assert buf.feed(" you back.") == []
    assert buf.flush() == ["Mr. Doe will call you back."]
    assert buf.flush() == []
    # A space after the end mark already shows the sentence is over.
    assert buf.feed("Thanks for holding. Your") == ["Thanks for holding."]
    assert SentenceBuffer().feed("Thanks. ") == ["Thanks."]


def test_fake_streams_its_script_and_keeps_calls_inspectable() -> None:
    fake = FakeProvider(scripted=[streamed("Hi. ", "There.", tool_calls=[tool_call("end_call")])])
    provider = Provider(base_url="fake://local", model="fake", responder=fake)
    parts = list(chat_stream([{"role": "user", "content": "x"}], provider=provider))
    assert parts[:2] == ["Hi. ", "There."]
    final = parts[-1]
    assert isinstance(final, LLMResponse) and final.content == "Hi. There."
    assert final.tool_calls[0]["function"]["name"] == "end_call"
    assert fake.calls == [[{"role": "user", "content": "x"}]]
    assert fake.kwargs[0]["stream"] is True and fake.streamed == 1
    # The same script entry, not streamed, is the whole reply at once.
    assert fake([{"role": "user", "content": "y"}]).content == "Hi. There."


def test_openai_stream_reader_puts_text_and_tool_calls_together() -> None:
    events = [
        {"choices": [{"delta": {"role": "assistant", "content": "Let me "}}]},
        {"choices": [{"delta": {"content": "check."}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "function": {"name": "end_call", "arguments": '{"rea'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": 'son": "done"}'}}]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = Provider(base_url="http://llm.test/v1", model="m")
    parts = list(stream_openai(provider, [{"role": "user", "content": "x"}], client=client))
    assert parts[:2] == ["Let me ", "check."]
    final = parts[-1]
    assert isinstance(final, LLMResponse)
    assert final.content == "Let me check." and final.finish_reason == "tool_calls"
    assert final.tool_calls == [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "end_call", "arguments": '{"reason": "done"}'},
        }
    ]
    assert seen[0]["stream"] is True


def test_run_stream_stopped_mid_reply_leaves_a_valid_history(tmp_path: Path) -> None:
    fake = FakeProvider(scripted=[streamed("One. ", "Two. ", tool_calls=[tool_call("end_call")])])
    provider = Provider(base_url="fake://local", model="fake", responder=fake)
    session = JsonlSession.for_call(tmp_path, "c")
    agent = Agent(provider=provider, session=session, tools=ToolRegistry([]), skills_dir=tmp_path)
    stream = agent.run_stream("hi")
    assert next(stream) == "One. "
    stream.close()  # stopped before the rest, and before any tool request
    assert agent.messages[-1] == {"role": "assistant", "content": "One. "}
    assert session.load() == agent.messages

    # Stopped at the end of a model call that asked for a tool: the request is dropped.
    agent2 = Agent(provider=provider, session=None, tools=ToolRegistry([]), skills_dir=tmp_path)
    stream2 = agent2.run_stream("hi")
    assert [next(stream2), next(stream2), next(stream2)] == ["One. ", "Two. ", None]
    stream2.close()
    assert agent2.messages[-1] == {"role": "assistant", "content": "One. Two. "}


def test_reply_of_reads_both_reply_kinds() -> None:
    plain = httpx.Response(200, json={"text": "Hi.", "hangup": True})
    lines = '{"text":"Hi.","interim":true}\n{"text":"","hangup":true}\n'
    streamed_resp = httpx.Response(
        200, text=lines, headers={"content-type": "application/x-ndjson"}
    )
    assert reply_of(plain) == reply_of(streamed_resp) == {"text": "Hi.", "hangup": True}
    transfer = httpx.Response(200, json={"action": "transfer"})
    assert reply_of(transfer) == {"action": "transfer"}
