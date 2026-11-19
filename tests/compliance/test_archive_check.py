"""The archived pre-call check: an archived person is blocked, and unknown blocks too."""

from __future__ import annotations

from compliance.archive import check_archived


def test_archived_blocks_and_unknown_blocks_and_only_a_known_live_person_passes():
    assert check_archived(True).allow is False
    assert check_archived(None).allow is False  # missing data never passes
    assert check_archived(False).allow is True
    assert {check_archived(x).rule_id for x in (True, None, False)} == {"archived"}
