"""``uv run dialer`` on a database with nobody in it.

What it checks: the command exits cleanly (status 0, no traceback) and says there
is nothing to dial and how to add people.
"""

from __future__ import annotations

import pytest

from dialer.cli import NOTHING_TO_DIAL, main

pytestmark = pytest.mark.db


def test_the_dialer_with_nothing_to_dial_exits_cleanly_and_explains(empty_database, capsys):
    assert main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert NOTHING_TO_DIAL in out
    assert "uv run seed-demo" in out and "Add people" in out
    assert "dialer at" not in out  # it never got as far as a queue
