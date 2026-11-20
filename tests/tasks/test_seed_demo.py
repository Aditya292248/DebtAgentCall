"""``uv run seed-demo``: sample people go in only when asked, and never over real ones.

What it checks: a fresh migrated database holds no people, phones or debts;
``seed-demo`` loads the made-up people once and is safe to repeat; it
refuses a database that already holds a person who is not a demo person, and
leaves it untouched, unless ``--force``; ``--check`` reports without writing.
Each test uses its own empty ``_test`` database, never the one the app uses.
"""

from __future__ import annotations

import psycopg
import pytest

from tasks.dbsetup import DEMO_DEBTOR_IDS
from tasks.seed_demo import main as seed_demo

pytestmark = pytest.mark.db

TABLES = ("debtor", "phone", "debt", "call_attempt", "flags")


def counts(url: str) -> dict[str, int]:
    with psycopg.connect(url) as conn:
        return {t: conn.execute(f"select count(*) from public.{t}").fetchone()[0] for t in TABLES}


def add_real_person(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "insert into public.debtor (first_name, last_name, dob, address, zip, state)"
            " values ('Real', 'Person', '1990-01-01', '1 Real St', '10001', 'NY')"
        )


def test_a_fresh_migrated_database_has_no_people_phones_or_debts(empty_database):
    assert counts(empty_database) == dict.fromkeys(TABLES, 0)


def test_seed_demo_loads_the_sample_people_and_is_safe_to_repeat(empty_database, capsys):
    assert seed_demo([]) == 0
    first = counts(empty_database)
    people = len(DEMO_DEBTOR_IDS)
    assert people >= 9
    assert first["debtor"] == first["debt"] == first["phone"] == people
    assert seed_demo([]) == 0  # second run changes nothing
    assert counts(empty_database) == first
    assert "already loaded" in capsys.readouterr().out


def test_seed_demo_refuses_a_database_with_a_non_demo_person_and_leaves_it_alone(
    empty_database, capsys
):
    add_real_person(empty_database)
    before = counts(empty_database)
    assert before["debtor"] == 1
    assert seed_demo([]) == 2
    assert counts(empty_database) == before
    err = capsys.readouterr().err
    assert "refusing" in err and "--force" in err


def test_force_loads_the_demo_people_beside_the_real_one(empty_database):
    add_real_person(empty_database)
    assert seed_demo(["--force"]) == 0
    assert counts(empty_database)["debtor"] == len(DEMO_DEBTOR_IDS) + 1
    with psycopg.connect(empty_database) as conn:
        row = conn.execute("select 1 from public.debtor where last_name = 'Person'").fetchone()
    assert row is not None  # the real row is still there


def test_check_reports_without_writing(empty_database, capsys):
    assert seed_demo(["--check"]) == 1  # absent
    assert "absent" in capsys.readouterr().out
    assert counts(empty_database)["debtor"] == 0
    seed_demo([])
    assert seed_demo(["--check"]) == 0
    n = len(DEMO_DEBTOR_IDS)
    assert f"present ({n} of {n}" in capsys.readouterr().out
