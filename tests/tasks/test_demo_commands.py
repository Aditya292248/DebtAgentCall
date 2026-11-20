"""Demos and acceptance checks on a database with no sample people.

What it checks: a part that needs the sample people says so, and names
``uv run seed-demo``, instead of failing on an empty database; it refuses a
database that holds a real person; the early parts need no database at all.
"""

from __future__ import annotations

import psycopg
import pytest

from tasks import dbsetup, demo


def test_early_parts_need_no_database_and_later_parts_do():
    assert not dbsetup.part_needs_demo_data("part-05")
    assert dbsetup.part_needs_demo_data("part-06")
    assert dbsetup.part_needs_demo_data("part-24")


@pytest.mark.db
def test_a_demo_on_an_empty_database_explains_seed_demo(empty_database, capsys):
    assert demo.main(["part-07"]) == 1
    out = capsys.readouterr().out
    assert "uv run seed-demo" in out and "no sample people" in out


@pytest.mark.db
def test_a_demo_refuses_a_database_with_a_real_person(empty_database, capsys):
    with psycopg.connect(empty_database) as conn:
        conn.execute(
            "insert into public.debtor (first_name, last_name, dob, address, zip, state)"
            " values ('Real', 'Person', '1990-01-01', '1 Real St', '10001', 'NY')"
        )
    assert demo.main(["part-07"]) == 1
    assert "--demo-db" in capsys.readouterr().out


@pytest.mark.db
def test_a_demo_runs_once_the_sample_people_are_loaded(empty_database, capsys):
    from tasks.seed_demo import main as seed_demo

    assert seed_demo([]) == 0
    assert demo.main(["part-07"]) == 0
    assert "MAY CALL" in capsys.readouterr().out
