"""A small read-only web API, so the browser can show what the Python checks decide.

``GET /api/precall`` runs ``compliance.check_before_call`` for every debt and
returns all the decisions, for now or for the time in ``?at=`` (an ISO time with
a zone, e.g. ``2026-11-05T21:30:00-05:00``). It only reads: nothing is written to
the audit log, because looking at the screen is not placing a call.

``GET /api/audit/verify`` runs ``compliance.audit.verify_chain`` and says whether
the audit chain is whole.

The checks stay in ``compliance/``; this file only loads, calls and formats.
Run it with ``uv run api`` (http://127.0.0.1:8000); the web app in ``web/``
forwards ``/api`` to it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from compliance import check_before_call, may_call
from compliance.audit import verify_chain
from harness.db import connect, load_call_facts

DEBTS_SQL = """
    select d.account_number, p.first_name || ' ' || p.last_name
    from public.debt d
    join public.debtor p on p.id = d.debtor_id
    order by d.account_number
"""

app = FastAPI(title="DebtAgentCall read-only API")


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


def main() -> None:
    import uvicorn

    port = int(os.environ.get("API_PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
