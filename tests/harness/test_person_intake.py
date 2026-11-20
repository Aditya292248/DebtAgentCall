"""Reading a person's details as an operator types them (``harness.person_intake``).

What it checks, with no database:
  - Amounts, phone numbers and dates in the many ways people write them come out
    in one form, and the ones that could mean two things are refused, not guessed.
  - A ZIP gives the state only when the state was left out.
  - Each field that is missing, wrong or sensitive is reported in plain words;
    consent is never filled in: not asked, none, and given are three states.
  - What to ask next is at most two groups, with related fields together.
"""

from __future__ import annotations

from datetime import date

import pytest

from harness.person_intake import ask_next, intake, parse_amount, parse_date, parse_phone

TODAY = date(2026, 11, 5)
FULL = {
    "first_name": "Sam",
    "last_name": "Lee",
    "date_of_birth": "March 3 85",
    "address": "12 Elm Street",
    "zip": "02115",
    "phone": "617 555 0123",
    "creditor": "Acme Credit",
    "amount": "$480",
}


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("$480", 48000),
        ("480", 48000),
        ("480.5", 48050),
        ("1,200.50", 120050),
        ("1.2k", 120000),
        ("about 1.2k", 120000),
        ("$2K", 200000),
        ("480 dollars", 48000),
    ],
)
def test_amounts(text, cents):
    assert parse_amount(text) == (cents, None)


@pytest.mark.parametrize("text", ["lots", "0", "-5", "$0.00", "480x", "99999999999"])
def test_bad_amounts_get_a_reason(text):
    cents, why = parse_amount(text)
    assert cents is None and why


@pytest.mark.parametrize(
    ("text", "e164"),
    [
        ("617 555 0123", "+16175550123"),
        ("(617) 555-0123", "+16175550123"),
        ("617.555.0123", "+16175550123"),
        ("1-617-555-0123", "+16175550123"),
        ("+1 617 555 0123", "+16175550123"),
        ("+16175550123", "+16175550123"),
    ],
)
def test_phones(text, e164):
    assert parse_phone(text) == (e164, None)


@pytest.mark.parametrize("text", ["555 0123", "061 555 0123", "12345", "call him"])
def test_bad_phones(text):
    e164, why = parse_phone(text)
    assert e164 is None and why


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1985-03-03", date(1985, 3, 3)),
        ("March 3 85", date(1985, 3, 3)),
        ("March 3, 1985", date(1985, 3, 3)),
        ("3 March 1985", date(1985, 3, 3)),
        ("3rd of Mar '85", date(1985, 3, 3)),
        ("Sept 9 2001", date(2001, 9, 9)),
        ("3/3/85", date(1985, 3, 3)),  # the same either way
        ("13/4/85", date(1985, 4, 13)),  # only day first is possible
        ("4/13/85", date(1985, 4, 13)),  # only month first is possible
        ("1/2/03", None),
    ],
)
def test_dates(text, expected):
    got, why = parse_date(text, TODAY)
    if expected is None:
        assert got is None and "two ways" in why
    else:
        assert (got, why) == (expected, None)


@pytest.mark.parametrize("text", ["Feb 30 1990", "someday", "March 1985", "31/31/90"])
def test_unreadable_dates_get_a_reason(text):
    got, why = parse_date(text, TODAY)
    assert got is None and why


def test_a_complete_person_with_consent_unasked_only_misses_consent():
    got = intake(FULL, TODAY)
    assert got.status == "needs_more"
    assert got.missing == ["consent_source"]
    assert got.form["dob"] == "1985-03-03"
    assert got.form["balance_cents"] == 48000
    assert got.form["phone"] == "+16175550123"
    assert got.form["state"] == "MA"  # worked out from 02115
    assert any("worked out from the ZIP" in n for n in got.notes)
    assert "consent_source" not in got.form


def test_a_stated_state_is_never_replaced_by_the_zip():
    got = intake({**FULL, "state": "California"}, TODAY)
    assert got.form["state"] == "CA"
    assert got.notes == ["line type was not given, so it is recorded as cell"]


def test_an_unknown_zip_leaves_the_state_to_ask_about():
    got = intake({**FULL, "zip": "09999"}, TODAY)  # military range
    assert "state" in got.missing
    assert ask_next(got)[0]["fields"] == ["state"]


def test_the_three_consent_states():
    asked = intake(FULL, TODAY)
    assert asked.consent == "unasked" and "consent_source" in asked.missing

    none = intake({**FULL, "consent_source": "not sure"}, TODAY)
    assert none.consent == "none" and "consent_source" not in none.missing
    assert none.status == "ready"

    half = intake({**FULL, "consent_source": "signed form"}, TODAY)
    assert half.consent == "given" and half.missing == ["consent_date"]

    done = intake({**FULL, "consent_source": "signed form", "consent_date": "2026-10-01"}, TODAY)
    assert done.status == "ready"
    assert done.form["consent_date"] == "2026-10-01"


def test_a_consent_date_without_a_source_or_with_none_is_a_problem():
    assert "consent_source" in intake({**FULL, "consent_date": "2026-10-01"}, TODAY).problems
    both = intake({**FULL, "consent_source": "none", "consent_date": "2026-10-01"}, TODAY)
    assert "consent_date" in both.problems


def test_the_consent_note_is_shown_with_the_source():
    got = intake(
        {
            **FULL,
            "consent_source": "web form",
            "consent_date": "2026-10-01",
            "consent_note": "ticked the box",
        },
        TODAY,
    )
    assert got.consent_text == "web form (ticked the box)"
    assert got.form["consent_source"] == "web form (ticked the box)"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("date_of_birth", "2030-01-01", "in the future"),
        ("date_of_birth", "3/4/85", "two ways"),
        ("date_of_birth", "1850-01-01", "before 1900"),
        ("phone", "555 0123", "phone number"),
        ("zip", "2115", "5 digits"),
        ("state", "Narnia", "not a US state"),
        ("amount", "lots", "amount"),
        ("address", "Boston", "street address"),
        ("consent_date", "2999-01-01", "future"),
    ],
)
def test_a_wrong_value_is_a_problem_with_a_plain_reason(field, value, reason):
    args = {**FULL, "consent_source": "signed form", "consent_date": "2026-10-01"}
    args[field] = value
    got = intake(args, TODAY)
    assert got.status == "needs_fix"
    assert reason in got.problems[field]


def test_a_wrong_value_is_not_also_listed_as_missing():
    got = intake({**FULL, "date_of_birth": "2030-01-01"}, TODAY)
    assert "date_of_birth" not in got.missing


def test_with_almost_nothing_it_asks_for_at_most_two_things_grouped():
    got = intake({"first_name": "Sam", "last_name": "Lee", "amount": "480"}, TODAY)
    assert got.status == "needs_more"
    ask = ask_next(got)
    assert len(ask) <= 2
    assert ask[0]["fields"] == ["phone"]
    # address, ZIP and state travel together
    later = intake({**FULL, "address": "", "zip": "", "date_of_birth": ""}, TODAY)
    groups = [g["fields"] for g in ask_next(later)]
    assert ["address", "zip", "state"] in groups


def test_what_was_given_is_never_asked_for_again():
    got = intake(FULL, TODAY)
    asked = {f for g in ask_next(got) for f in g["fields"]}
    assert asked.isdisjoint(FULL)


def test_a_made_up_field_cannot_slip_through_as_blank():
    got = intake({"first_name": "  ", "last_name": ""}, TODAY)
    assert "first_name" in got.missing and "last_name" in got.missing
    assert got.problems == {}
