"""Call stages: the required order of every call, decided in code.

``next_stage(state, caller_text, debtor)`` is a pure function. It looks at
where the call is and what the caller just said, and returns the new state plus
either a fixed line for code to speak, or ``None``, which means "let the model
answer, with this stage's instructions".

    GREETING     First turn: ask for the person by full name, nothing else.
                 "Yes, speaking" -> VERIFY. "He's not here" -> THIRD_PARTY.
                 "Wrong number" -> WRAP. A question ("Who is this?") goes to
                 the model; anything else, such as a cut-off "Yeah. This is",
                 gets a fixed re-ask and no model call.
    VERIFY       Ask for date of birth and ZIP and compare them with the
                 database here, in code (FDCPA 805(b): no debt talk with anyone
                 but the debtor). They may come in one turn or two. A match
                 -> DISCLOSE. Two full answers that don't match -> WRAP, with a
                 polite close and nothing about a debt. Answers may be spoken
                 in words ("the fifteenth of January nineteen ninety", "zero
                 two one one five"). A turn with no answer in it is not an
                 attempt: a question goes to the model, anything else gets a
                 fixed re-ask.
    DISCLOSE     Fixed text: recording notice, "automated assistant", and the
                 mini-Miranda (FDCPA 807(11)). Then DISCUSS. It never waits for
                 the caller, so it is said in the same turn the match happened.
    DISCUSS      The model talks; every sentence still goes through the filter.
    THIRD_PARTY  Fixed question: a good time to reach the person. Their answer
                 gets a fixed goodbye -> WRAP. A question back goes to the model.
    WRAP         The call is ending. Fixed closes also hang up.
    STOPPED      Set by the stop triggers, which the webhook runs before this
                 function (``compliance/triggers.py``). Fixed close only.

The text matching is kept small and cautious: when unsure, the call does not
move forward. A spoken date that could be read two ways, or is not a real date,
is no answer at all, never a match. A "yes" with a "no" or "not" in it is not a
yes. The full list of stop phrases (lawyer, bankruptcy, "stop calling") is
``compliance/triggers.py``.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from pathlib import Path

from compliance.triggers import CLOSES
from harness.call_state import CallState, Debtor, Stage

STAGES_DIR = Path(__file__).resolve().parent.parent / "instructions" / "stages"
MAX_VERIFY_ATTEMPTS = 2
ENDING = (Stage.WRAP, Stage.STOPPED)  # a fixed line in these stages also hangs up

# --- the fixed lines (code speaks these; the model never writes them) ----------

GREETING = "Hi, may I please speak with {full_name}?"
VERIFY_ASK = (
    "Thank you. Before we go on, I need to make sure I'm speaking with the right person. "
    "Could you tell me your date of birth and ZIP code?"
)
VERIFY_RETRY = (
    "Sorry, that doesn't match what I have. "
    "Could you tell me your date of birth and ZIP code again?"
)
ASK_DOB = "Thanks. And your date of birth?"
ASK_ZIP = "Thanks. And your ZIP code?"
# Said when a turn has nothing code can use (speech-to-text often sends half a
# sentence as its own turn). None of them says anything was checked.
GREETING_REASK = "Sorry, I didn't catch that. Am I speaking with {full_name}?"
VERIFY_REASK = "Sorry, I didn't catch that. Could you tell me your date of birth and ZIP code?"
REASK_DOB = "Sorry, I didn't catch that. Could you tell me your date of birth?"
REASK_ZIP = "Sorry, I didn't catch that. Could you tell me your ZIP code?"
VERIFY_FAILED = (
    "I'm sorry, I wasn't able to confirm who I'm speaking with, so I can't go any further "
    "today. Thank you for your time. Goodbye."
)
DISCLOSURE = (
    "Thank you, {first_name}. This call is being recorded. "
    "You're speaking with an automated assistant. "
    "This is a debt collector. This is an attempt to collect a debt, and any information "
    "obtained will be used for that purpose. How can I help you today?"
)
LOCATION_ASK = "Okay, thank you. Is there a good time to reach {full_name}?"
THIRD_PARTY_CLOSE = "Thank you for your help. Goodbye."
WRONG_NUMBER_CLOSE = CLOSES["wrong_number"]  # one wrong-number goodbye, wherever it's caught
STOPPED_CLOSE = "Thank you. Goodbye."

# --- what the caller said -----------------------------------------------------


def _any(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(rf"(?:{p})" for p in patterns), re.IGNORECASE)


_WRONG_NUMBER = _any(
    r"\bwrong number\b",
    r"\b(?:nobody|no one|no-one)\b[^.?!]*\bby that name\b",
    r"\bdoesn'?t live here\b",
    r"\bnever heard of\b",
)
_NOT_HERE = _any(
    r"\b(?:not|isn'?t|aren'?t)\s+(?:here|home|in|around|available)\b",
    r"\bwrong person\b",
    r"\bnot me\b",
    r"^\W*(?:no|nope)\b",
)
_YES = _any(
    r"\b(?:yes|yeah|yep|yup|speaking)\b",
    r"\bthat'?s me\b",
    r"\bit'?s me\b",
    r"\bthis is (?:he|she|they|him|her|me)\b",
)
_NEGATION = _any(r"\b(?:no|nope|not|isn'?t|wrong|nobody|never)\b", r"n't\b")
_QUESTION = _any(r"\?", r"^\W*(?:who|what|why|where|which|how)\b")
# A turn that stops mid-sentence: "Yeah. This is", "My birthday is the", "Yes, and".
# Only endings no finished answer has ("Yes I am" and "What's it for?" are finished).
_CUT_OFF = re.compile(
    r"\b(?:this is|it'?s|i'?m|name is|birthday is|zip is|code is|my|the|an?|and|or|but|"
    r"um+|uh+|er)[^\w?]*$",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
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


# --- spoken numbers: what speech-to-text sends ("zero two one one five") ---------

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
_MONTH_WORDS = (
    "january february march april may june july august september october november december"
)
_MONTH_NAMES = {
    name: i + 1 for i, full in enumerate(_MONTH_WORDS.split()) for name in (full, full[:3])
} | {"sept": 9}
_TOKEN = re.compile(r"[a-z]+(?:'[a-z]+)?|\d+[a-z]*")
_DIGIT_DAY = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?")

Words = list[str]


def _at(words: Words, i: int) -> str:
    return words[i] if 0 <= i < len(words) else ""


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


def _two_digits(words: Words, i: int) -> tuple[int, int] | None:
    """1 to 99 in words: "four", "fifteen", "ninety", "twenty three"."""
    w = _at(words, i)
    if w in _TENS:
        if (nxt := _at(words, i + 1)) in _UNIT:
            return _TENS[w] + _UNIT[nxt], i + 2
        return _TENS[w], i + 1
    for table in (_TEEN, _UNIT):
        if w in table:
            return table[w], i + 1
    return None


UNCLEAR_YEAR = -1  # a year whose last digit might be the first digit of the ZIP


def _digit(w: str) -> str | None:
    """One digit said as a word or written: "oh", "zero", "five", "5"."""
    if w in _ZERO:
        return "0"
    return str(_UNIT[w]) if w in _UNIT else (w if re.fullmatch(r"\d", w) else None)


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


def _year(words: Words, glued: frozenset[int], i: int) -> tuple[int, int] | None:
    """A whole year at ``words[i]``: "1990", "nineteen ninety", "two thousand (and)
    four", "twenty oh four". A bare "ninety" is not a year. The year is
    ``UNCLEAR_YEAR`` when its last digit might belong to the ZIP."""
    w, nxt = _at(words, i), _at(words, i + 1)
    if re.fullmatch(r"\d{4}", w):
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


def _spoken_dates(
    words: Words, glued: frozenset[int], i: int
) -> list[tuple[tuple[int, int, int], int]]:
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
        if not re.search(r"[,.;:!?]", text[toks[k - 1].end() : toks[k].start()])
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


def _part(value: str) -> int:
    return _MONTHS[value[:3].lower()] if value[:1].isalpha() else int(value)


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


def said_yes(text: str, debtor: Debtor) -> bool:
    """A clear "that's me": a yes word or their first name after "this is", and no "no"."""
    first = re.escape(debtor.first_name)
    names_self = re.search(rf"\b(?:this is|i am|i'm|it's)\s+{first}\b", text, re.IGNORECASE)
    return bool(_YES.search(text) or names_self) and not _NEGATION.search(text)


# --- the transition ------------------------------------------------------------


def next_stage(state: CallState, caller_text: str, debtor: Debtor) -> tuple[CallState, str | None]:
    """Where the call goes after ``caller_text``, and the fixed line to say, if any."""
    text = caller_text.replace("’", "'").strip()
    first_turn = state.turns == 0
    state = replace(state, turns=state.turns + 1)
    names = {"full_name": debtor.full_name, "first_name": debtor.first_name}

    if state.stage == Stage.STOPPED:
        return state, STOPPED_CLOSE

    if state.stage == Stage.GREETING:
        if first_turn:
            return state, GREETING.format(**names)
        if _WRONG_NUMBER.search(text):
            return replace(state, stage=Stage.WRAP), WRONG_NUMBER_CLOSE
        if _NOT_HERE.search(text):
            return replace(state, stage=Stage.THIRD_PARTY), LOCATION_ASK.format(**names)
        if _CUT_OFF.search(text):
            return state, GREETING_REASK.format(**names)  # "Yeah. This is": half a sentence
        if said_yes(text, debtor):
            return replace(state, stage=Stage.VERIFY), VERIFY_ASK
        if _QUESTION.search(text):
            return state, None  # "Who is this?": the model answers, still in GREETING
        return state, GREETING_REASK.format(**names)  # "could": nothing to go on

    if state.stage == Stage.VERIFY:
        dob, rest = find_dob(text)
        zip_code = find_zip(rest)
        if dob is None and zip_code is None:  # no answer, so not an attempt either
            if _QUESTION.search(text) and not _CUT_OFF.search(text):
                return state, None  # "Why do you need that?": the model answers
            return state, _verify_reask(state)  # "zero two one", "hold on": ask again
        # Keep only whether each answer matched (never the answer itself), so the
        # caller can give the two in separate turns.
        if dob is not None:
            state = replace(state, dob_ok=dob == debtor.dob)
        if zip_code is not None:
            state = replace(state, zip_ok=zip_code == debtor.zip.strip())
        if state.dob_ok is None:
            return state, ASK_DOB  # no hint yet whether the ZIP was right
        if state.zip_ok is None:
            return state, ASK_ZIP
        if state.dob_ok and state.zip_ok:
            return _disclose(replace(state, verified=True, stage=Stage.DISCLOSE), names)
        attempts = state.verify_attempts + 1
        state = replace(state, verify_attempts=attempts, dob_ok=None, zip_ok=None)
        if attempts >= MAX_VERIFY_ATTEMPTS:
            return replace(state, stage=Stage.WRAP), VERIFY_FAILED
        return state, VERIFY_RETRY

    if state.stage == Stage.DISCLOSE:
        return _disclose(state, names)

    if state.stage == Stage.THIRD_PARTY:
        if _QUESTION.search(text):
            return state, None  # "Who's calling?": the model answers, no debt talk allowed
        return replace(state, stage=Stage.WRAP), THIRD_PARTY_CLOSE

    return state, None  # DISCUSS and WRAP: the model answers


def _verify_reask(state: CallState) -> str:
    """Ask again for whatever is still missing, with no word on what was said before."""
    if state.dob_ok is not None and state.zip_ok is None:
        return REASK_ZIP
    if state.zip_ok is not None and state.dob_ok is None:
        return REASK_DOB
    return VERIFY_REASK


def _disclose(state: CallState, names: dict[str, str]) -> tuple[CallState, str]:
    """Say the disclosure, then move on to talking about the debt."""
    if not state.verified:  # can't happen through next_stage; refuse rather than leak
        return replace(state, stage=Stage.WRAP), VERIFY_FAILED
    return replace(state, disclosed=True, stage=Stage.DISCUSS), DISCLOSURE.format(**names)


def instructions_path(stage: Stage) -> Path:
    """The file of instructions the model gets in ``stage``, after ``system.md``."""
    return STAGES_DIR / f"{stage.value.lower()}.md"
