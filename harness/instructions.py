"""The system prompt: read it from a file on disk.

A missing file means "no system prompt", not an error, so the agent still runs
without one.

Adapted from codingHarness.
"""

from __future__ import annotations

from pathlib import Path


def load_system_prompt(path: str | Path) -> str:
    """Return the file's text with surrounding whitespace removed, or "" if it is missing."""
    p = Path(path)
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return ""
