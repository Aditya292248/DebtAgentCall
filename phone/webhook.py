"""The webhook AgentPhone calls during a phone call.

AgentPhone does the speech: it turns what the caller says into text, POSTs it
here, and speaks whatever text we answer with. Each request goes through:

1. Read the raw body, before any JSON parsing, and check the signature and
   timestamp with ``phone.signing.verify``. Anything that fails gets a 401 and
   never reaches the model.
2. If this ``X-Webhook-ID`` was seen before, send back the same reply as the
   first time without calling the model again. Recent ids are kept in memory;
   on a staged call the id and its reply are also saved with the call state,
   so a retry after a restart, or one that waited for the turn in progress,
   gets the saved reply too. A retry of a turn that saved a tool's row and
   then died before replying is not run again: fixed line, hang up, error
   logged. This covers the newest turn of a call, in one server process.
3. Act on the event:
   - ``agent.message`` on the voice channel: one caller turn (below).
   - ``agent.call_ended``: recorded by ``phone/call_end.py`` (the attempt's outcome and the
     two 7-in-7 flags, the saved state marked ended, an audit row, the call summary),
     under the same per-call lock as a caller turn, then answered ``{}`` whatever
     happened: the event is never retried, so a failure is logged for a person and the
     reconciler (``uv run reconcile``) finds the attempt later. A call we did not place
     is audited and answered 200 too.
   - anything else: ``{}``.

One caller turn, with call stages on (the default):

1. Find who the call is for from the phone number we dialed (``data.to`` on an
   outbound call), and open the session for ``data.callId``. An unknown number,
   or a call with no id, gets a fixed goodbye: no model, no debt facts.
2. Run the stop triggers (``compliance.triggers.detect``) on what the caller
   said, before anything else sees it. If one fires ("stop calling", a
   dispute, a recording objection, a wrong number):
   save its flag (for "stop calling", also revoke the number's consent; for a
   dispute, add a ``dispute`` row and mark the debt ``disputed`` so the dialer
   stops) and an audit row
   (``kind="stop_trigger"``), all in one transaction; then move the call to
   STOPPED, speak the trigger's fixed close and hang up. The model is
   not called. Before the person is verified a dispute changes nothing on the
   debt and gets a neutral goodbye (it may be a stranger saying "this number
   isn't mine"); its ``dispute`` row says ``verified = false`` for a person to
   review, the same as ``log_dispute`` in ``harness/call_tools.py``. If saving fails,
   the close and hangup still happen and the failure is logged as an error
   for a person to fix: the call never goes on because a write failed.
3. Load the call's ``CallState`` and run ``harness.stages.next_stage``. Turns
   of the same call wait for each other, so they never see stale state.
4. If it gives a fixed line (greeting, identity check, disclosure, closes),
   speak it. The model is not called. The disclosure turn also reads the debt's
   row again (``read_debt``) and adds the purpose line from
   ``harness.stages.purpose_line``: the creditor and balance from that row and a
   payment ask; for a disputed, paused or closed debt, no amount and no ask; if
   the row can't be read, or doesn't match, no amount and the call ends.
5. Otherwise run the agent with ``system.md`` plus the stage's file from
   ``instructions/stages/``, and the call tools from ``harness/call_tools.py``
   (``phone/live_tools.py`` builds this turn's ``CallContext`` from the saved
   state). Every tool call passes the compliance gate, then ``PolicyApprover``.
   Until the person is verified and has heard the disclosure, the tools are not
   offered and there are no skills; a tool the model asks for anyway is
   blocked by the gate. Rows a tool saves are committed before the model hears
   the result. If a save, an audit write or the database fails, the model's
   reply is not spoken: a fixed line, hang up, and an error is logged.
   ``end_call`` hangs up after the reply. ``escalate_to_human`` saves a request
   for a person (worked as a callback) and, only if ``SUPERVISOR_NUMBER`` is
   set, transfers the call to the agent's transfer number; nothing more is said.
6. The reply is streamed (``application/x-ndjson``, see ``phone/replies.py``).
   The model's text is cut into sentences as it arrives, and each complete
   sentence is checked with ``compliance.check_sentence`` before it is sent,
   using the call state as it is at that moment (verified, disclosed, and the
   amounts of a plan approved so far this turn), plus the check that nothing
   is called approved or booked unless code saved it. A sentence that passes
   goes out at once as an ``interim`` line, so the caller hears the first one
   while the model is still writing. The first blocked sentence is never sent:
   it is written to the audit log (``kind="utterance_blocked"``), the model is
   stopped, and the turn ends with a fixed safe line (the pending line if a
   plan was only saved as pending). Tool calls mid-reply run as in step 5, so
   a row is saved before any later sentence can speak of it. The last line has
   no ``interim`` and carries ``hangup`` or ``action`` if the turn ends the
   call. The saved history keeps what was really said. A failed audit write is
   logged; the blocked sentence is still not spoken. If a tool fails, nothing
   more the model wrote is sent: the fixed line, then hang up.
7. Save the new ``CallState`` in the session.

If no line is ready within ``HOLD_AFTER`` seconds (a slow model, database or
tool), the stream opens with the fixed ``HOLD_LINE`` and the turn carries on.
A turn whose whole reply is ready before anything was sent (fixed lines, stop
triggers, a reply blocked outright) is one plain JSON reply.

``create_app(stages=False)`` is the bare webhook from before: every turn goes
straight to the model, unchecked. Only the older tests use it.

Each turn builds a new ``Agent``, but on that call's saved session, so it sees
every earlier turn of the same call and nothing from any other call. Sessions
are kept in Postgres (``call_message``) unless ``open_session`` says otherwise.

Run it with ``uv run serve``. Tests build their own app with ``create_app``,
passing a fake model, a test secret, JSONL sessions in a temp folder, and their
own debtor lookup, audit log and stop recorder.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from compliance import Decision, Flag, SpeechState, block, check_sentence, split_sentences
from compliance.triggers import Trigger, detect
from harness import Agent
from harness.call_state import CallState, Debtor, DebtRow, Stage, load_state
from harness.call_summary import LoadEarlier, context_block, earlier_from_db, model_narrator
from harness.memory import PostgresSession, Session
from harness.stages import ENDING, STOPPED_CLOSE, instructions_path, next_stage, purpose_line
from model import Provider
from model.provider import _load_dotenv
from phone import timing
from phone.call_end import EndedCall, EndResult, parse_ended, record_call_end
from phone.live_tools import (
    COMMITMENT_RULE,
    PENDING_LINE,
    LiveTools,
    OpenBackend,
    check_commitment,
    open_backend_in_db,
)
from phone.replies import (
    NDJSON,
    SentenceBuffer,
    interim,
    ndjson_line,
    say,
    say_and_hangup,
    transfer,
)
from phone.signing import ID_HEADER, verify

WEBHOOK_PATH = "/webhooks/agentphone"
SECRET_ENV = "AGENTPHONE_WEBHOOK_SECRET"
SUPERVISOR_ENV = "SUPERVISOR_NUMBER"  # set: escalate_to_human transfers the call
# In each saved state record: the webhook that turn answered and the reply sent,
# so a retry is answered from here instead of running the turn again.
REPLAY_KEY = "webhook"
DIDNT_CATCH = "I’m here. Go ahead when you’re ready."
SAFE_LINE = "I can’t confirm that detail. Would you like to speak with a specialist?"
CANT_CONTINUE = "Sorry, I can't continue this call right now. Goodbye."
# Sent first if nothing safe is ready to say by HOLD_AFTER seconds into a turn.
HOLD_LINE = "One moment while I check that."
HOLD_AFTER = 2.5
STOP_SOURCE = "live_call"  # flags.source for a flag a stop trigger set
# Said instead of the dispute close before verification: promises nothing and
# doesn't hint that there is a debt.
UNVERIFIED_DISPUTE_CLOSE = "Thank you for letting me know. Goodbye."
MAX_REMEMBERED_IDS = 10_000  # enough to spot repeats, without growing forever
# Turns of the same call run one at a time. A fixed set of locks, picked by call
# id, so memory doesn't grow with calls; two calls rarely share one. This only
# covers one server process; several servers would need a database lock.
_CALL_LOCKS = [threading.Lock() for _ in range(64)]

FindDebtor = Callable[[str], Debtor | None]
CallEnded = Callable[[EndedCall], EndResult]
ReadDebt = Callable[[Debtor], DebtRow | None]  # the debt's row as stored now
Audit = Callable[[Decision, str, str | None], None]  # (decision, call_id, debt_id)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Stop:
    """A stop trigger that fired on a live call, and what to write down for it."""

    trigger: Trigger
    call_id: str
    debtor: Debtor
    phone: str  # the number we called
    stage: Stage | None  # where the call was; None if its state couldn't be read
    verified: bool = False  # whether the person had passed the identity check

    @property
    def marks_disputed(self) -> bool:
        """Only the verified debtor can dispute the debt; anyone else changes nothing."""
        return self.trigger.kind == "dispute" and self.verified

    @property
    def close(self) -> str:
        """The fixed line to say before hanging up."""
        if self.trigger.kind == "dispute" and not self.verified:
            return UNVERIFIED_DISPUTE_CLOSE
        return self.trigger.close

    def flag(self) -> Flag | None:
        """The flag to save, or None for a trigger that sets no flag."""
        kind = self.trigger.flag
        if kind is None:
            return None
        return Flag(
            kind=kind,
            debt_id=None,  # a stop request is about the person: it covers every debt
            data={"call_id": self.call_id, "matched": self.trigger.matched},
            source=STOP_SOURCE,
        )

    def decision(self, debt_disputed: bool | None = None) -> Decision:
        """The audit row: the call was stopped, and why.

        ``debt_disputed`` says whether the debt's status really moved to
        ``disputed`` (False if it wasn't active); None when nothing was tried.
        """
        t = self.trigger
        return block(
            t.kind,
            f"caller said {t.matched!r}, so the call was stopped",
            {
                "matched": t.matched,
                "flag": t.flag,
                "escalate": t.escalate,
                "stage": self.stage.value if self.stage else None,
                "verified": self.verified,
                "debt_disputed": debt_disputed,
            },
        )


RecordStop = Callable[[Stop], None]


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


def read_debt_in_db(debtor: Debtor) -> DebtRow | None:
    """The call's debt, read again from ``public.debt``."""
    from harness.db import read_debt_row

    return read_debt_row(debtor.debt_id)


def debt_as_found(debtor: Debtor) -> DebtRow:
    """The debt as ``find_debtor`` gave it, for tests with no database (status active)."""
    return DebtRow(debtor.creditor, debtor.balance_cents, "active")


def audit_to_db(decision: Decision, call_id: str, debt_id: str | None) -> None:
    """Write one blocked sentence to the audit log, committed at once."""
    from compliance.audit import record
    from harness.db import connect

    with connect() as conn:
        record(conn, decision, kind="utterance_blocked", call_id=call_id, debt_id=debt_id)


def record_stop_in_db(stop: Stop) -> None:
    """Save a stop trigger's flag, consent or debt status change and audit row together."""
    from compliance.audit import record
    from harness.db import connect, mark_disputed, revoke_phone, save_dispute, save_flag

    with connect() as conn:  # commits on a clean exit, rolls back on an error
        if (flag := stop.flag()) is not None:
            save_flag(conn, flag, stop.debtor.debtor_id)
        if stop.trigger.kind == "cease":
            revoke_phone(conn, stop.debtor.debtor_id, stop.phone)
        disputed = None
        if stop.trigger.kind == "dispute":
            # Kept either way, like log_dispute: a person reviews an unverified one.
            reason = f"caller said {stop.trigger.matched!r}"
            save_dispute(conn, stop.call_id, stop.debtor.debt_id, reason, stop.verified)
        if stop.marks_disputed:
            # The close promises a follow-up; until then the dialer must not call.
            disputed = mark_disputed(conn, stop.debtor.debt_id)
        record(
            conn,
            stop.decision(disputed),
            kind="stop_trigger",
            call_id=stop.call_id,
            debt_id=stop.debtor.debt_id,
        )


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
    record_stop: RecordStop = record_stop_in_db,
    open_backend: OpenBackend = open_backend_in_db,
    supervisor_number: str | None = None,
    read_debt: ReadDebt = read_debt_in_db,
    hold_after: float = HOLD_AFTER,
    on_call_ended: CallEnded | None = None,
    load_earlier: LoadEarlier = earlier_from_db,
    record_timing: timing.RecordTiming | None = None,
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
    ``record_stop(stop)`` saves what a stop trigger found (database by default).
    ``open_backend(debtor)`` gives the call tools their store, audit writer,
    flags and plan policy (database by default, opened on the first tool call).
    ``read_debt(debtor)`` reads the debt's row for the line after the disclosure
    (database by default; ``debt_as_found`` for tests without one).
    ``supervisor_number`` defaults to ``SUPERVISOR_NUMBER``; without one,
    ``escalate_to_human`` never transfers.
    ``hold_after`` is how long a turn may stay silent before ``HOLD_LINE``.
    ``on_call_ended(ended)`` records a finished call (database, and ``provider`` for the
    summary's narrative, by default). ``load_earlier(debt_id, call_id)`` reads the
    earlier calls' summaries; they reach the model only once the person is verified.
    ``record_timing(row)`` stores each staged turn's timing (``phone/timing.py``) after
    the reply has been sent; None (the default) turns timing off. ``uv run serve``
    passes the database writer. A failed write is logged and changes nothing.
    ``stages=False`` turns the call stages and the sentence filter off.
    """
    if secret is None:
        _load_dotenv()
        secret = os.environ.get(SECRET_ENV) or None
    if supervisor_number is None:
        _load_dotenv()
        supervisor_number = os.environ.get(SUPERVISOR_ENV, "")
    make_agent = agent_factory or (lambda session: Agent(provider=provider, session=session))
    if on_call_ended is None:
        on_call_ended = _record_in_db(open_session, provider)
    turn = _Turn(
        make_agent,
        open_session,
        stages,
        find_debtor,
        audit,
        record_stop,
        open_backend,
        bool(supervisor_number.strip()),
        read_debt,
        hold_after,
        on_call_ended,
        load_earlier,
    )
    seen = _SeenIds()
    app = FastAPI(title="DebtAgentCall webhook")
    app.state.seen = seen

    @app.post(WEBHOOK_PATH)
    async def agentphone(request: Request) -> Response:
        timer = timing.start(record_timing)
        raw = await request.body()  # the exact bytes that were signed
        problem = verify(raw, request.headers, secret, clock())
        if problem:
            return JSONResponse({"error": problem}, status_code=401)
        if timer is not None:
            timer.mark_verified()

        webhook_id = request.headers.get(ID_HEADER)
        if webhook_id and (earlier := seen.get(webhook_id)) is not None:
            return JSONResponse(earlier)

        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            return JSONResponse({"error": "body is not valid JSON"}, status_code=400)
        if not isinstance(envelope, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

        reply = await _handle(envelope, turn, webhook_id)
        if isinstance(reply, _Live):
            return await reply.respond(seen, webhook_id, timer)
        if webhook_id:
            seen.remember(webhook_id, reply)
        return _json(reply, timer)

    return app


def create_served_app() -> FastAPI:
    """The app ``uv run serve`` runs: ``create_app`` with each turn's timing saved."""
    return create_app(record_timing=timing.record_in_db)


def _json(body: dict, timer: timing.TurnTimer | None) -> JSONResponse:
    """A plain reply; the turn's timing is written after it has been sent."""
    if timer is None:
        return JSONResponse(body)
    timer.first_line()
    timer.speech(body.get("text", ""))
    timer.finish()
    return JSONResponse(body, background=BackgroundTask(timer.write))


@dataclass(frozen=True)
class _Turn:
    """What one caller turn needs, as ``create_app`` was set up."""

    make_agent: Callable[[Session | None], Agent]
    open_session: Callable[[str], Session]
    stages: bool
    find_debtor: FindDebtor
    audit: Audit
    record_stop: RecordStop
    open_backend: OpenBackend
    can_transfer: bool  # a supervisor number is configured
    read_debt: ReadDebt
    hold_after: float = HOLD_AFTER  # seconds of silence before HOLD_LINE
    on_call_ended: CallEnded | None = None
    load_earlier: LoadEarlier = earlier_from_db


@dataclass(frozen=True)
class _Said:
    """How a staged turn ended: the last line to send, and the whole reply.

    ``last`` closes the stream. ``whole`` is everything said this turn as one
    plain reply: saved for a retry, and sent as is if nothing was streamed.
    """

    last: dict
    whole: dict


Emit = Callable[[str], None]  # sends one checked sentence as an interim line


class _Live:
    """A staged turn running in a worker thread, streamed as it speaks."""

    def __init__(self, hold_after: float) -> None:
        self.hold_after = hold_after
        self._loop = asyncio.get_running_loop()
        self._events: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        self._task: asyncio.Future[None] | None = None

    def emit(self, text: str) -> None:
        """Called from the worker thread for each sentence that passed the checks."""
        self._loop.call_soon_threadsafe(self._events.put_nowait, ("say", text))

    def start(self, work: Callable[[Emit], _Said]) -> _Live:
        async def run() -> None:
            try:
                said = await run_in_threadpool(work, self.emit)
            except Exception as exc:  # noqa: BLE001 - handed to respond() below
                self._events.put_nowait(("error", exc))
            else:
                self._events.put_nowait(("done", said))

        # Kept on self so it isn't collected mid-turn; it runs on even if the
        # caller hangs up, so the state is still saved.
        self._task = asyncio.ensure_future(run())
        return self

    async def respond(
        self, seen: _SeenIds, webhook_id: str | None, timer: timing.TurnTimer | None = None
    ) -> Response:
        """One JSON reply if the turn finished before anything was sent, else NDJSON."""
        first: tuple[str, object] | None
        try:
            first = await asyncio.wait_for(self._events.get(), self.hold_after)
        except TimeoutError:
            first = None
        if first is not None and isinstance(first[1], _Said):
            if webhook_id:
                seen.remember(webhook_id, first[1].whole)
            return _json(first[1].whole, timer)
        if first is not None and isinstance(first[1], Exception):
            raise first[1]  # nothing was sent yet: a 500, as before streaming
        lead = HOLD_LINE if first is None else str(first[1])
        if timer is not None:
            timer.hold, timer.streamed = first is None, True
        return StreamingResponse(
            self._lines(lead, seen, webhook_id, timer),
            media_type=NDJSON,
            background=None if timer is None else BackgroundTask(timer.write),
        )

    async def _lines(
        self,
        lead: str,
        seen: _SeenIds,
        webhook_id: str | None,
        timer: timing.TurnTimer | None = None,
    ) -> AsyncIterator[bytes]:
        if timer is not None:
            timer.first_line()
            if not timer.hold:
                timer.speech(lead)
        yield ndjson_line(interim(lead))
        while True:
            _, value = await self._events.get()
            if isinstance(value, _Said):
                if webhook_id:
                    seen.remember(webhook_id, value.whole)
                if timer is not None:
                    timer.speech(value.last.get("text", ""))
                    timer.finish()
                yield ndjson_line(value.last)
                return
            if isinstance(value, Exception):
                # Lines were already spoken, so a 500 is too late: close the turn safely.
                log.error("a turn failed after it started speaking", exc_info=value)
                if timer is not None:
                    timer.speech(SAFE_LINE)
                    timer.kind = "failure"
                    timer.finish()
                yield ndjson_line(say(SAFE_LINE))
                return
            if timer is not None:
                timer.speech(str(value))
            yield ndjson_line(interim(str(value)))


async def _handle(envelope: dict, turn: _Turn, webhook_id: str | None = None) -> dict | _Live:
    """The reply for one verified webhook."""
    event = envelope.get("event")
    data = envelope.get("data") or {}
    if event == "agent.message" and envelope.get("channel") == "voice":
        transcript = data.get("transcript") if isinstance(data, dict) else None
        if not isinstance(transcript, str) or not transcript.strip():
            return say(DIDNT_CATCH)
        # Sessions, the database and the model all block, so keep them off the event loop.
        if turn.stages:
            live = _Live(turn.hold_after)
            return live.start(lambda emit: _staged_turn(turn, data, transcript, webhook_id, emit))
        return await run_in_threadpool(_bare_turn, turn, data, transcript)
    if event == "agent.call_ended" and turn.on_call_ended is not None:
        await run_in_threadpool(_call_ended, turn, envelope)
    return {}  # a call_ended we could not read, and any other event, is just acknowledged


def _record_in_db(open_session: Callable[[str], Session], provider: Provider | None) -> CallEnded:
    """The default ``on_call_ended``: the real database, and a model for the narrative."""
    from harness.db import connect

    narrator = model_narrator(provider)
    return lambda ended: record_call_end(
        ended, connect=connect, open_session=open_session, narrator=narrator
    )


def _call_ended(turn: _Turn, envelope: dict) -> None:
    """Record a finished call. Never raises: the event is not retried, so a failure is
    logged for a person to fix and the reconciler picks the attempt up later."""
    ended = parse_ended(envelope)
    if ended is None or turn.on_call_ended is None:
        log.error("agent.call_ended without a call id was ignored")
        return
    try:
        with _CALL_LOCKS[zlib.crc32(ended.call_id.encode()) % len(_CALL_LOCKS)]:
            turn.on_call_ended(ended)
    except Exception:
        log.exception("call %s: could not record the end of the call", ended.call_id)


def _bare_turn(turn: _Turn, data: dict, transcript: str) -> dict:
    """Without stages: straight to the model. A turn with no callId has nothing to remember."""
    call_id = data.get("callId")
    session = turn.open_session(call_id) if isinstance(call_id, str) and call_id else None
    return say(turn.make_agent(session).run(transcript))


def _staged_turn(
    turn: _Turn,
    data: dict,
    transcript: str,
    webhook_id: str | None = None,
    emit: Emit = lambda text: None,
) -> _Said:
    """One caller turn through the call stages and the sentence filter."""
    said = _staged_reply(turn, data, transcript, webhook_id, emit)
    return said if isinstance(said, _Said) else _Said(said, said)


def _staged_reply(
    turn: _Turn, data: dict, transcript: str, webhook_id: str | None, emit: Emit
) -> dict | _Said:
    call_id = data.get("callId")
    outbound = data.get("direction", "outbound") == "outbound"
    phone = data.get("to") if outbound else data.get("from")
    if not (isinstance(call_id, str) and call_id and isinstance(phone, str) and phone):
        return say_and_hangup(CANT_CONTINUE)  # can't tell whose call this is
    timing.set_turn(call_id=call_id)
    debtor = turn.find_debtor(phone)
    if debtor is None:
        return say_and_hangup(CANT_CONTINUE)
    # One turn per call at a time, so two webhooks for the same call can't both
    # read the same state (e.g. two wrong answers each counted as the first try).
    with _CALL_LOCKS[zlib.crc32(call_id.encode()) % len(_CALL_LOCKS)]:
        # A webhook we already answered (a retry, maybe after a restart) gets the
        # same reply again: no model, no tools, nothing saved twice.
        with timing.span():
            hits = detect(transcript)
        try:
            session = turn.open_session(call_id)
            data = session.last_state() or {}
        except Exception:
            if hits:
                return _stop_turn(turn, call_id, debtor, phone, transcript, hits, webhook_id)
            raise
        if webhook_id and (earlier := _earlier_reply(data, call_id, webhook_id)) is not None:
            timing.set_kind("replay")
            return earlier
        if hits:
            return _stop_turn(turn, call_id, debtor, phone, transcript, hits, webhook_id)
        state = CallState.from_dict(data) if data else CallState.new(call_id, debtor)
        return _locked_turn(
            turn, session, state, call_id, debtor, phone, transcript, webhook_id, emit
        )


def _earlier_reply(data: dict, call_id: str, webhook_id: str) -> dict | None:
    """The saved reply if this webhook was handled before, else None.

    If an earlier try of it saved a tool's row and then died before replying,
    there is no reply to repeat and running it again could save the row twice:
    fixed line and hang up, for a person to check.
    """
    seen = data.get(REPLAY_KEY)
    if not isinstance(seen, dict) or seen.get("id") != webhook_id:
        return None
    if isinstance(seen.get("reply"), dict):
        return seen["reply"]
    log.error(
        "call %s: webhook %s came again after a try that saved a tool row and never "
        "replied; not running it twice, check this call by hand",
        call_id,
        webhook_id,
    )
    return say_and_hangup(CANT_CONTINUE)


def _save(session: Session, state: CallState, webhook_id: str | None, reply: dict | None) -> None:
    """Save the state, with the webhook it answered and the reply (None: not replied yet)."""
    data = state.to_dict()
    if webhook_id:
        data[REPLAY_KEY] = {"id": webhook_id, "reply": reply}
    session.put_state(data)


def _stop_turn(
    turn: _Turn,
    call_id: str,
    debtor: Debtor,
    phone: str,
    transcript: str,
    hits: list[Trigger],
    webhook_id: str | None = None,
) -> dict:
    """Save the stop, then the fixed close and hang up, whatever fails on the way."""
    session: Session | None = None
    state: CallState | None = None
    try:
        session = turn.open_session(call_id)
        state = load_state(session) or CallState.new(call_id, debtor)
    except Exception:
        log.exception("call %s: could not read the call state for a stop trigger", call_id)
    verified = state is not None and state.verified  # unreadable state: not verified
    timing.set_kind("stop")
    if state is not None:
        timing.set_turn(seq=state.turns + 1, stage=state.stage.value)
    trigger = _pick(hits, verified)
    stop = Stop(trigger, call_id, debtor, phone, state.stage if state else None, verified)
    try:
        turn.record_stop(stop)
    except Exception:
        # Fail closed: the call still ends. A person has to save this by hand.
        log.exception(
            "call %s: stop trigger %r (flag %s) for debt %s was NOT saved; record it by hand",
            call_id,
            trigger.kind,
            trigger.flag,
            debtor.debt_id,
        )
    reply = say_and_hangup(stop.close)
    if session is not None and state is not None:
        try:
            session.append({"role": "user", "content": transcript})
            session.append({"role": "assistant", "content": stop.close})
            stopped = replace(state, stage=Stage.STOPPED, turns=state.turns + 1)
            _save(session, stopped, webhook_id, reply)
        except Exception:
            log.exception("call %s: could not save the STOPPED state", call_id)
    return reply


def _pick(hits: list[Trigger], verified: bool) -> Trigger:
    """The trigger to act on. Before verification a dispute gives way to any other
    hit ("that phone wasn't mine, wrong number" is a wrong number), since a
    dispute only means something from the debtor."""
    if not verified:
        others = [t for t in hits if t.kind != "dispute"]
        if others:
            return others[0]
    return hits[0]


def _locked_turn(
    turn: _Turn,
    session: Session,
    state: CallState,
    call_id: str,
    debtor: Debtor,
    phone: str,
    transcript: str,
    webhook_id: str | None = None,
    emit: Emit = lambda text: None,
) -> dict | _Said:
    if state.debt_id != debtor.debt_id:
        return say_and_hangup(CANT_CONTINUE)  # the saved call is about another debt

    disclosed_before = state.disclosed
    with timing.span():
        state, fixed = next_stage(state, transcript, debtor)
    timing.set_turn(seq=state.turns, stage=state.stage.value)
    if fixed is not None and state.disclosed and not disclosed_before:
        state, fixed = _add_purpose(turn, state, fixed, call_id, debtor)
    if fixed is not None:
        session.append({"role": "user", "content": transcript})
        session.append({"role": "assistant", "content": fixed})
        said = say_and_hangup(fixed) if state.stage in ENDING else say(fixed)
        _save(session, state, webhook_id, said)
        return said

    agent = turn.make_agent(session)
    agent.stage_prompt_path = instructions_path(state.stage)
    agent.extra_context = _earlier_calls(turn, state, call_id)
    if state.stage == Stage.VERIFY:
        missing = [
            name
            for name, value in (("date of birth", state.dob_ok), ("ZIP code", state.zip_ok))
            if value is None
        ]
        agent.extra_context = (
            f"Identity fields still needed: {', '.join(missing)}. "
            f"Turns since the last usable identity detail: {state.verification_prompts}."
        )
    elif state.stage == Stage.DISCUSS and state.requested_months is not None:
        agent.extra_context += (
            f"\nLast explicit monthly term recorded: {state.requested_months} months."
        )

    # The call tools and nothing else: no subagents, no fan-out, no lookup by name.
    # Built from the saved state, so only the server can say who is verified.
    # Before a tool's first row is saved, mark this webhook as started, so a retry
    # after a crash can't save that row again.
    started = state

    def mark_started() -> None:
        _save(session, started, webhook_id, None)

    live = LiveTools(debtor, state, phone, turn.open_backend, before_first_save=mark_started)
    agent.tools = live.registry(offered=state.stage == Stage.DISCUSS)
    agent.on_tool_outcome = live.on_outcome
    timing.attach(agent)
    if state.stage != Stage.DISCUSS:
        # No tools offered and no skill playbooks before verify and disclosure.
        agent.skills = lambda: []  # type: ignore[method-assign]
    return _model_turn(turn, session, agent, live, transcript, call_id, debtor, webhook_id, emit)


def _earlier_calls(turn: _Turn, state: CallState, call_id: str) -> str:
    """Summaries of earlier calls about this debt, only for a verified person."""
    try:
        return context_block(state, turn.load_earlier)
    except Exception:
        log.exception("call %s: could not load earlier call summaries", call_id)
        return ""  # the call goes on without them


def _model_turn(
    turn: _Turn,
    session: Session,
    agent: Agent,
    live: LiveTools,
    transcript: str,
    call_id: str,
    debtor: Debtor,
    webhook_id: str | None,
    emit: Emit,
) -> dict | _Said:
    """Stream the model's reply, one checked sentence at a time (step 6 above)."""
    spoken: list[str] = []  # every sentence sent this turn, in order
    step: list[str] = []  # those from the model call being read now
    blocked: Decision | None = None
    buffer = SentenceBuffer()

    def stop_speaking() -> bool:
        # A failed tool, or a transfer: nothing more the model writes is said.
        return live.failure is not None or (live.escalated and turn.can_transfer)

    stream = agent.run_stream(transcript)
    try:
        for piece in stream:
            sentences = buffer.flush() if piece is None else buffer.feed(piece)
            for sentence in sentences:
                if stop_speaking():
                    break
                # Checked against the state as it is now: a tool may have changed it.
                speech = speech_state(live.state, debtor, live.approved_amounts())
                decision = check_utterance(sentence, speech, live.backs_commitments())
                if not decision.allow:
                    blocked = decision
                    break
                spoken.append(sentence)
                step.append(sentence)
                emit(sentence)
            if blocked is not None or stop_speaking():
                break  # closing the stream stops the model; no tool runs after this
            if piece is None:
                step = []  # that model call is over and recorded as it was said
    finally:
        stream.close()
        live.finish()
    if live.failure is not None:
        return _tools_failed(session, agent, live, call_id, debtor, webhook_id)
    state = live.state  # end_call may have moved it to WRAP

    fallback = ""
    if blocked is not None:
        # If code saved the plan as pending, that is what the caller must hear.
        pending = live.pending_only() and blocked.rule_id == COMMITMENT_RULE
        fallback = PENDING_LINE if pending else SAFE_LINE
    elif not spoken:
        fallback = SAFE_LINE
    if blocked is not None:
        timing.set_kind("blocked")
    if live.ended and fallback == SAFE_LINE:
        fallback = STOPPED_CLOSE
    heard = " ".join(x for x in [*step, fallback] if x)
    last = agent.messages[-1] if agent.messages else {}
    written = " ".join(split_sentences(str(last.get("content") or "")))
    if last.get("role") == "assistant" and not last.get("tool_calls") and heard != written:
        # Keep the history to what the caller heard, so the model doesn't build on
        # a sentence that was never said. Done before the audit write, so a failed
        # write can't leave the blocked sentence in the history.
        agent.messages[-1] = {**last, "content": heard}
        session.replace(agent.messages)
    if blocked is not None:
        try:
            turn.audit(blocked, call_id, debtor.debt_id)
        except Exception:
            # The sentence was never sent; only the record of it is missing.
            log.exception("call %s: blocked sentence (%s) not audited", call_id, blocked.rule_id)
    if live.escalated and turn.can_transfer:
        said = _Said(transfer(), transfer())
    else:
        ending = {"hangup": True} if live.ended else {}
        everything = " ".join(x for x in [*spoken, fallback] if x)
        said = _Said({"text": fallback, **ending}, {"text": everything, **ending})
    _save(session, state, webhook_id, said.whole)
    return said


def _add_purpose(
    turn: _Turn, state: CallState, disclosure: str, call_id: str, debtor: Debtor
) -> tuple[CallState, str]:
    """The disclosure plus why we called, built from the debt's row, never the model."""
    try:
        row = turn.read_debt(debtor)
    except Exception:
        log.exception(
            "call %s: could not read debt %s for the purpose line", call_id, debtor.debt_id
        )
        row = None
    line, ends = purpose_line(debtor, row)
    if ends:
        log.error(
            "call %s: debt %s unreadable or changed; ended after the disclosure",
            call_id,
            debtor.debt_id,
        )
        state = replace(state, stage=Stage.WRAP)
    state = replace(
        state,
        plan_discussion=bool(row and not ends and row.status == "active" and row.balance_cents > 0),
    )
    return state, f"{disclosure} {line}"


def _tools_failed(
    session: Session,
    agent: Agent,
    live: LiveTools,
    call_id: str,
    debtor: Debtor,
    webhook_id: str | None = None,
) -> dict:
    """A tool's save, audit or database failed: say nothing the model wrote, hang up."""
    log.error(
        "call %s: call tool failure for debt %s (%s); the model's reply was not spoken",
        call_id,
        debtor.debt_id,
        live.failure,
    )
    last = agent.messages[-1] if agent.messages else {}
    if last.get("role") == "assistant" and not last.get("tool_calls"):
        agent.messages[-1] = {**last, "content": CANT_CONTINUE}
        session.replace(agent.messages)
    timing.set_kind("failure")
    state = live.state
    if state.stage != Stage.STOPPED:
        state = replace(state, stage=Stage.WRAP)
    reply = say_and_hangup(CANT_CONTINUE)
    _save(session, state, webhook_id, reply)
    return reply


def speech_state(
    state: CallState, debtor: Debtor, extra_amounts: set[int] | None = None
) -> SpeechState:
    """What the sentence filter knows: how far the call got, and the amounts on file
    (the balance, plus any ``extra_amounts`` code approved on this turn)."""
    return SpeechState(
        verified=state.verified,
        disclosed=state.disclosed,
        known_amounts_cents={debtor.balance_cents, *(extra_amounts or ())},
        creditor=debtor.creditor,
    )


def filter_reply(
    text: str, speech: SpeechState, commitments_backed: bool | None = None
) -> tuple[str, list[Decision]]:
    """The sentences that may be spoken, joined, and the decisions that blocked the rest.

    With ``commitments_backed`` given, a sentence saying something was approved
    or booked is also dropped unless it is True (code saved one this turn).
    """
    kept: list[str] = []
    blocked: list[Decision] = []
    for sentence in split_sentences(text):
        decision = check_utterance(sentence, speech, commitments_backed)
        if decision.allow:
            kept.append(sentence)
        else:
            blocked.append(decision)
    return " ".join(kept) or SAFE_LINE, blocked


def check_utterance(
    sentence: str, speech: SpeechState, commitments_backed: bool | None = None
) -> Decision:
    """The sentence filter, then (with ``commitments_backed`` given) the check that
    nothing is called approved or booked unless code saved it this turn."""
    decision = check_sentence(sentence, speech)
    if decision.allow and commitments_backed is not None:
        decision = check_commitment(sentence, commitments_backed)
    return decision
