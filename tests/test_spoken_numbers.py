"""Spoken numbers: ``find_dob`` and ``find_zip`` from ``harness/spoken.py``.

Each table is what the caller said, then what must come out: a date, a ZIP, or
``None``. ``None`` is the fail-closed answer: a date that could be read two
ways, is not a real date, or is outside 1900 to today is no answer at all.
Only made-up dates and ZIP codes.
"""

from __future__ import annotations

from datetime import date

import pytest

from harness.spoken import find_dob, find_zip

JAN_15_1990 = date(1990, 1, 15)
APR_12_1985 = date(1985, 4, 12)


def _dob(said: str) -> date | None:
    return find_dob(said)[0]


def _both(said: str) -> tuple[date | None, str | None]:
    """The date, then the ZIP read from what is left once the date is cut out."""
    dob, rest = find_dob(said)
    return dob, find_zip(rest)


# --- dates written with digits ----------------------------------------------------


@pytest.mark.parametrize(
    "said, expected",
    [
        ("1985-04-12", APR_12_1985),
        ("4/12/1985", APR_12_1985),
        ("4-12-1985", APR_12_1985),
        ("4/12/85", APR_12_1985),  # two digits: the most recent such year that has passed
        ("April 12, 1985", APR_12_1985),
        ("Apr. 12 1985", APR_12_1985),
        ("april 12th, '85", APR_12_1985),
        ("12 April 1985", APR_12_1985),
        ("12th of April 1985", APR_12_1985),
        ("It's 1/15/1990.", JAN_15_1990),
        ("13/45/1985", None),  # looks like a date, is not one
        ("1985-02-30", None),
        ("February 30, 1985", None),
    ],
)
def test_digit_dates(said: str, expected: date | None) -> None:
    assert _dob(said) == expected


# --- dates said in words ---------------------------------------------------------


@pytest.mark.parametrize(
    "said, expected",
    [
        # month first
        ("January fifteenth nineteen ninety", JAN_15_1990),
        ("January the fifteenth, nineteen ninety", JAN_15_1990),
        ("January fifteen nineteen ninety", JAN_15_1990),
        ("Jan fifteenth nineteen ninety", JAN_15_1990),
        ("October twenty second twenty oh four", date(2004, 10, 22)),
        ("March thirty first nineteen eighty five", date(1985, 3, 31)),
        ("December ninth two thousand twelve", date(2012, 12, 9)),
        ("Sept 3rd nineteen oh five", date(1905, 9, 3)),
        ("June tenth nineteen hundred", date(1900, 6, 10)),
        # day first
        ("fifteenth January nineteen ninety", JAN_15_1990),
        ("the fifteenth of January nineteen ninety", JAN_15_1990),
        ("Fifteenth January, nineteen ninety.", JAN_15_1990),
        ("Twenty second October two thousand four", date(2004, 10, 22)),
        ("twenty-second of October, two thousand and four", date(2004, 10, 22)),
        ("the first of March two thousand", date(2000, 3, 1)),
        ("thirtieth of April nineteen seventy", date(1970, 4, 30)),
        # digits and words mixed
        ("15th January nineteen ninety", JAN_15_1990),
        ("15 January nineteen ninety", JAN_15_1990),
        ("fifteenth of January 1990", JAN_15_1990),
        ("January 15th nineteen ninety", JAN_15_1990),
        ("My birthday is January fifteenth, 1990, thanks", JAN_15_1990),
    ],
)
def test_spoken_dates(said: str, expected: date) -> None:
    assert _dob(said) == expected


@pytest.mark.parametrize(
    "said",
    [
        "one fifteen nineteen ninety",  # no month name: could be read many ways
        "thirty second January nineteen ninety",  # day 32
        "January thirty second nineteen ninety",
        "fifteenth of the thirteenth nineteen ninety",  # month 13
        "February thirtieth nineteen ninety",  # not a real date
        "January fifteenth ninety",  # not a whole year
        "January fifteenth",  # no year
        "January fifteenth twenty fifty",  # in the future
        "January fifteenth eighteen ninety",  # before 1900
        "nineteen ninety",  # a year alone
        "zero two one one five",  # a ZIP is not a date
        "",
    ],
)
def test_impossible_or_incomplete_dates_are_no_answer(said: str) -> None:
    assert _dob(said) is None


# --- ZIP codes -----------------------------------------------------------------


@pytest.mark.parametrize(
    "said, expected",
    [
        ("02115", "02115"),
        ("02115-1234", "02115"),
        ("my zip is 90012.", "90012"),
        ("zero two one one five", "02115"),
        ("oh two one one five", "02115"),
        ("O two one one five", "02115"),
        ("zero, two, one, one, five", "02115"),
        ("zero two one one and five", "02115"),
        ("my zip is 0 2 1 1 5", "02115"),
        ("0 two 1 one 5", "02115"),  # digits and words mixed
        ("nine oh two one oh", "90210"),
        ("zero two one one", None),  # four digits
        ("zero two one one five five", None),  # six digits
        ("two one one five", None),
        ("twenty one fifteen", None),  # not said digit by digit
        ("2115", None),
        ("021155", None),
        ("4/12/85", None),
        ("", None),
    ],
)
def test_zips(said: str, expected: str | None) -> None:
    assert find_zip(said) == expected


# --- a date and a ZIP in one turn ---------------------------------------------------


@pytest.mark.parametrize(
    "said, expected",
    [
        ("April 12, 1985 and 90012", (APR_12_1985, "90012")),
        ("12th of April 1985, my zip code is 90012", (APR_12_1985, "90012")),
        ("4/12/1985 90012", (APR_12_1985, "90012")),
        ("1985-04-12 90012", (APR_12_1985, "90012")),
        ("4/12/85 90012", (APR_12_1985, "90012")),
        (
            "It's January fifteenth nineteen ninety, and zero two one one five.",
            (JAN_15_1990, "02115"),
        ),
        ("fifteenth January 1990 oh two one one five", (JAN_15_1990, "02115")),
        # The year's last word could be the ZIP's first digit: the ZIP's five digits decide.
        ("nineteen ninety, nine oh two one oh", (None, "90210")),  # no day or month: no date
        ("January fifteenth nineteen ninety, nine oh two one oh", (JAN_15_1990, "90210")),
        ("January fifteenth nineteen ninety nine oh two one oh", (JAN_15_1990, "90210")),
        ("January fifteenth nineteen ninety. Nine zero two one zero.", (JAN_15_1990, "90210")),
        (
            "January fifteenth nineteen ninety nine zero two one one five",
            (date(1999, 1, 15), "02115"),
        ),
        ("January fifteenth nineteen ninety nine", (date(1999, 1, 15), None)),
        ("March first nineteen eighty, six oh six oh one", (date(1980, 3, 1), "60601")),
        ("the first of March two thousand, nine oh two one oh", (date(2000, 3, 1), "90210")),
        ("the first of March two thousand, four", (date(2004, 3, 1), None)),
        (
            "the first of March two thousand twenty one oh two one one five",
            (date(2021, 3, 1), "02115"),
        ),
    ],
)
def test_date_then_zip(said: str, expected: tuple[date | None, str | None]) -> None:
    assert _both(said) == expected


@pytest.mark.parametrize(
    "said",
    [
        # 1999 with half a ZIP, or 1990 with a ZIP starting nine: can't tell, so no date.
        "January fifteenth nineteen ninety nine oh two",
        "January fifteenth nineteen ninety one one one five",
        "the first of March two thousand four oh two",
    ],
)
def test_ambiguous_spoken_date_is_no_answer(said: str) -> None:
    assert _dob(said) is None


def test_the_date_is_cut_out_of_the_rest() -> None:
    """What ``find_dob`` hands back must not still hold the date's digits."""
    dob, rest = find_dob("January fifteenth nineteen ninety, nine oh two one oh")
    assert dob == JAN_15_1990
    assert "nineteen" not in rest and "nine oh two one oh" in rest
    assert find_dob("no date here") == (None, "no date here")
