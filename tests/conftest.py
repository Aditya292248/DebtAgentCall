"""Shared pytest setup for the episode tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def no_instructions(tmp_path: Path) -> Path:
    """A system-prompt path that doesn't exist, so the payload is history only.

    Tests that check the exact messages sent to the model use this so they
    don't depend on whatever is in the repo's instructions/system.md.
    """
    return tmp_path / "missing.md"
