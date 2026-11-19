"""A read-only view of one call, for the web UI's live call panel.

``GET /api/calls`` lists the most recent calls (id, debtor name and account,
when it was placed, its stage, outcome and whether it has ended).
``GET /api/calls/{call_id}`` gives one call: the conversation (the caller's
words as transcribed and what the agent said, from ``call_message``), the saved
call state (stage, verified, disclosed), the tools the agent ran with their
arguments and results, any payment plan still waiting for a person, the
compliance events written for this call (``audit_event``) and, once the call has
ended, its recorded outcome and summary (``call_attempt``, ``call_summary``).

It only reads. The connection is read-only, there is no write route, and nothing
here can dial, approve or change anything: a POST, PUT, PATCH or DELETE gets a 405.

What it never returns: a phone number (the debtor's number is not read at all,
and any long digit run in saved text is masked to its last four digits), a key,
token or password (those keys and token-shaped strings are replaced), or another
call's rows (every query is filtered by the one call id). Sizes are capped.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException

from harness.db import connect

router = APIRouter(prefix="/api/calls", tags=["calls"])

MAX_CALLS = 20
MAX_MESSAGES = 500  # newest kept
MAX_AUDIT = 200  # newest kept
MAX_TEXT = 2000  # characters of any one saved text
MAX_EVIDENCE_TEXT = 300

_CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

_DIGIT_RUN = re.compile(r"[+(]?\d[\d\s().-]{8,}\d")
_TOKEN_SHAPES = re.compile(
    r"\b(?:sk|pk|rk|whsec|ap|key|tok|token)[-_][A-Za-z0-9_-]{8,}"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"
    r"|\bBearer\s+\S+",
    re.IGNORECASE,
)
_SECRET_KEY = re.compile(r"secret|token|password|passwd|api[_-]?key|authorization|credential", re.I)
_PHONE_KEY = re.compile(r"phone|e164|number|msisdn|caller_id|from_number|to_number", re.I)


def _mask_digits(match: re.Match[str]) -> str:
    text = match.group(0)
    digits = re.sub(r"\D", "", text)
    if len(digits) < 10:  # an amount, an account number, a date: not a phone number
        return text
    return f"***-{digits[-4:]}"


def scrub_text(text: str, limit: int = MAX_TEXT) -> str:
    """``text`` with phone-number-sized digit runs masked and token-shaped strings hidden."""
    out = _TOKEN_SHAPES.sub("[hidden]", _DIGIT_RUN.sub(_mask_digits, text))
    return out if len(out) <= limit else out[:limit] + "..."


def scrub_value(value: Any, limit: int = MAX_EVIDENCE_TEXT) -> Any:
    """``scrub_text`` through a JSON value; secret and phone keys lose their values."""
    if isinstance(value, str):
        return scrub_text(value, limit)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                out[str(key)] = "[hidden]"
            elif _PHONE_KEY.search(str(key)) and isinstance(item, str):
                out[str(key)] = scrub_text(item, limit)
            else:
                out[str(key)] = scrub_value(item, limit)
        return out
    if isinstance(value, list):
        return [scrub_value(item, limit) for item in value[:50]]
    return value


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _stage(state: dict | None) -> str | None:
    return str(state["stage"]).lower() if state and state.get("stage") else None


def _arguments(raw: Any) -> Any:
    """A tool call's arguments as JSON if they parse, else as scrubbed text."""
    if isinstance(raw, str):
        try:
            return scrub_value(json.loads(raw), MAX_TEXT)
        except ValueError:
            return scrub_text(raw)
    return scrub_value(raw, MAX_TEXT)


def transcript(rows: list[tuple[int, Any, dict]]) -> tuple[list[dict], list[dict]]:
    """(conversation, tool calls) from ``(seq, created_at, body)`` message rows."""
    results: dict[str, str] = {}
    for _seq, _at, body in rows:
        message = body.get("message") or {}
        if message.get("role") == "tool" and message.get("tool_call_id"):
            content = message.get("content")
            results[str(message["tool_call_id"])] = scrub_text(
                content if isinstance(content, str) else json.dumps(content)
            )
    turns: list[dict] = []
    tools: list[dict] = []
    for seq, at, body in rows:
        message = body.get("message") or {}
        role = message.get("role")
        content = message.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            turns.append(
                {
                    "seq": seq,
                    "at": _iso(at),
                    "speaker": "caller" if role == "user" else "agent",
                    "text": scrub_text(content),
                }
            )
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                call_id = str(call.get("id", ""))
                tools.append(
                    {
                        "seq": seq,
                        "at": _iso(at),
                        "name": str(function.get("name") or call.get("name") or "tool"),
                        "arguments": _arguments(function.get("arguments", call.get("arguments"))),
                        "result": results.get(call_id),  # None: no answer saved yet
                    }
                )
    return turns, tools


def _read_only_connection():
    conn = connect()
    conn.read_only = True
    return conn


LIST_SQL = """
    select a.agentphone_call_id, a.placed_at, a.outcome, a.ended_at,
           d.account_number, p.first_name || ' ' || p.last_name,
           (select m.body -> 'state' from public.call_message m
             where m.call_id = a.agentphone_call_id and m.kind = 'state'
             order by m.seq desc limit 1)
    from public.call_attempt a
    join public.debt d on d.id = a.debt_id
    join public.debtor p on p.id = d.debtor_id
    where a.agentphone_call_id is not null
    order by a.placed_at desc, a.id
    limit %s
"""

ATTEMPT_SQL = """
    select a.placed_at, a.outcome, a.ended_at, d.account_number,
           p.first_name || ' ' || p.last_name
    from public.call_attempt a
    join public.debt d on d.id = a.debt_id
    join public.debtor p on p.id = d.debtor_id
    where a.agentphone_call_id = %s
"""

DEBT_SQL = """
    select d.account_number, p.first_name || ' ' || p.last_name
    from public.debt d join public.debtor p on p.id = d.debtor_id
    where d.id::text = %s
"""

MESSAGES_SQL = """
    select seq, created_at, body from (
        select seq, created_at, body from public.call_message
        where call_id = %s and kind = 'message' order by seq desc limit %s
    ) newest order by seq
"""

STATE_SQL = """
    select body -> 'state' from public.call_message
    where call_id = %s and kind = 'state' order by seq desc limit 1
"""

AUDIT_SQL = """
    select seq, ts, kind, rule_id, decision, reason, evidence from (
        select seq, ts, kind, rule_id, decision, reason, evidence from public.audit_event
        where call_id = %s order by seq desc limit %s
    ) newest order by seq
"""

PLANS_SQL = """
    select down_cents, installments, frequency, installment_cents, created_at
    from public.payment_plan where call_id = %s and status = 'pending' order by created_at
"""

SUMMARY_SQL = """
    select outcome, facts_text, narrative, created_at
    from public.call_summary where call_id = %s
"""


def _valid(call_id: str) -> str:
    if not _CALL_ID.fullmatch(call_id):
        raise HTTPException(404, "no such call")
    return call_id


@router.get("")
def list_calls() -> dict[str, Any]:
    """The newest calls, for the picker: who, when, stage, outcome, whether it has ended."""
    with _read_only_connection() as conn:
        rows = conn.execute(LIST_SQL, (MAX_CALLS,)).fetchall()
    calls = []
    for call_id, placed_at, outcome, ended_at, account, name, state in rows:
        ended = ended_at is not None or bool(state and state.get("ended"))
        calls.append(
            {
                "call_id": call_id,
                "debtor_name": name,
                "account_number": account,
                "started": _iso(placed_at),
                "stage": _stage(state),
                "outcome": outcome,
                "ended": ended,
            }
        )
    return {"calls": calls}


@router.get("/{call_id}")
def get_call(call_id: str) -> dict[str, Any]:
    """Everything the live view shows for one call, read from this call's rows only."""
    _valid(call_id)
    with _read_only_connection() as conn:
        attempt = conn.execute(ATTEMPT_SQL, (call_id,)).fetchone()
        message_rows = conn.execute(MESSAGES_SQL, (call_id, MAX_MESSAGES)).fetchall()
        state_row = conn.execute(STATE_SQL, (call_id,)).fetchone()
        state = state_row[0] if state_row else None
        if attempt is None and not message_rows and state is None:
            raise HTTPException(404, "no such call")
        if attempt:
            started, outcome, ended_at, account, name = attempt
        else:  # messages without an attempt row: the debt the saved state names
            started = outcome = ended_at = None
            debt = conn.execute(DEBT_SQL, (str((state or {}).get("debt_id", "")),)).fetchone()
            account, name = debt if debt else (None, None)
        audit_rows = conn.execute(AUDIT_SQL, (call_id, MAX_AUDIT)).fetchall()
        plans = conn.execute(PLANS_SQL, (call_id,)).fetchall()
        summary = conn.execute(SUMMARY_SQL, (call_id,)).fetchone()
    ended = ended_at is not None or bool(state and state.get("ended"))
    turns, tools = transcript(message_rows)
    events = []
    for seq, ts, kind, rule_id, decision, reason, evidence in audit_rows:
        sentence = evidence.get("sentence") if isinstance(evidence, dict) else None
        events.append(
            {
                "seq": seq,
                "at": _iso(ts),
                "kind": kind,
                "rule_id": rule_id,
                "decision": decision,
                "reason": scrub_text(reason, MAX_EVIDENCE_TEXT),
                "sentence": scrub_text(sentence) if isinstance(sentence, str) else None,
            }
        )
    return {
        "call_id": call_id,
        "debtor_name": name,
        "account_number": account,
        "started": _iso(started),
        "ended": ended,
        "outcome": outcome,
        "state": {
            "stage": _stage(state),
            "verified": bool(state and state.get("verified")),
            "disclosed": bool(state and state.get("disclosed")),
            "turns": int(state.get("turns", 0)) if state else 0,
        },
        "messages": turns,
        "tool_calls": tools,
        "pending_plans": [
            {
                "down_cents": down,
                "installments": count,
                "frequency": frequency,
                "installment_cents": each,
                "created": _iso(created),
            }
            for down, count, frequency, each, created in plans
        ],
        "audit_events": events,
        "summary": _summary(summary),
    }


def _summary(row: tuple | None) -> dict | None:
    if row is None:
        return None
    outcome, facts_text, narrative, created = row
    return {
        "outcome": outcome,
        "facts": scrub_text(facts_text),
        "narrative": scrub_text(narrative) if narrative else None,
        "created": _iso(created),
    }
