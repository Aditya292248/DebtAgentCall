"""The database: made-up debtors kept in Postgres (local Supabase).

``connect()`` opens a plain ``psycopg`` connection to ``DATABASE_URL``, falling
back to a local ``.env`` file and then to the local Supabase default. The server
talks to Postgres directly like this; row level security only limits users who
come in through Supabase's API.

``find_account(name)`` is the one query so far: the debt for a person, matched
on their full name without caring about case or extra spaces, or ``None``.

The tables and seed data live in ``supabase/``. Run ``supabase start`` to get a
local database, and ``supabase db reset`` to reload the seed.
"""

from __future__ import annotations

import os

import psycopg

from model.provider import _load_dotenv

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
CONNECT_TIMEOUT = 3  # seconds; a database that isn't running should fail fast

FIND_ACCOUNT_SQL = """
    select d.account_number, p.first_name || ' ' || p.last_name, d.creditor, d.balance_cents
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
