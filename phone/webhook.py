"""The webhook AgentPhone calls during a phone call.

AgentPhone does the speech: it turns what the caller says into text, POSTs it
here, and speaks whatever text we answer with. Each request goes through:

1. Read the raw body, before any JSON parsing, and check the signature and
   timestamp with ``phone.signing.verify``. Anything that fails gets a 401 and
   never reaches the model.
2. If this ``X-Webhook-ID`` was seen before, send back the same reply as the
   first time without calling the model again.
3. Act on the event:
   - ``agent.message`` on the voice channel: one caller turn (below).
   - ``agent.call_ended``: accepted with ``{}``. Nothing is stored yet.
   - anything else: ``{}``.

One caller turn, with call stages on (the default):

1. Find who the call is for from the phone number we dialed (``data.to`` on an
   outbound call), and open the session for ``data.callId``. An unknown number,
   or a call with no id, gets a fixed goodbye: no model, no debt facts.
2. Load the call's ``CallState`` and run ``harness.stages.next_stage``. Turns
   of the same call wait for each other, so they never see stale state.
3. If it gives a fixed line (greeting, identity check, disclosure, closes),
   speak it. The model is not called.
4. Otherwise run the agent with ``system.md`` plus the stage's file from
   ``instructions/stages/``. Until the person is verified and has heard the
   disclosure, it gets no tools and no skills.
5. Cut the reply into sentences and check each one with
   ``compliance.check_sentence``. Blocked sentences are dropped and written to
   the audit log (``kind="utterance_blocked"``). If nothing is left, a fixed
   safe line is spoken instead. The saved history keeps what was really said.
6. Save the new ``CallState`` in the session.

``create_app(stages=False)`` is the bare webhook from before: every turn goes
straight to the model, unchecked. Only the older tests use it.

Each turn builds a new ``Agent``, but on that call's saved session, so it sees
every earlier turn of the same call and nothing from any other call. Sessions
are kept in Postgres (``call_message``) unless ``open_session`` says otherwise.

Run it with ``uv run serve``. Tests build their own app with ``create_app``,
passing a fake model, a test secret, JSONL sessions in a temp folder, and their
own debtor lookup and audit log.
"""

from __future__ import annotations

import json
import os
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from compliance import Decision, SpeechState, check_sentence, split_sentences
from harness import Agent
from harness.call_state import CallState, Debtor, Stage, load_state, save_state
from harness.memory import PostgresSession, Session
from harness.stages import ENDING, instructions_path, next_stage
from harness.tools import ToolRegistry
from model import Provider
from model.provider import _load_dotenv
from phone.replies import say, say_and_hangup
from phone.signing import ID_HEADER, verify

WEBHOOK_PATH = "/webhooks/agentphone"
SECRET_ENV = "AGENTPHONE_WEBHOOK_SECRET"
DIDNT_CATCH = "Sorry, I didn't catch that. Could you say it again?"
SAFE_LINE = "Sorry, could you say that again?"  # when every sentence was blocked
CANT_CONTINUE = "Sorry, I can't continue this call right now. Goodbye."
MAX_REMEMBERED_IDS = 10_000  # enough to spot repeats, without growing forever
# Turns of the same call run one at a time. A fixed set of locks, picked by call
# id, so memory doesn't grow with calls; two calls rarely share one. This only
# covers one server process; several servers would need a database lock.
_CALL_LOCKS = [threading.Lock() for _ in range(64)]

FindDebtor = Callable[[str], Debtor | None]
Audit = Callable[[Decision, str, str | None], None]  # (decision, call_id, debt_id)


class _SeenIds:
    """The reply sent for each recent webhook id, oldest dropped first."""

    def __init__(self, limit: int = MAX_REMEMBERED_IDS) -> None:
        self.limit = limit
        self._replies: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, webhook_id: str) -> dict | None:
        with self._lock:
            return self._replies.get(webhook_id)

    def remember(self, webhook_id: str, reply: dict) -> None:
        with self._lock:
            self._replies[webhook_id] = reply
            while len(self._replies) > self.limit:
                self._replies.popitem(last=False)


def find_debtor_in_db(e164: str) -> Debtor | None:
    """The debtor behind a phone number, from the database."""
    from harness.db import find_debtor_by_phone

    return find_debtor_by_phone(e164)


def audit_to_db(decision: Decision, call_id: str, debt_id: str | None) -> None:
    """Write one blocked sentence to the audit log, committed at once."""
    from compliance.audit import record
    from harness.db import connect

    with connect() as conn:
        record(conn, decision, kind="utterance_blocked", call_id=call_id, debt_id=debt_id)


def create_app(
    *,
    provider: Provider | None = None,
    secret: str | None = None,
    agent_factory: Callable[[Session | None], Agent] | None = None,
    open_session: Callable[[str], Session] = PostgresSession.for_call,
    clock: Callable[[], float] = time.time,
    stages: bool = True,
    find_debtor: FindDebtor = find_debtor_in_db,
    audit: Audit = audit_to_db,
) -> FastAPI:
    """The webhook app.

    ``secret`` defaults to ``AGENTPHONE_WEBHOOK_SECRET`` (``.env`` is a fallback).
    Without one every request is refused, so a missing setting fails closed.
    ``provider`` is the model to use (the real one from the environment if not
    given), and ``agent_factory(session)`` replaces how each turn's ``Agent`` is
    built. ``open_session(call_id)`` returns the saved history for a call:
    Postgres by default, or e.g. ``partial(JsonlSession.for_call, folder)``.
    ``find_debtor(phone)`` and ``audit(decision, call_id, debt_id)`` read the
    debtor and record blocked sentences; both use the database unless replaced.
    ``stages=False`` turns the call stages and the sentence filter off.
    """
    if secret is None:
        _load_dotenv()
        secret = os.environ.get(SECRET_ENV) or None
    make_agent = agent_factory or (lambda session: Agent(provider=provider, session=session))
    turn = _Turn(make_agent, open_session, stages, find_debtor, audit)
    seen = _SeenIds()
    app = FastAPI(title="DebtAgentCall webhook")
    app.state.seen = seen

    @app.post(WEBHOOK_PATH)
    async def agentphone(request: Request) -> JSONResponse:
        raw = await request.body()  # the exact bytes that were signed
        problem = verify(raw, request.headers, secret, clock())
        if problem:
            return JSONResponse({"error": problem}, status_code=401)

        webhook_id = request.headers.get(ID_HEADER)
        if webhook_id and (earlier := seen.get(webhook_id)) is not None:
            return JSONResponse(earlier)

        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            return JSONResponse({"error": "body is not valid JSON"}, status_code=400)
        if not isinstance(envelope, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

        reply = await _handle(envelope, turn)
        if webhook_id:
            seen.remember(webhook_id, reply)
        return JSONResponse(reply)

    return app


@dataclass(frozen=True)
class _Turn:
    """What one caller turn needs, as ``create_app`` was set up."""

    make_agent: Callable[[Session | None], Agent]
    open_session: Callable[[str], Session]
    stages: bool
    find_debtor: FindDebtor
    audit: Audit


async def _handle(envelope: dict, turn: _Turn) -> dict:
    """The reply for one verified webhook."""
    event = envelope.get("event")
    data = envelope.get("data") or {}
    if event == "agent.message" and envelope.get("channel") == "voice":
        transcript = data.get("transcript") if isinstance(data, dict) else None
        if not isinstance(transcript, str) or not transcript.strip():
            return say(DIDNT_CATCH)
        # Sessions, the database and the model all block, so keep them off the event loop.
        if turn.stages:
            return await run_in_threadpool(_staged_turn, turn, data, transcript)
        return await run_in_threadpool(_bare_turn, turn, data, transcript)
    # agent.call_ended is accepted; storing the call comes later. Unknown events too.
    return {}


def _bare_turn(turn: _Turn, data: dict, transcript: str) -> dict:
    """Without stages: straight to the model. A turn with no callId has nothing to remember."""
    call_id = data.get("callId")
    session = turn.open_session(call_id) if isinstance(call_id, str) and call_id else None
    return say(turn.make_agent(session).run(transcript))


def _staged_turn(turn: _Turn, data: dict, transcript: str) -> dict:
    """One caller turn through the call stages and the sentence filter."""
    call_id = data.get("callId")
    outbound = data.get("direction", "outbound") == "outbound"
    phone = data.get("to") if outbound else data.get("from")
    if not (isinstance(call_id, str) and call_id and isinstance(phone, str) and phone):
        return say_and_hangup(CANT_CONTINUE)  # can't tell whose call this is
    debtor = turn.find_debtor(phone)
    if debtor is None:
        return say_and_hangup(CANT_CONTINUE)
    # One turn per call at a time, so two webhooks for the same call can't both
    # read the same state (e.g. two wrong answers each counted as the first try).
    with _CALL_LOCKS[zlib.crc32(call_id.encode()) % len(_CALL_LOCKS)]:
        return _locked_turn(turn, call_id, debtor, transcript)


def _locked_turn(turn: _Turn, call_id: str, debtor: Debtor, transcript: str) -> dict:
    session = turn.open_session(call_id)
    state = load_state(session) or CallState.new(call_id, debtor)
    if state.debt_id != debtor.debt_id:
        return say_and_hangup(CANT_CONTINUE)  # the saved call is about another debt

    state, fixed = next_stage(state, transcript, debtor)
    if fixed is not None:
        session.append({"role": "user", "content": transcript})
        session.append({"role": "assistant", "content": fixed})
        save_state(session, state)
        return say_and_hangup(fixed) if state.stage in ENDING else say(fixed)

    agent = turn.make_agent(session)
    agent.stage_prompt_path = instructions_path(state.stage)
    if state.stage != Stage.DISCUSS:
        # No account lookups and no skill playbooks before verify and disclosure.
        agent.tools = ToolRegistry()
        agent.skills = lambda: []  # type: ignore[method-assign]
    reply = agent.run(transcript)

    spoken, blocked = filter_reply(reply, speech_state(state, debtor))
    last = agent.messages[-1] if agent.messages else {}
    if spoken != reply and last.get("role") == "assistant" and not last.get("tool_calls"):
        # Keep the history to what the caller heard, so the model doesn't build on
        # a sentence that was never said. Done before the audit write, so a failed
        # write can't leave the blocked sentence in the history.
        agent.messages[-1] = {**last, "content": spoken}
        session.replace(agent.messages)
    for decision in blocked:
        turn.audit(decision, call_id, debtor.debt_id)
    save_state(session, state)
    return say(spoken)


def speech_state(state: CallState, debtor: Debtor) -> SpeechState:
    """What the sentence filter knows: how far the call got, and the amounts on file."""
    return SpeechState(
        verified=state.verified,
        disclosed=state.disclosed,
        known_amounts_cents={debtor.balance_cents},
        creditor=debtor.creditor,
    )


def filter_reply(text: str, speech: SpeechState) -> tuple[str, list[Decision]]:
    """The sentences that may be spoken, joined, and the decisions that blocked the rest."""
    kept: list[str] = []
    blocked: list[Decision] = []
    for sentence in split_sentences(text):
        decision = check_sentence(sentence, speech)
        if decision.allow:
            kept.append(sentence)
        else:
            blocked.append(decision)
    return " ".join(kept) or SAFE_LINE, blocked
