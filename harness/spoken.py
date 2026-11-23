"""Reading a date of birth and a ZIP code out of what the caller said.

Speech-to-text sends numbers as digits or as words, so both are read:

    Dates   Digits: "1990-01-15", "1/15/1990", "1/15/90", "Jan 15, 1990",
            "15th of January 1990". Words: a month name, a day and a whole
            year, in either order: "January (the) fifteenth nineteen ninety",
            "(the) fifteenth (of) January two thousand (and) four". A day is a
            number or an ordinal ("fifteen", "fifteenth", "twenty second",
            "15th"); a year is four digits or "nineteen ninety", "nineteen
            hundred", "twenty oh four", "two thousand four".
    ZIPs    Five digits ("02115", "02115-1234"), or exactly five single digits
            said one by one ("zero two one one five", "oh two, one, one and
            five"). Four or six in a row is no ZIP.

The rule is fail closed: an answer that could be read two ways is no answer.
A spoken date with two possible readings, an impossible day or month, or a
year outside 1900 to today gives no date, never a guess. When a year's last
digit could also be a ZIP's first ("nineteen ninety nine oh two one oh"), the
number of digits that follow decides; if they don't, a pause ("ninety, nine")
does; if nothing does, there is no date.

``find_dob`` and ``find_zip`` are the whole interface. ``harness/stages.py``
compares what they return with the database.
"""

from __future__ import annotations

import re
from datetime import date

# --- number words -------------------------------------------------------------

_UNIT = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}  # fmt: skip
_ZERO = {"zero", "oh", "o"}
_TEEN = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}  # fmt: skip
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}  # fmt: skip
_ORDINAL = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
    "thirteenth": 13, "fourteenth": 14, "fifteenth": 15, "sixteenth": 16,
    "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20, "thirtieth": 30,
}  # fmt: skip

# --- month names: "january" and "jan" -> 1, and "sept" -> 9 -----------------------

_MONTH_WORDS = (
    "january february march april may june july august september october november december"
)
_MONTH_NAMES = {
    name: i + 1 for i, full in enumerate(_MONTH_WORDS.split()) for name in (full, full[:3])
} | {"sept": 9}

# --- dates and ZIPs written with digits --------------------------------------------

_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(\d{4}|\d{2})"  # "85" works too
_DATES = [  # (pattern, order of the groups: y=year, m=month, d=day)
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4}|\d{2})\b"), "mdy"),
    (re.compile(rf"\b{_MONTH}\s+{_DAY},?\s+'?{_YEAR}\b", re.IGNORECASE), "mdy"),
    (re.compile(rf"\b{_DAY}\s+(?:of\s+)?{_MONTH},?\s+'?{_YEAR}\b", re.IGNORECASE), "dmy"),
]
_ZIP = re.compile(r"(?<![\d/-])(\d{5})(?:-\d{4})?(?![\d/-])")

# --- tokens of spoken text --------------------------------------------------------

_TOKEN = re.compile(r"[a-z]+(?:'[a-z]+)?|\d+[a-z]*")
_DIGIT_DAY = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?")
_ONE_DIGIT = re.compile(r"\d")
_FOUR_DIGITS = re.compile(r"\d{4}")
_PAUSE = re.compile(r"[,.;:!?]")  # between two words, it ends a number

UNCLEAR_YEAR = -1  # a year whose last digit might be the first digit of the ZIP

Words = list[str]
SpokenDate = tuple[tuple[int, int, int], int]  # ((year, month, day), index just past it)


# --- the interface -------------------------------------------------------------


def find_dob(text: str) -> tuple[date | None, str]:
    """The first date in ``text``, and the text with that date cut out."""
    for pattern, order in _DATES:
        if m := pattern.search(text):
            parts = dict(zip(order, (_part(g) for g in m.groups()), strict=True))
            rest = text[: m.start()] + " " + text[m.end() :]
            if parts["y"] < 100:  # "4/12/85": the most recent such year that has passed
                year = 2000 + parts["y"]
                parts["y"] = year if year <= date.today().year else year - 100
            try:
                return date(parts["y"], parts["m"], parts["d"]), rest
            except ValueError:
                return None, rest  # "13/45/1985": said a date, but not a real one
    return _find_spoken_dob(text) or (None, text)


def find_zip(text: str) -> str | None:
    """Five digits, as digits ("02115", "02115-1234") or said one by one in words."""
    m = _ZIP.search(text)
    return m.group(1) if m else _find_spoken_zip(text)


# --- digit forms ---------------------------------------------------------------


def _part(value: str) -> int:
    """A month name ("Jan", "january") or a number, from one group of a digit form."""
    return _MONTH_NAMES[value[:3].lower()] if value[:1].isalpha() else int(value)


# --- spoken dates --------------------------------------------------------------


def _find_spoken_dob(text: str) -> tuple[date | None, str] | None:
    """A date said in words, or ``None`` if none was said. One that reads two ways,
    has an impossible day or month, or a year outside 1900 to today is said, but
    gives no date."""
    toks = list(_TOKEN.finditer(text.lower()))
    words = [t.group() for t in toks]
    # Words said with no comma or full stop before them: "ninety, nine" is not 99.
    glued = frozenset(
        k
        for k in range(1, len(toks))
        if not _PAUSE.search(text[toks[k - 1].end() : toks[k].start()])
    )
    for i in range(len(words)):
        if not (found := _spoken_dates(words, glued, i)):
            continue
        end = max(e for _, e in found)
        rest = text[: toks[i].start()] + " " + text[toks[end - 1].end() :]
        if len({ymd for ymd, _ in found}) != 1:
            return None, rest  # "January twenty one nineteen ninety one ...": unclear
        y, m, d = found[0][0]
        if y == UNCLEAR_YEAR:
            return None, rest  # "nineteen ninety nine oh two": 1999, or 1990 and a ZIP?
        try:
            said = date(y, m, d)
        except ValueError:
            return None, rest  # "thirty second of January"
        return (said if y >= 1900 and said <= date.today() else None), rest
    return None


def _spoken_dates(words: Words, glued: frozenset[int], i: int) -> list[SpokenDate]:
    """Every (year, month, day) a spoken date starting at ``words[i]`` could mean,
    with the index just past it."""
    found = []
    if month := _MONTH_NAMES.get(words[i]):  # "January (the) fifteenth nineteen ninety"
        j = i + 2 if _at(words, i + 1) == "the" else i + 1
        for day, k in _days(words, j):
            if year := _year(words, glued, k):
                found.append(((year[0], month, day), year[1]))
    for day, j in _days(words, i):  # "(the) fifteenth (of) January nineteen ninety"
        j += _at(words, j) == "of"
        if month := _MONTH_NAMES.get(_at(words, j)):
            k = j + 2 if _at(words, j + 1) == "of" else j + 1
            if year := _year(words, glued, k):
                found.append(((year[0], month, day), year[1]))
    return found


def _days(words: Words, i: int) -> list[tuple[int, int]]:
    """Every way a day of the month could start at ``words[i]``: (day, next index)."""
    w = _at(words, i)
    if m := _DIGIT_DAY.fullmatch(w):
        return [(int(m.group(1)), i + 1)]
    if w in _TENS:
        nxt = _at(words, i + 1)
        unit = _UNIT.get(nxt) or (_ORDINAL[nxt] if _ORDINAL.get(nxt, 10) < 10 else 0)
        whole = [(_TENS[w] + unit, i + 2)] if unit else []  # "twenty second", "thirty one"
        return [*whole, (_TENS[w], i + 1)]
    for table in (_ORDINAL, _TEEN, _UNIT):
        if w in table:
            return [(table[w], i + 1)]
    return []


def _year(words: Words, glued: frozenset[int], i: int) -> tuple[int, int] | None:
    """A whole year at ``words[i]``: "1990", "nineteen ninety", "two thousand (and)
    four", "twenty oh four". A bare "ninety" is not a year. The year is
    ``UNCLEAR_YEAR`` when its last digit might belong to the ZIP."""
    w, nxt = _at(words, i), _at(words, i + 1)
    if _FOUR_DIGITS.fullmatch(w):
        return int(w), i + 1
    if w == "two" and nxt == "thousand":
        j = i + 3 if _at(words, i + 2) == "and" else i + 2
        if (tail := _year_tail(words, glued, j)) is None:
            return 2000, i + 2
        return (UNCLEAR_YEAR if tail[0] is None else 2000 + tail[0]), tail[1]
    century = _TEEN.get(w) or (20 if w == "twenty" else None)
    if century is None:
        return None
    if nxt == "hundred":
        return century * 100, i + 2
    if nxt in _ZERO and (unit := _at(words, i + 2)) in _UNIT:
        return century * 100 + _UNIT[unit], i + 3
    if (nxt in _TEEN or nxt in _TENS) and (tail := _year_tail(words, glued, i + 1)):
        return (UNCLEAR_YEAR if tail[0] is None else century * 100 + tail[0]), tail[1]
    return None


def _year_tail(
    words: Words, glued: frozenset[int], k: int
) -> tuple[int, int] | tuple[None, int] | None:
    """The 1 to 99 part of a year at ``words[k]`` ("ninety", "ninety one", "four"),
    ``None`` if there is none, or ``(None, end)`` if its last digit is unclear."""
    w = _at(words, k)
    if w in _TEEN:
        return _TEEN[w], k + 1
    if w in _TENS:
        if _at(words, k + 1) not in _UNIT:
            return _TENS[w], k + 1
        ends = _last_digit_of_year(words, glued, k + 1)
        if ends is None:
            return None, k + 2
        return (_TENS[w] + _UNIT[words[k + 1]], k + 2) if ends else (_TENS[w], k + 1)
    if w in _UNIT:  # only after "two thousand": "two thousand four"
        ends = _last_digit_of_year(words, glued, k)
        if ends is None:
            return None, k + 1
        return (_UNIT[w], k + 1) if ends else None
    return None


def _last_digit_of_year(words: Words, glued: frozenset[int], k: int) -> bool | None:
    """Whether the single digit at ``words[k]`` ends the year (True), starts the ZIP
    (False), or could be either (None). The five digits of a ZIP decide: in
    "nineteen ninety nine oh two one oh" five digits follow "ninety", so the nine
    starts the ZIP (1990, 90210); in "... nine oh two one one five" six follow, so it
    ends the year (1999, 02115); a lone "nine" ends it too. Any other count is unclear,
    unless a pause ("ninety, nine oh two") ends the year first."""
    run = _run_length(words, k)
    if run in (1, 6):
        return True
    if run == 5:
        return False
    return False if k not in glued else None


# --- spoken digits and ZIPs -------------------------------------------------------


def _find_spoken_zip(text: str) -> str | None:
    """Exactly five digits said one by one: "zero two one one five", "oh two, one,
    one and five". A run of four or six digits is no ZIP."""
    words = [t.group() for t in _TOKEN.finditer(text.lower())] + [""]
    run: list[str] = []
    for i, w in enumerate(words):
        if (d := _digit(w)) is not None:
            run.append(d)
        elif w == "and" and run and _digit(words[i + 1]) is not None:
            continue
        elif len(run) == 5:
            return "".join(run)
        else:
            run = []
    return None


def _run_length(words: Words, k: int) -> int:
    """How many single digits are said in a row from ``words[k]`` ("and" between is fine)."""
    n = 0
    while True:
        if _digit(_at(words, k)) is not None:
            n, k = n + 1, k + 1
        elif _at(words, k) == "and" and n and _digit(_at(words, k + 1)) is not None:
            k += 1
        else:
            return n


def _digit(w: str) -> str | None:
    """One digit said as a word or written: "oh", "zero", "five", "5"."""
    if w in _ZERO:
        return "0"
    return str(_UNIT[w]) if w in _UNIT else (w if _ONE_DIGIT.fullmatch(w) else None)


def _at(words: Words, i: int) -> str:
    """``words[i]``, or "" past either end."""
    return words[i] if 0 <= i < len(words) else ""


_COUNTS = _UNIT | _TEEN | _TENS
_MONTH_COUNT = re.compile(
    r"\b(?P<count>\d{1,2}|"
    + "|".join(_COUNTS)
    + r")(?:[- ](?P<unit>"
    + "|".join(_UNIT)
    + r"))? months?\b",
    re.I,
)


def requested_months(text: str) -> int | None:
    """A single monthly term the caller requests. Leave ambiguous phrasing to dialogue."""
    text = text.lower()
    if re.search(r"\b(no|not|less|more|than|ago|late|behind|instead of|hundred)\b|n't", text):
        return None
    matches = list(_MONTH_COUNT.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    if text[: match.start()].strip() and not re.search(
        r"\b(pay|payment|installments?|over|in|want|like|prefer)\b", text
    ):
        return None
    token, unit = match.group("count", "unit")
    if unit and token not in _TENS:
        return None
    count = int(token) if token.isdigit() else _COUNTS[token]
    return (count + (_UNIT[unit] if unit else 0)) or None
