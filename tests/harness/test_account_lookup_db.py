"""Supabase and the made-up debtors.

What it checks: ``lookup_account`` reads the debtors from the real local Postgres
(``supabase start``, seeded by ``supabase/seed.sql``), keeps the reply it had
before, and answers an unknown name with an ``error:`` string, not an exception.
Nothing here is faked except the model: tests marked ``db`` talk to the real
database, or are skipped with "run `supabase start`" when it can't be reached.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import Agent
from harness.db import DEFAULT_DATABASE_URL, database_url, find_account
from harness.tool_outcome import tool_body
from harness.tools import default_tools, lookup_account, run_tool
from model import fake, tool_call, tool_reply

# --- without a database -------------------------------------------------------


def test_database_url_defaults_to_local_supabase(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", "")
    assert database_url() == DEFAULT_DATABASE_URL


def test_a_database_that_is_down_becomes_an_error_string(monkeypatch: pytest.MonkeyPatch):
    # Nothing listens on port 1. Linux refuses at once; Windows waits for the timeout.
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:1/postgres")
    result = run_tool(default_tools(), "lookup_account", '{"name": "Sam Doe"}')
    assert result.startswith("error: lookup_account failed: ")
    assert "OperationalError" in result or "ConnectionTimeout" in result


# --- against the real local database ------------------------------------------


@pytest.mark.db
def test_sam_does_balance_comes_from_the_database():
    account = find_account("Sam Doe")
    assert account == {
        "account_id": "ACC-1001",
        "name": "Sam Doe",
        "creditor": "Northwind Furniture",
        "balance": "1250.00",
        "currency": "USD",
    }


@pytest.mark.db
def test_names_match_without_case_or_extra_spaces():
    assert json.loads(lookup_account("  jordan   LEE "))["balance"] == "89.99"
    assert json.loads(lookup_account("Alex Rivera"))["account_id"] == "ACC-1002"


@pytest.mark.db
def test_the_agent_loop_gets_its_answer_from_the_database(no_instructions: Path):
    def model(msgs: list[dict]):
        if msgs[-1]["role"] == "tool":
            account = json.loads(tool_body(msgs[-1]["content"]))
            return f"You owe ${account['balance']} to {account['creditor']}."
        return tool_reply(tool_call("lookup_account", name="Sam Doe"))

    agent = Agent(provider=fake(scripted=model), system_prompt_path=no_instructions)

    reply = agent.run("Hi, this is Sam Doe. How much do I owe?")

    assert reply == "You owe $1250.00 to Northwind Furniture."
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "tool", "assistant"]
    assert json.loads(tool_body(agent.messages[2]["content"]))["account_id"] == "ACC-1001"


@pytest.mark.db
def test_row_level_security_is_on_for_every_table():
    from harness.db import connect

    with connect() as conn:
        rows = conn.execute(
            "select relname, relrowsecurity from pg_class "
            "where relnamespace = 'public'::regnamespace and relname = any(%s)",
            (["debtor", "phone", "debt"],),
        ).fetchall()
    assert dict(rows) == {"debtor": True, "phone": True, "debt": True}


# --- negative case -----------------------------------------------------------


@pytest.mark.db
def test_an_unknown_name_is_an_error_string_not_an_exception():
    assert find_account("Nobody Here") is None
    assert lookup_account("Nobody Here") == "error: no account found for 'Nobody Here'"
    result = run_tool(default_tools(), "lookup_account", '{"name": "Nobody Here"}')
    assert result == "error: no account found for 'Nobody Here'"
