"""A small web API: what the Python checks decide, the operator chat, and bill upload.

``GET /api/precall`` runs ``compliance.check_before_call`` for every debt and
returns all the decisions, for now or for the time in ``?at=`` (an ISO time with
a zone, e.g. ``2026-11-05T21:30:00-05:00``). It only reads: nothing is written to
the audit log, because looking at the screen is not placing a call.

``GET /api/audit/verify`` runs ``compliance.audit.verify_chain`` and says whether
the audit chain is whole.

``POST /api/chat`` takes ``{session_id, message, tz?}``, runs the operator agent
(``harness/operator.py``) for one turn and returns its reply, the tool calls it
made and anything waiting for approval (a call, a payment or a reversal).
``tz`` is the operator's IANA time zone, which the browser sends so "paid
yesterday" means the operator's yesterday; without it ``OPERATOR_TZ`` is used.
``POST /api/chat/approve`` takes ``{session_id, approval_id, approve, tz?}`` and
approves or denies one waiting approval; approving a call dials through the
dialer, which runs the checks again, and approving a payment checks it against
the balance again before writing it.
Each chat's history is kept in the session store under ``chat:<session_id>``.
Turns for one chat run one at a time in this process.

``POST /api/bills`` takes a bill (multipart, field ``file``: PNG, JPEG, WebP or
PDF), has the model read it (``harness/bills.py``) and returns a draft: its id,
the fields read and the ones a person must look at. It saves no debtor, phone
or debt. ``POST /api/bills/{id}/confirm`` takes ``{fields}`` as the person
edited them, checks them again (422 with each problem if any fails) and only
then saves the debtor, the phone (with no consent, so no call is allowed yet)
and the debt. ``GET /api/bills/{id}`` returns a draft again.

The checks stay in ``compliance/``; this file only loads, calls and formats.
Run it with ``uv run api`` (http://127.0.0.1:8000); the web app in ``web/``
forwards ``/api`` to it.
"""

from __future__ import annotations

import os
import re
import threading
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import psycopg
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from compliance import check_before_call, may_call
from compliance.audit import verify_chain
from harness.bills import (
    MAX_BYTES,
    AccountExists,
    AlreadyConfirmed,
    BillRejected,
    Bills,
    DraftNotFound,
    InvalidFields,
    PhoneExists,
    PostgresBillDb,
    bills_from_env,
)
from harness.db import connect, load_call_facts
from harness.memory import PostgresSession
from harness.operator import ChatTurn, Operator, PhoneUnavailable, operator_tz
from harness.people_api import router as people_router

DEBTS_SQL = """
    select d.account_number, p.first_name || ' ' || p.last_name
    from public.debt d
    join public.debtor p on p.id = d.debtor_id
    order by d.account_number
"""

app = FastAPI(title="DebtAgentCall read-only API")
app.include_router(people_router)


@app.exception_handler(psycopg.Error)
def _database_error(_request: Request, exc: psycopg.Error) -> JSONResponse:
    # Say plainly that the database is the problem, instead of a bare 500: 503
    # when it can't be reached, 500 with the reason for anything else (such as a
    # table that a missing migration should have made).
    first_line = (str(exc).strip().splitlines() or [repr(exc)])[0]
    if isinstance(exc, psycopg.OperationalError):
        detail = f"database not reachable ({first_line}); run `supabase start`"
        return JSONResponse(status_code=503, content={"detail": detail})
    detail = f"database error ({type(exc).__name__}: {first_line})"
    return JSONResponse(status_code=500, content={"detail": detail})


def _parse_at(at: str | None) -> datetime:
    if not at:
        return datetime.now(UTC)
    try:
        when = datetime.fromisoformat(at)
    except ValueError as exc:
        raise HTTPException(400, f"at must be an ISO time, got {at!r}") from exc
    if when.tzinfo is None or when.utcoffset() is None:
        raise HTTPException(400, "at must include a time zone, e.g. 2026-11-05T21:30:00-05:00")
    return when


@app.get("/api/precall")
def precall(at: str | None = Query(default=None)) -> dict[str, Any]:
    """Every debt's pre-call decisions at ``at`` (default: now)."""
    when = _parse_at(at)
    debts: list[dict[str, Any]] = []
    with connect() as conn:
        for account_number, name in conn.execute(DEBTS_SQL).fetchall():
            decisions = check_before_call(load_call_facts(account_number, conn), when)
            debts.append(
                {
                    "account_number": account_number,
                    "name": name,
                    "may_call": may_call(decisions),
                    "decisions": [
                        {"rule_id": d.rule_id, "allow": d.allow, "reason": d.reason}
                        for d in decisions
                    ],
                }
            )
    return {"at": when.isoformat(), "debts": debts}


@app.get("/api/audit/verify")
def audit_verify() -> dict[str, Any]:
    """Whether every hash in the audit log matches."""
    with connect() as conn:
        count_row = conn.execute("select count(*) from public.audit_event").fetchone()
        problems = verify_chain(conn)
    return {"ok": not problems, "rows": count_row[0] if count_row else 0, "problems": problems}


# --- operator chat ------------------------------------------------------------

_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_chat_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
_chat_locks_guard = threading.Lock()


def make_operator(session_id: str, tz: str | None = None) -> Operator:
    """The operator for one chat, on its saved history. Tests swap this out."""
    return Operator(
        PostgresSession.for_call(f"chat:{session_id}"), tz=operator_tz(tz), bills=PostgresBillDb()
    )


class ChatIn(BaseModel):
    session_id: str
    message: str
    tz: str | None = None


class ApproveIn(BaseModel):
    session_id: str
    approval_id: str
    approve: bool
    tz: str | None = None


def _checked_tz(tz: str | None) -> str | None:
    try:
        operator_tz(tz)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return tz


def _chat_lock(session_id: str) -> threading.Lock:
    if not _SESSION_ID.fullmatch(session_id):
        raise HTTPException(400, "session_id must be 1-64 letters, digits, _ or -")
    with _chat_locks_guard:
        return _chat_locks[session_id]


def _turn_json(turn: ChatTurn) -> dict[str, Any]:
    return {
        "reply": turn.reply,
        "tool_calls": turn.tool_calls,
        "pending_approvals": [asdict(a) for a in turn.pending_approvals],
        "dial": turn.dial,
    }


@app.post("/api/chat")
def chat(body: ChatIn) -> dict[str, Any]:
    """One operator message: the agent's reply, its tool calls and what awaits approval."""
    if not body.message.strip():
        raise HTTPException(400, "message is empty")
    tz = _checked_tz(body.tz)
    with _chat_lock(body.session_id):
        return _turn_json(make_operator(body.session_id, tz).chat(body.message))


@app.post("/api/chat/approve")
def chat_approve(body: ApproveIn) -> dict[str, Any]:
    """Approve or deny one waiting approval. Approving still runs every check."""
    tz = _checked_tz(body.tz)
    with _chat_lock(body.session_id):
        operator = make_operator(body.session_id, tz)
        try:
            turn = operator.decide(body.approval_id, body.approve)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PhoneUnavailable as exc:
            # Nothing recorded and the approval is still pending: fix the setup, retry.
            raise HTTPException(503, f"AgentPhone is not set up ({exc})") from exc
        return _turn_json(turn)


# --- bills -----------------------------------------------------------------------


def make_bills() -> Bills:
    """Where bills are stored and drafts are kept. Tests swap this out."""
    return bills_from_env()


class ConfirmIn(BaseModel):
    fields: dict[str, Any]


@app.post("/api/bills")
def upload_bill(file: UploadFile = File(...)) -> dict[str, Any]:  # noqa: B008
    """Read a bill into a draft. Nothing is saved as a debtor or debt."""
    data = file.file.read(MAX_BYTES + 1)  # one byte over, so sniff() can say it's too big
    try:
        draft = make_bills().upload(data, file.content_type)
    except BillRejected as exc:
        raise HTTPException(415, str(exc)) from exc
    return draft.as_json()


@app.get("/api/bills/{draft_id}")
def get_bill(draft_id: str) -> dict[str, Any]:
    draft = make_bills().get(draft_id)
    if draft is None:
        raise HTTPException(404, f"no bill draft {draft_id!r}")
    return draft.as_json()


@app.post("/api/bills/{draft_id}/confirm")
def confirm_bill(draft_id: str, body: ConfirmIn) -> dict[str, Any]:
    """Save a draft's fields as the person confirmed them, after checking them again."""
    try:
        saved = make_bills().confirm(draft_id, body.fields)
    except InvalidFields as exc:
        raise HTTPException(422, {"problems": exc.problems}) from exc
    except DraftNotFound as exc:
        raise HTTPException(404, f"no bill draft {draft_id!r}") from exc
    except AlreadyConfirmed as exc:
        raise HTTPException(409, f"bill draft {draft_id!r} is already confirmed") from exc
    except AccountExists as exc:
        raise HTTPException(409, f"a debt with account number {exc} already exists") from exc
    except PhoneExists as exc:
        raise HTTPException(
            409, "this phone number is already on file for a debtor; nothing was saved"
        ) from exc
    return {
        "draft_id": saved.draft_id,
        "debtor_id": saved.debtor_id,
        "debt_id": saved.debt_id,
        "account_number": saved.account_number,
        "note": "Saved with no consent on file, so the pre-call checks block calls "
        "to this number until consent is recorded.",
    }


def main() -> None:
    import uvicorn

    port = int(os.environ.get("API_PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
