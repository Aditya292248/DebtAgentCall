"""Turn what an operator typed about a new person into checked fields, in code.

The operator chat's ``add_person`` tool is stateless: the model passes everything it
knows on every call, and ``intake`` reads it all again. Nothing half-built is kept.
This module does the reading and no writing: it parses messy input (a date as
"March 3 85", an amount as "1.2k", a phone as "617 555 0123"), says what is still
missing and what is wrong in plain words, and hands the clean fields to
``harness.people.check_person``, the same check the web form and API go through.

Consent is never filled in. ``consent_source`` empty means the operator has not been
asked yet, and that is listed as missing. ``none``, ``no``, ``unknown`` and the like
mean they were asked and there is no consent to record: the person is saved without
it, and calls stay blocked. A real source needs a real date; neither is ever guessed.

A date that could be read two ways ("3/4/85") is refused, never guessed. A ZIP gives
the state only when the state is left out and the ZIP's first three digits fall in
one state's range.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from harness.bills import MAX_BALANCE_CENTS, _text, e164_from_printed
from harness.people import MAX_TEXT, US_STATES, check_person

# The tool's argument names, in the order the model is told them.
ARGUMENTS = (
    "first_name",
    "last_name",
    "date_of_birth",
    "address",
    "zip",
    "state",
    "phone",
    "creditor",
    "account_number",
    "amount",
    "consent_source",
    "consent_date",
    "consent_note",
    "line_type",
)
# What to ask for, in the order a person would answer: who and what is owed, then the
# contact details, then consent last.
ASK_ORDER = (
    "first_name",
    "last_name",
    "phone",
    "creditor",
    "amount",
    "date_of_birth",
    "address",
    "zip",
    "state",
    "consent_source",
    "consent_date",
)
# Fields that are best asked together, so the operator answers once.
GROUPS = (
    ("first_name", "last_name"),
    ("address", "zip", "state"),
    ("consent_source", "consent_date"),
)
PLAIN = {
    "first_name": "first name",
    "last_name": "last name",
    "phone": "phone number",
    "creditor": "who the debt is owed to",
    "amount": "how much they owe",
    "date_of_birth": "date of birth",
    "address": "street address",
    "zip": "ZIP code",
    "state": "state",
    "consent_source": "whether they have permission to call this number, and how it was given",
    "consent_date": "roughly when they gave that permission (a date)",
}
MAX_ASK_GROUPS = 2
NO_CONSENT = frozenset({"none", "no", "no consent", "unknown", "unsure", "not sure", "n/a", "na"})
# Names a form field would have, so a problem found by check_person is told in the
# tool's own words.
FORM_TO_TOOL = {"dob": "date_of_birth", "balance_cents": "amount", "line_type": "line_type"}

MONTHS = {
    name: i
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sept", "sep"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC", "puerto rico": "PR",
}  # fmt: skip
# First three digits of a ZIP -> state, as USPS assigns them. A prefix that is not
# here (military, and the few that straddle states) gives no state: the operator is asked.
_ZIP3 = (
    (5, 5, "NY"), (6, 9, "PR"), (10, 27, "MA"), (28, 29, "RI"), (30, 38, "NH"),
    (39, 49, "ME"), (50, 59, "VT"), (60, 69, "CT"), (70, 89, "NJ"), (100, 149, "NY"),
    (150, 196, "PA"), (197, 199, "DE"), (200, 200, "DC"), (202, 205, "DC"), (206, 219, "MD"),
    (220, 246, "VA"), (247, 268, "WV"), (270, 289, "NC"), (290, 299, "SC"), (300, 319, "GA"),
    (320, 339, "FL"), (341, 342, "FL"), (344, 344, "FL"), (346, 347, "FL"), (349, 349, "FL"),
    (350, 369, "AL"), (370, 385, "TN"), (386, 397, "MS"), (398, 399, "GA"), (400, 427, "KY"),
    (430, 459, "OH"), (460, 479, "IN"), (480, 499, "MI"), (500, 528, "IA"), (530, 549, "WI"),
    (550, 567, "MN"), (570, 577, "SD"), (580, 588, "ND"), (590, 599, "MT"), (600, 629, "IL"),
    (630, 658, "MO"), (660, 679, "KS"), (680, 693, "NE"), (700, 714, "LA"), (716, 729, "AR"),
    (730, 749, "OK"), (750, 799, "TX"), (800, 816, "CO"), (820, 831, "WY"), (832, 838, "ID"),
    (840, 847, "UT"), (850, 865, "AZ"), (870, 884, "NM"), (889, 898, "NV"), (900, 961, "CA"),
    (967, 968, "HI"), (970, 979, "OR"), (980, 994, "WA"), (995, 999, "AK"),
)  # fmt: skip

_ZIP = re.compile(r"[0-9]{5}(-[0-9]{4})?")
_ISO = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_NUMERIC = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})")
_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?|\.\d+)\s*(k|thousand)?")
_ACCOUNT = re.compile(r"[A-Za-z0-9-]{1,40}")
_NANP = re.compile(r"\+1[2-9][0-9]{2}[2-9][0-9]{6}")


def state_for_zip(zip_code: str) -> str | None:
    """The one state a ZIP's first three digits belong to, or None."""
    if not _ZIP.fullmatch(zip_code):
        return None
    prefix = int(zip_code[:3])
    hits = {code for low, high, code in _ZIP3 if low <= prefix <= high}
    return hits.pop() if len(hits) == 1 else None


# --- dates -----------------------------------------------------------------------------


def _year(raw: str, today: date) -> int:
    """A four-digit year as it is; two digits become the latest year not after today."""
    if len(raw) == 4:
        return int(raw)
    year = 2000 + int(raw)
    return year if year <= today.year else year - 100


def parse_date(text: str, today: date) -> tuple[date | None, str | None]:
    """``(date, None)``, or ``(None, plain reason)`` when it is unclear or not a date."""
    raw = " ".join(text.lower().replace("'", " ").replace(",", " ").split())
    day = month = year = None
    if m := _ISO.fullmatch(raw):
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif m := _NUMERIC.fullmatch(raw):
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 and b <= 12:
            day, month = a, b
        elif b > 12 and a <= 12:
            month, day = a, b
        elif a == b:
            month, day = a, b
        elif a <= 12 and b <= 12:
            return None, (
                f"{text.strip()!r} could be read two ways (month first or day first); "
                "give the month as a word, like March 4 1985, or as YYYY-MM-DD"
            )
        else:
            return None, f"{text.strip()!r} is not a real date"
        year = _year(m.group(3), today)
    else:
        words = [w for w in re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw).split() if w != "of"]
        months = [i for i, w in enumerate(words) if w.rstrip(".") in MONTHS]
        numbers = [w for w in words if w.isdigit()]
        if len(months) == 1 and len(numbers) == 2 and len(words) == 3:
            month = MONTHS[words[months[0]].rstrip(".")]
            if months[0] == 2:  # 1985 March 3
                year_raw, day_raw = numbers
            else:  # March 3 1985, 3 March 1985
                day_raw, year_raw = numbers
            if len(year_raw) not in (2, 4) or len(day_raw) > 2:
                return None, f"I couldn't read {text.strip()!r} as a date"
            day, year = int(day_raw), _year(year_raw, today)
        else:
            return None, f"I couldn't read {text.strip()!r} as a date"
    try:
        return date(year, month, day), None
    except (ValueError, TypeError):
        return None, f"{text.strip()!r} is not a real date"


# --- amounts, phones, text ---------------------------------------------------------------


def parse_amount(text: str) -> tuple[int | None, str | None]:
    """Cents from ``$480``, ``480``, ``1,200.50``, ``about 1.2k``; or a plain reason."""
    words = " ".join(text.lower().split())
    for lead in ("about", "around", "roughly", "approximately", "approx", "~", "owes", "of"):
        words = words.removeprefix(lead).strip()
    words = words.replace("$", "").removesuffix("dollars").removesuffix("usd").strip()
    m = _AMOUNT.fullmatch(words)
    if not m:
        return None, f"{text.strip()!r} is not an amount; give dollars like 480 or 1.2k"
    try:
        value = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None, f"{text.strip()!r} is not an amount"
    if m.group(2):
        value *= 1000
    cents = int((value * 100).to_integral_value())
    if cents <= 0:
        return None, "the amount must be more than zero"
    if cents > MAX_BALANCE_CENTS:
        return None, "that amount is too large to be right; please check it"
    return cents, None


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def parse_phone(text: str) -> tuple[str | None, str | None]:
    """E.164 from a printed number (ten digits are US), or a plain reason."""
    e164 = e164_from_printed(text)
    if e164 is None or (e164.startswith("+1") and not _NANP.fullmatch(e164)):
        return None, f"{text.strip()!r} doesn't look like a full phone number with area code"
    return e164, None


# --- the intake --------------------------------------------------------------------------


@dataclass
class Intake:
    """What the operator's fields come to. ``form`` holds the checked values by the
    names ``harness.people.add_person`` takes, with the full phone number: it is never
    put in a tool result, only in code."""

    form: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    problems: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    consent: str = "unasked"  # unasked | none | given
    consent_text: str | None = None
    account_given: bool = False

    @property
    def status(self) -> str:
        if self.problems:
            return "needs_fix"
        return "needs_more" if self.missing else "ready"

    def fingerprint(self) -> str:
        """A hash of the checked fields, so an approval can prove its card still matches."""
        blob = json.dumps(self.form, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _group_ask(missing: list[str]) -> list[dict[str, Any]]:
    """At most two things to ask now, related fields together, in the order to ask."""
    out: list[dict[str, Any]] = []
    done: set[str] = set()
    for name in ASK_ORDER:
        if name not in missing or name in done:
            continue
        group = next((g for g in GROUPS if name in g), (name,))
        fields = [f for f in group if f in missing]
        done.update(fields)
        out.append({"fields": fields, "about": [PLAIN[f] for f in fields]})
        if len(out) == MAX_ASK_GROUPS:
            break
    return out


def ask_next(intake: Intake) -> list[dict[str, Any]]:
    """What to ask the operator for next: at most two groups, in plain words."""
    return _group_ask(intake.missing)


def intake(args: dict[str, str], today: date | None = None) -> Intake:
    """Read every field the operator has given; say what is missing and what is wrong."""
    today = today or datetime.now(UTC).date()
    got = {k: _text(args.get(k)) for k in ARGUMENTS}
    out = Intake()
    form = out.form
    problems = out.problems
    missing = out.missing
    given: set[str] = set()  # fields that were given, even if wrong, so they are not "missing"

    for name in ("first_name", "last_name", "creditor", "address"):
        if got[name]:
            given.add(name)
            form[name] = got[name]
    if got["address"] and not (
        re.search(r"\d", got["address"]) and re.search(r"[A-Za-z]", got["address"])
    ):
        problems["address"] = "I need the street address (a number and street), not just a city"

    if got["date_of_birth"]:
        given.add("date_of_birth")
        when, why = parse_date(got["date_of_birth"], today)
        if when is None:
            problems["date_of_birth"] = why or "that isn't a date I can read"
        elif when >= today:
            problems["date_of_birth"] = "that date of birth is in the future"
        elif when.year < 1900:
            problems["date_of_birth"] = "that date of birth is before 1900; please check it"
        else:
            form["dob"] = when.isoformat()

    if got["phone"]:
        given.add("phone")
        e164, why = parse_phone(got["phone"])
        if e164 is None:
            problems["phone"] = why or "that isn't a phone number I can read"
        else:
            form["phone"] = e164

    if got["amount"]:
        given.add("amount")
        cents, why = parse_amount(got["amount"])
        if cents is None:
            problems["amount"] = why or "that isn't an amount I can read"
        else:
            form["balance_cents"] = cents

    zip_code = got["zip"]
    if zip_code:
        given.add("zip")
        if _ZIP.fullmatch(zip_code):
            form["zip"] = zip_code
        else:
            problems["zip"] = "a ZIP code is 5 digits (or ZIP+4, like 90012-1234)"
    if got["state"]:
        given.add("state")
        raw = got["state"].rstrip(".")
        code = STATE_NAMES.get(raw.lower(), raw.upper())
        if code in US_STATES:
            form["state"] = code
        else:
            problems["state"] = (
                f"{got['state']!r} is not a US state; use the name or the two-letter code"
            )
    elif "zip" in form:
        if inferred := state_for_zip(form["zip"]):
            given.add("state")
            form["state"] = inferred
            out.notes.append(f"state {inferred} was worked out from the ZIP code")

    if got["account_number"]:
        out.account_given = True
        if _ACCOUNT.fullmatch(got["account_number"]):
            form["account_number"] = got["account_number"]
        else:
            problems["account_number"] = "an account number is letters, digits and dashes only"

    line = (got["line_type"] or "").lower()
    if line in ("cell", "landline"):
        form["line_type"] = line
    elif line:
        problems["line_type"] = "line type is cell or landline"
    else:
        form["line_type"] = "cell"
        out.notes.append("line type was not given, so it is recorded as cell")

    _consent(out, got, today, given)

    for name in ASK_ORDER:
        if name == "consent_date" or name == "consent_source":
            continue
        if name not in given:
            missing.append(name)
    _consent_missing(out)
    missing.sort(key=ASK_ORDER.index)

    # The shared check, on what has been given so far, for problems this module
    # does not look for itself (lengths, a real state code, half a consent record).
    probe = {k: v for k, v in form.items() if k != "account_number" or out.account_given}
    for key, why in check_person(probe, today).items():
        tool = FORM_TO_TOOL.get(key, key)
        if why == "is needed" or tool in problems or tool in missing:
            continue
        if tool in given or key in ("fields",) or tool.startswith("consent"):
            problems[tool] = why
    return out


def _consent(out: Intake, got: dict[str, str | None], today: date, given: set[str]) -> None:
    source, note = got["consent_source"], got["consent_note"]
    if source is None:
        if got["consent_date"]:
            out.problems["consent_source"] = "a consent date needs the way consent was given"
        return
    given.add("consent_source")
    if " ".join(source.lower().split()) in NO_CONSENT:
        out.consent = "none"
        if got["consent_date"]:
            out.problems["consent_date"] = "no consent was recorded, so there is no consent date"
        return
    out.consent = "given"
    if got["consent_date"] is None:
        return
    given.add("consent_date")
    when, why = parse_date(got["consent_date"], today)
    if when is None:
        out.problems["consent_date"] = why or "that isn't a date I can read"
    elif when > today:
        out.problems["consent_date"] = "that consent date is in the future"
    elif when.year < 1990:
        out.problems["consent_date"] = "that consent date is too far back; please check it"
    else:
        text = source if not note else f"{source} ({note})"
        if len(text) > MAX_TEXT:
            out.problems["consent_source"] = (
                f"keep the consent source and note under {MAX_TEXT} characters"
            )
        out.form["consent_source"] = text
        out.form["consent_date"] = when.isoformat()
        out.consent_text = text


def _consent_missing(out: Intake) -> None:
    if out.consent == "unasked":
        out.missing.append("consent_source")
    elif out.consent == "given" and "consent_date" not in out.form:
        if "consent_date" not in out.problems:
            out.missing.append("consent_date")
