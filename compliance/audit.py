"""The audit log: every compliance decision, kept in a chain nobody can edit.

``record(conn, decision)`` adds one row to ``audit_event``. The database does
the rest (``supabase/migrations/*_audit_log.sql``): it numbers the row, stamps
the time, and stores ``prev_hash`` (the last row's hash) and ``hash``. It also
refuses every update, delete and truncate, so a correction is a new row.

``verify_chain(conn)`` reads every row in order and recomputes each hash here
in Python. It returns a list of problems: a gap in ``seq``, a ``prev_hash`` that
doesn't match the row before, or a ``hash`` that doesn't match the row's own
fields. An empty list means the chain is whole. If someone gets past the
database's rules and edits a row, its hash no longer matches and this says so.

The hash has no secret key, so someone who edits a row and recomputes every
later hash (or cuts rows off the end) leaves a chain that still checks out.
Catching that needs a copy of the newest seq and hash kept outside the
database, which comes later.

How a hash is made (the SQL trigger does exactly the same):

    hash = sha256(prev_hash + canonical(row)), as lowercase hex

``canonical`` is a JSON array of the row's fields, written the way Postgres
writes ``jsonb`` text (``", "`` between items, non-ASCII kept as is). The first
row's ``prev_hash`` is ``GENESIS``, 64 zeros.

These functions take an open ``psycopg`` connection and leave committing to the
caller, so a decision and the action it allowed can commit together.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from compliance.decision import Decision

GENESIS = "0" * 64

INSERT_SQL = """
    insert into public.audit_event (call_id, debt_id, kind, rule_id, decision, reason, evidence)
    values (%s, %s, %s, %s, %s, %s, %s)
    returning seq, hash
"""

# evidence::text is Postgres's own spelling of the jsonb value: the same text the
# trigger hashed. The other fields come back as plain values.
_SELECT = """
    select seq, ts, call_id, debt_id::text, kind, rule_id, decision, reason,
           evidence::text, prev_hash, hash
    from public.audit_event
"""
ROWS_SQL = _SELECT + "order by seq"
ROWS_AFTER_SQL = _SELECT + "where seq > %s order by seq"
ROW_NAMES = (
    "seq",
    "ts",
    "call_id",
    "debt_id",
    "kind",
    "rule_id",
    "decision",
    "reason",
    "evidence",
    "prev_hash",
    "hash",
)
BATCH_ROWS = 10_000  # rows fetched per round trip when reading the whole log


@dataclass(frozen=True)
class Recorded:
    seq: int
    hash: str


def record(
    conn: Connection,
    decision: Decision,
    *,
    kind: str = "gate",
    call_id: str | None = None,
    debt_id: str | None = None,
) -> Recorded:
    """Add one audit row for ``decision``; returns the row's ``seq`` and ``hash``."""
    row = conn.execute(
        INSERT_SQL,
        (
            call_id,
            debt_id,
            kind,
            decision.rule_id,
            "allow" if decision.allow else "block",
            decision.reason,
            Jsonb(decision.evidence),
        ),
    ).fetchone()
    assert row is not None  # insert ... returning always gives a row
    return Recorded(seq=row[0], hash=row[1])


def canonical(
    seq: int,
    ts: datetime,
    call_id: str | None,
    debt_id: str | None,
    kind: str,
    rule_id: str,
    decision: str,
    reason: str,
    evidence_text: str,
) -> str:
    """The text that gets hashed, matching ``public.audit_event_canonical`` in SQL."""
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("ts must be timezone-aware")
    ts_text = ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    fields = [seq, ts_text, call_id, debt_id, kind, rule_id, decision, reason, evidence_text]
    return json.dumps(fields, ensure_ascii=False, separators=(", ", ": "))


def chain_hash(prev_hash: str, canonical_text: str) -> str:
    return hashlib.sha256((prev_hash + canonical_text).encode("utf-8")).hexdigest()


def verify_chain(conn: Connection) -> list[str]:
    """Every problem found replaying ``audit_event`` in ``seq`` order; empty means OK."""
    problems: list[str] = []
    expected_seq = 1
    expected_prev = GENESIS
    for row in _stream(conn, ROWS_SQL):
        seq, ts, call_id, debt_id, kind, rule_id, decision, reason, evidence, prev, stored = row
        if seq != expected_seq:
            problems.append(f"seq {seq}: expected seq {expected_seq}, so rows are missing")
        if prev != expected_prev:
            problems.append(f"seq {seq}: prev_hash does not match the hash of the row before")
        text = canonical(seq, ts, call_id, debt_id, kind, rule_id, decision, reason, evidence)
        if chain_hash(prev, text) != stored:
            problems.append(f"seq {seq}: hash does not match the row's contents ({rule_id})")
        # Carry the stored hash forward, so one edited row is reported once.
        expected_seq = seq + 1
        expected_prev = stored
    return problems


def rows(conn: Connection, after_seq: int = 0) -> Iterator[dict[str, Any]]:
    """The audit rows after ``after_seq``, in order, for printing."""
    for r in _stream(conn, ROWS_AFTER_SQL, (after_seq,)):
        yield dict(zip(ROW_NAMES, r, strict=True))


def _stream(conn: Connection, sql: str, params: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    """Rows from a server-side cursor, a batch at a time, so a long log never
    has to fit in memory at once."""
    with conn.cursor(name="audit_event_read") as cur:
        cur.itersize = BATCH_ROWS
        cur.execute(sql, params)
        yield from cur
