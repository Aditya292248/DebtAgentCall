"""The database: made-up debtors kept in Postgres (local Supabase).

``connect()`` opens a plain ``psycopg`` connection to ``DATABASE_URL``, falling
back to a local ``.env`` file and then to the local Supabase default. The server
talks to Postgres directly like this; row level security only limits users who
come in through Supabase's API.

``find_account(name)`` is the debt for a person, matched on their full name
without caring about case or extra spaces, or ``None``.
``find_debtor_by_phone(e164)`` is the person and debt behind a phone number,
which the call stages compare the caller's answers against.

Both give the balance owed now, ``public.current_balance_cents(debt)`` (the
original ``balance_cents`` less payments, plus reversals; see
``harness/payments.py``). That is the amount a call may say and the sentence
filter checks against, so a recorded payment shows on the next turn.

The pre-call loaders read what ``compliance.check_before_call`` needs for one
debt: ``load_attempts``, ``load_phone`` and ``load_flags`` each read one thing,
and ``load_call_facts(account_number)`` puts them together as ``CallFacts``.
They only load; all the deciding stays in ``compliance/``.

``save_flag``, ``revoke_phone``, ``save_dispute`` and ``mark_disputed`` are what a stop
trigger writes during a
call (``phone/webhook.py``). Like ``compliance.audit.record`` they take an open
connection and leave committing to the caller, so the flag and its audit row
commit together.

The tables live in ``supabase/migrations/``. Run ``supabase start`` to get an
empty local database; ``uv run seed-demo`` adds made-up people to it on request.
"""

from __future__ import annotations

import os

import psycopg

from compliance import Attempt, CallFacts, Flag, PhoneConsent
from harness.call_state import Debtor, DebtRow
from model.provider import _load_dotenv

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
CONNECT_TIMEOUT = 3  # seconds; a database that isn't running should fail fast

FIND_ACCOUNT_SQL = """
    select d.account_number, p.first_name || ' ' || p.last_name, d.creditor,
           public.current_balance_cents(d)
    from public.debt d
    join public.debtor p on p.id = d.debtor_id
    where lower(p.first_name || ' ' || p.last_name) = lower(%s)
    order by d.account_number
    limit 1
"""


def database_url() -> str:
    """``DATABASE_URL`` from the environment (``.env`` is a fallback), or the local default."""
    _load_dotenv()
    return os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL


def connect() -> psycopg.Connection:
    return psycopg.connect(database_url(), connect_timeout=CONNECT_TIMEOUT)


def find_account(name: str) -> dict | None:
    """The account for ``name`` in the same shape the tool has always returned, or None."""
    wanted = " ".join(name.split())
    with connect() as conn:
        row = conn.execute(FIND_ACCOUNT_SQL, (wanted,)).fetchone()
    if row is None:
        return None
    account_number, full_name, creditor, balance_cents = row
    return {
        "account_id": account_number,
        "name": full_name,
        "creditor": creditor,
        "balance": f"{balance_cents // 100}.{balance_cents % 100:02d}",
        "currency": "USD",
    }


# The debtor and debt behind a phone number: what the call stages check against.
DEBTOR_BY_PHONE_SQL = """
    select p.id::text, d.id::text, p.first_name, p.last_name, p.dob, p.zip,
           d.creditor, public.current_balance_cents(d)
    from public.phone ph
    join public.debtor p on p.id = ph.debtor_id
    join public.debt d on d.debtor_id = p.id
    where ph.e164 = %s
    order by d.account_number
    limit 1
"""


def find_debtor_by_phone(e164: str, conn: psycopg.Connection | None = None) -> Debtor | None:
    """Who a call to ``e164`` is for, and their debt, or None if the number is unknown.

    Pass ``conn`` to read inside a transaction you already have open.
    """
    if conn is None:
        with connect() as own:
            return find_debtor_by_phone(e164, own)
    row = conn.execute(DEBTOR_BY_PHONE_SQL, (e164.strip(),)).fetchone()
    if row is None:
        return None
    debtor_id, debt_id, first, last, dob, zip_code, creditor, balance_cents = row
    return Debtor(
        debtor_id=debtor_id,
        debt_id=debt_id,
        first_name=first,
        last_name=last,
        dob=dob,
        zip=zip_code,
        creditor=creditor,
        balance_cents=balance_cents,
    )


DEBT_ROW_SQL = (
    "select d.creditor, public.current_balance_cents(d), d.status from public.debt d"
    " where d.id = %s"
)


def read_debt_row(debt_id: str, conn: psycopg.Connection | None = None) -> DebtRow | None:
    """The debt's creditor, balance owed now (after payments) and status, or None.

    Pass ``conn`` to read inside a transaction you already have open.
    """
    if conn is None:
        with connect() as own:
            return read_debt_row(debt_id, own)
    row = conn.execute(DEBT_ROW_SQL, (debt_id,)).fetchone()
    return DebtRow(*row) if row else None


# --- pre-call loaders ----------------------------------------------------------

DEBT_SQL = """
    select d.id::text, p.zip, p.archived_at is not null
    from public.debt d
    join public.debtor p on p.id = d.debtor_id
    where d.account_number = %s
"""

ATTEMPTS_SQL = """
    select debt_id::text, placed_at, connected, conversation_with_debtor, outcome
    from public.call_attempt
    where debt_id = %s
    order by placed_at
"""

# One number per debtor for now: the first by number, so the choice is stable.
PHONE_SQL = """
    select ph.e164, ph.consent_at, ph.consent_source, ph.revoked_at, ph.line_type
    from public.debt d
    join public.phone ph on ph.debtor_id = d.debtor_id
    where d.id = %s
    order by ph.e164
    limit 1
"""

# Flags for the debt's debtor: the ones for every debt, and the ones for this one.
FLAGS_SQL = """
    select f.kind, f.debt_id::text, f.data, f.set_at, f.source
    from public.debt d
    join public.flags f on f.debtor_id = d.debtor_id
    where d.id = %s and (f.debt_id is null or f.debt_id = d.id)
    order by f.set_at, f.kind
"""


def load_attempts(conn: psycopg.Connection, debt_id: str) -> list[Attempt]:
    rows = conn.execute(ATTEMPTS_SQL, (debt_id,)).fetchall()
    return [
        Attempt(
            debt_id=d,
            placed_at=placed_at,
            connected=connected,
            conversation_with_debtor=talked,
            outcome=outcome,
        )
        for d, placed_at, connected, talked, outcome in rows
    ]


def load_phone(conn: psycopg.Connection, debt_id: str) -> PhoneConsent | None:
    row = conn.execute(PHONE_SQL, (debt_id,)).fetchone()
    if row is None:
        return None
    e164, consent_at, consent_source, revoked_at, line_type = row
    return PhoneConsent(
        e164=e164,
        consent_at=consent_at,
        consent_source=consent_source,
        revoked_at=revoked_at,
        line_type=line_type,
    )


def load_flags(conn: psycopg.Connection, debt_id: str) -> list[Flag]:
    rows = conn.execute(FLAGS_SQL, (debt_id,)).fetchall()
    return [
        Flag(kind=kind, debt_id=d, data=dict(data or {}), set_at=set_at, source=source)
        for kind, d, data, set_at, source in rows
    ]


def load_call_facts(
    account_number: str, conn: psycopg.Connection | None = None
) -> CallFacts | None:
    """Everything the pre-call checks need for one debt, or None if there's no such debt.

    Pass ``conn`` to read inside a transaction you already have open.
    """
    if conn is None:
        with connect() as own:
            return load_call_facts(account_number, own)
    row = conn.execute(DEBT_SQL, (account_number,)).fetchone()
    if row is None:
        return None
    debt_id, zip_code, archived = row
    return CallFacts(
        debt_id=debt_id,
        zip=zip_code,
        phone=load_phone(conn, debt_id),
        attempts=load_attempts(conn, debt_id),
        flags=load_flags(conn, debt_id),
        archived=archived,
    )


# --- writes from a live call ---------------------------------------------------

INSERT_FLAG_SQL = """
    insert into public.flags (debtor_id, debt_id, kind, data, source)
    values (%s, %s, %s, %s, %s)
"""

# Robocall consent ends when the person says stop (TCPA). Only the number we
# called, and only if it isn't revoked already, so the first time is kept.
REVOKE_PHONE_SQL = """
    update public.phone set revoked_at = now()
    where debtor_id = %s and e164 = %s and revoked_at is null
"""


# Only an active debt moves: a closed or paused one keeps its status.
MARK_DISPUTED_SQL = "update public.debt set status = 'disputed' where id = %s and status = 'active'"


INSERT_DISPUTE_SQL = """
    insert into public.dispute (call_id, debt_id, reason, verified)
    values (%s, %s, %s, %s)
"""


def save_dispute(
    conn: psycopg.Connection, call_id: str, debt_id: str, reason: str, verified: bool
) -> None:
    """Add a dispute row, the same table ``log_dispute`` writes. One that isn't
    ``verified`` is kept for a person to review. Committing is left to the caller."""
    conn.execute(INSERT_DISPUTE_SQL, (call_id, debt_id, reason, verified))


def mark_disputed(conn: psycopg.Connection, debt_id: str) -> bool:
    """Set an active debt's status to ``disputed``; True if it changed.

    Committing is left to the caller.
    """
    return conn.execute(MARK_DISPUTED_SQL, (debt_id,)).rowcount == 1


def save_flag(conn: psycopg.Connection, flag: Flag, debtor_id: str) -> None:
    """Add one flag for ``debtor_id``. Committing is left to the caller."""
    from psycopg.types.json import Jsonb

    conn.execute(
        INSERT_FLAG_SQL,
        (debtor_id, flag.debt_id, flag.kind, Jsonb(dict(flag.data)), flag.source),
    )


def revoke_phone(conn: psycopg.Connection, debtor_id: str, e164: str) -> None:
    """Mark the number's consent as revoked now. Committing is left to the caller."""
    conn.execute(REVOKE_PHONE_SQL, (debtor_id, e164.strip()))
