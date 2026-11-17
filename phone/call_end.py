"""What happens when a call ends: the attempt, the state, the audit row, the summary.

``agent.call_ended`` is sent once and never retried (AgentPhone docs), so
nothing here may depend on a second delivery. It carries ``callId``,
``startedAt``, ``endedAt``, ``durationSeconds``, ``disconnectionReason`` and the
whole ``transcript`` as ``[{role, content}]`` (roles ``user`` and ``agent``).
``uv run reconcile`` (``dialer/reconcile.py``) feeds the same function for calls
whose event never arrived.

``record_call_end`` does, in order:

1. Find the attempt by ``call_attempt.agentphone_call_id``, locked for update.
   No attempt (a call we did not place): audit it and stop; the webhook still
   answers 200. An attempt that already has ``ended_at``: nothing changes.
2. Read the call's saved state (if that fails, raise and change nothing; the
   reconciler picks it up) and ``derive`` the outcome from it and from what the
   caller said. ``connected`` and ``conversation_with_debtor`` feed the 7-in-7
   rule (a conversation starts a 7-day pause), so a conversation is recorded
   only with positive evidence: the caller spoke *and* the state says the person
   was verified. Anything unexplained becomes ``ended_unknown``, counted as no
   conversation, with a blocked audit row for a person to look at.
3. Update the attempt and write the audit row in one transaction.
4. Mark the saved state ``ended`` and close its stage (WRAP, unless it is
   already WRAP or STOPPED).
5. Store the call summary (once per call; a repeat that finds none makes it).
   A failure here is logged, never raised: the attempt is already right.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from compliance import Decision, allow, block
from compliance.audit import record
from harness import call_summary
from harness.call_state import CallState, Stage
from harness.memory import Session

log = logging.getLogger(__name__)

RULE_ENDED = "call_end"
RULE_UNKNOWN_OUTCOME = "call_end_unknown_outcome"
RULE_UNKNOWN_CALL = "call_end_unknown_call"
AUDIT_KIND = "call_ended"

# disconnectionReason values AgentPhone documents: only "agent_hangup" (in its example)
# and, from our simulator, "user_hangup". The rest is matched by words, because the
# full list is not published; an unmatched reason is never guessed at.
_VOICEMAIL = ("voicemail", "machine")
_BUSY = ("busy",)
_NO_ANSWER = ("no_answer", "no-answer", "not_answered", "ring_timeout", "unanswered")
_FAILED = ("fail", "error", "invalid", "rejected")


@dataclass(frozen=True)
class EndedCall:
    """One ended call, from the webhook or from a lookup."""

    call_id: str
    reason: str | None = None  # disconnectionReason
    ended_at: datetime | None = None
    caller_spoke: bool | None = None  # None: this source cannot say
    source: str = "webhook"  # or "reconciler"


@dataclass(frozen=True)
class Verdict:
    outcome: str
    connected: bool
    conversation: bool
    unknown: bool = False
    why: str = ""


@dataclass(frozen=True)
class EndResult:
    status: str  # recorded | duplicate | unknown_call | dry_run
    outcome: str | None = None
    connected: bool | None = None
    conversation: bool | None = None
    attempt_id: str | None = None


def parse_ended(envelope: dict) -> EndedCall | None:
    """The ``EndedCall`` in an ``agent.call_ended`` envelope, or None without a call id."""
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    call_id = data.get("callId")
    if not isinstance(call_id, str) or not call_id.strip():
        return None
    reason = data.get("disconnectionReason")
    transcript = data.get("transcript")
    spoke: bool | None = None
    if isinstance(transcript, list):
        spoke = any(
            isinstance(t, dict)
            and t.get("role") == "user"
            and isinstance(t.get("content"), str)
            and t["content"].strip()
            for t in transcript
        )
    return EndedCall(
        call_id=call_id,
        reason=reason if isinstance(reason, str) else None,
        ended_at=parse_time(data.get("endedAt")),
        caller_spoke=spoke,
        source="webhook",
    )


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _has(reason: str | None, words: tuple[str, ...]) -> bool:
    return reason is not None and any(w in reason.lower() for w in words)


def derive(state: CallState | None, ended: EndedCall) -> Verdict:
    """The outcome and the two 7-in-7 flags, from the saved state and what the caller did.

    Pure. A conversation needs both ``state.verified`` and evidence the caller spoke.
    """
    reason = ended.reason
    spoke = bool(ended.caller_spoke) or bool(state and state.turns > 0)
    if state is not None and state.stage == Stage.STOPPED:
        return Verdict("ended_by_stop", True, state.verified and spoke)
    if state is not None and state.verified:
        return Verdict("answered", True, spoke)
    if _has(reason, _VOICEMAIL):
        return Verdict("voicemail", False, False)
    if state is not None and state.turns > 0:
        if state.verify_attempts >= 2:
            return Verdict("refused", True, False)
        if state.stage in (Stage.THIRD_PARTY, Stage.WRAP):
            return Verdict("third_party", True, False)
        return Verdict("abandoned", True, False)
    if spoke:  # the caller spoke but no state was saved: unexplained, never a conversation
        return Verdict("ended_unknown", True, False, True, "caller spoke, no call state saved")
    if _has(reason, _BUSY):
        return Verdict("busy", False, False)
    if _has(reason, _FAILED):
        return Verdict("failed", False, False)
    if _has(reason, _NO_ANSWER) or reason in ("user_hangup", "user_declined"):
        return Verdict("no_answer", False, False)
    return Verdict("ended_unknown", False, False, True, f"unrecognised end ({reason!r})")


def decision_for(ended: EndedCall, v: Verdict, state: CallState | None) -> Decision:
    evidence = {
        "outcome": v.outcome,
        "connected": v.connected,
        "conversation_with_debtor": v.conversation,
        "reason": ended.reason,
        "source": ended.source,
        "stage": state.stage.value if state else None,
        "verified": bool(state and state.verified),
        "caller_spoke": ended.caller_spoke,
    }
    if v.unknown:
        return block(RULE_UNKNOWN_OUTCOME, f"{v.why}; recorded as no conversation", evidence)
    return allow(RULE_ENDED, f"call ended: {v.outcome}", evidence)


OpenSession = Callable[[str], Session]
ConnectDb = Callable[[], psycopg.Connection]

FIND_ATTEMPT_SQL = """
    select id::text, debt_id::text, ended_at from public.call_attempt
    where agentphone_call_id = %s for update
"""
UPDATE_ATTEMPT_SQL = """
    update public.call_attempt
    set outcome = %s, connected = %s, conversation_with_debtor = %s,
        ended_at = %s, end_source = %s
    where id = %s and ended_at is null
"""


def read_state(open_session: OpenSession, call_id: str) -> tuple[CallState | None, dict | None]:
    """The call's newest saved state (as an object and as saved), or (None, None)."""
    raw = open_session(call_id).last_state()
    return (CallState.from_dict(raw), raw) if raw else (None, None)


def mark_state_ended(open_session: OpenSession, call_id: str, raw: dict | None) -> None:
    """Save the state again with ``ended`` set and the stage closed. Keeps its other fields,
    including the record of the last webhook answered."""
    if raw is None or raw.get("ended") is True:
        return
    closed = raw.get("stage") in (Stage.STOPPED.value, Stage.WRAP.value)
    open_session(call_id).put_state(
        {**raw, "ended": True, "stage": raw["stage"] if closed else Stage.WRAP.value}
    )


def record_call_end(
    ended: EndedCall,
    *,
    connect: ConnectDb,
    open_session: OpenSession,
    narrator: call_summary.Narrator | None = None,
    dry_run: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EndResult:
    """Apply one end of call (see the module docstring). ``dry_run`` writes nothing."""
    with connect() as conn:
        row = conn.execute(FIND_ATTEMPT_SQL, (ended.call_id,)).fetchone()
        if row is None:
            if not dry_run:
                record(
                    conn,
                    allow(RULE_UNKNOWN_CALL, "call_ended for a call we did not place", {}),
                    kind=AUDIT_KIND,
                    call_id=ended.call_id,
                )
            return EndResult("unknown_call")
        attempt_id, debt_id, already = row
        if already is not None:
            if not dry_run:  # a missing summary (it failed the first time) is made now
                _ensure_summary(conn, ended.call_id, debt_id, open_session, narrator)
            return EndResult("duplicate", attempt_id=attempt_id)
        state, raw = read_state(open_session, ended.call_id)  # raises: nothing changes
        verdict = derive(state, ended)
        result = EndResult(
            "dry_run" if dry_run else "recorded",
            verdict.outcome,
            verdict.connected,
            verdict.conversation,
            attempt_id,
        )
        if dry_run:
            return result
        conn.execute(
            UPDATE_ATTEMPT_SQL,
            (
                verdict.outcome,
                verdict.connected,
                verdict.conversation,
                ended.ended_at or now(),
                ended.source,
                attempt_id,
            ),
        )
        record(
            conn,
            decision_for(ended, verdict, state),
            kind=AUDIT_KIND,
            call_id=ended.call_id,
            debt_id=debt_id,
        )
    # Committed. The rest is best effort: the attempt, which the 7-in-7 rule reads, is right.
    try:
        mark_state_ended(open_session, ended.call_id, raw)
    except Exception:
        log.exception("call %s: ended, but its saved state was not marked ended", ended.call_id)
    try:
        with connect() as conn:
            _ensure_summary(conn, ended.call_id, debt_id, open_session, narrator, state)
    except Exception:
        log.exception("call %s: ended, but its summary was not stored", ended.call_id)
    return result


def _ensure_summary(
    conn: psycopg.Connection,
    call_id: str,
    debt_id: str,
    open_session: OpenSession,
    narrator: call_summary.Narrator | None,
    state: CallState | None = None,
) -> bool:
    """Store this call's summary if it has none. True if one was stored.

    ``state`` is the one read before the stage was closed, so the facts say how far
    the call really got; without it the saved state is read."""
    if call_summary.has_summary(conn, call_id):
        return False
    outcome = conn.execute(
        "select outcome from public.call_attempt where agentphone_call_id = %s", (call_id,)
    ).fetchone()
    assert outcome is not None  # the caller found this attempt
    if state is None:
        state, _ = read_state(open_session, call_id)
    facts = call_summary.build_facts(conn, call_id, debt_id, str(outcome[0]), state)
    checked = call_summary.summarise(conn, facts, narrator)
    if checked.narrative is None and checked.problems not in (("not asked for",),):
        record(
            conn,
            block(
                "call_summary_narrative",
                "narrative rejected; only the facts were stored",
                {"problems": list(checked.problems)},
            ),
            kind="call_summary",
            call_id=call_id,
            debt_id=debt_id,
        )
    return call_summary.save_summary(conn, facts, checked.narrative)
