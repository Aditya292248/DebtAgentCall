"""Archived people are never called.

The web UI's Remove button archives a person (``debtor.archived_at``) instead of
deleting them, because their history is kept for three years. This check is how
the archive takes effect: it is one of the pre-call checks, so the dialer, the
operator chat and every live-call tool stop at it.
"""

from __future__ import annotations

from compliance.decision import Decision, allow, block


def check_archived(archived: bool | None) -> Decision:
    """Blocks an archived person, and blocks when we don't know (missing data never passes)."""
    if archived is None:
        return block("archived", "could not tell whether this person is archived", {})
    if archived:
        return block("archived", "this person is archived and is not called", {"archived": True})
    return allow("archived", "not archived", {"archived": False})
