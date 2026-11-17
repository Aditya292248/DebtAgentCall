"""Calling hours.

What it checks: ``check_calling_hours`` allows a call only from 08:00 up to 21:00
in every time zone the debtor might be in (from their ZIP and their phone's area
code), blocks it when no zone is known, and blocks it inside any window the
debtor said is inconvenient. Pure Python with fixed times, so it is offline and
deterministic; ``uv run accept part-05`` runs the same gate on the example contacts.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from compliance import Contact, Decision, allow, block, check_calling_hours, zones_for

LA = ZoneInfo("America/Los_Angeles")
NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

CA_DEBTOR = Contact(zip="90012", phone_e164="+12135550101")
NY_DEBTOR = Contact(zip="10001", phone_e164="+12125550104")


def _at(hour: int, minute: int = 0, tz: ZoneInfo = LA) -> datetime:
    return datetime(2026, 11, 3, hour, minute, tzinfo=tz)


# --- Decision -------------------------------------------------------------------


def test_decision_helpers():
    assert allow("r", "ok") == Decision(allow=True, rule_id="r", reason="ok", evidence={})
    d = block("r", "no", {"k": 1})
    assert (d.allow, d.rule_id, d.reason, d.evidence) == (False, "r", "no", {"k": 1})


def test_decision_is_frozen():
    d = allow("r", "ok")
    with pytest.raises(AttributeError):
        d.allow = False  # type: ignore[misc]


# --- zones_for ------------------------------------------------------------------


def test_zip_and_area_code_that_agree_give_one_zone():
    assert zones_for("90012", "+12135550101") == {"America/Los_Angeles"}


def test_zip_and_area_code_that_disagree_give_both_zones():
    assert zones_for("10001", "+14155550102") == {"America/New_York", "America/Los_Angeles"}


def test_one_known_source_is_enough():
    assert zones_for("00000", "+13125550103") == {"America/Chicago"}
    assert zones_for("80202", None) == {"America/Denver"}


@pytest.mark.parametrize(
    "zip_code, phone",
    [("00000", "+19995550000"), (None, None), ("", ""), ("abc", "2135550101")],
)
def test_no_known_source_gives_none(zip_code, phone):
    assert zones_for(zip_code, phone) is None


# --- check_calling_hours ----------------------------------------------------------


def test_ten_am_local_is_allowed():
    d = check_calling_hours(CA_DEBTOR, _at(10))
    assert d.allow
    assert d.rule_id == "calling_hours"
    assert "10:00 America/Los_Angeles" in d.reason


def test_nine_oh_five_pm_pacific_is_blocked():
    d = check_calling_hours(CA_DEBTOR, _at(21, 5))
    assert not d.allow
    assert d.rule_id == "calling_hours"
    assert "21:05 America/Los_Angeles" in d.reason


def test_zip_in_eastern_area_code_in_pacific_is_blocked_at_830_eastern():
    # 8:30am in New York is 5:30am in Los Angeles: fine for one zone, not the other.
    mismatch = Contact(zip="10001", phone_e164="+14155550102")
    d = check_calling_hours(mismatch, _at(8, 30, tz=NY))
    assert not d.allow
    assert d.rule_id == "calling_hours"
    assert "05:30 America/Los_Angeles" in d.reason
    assert d.evidence["zones"] == ["America/Los_Angeles", "America/New_York"]


def test_unknown_zip_and_unknown_area_code_is_blocked():
    d = check_calling_hours(Contact(zip="00000", phone_e164="+19995550000"), _at(12, tz=UTC))
    assert not d.allow
    assert d.rule_id == "calling_hours"
    assert "no known time zone" in d.reason


def test_inside_a_debtor_stated_inconvenient_window_is_blocked():
    lunch = Contact(zip="90012", phone_e164="+12135550101", inconvenient=[(time(12), time(13))])
    d = check_calling_hours(lunch, _at(12, 30))
    assert not d.allow
    assert d.rule_id == "calling_hours"
    assert "inconvenient" in d.reason
    assert check_calling_hours(lunch, _at(13)).allow  # the end of the window is fine again


def test_inconvenient_window_is_checked_in_every_zone():
    # 9:30am Pacific is 12:30pm Eastern, inside the "12:00-13:00" window there.
    both = Contact(zip="10001", phone_e164="+14155550102", inconvenient=[(time(12), time(13))])
    d = check_calling_hours(both, _at(9, 30))
    assert not d.allow
    assert "12:30 America/New_York" in d.reason


def test_inconvenient_window_that_wraps_past_midnight():
    nights = Contact(zip="90012", phone_e164="+12135550101", inconvenient=[(time(20), time(9))])
    assert not check_calling_hours(nights, _at(8, 30)).allow
    assert check_calling_hours(nights, _at(9)).allow


def test_bad_inconvenient_windows_are_rejected():
    with pytest.raises(ValueError, match="without a time zone"):
        Contact(zip="90012", phone_e164=None, inconvenient=[(time(12, tzinfo=LA), time(13))])
    with pytest.raises(ValueError, match="is empty"):
        Contact(zip="90012", phone_e164=None, inconvenient=[(time(12), time(12))])


@pytest.mark.parametrize(
    "hour, minute, allowed",
    [(7, 59, False), (8, 0, True), (20, 59, True), (21, 0, False)],
)
def test_boundaries(hour, minute, allowed):
    assert check_calling_hours(NY_DEBTOR, _at(hour, minute, tz=NY)).allow is allowed


def test_daylight_saving_day():
    # US clocks spring forward on 2026-03-08. 12:30 UTC is 7:30am EST the day
    # before (blocked) but 8:30am EDT that morning (allowed).
    before = datetime(2026, 3, 7, 12, 30, tzinfo=UTC)
    after = datetime(2026, 3, 8, 12, 30, tzinfo=UTC)
    assert not check_calling_hours(NY_DEBTOR, before).allow
    assert check_calling_hours(NY_DEBTOR, after).allow
    # Arizona has no daylight saving: 15:30 UTC is 8:30am there on both days.
    phoenix = Contact(zip="85004", phone_e164="+16025550105")
    assert check_calling_hours(phoenix, datetime(2026, 3, 7, 15, 30, tzinfo=UTC)).allow
    assert check_calling_hours(phoenix, datetime(2026, 3, 8, 15, 30, tzinfo=UTC)).allow


def test_works_for_a_future_time():
    # A callback booked years ahead gets the same answer as a call right now.
    assert check_calling_hours(CA_DEBTOR, datetime(2030, 6, 14, 10, 0, tzinfo=LA)).allow
    assert not check_calling_hours(CA_DEBTOR, datetime(2030, 6, 14, 22, 0, tzinfo=LA)).allow


def test_naive_datetime_raises():
    with pytest.raises(ValueError, match="timezone-aware"):
        check_calling_hours(CA_DEBTOR, datetime(2026, 11, 3, 10, 0))
