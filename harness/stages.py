"""Call stages: the required order of every call, decided in code.

``next_stage(state, caller_text, debtor)`` is a pure function. It looks at
where the call is and what the caller just said, and returns the new state plus
either a fixed line for code to speak, or ``None``, which means "let the model
answer, with this stage's instructions".

    GREETING     First turn: ask for the person by full name, nothing else.
                 "Yes, speaking" -> VERIFY. "He's not here" -> THIRD_PARTY.
                 "Wrong number" -> WRAP. Anything else: the model answers.
    VERIFY       Ask for date of birth and ZIP and compare them with the
                 database here, in code (FDCPA 805(b): no debt talk with anyone
                 but the debtor). They may come in one turn or two. A match
                 -> DISCLOSE. Two full answers that don't match -> WRAP, with a
                 polite close and nothing about a debt.
    DISCLOSE     Fixed text: recording notice, "automated assistant", and the
                 mini-Miranda (FDCPA 807(11)). Then DISCUSS. It never waits for
                 the caller, so it is said in the same turn the match happened.
    DISCUSS      The model talks; every sentence still goes through the filter.
    THIRD_PARTY  Fixed question: a good time to reach the person. Their answer
                 gets a fixed goodbye -> WRAP. A question back goes to the model.
    WRAP         The call is ending. Fixed closes also hang up.
    STOPPED      Set by stop triggers (a later change). Fixed close only.

The text matching is kept small and cautious: when unsure, the call does not
move forward. A "yes" with a "no" or "not" in it is not a yes. The full list of
stop phrases (lawyer, bankruptcy, "stop calling") comes in a separate change.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from pathlib import Path

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
WRONG_NUMBER_CLOSE = "Sorry to bother you. Goodbye."
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
    return None, text


def find_zip(text: str) -> str | None:
    m = _ZIP.search(text)
    return m.group(1) if m else None


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
        if said_yes(text, debtor):
            return replace(state, stage=Stage.VERIFY), VERIFY_ASK
        return state, None  # "Who is this?": the model answers, still in GREETING

    if state.stage == Stage.VERIFY:
        dob, rest = find_dob(text)
        zip_code = find_zip(rest)
        if dob is None and zip_code is None:
            return state, None  # "Why do you need that?": the model answers
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


def _disclose(state: CallState, names: dict[str, str]) -> tuple[CallState, str]:
    """Say the disclosure, then move on to talking about the debt."""
    if not state.verified:  # can't happen through next_stage; refuse rather than leak
        return replace(state, stage=Stage.WRAP), VERIFY_FAILED
    return replace(state, disclosed=True, stage=Stage.DISCUSS), DISCLOSURE.format(**names)


def instructions_path(stage: Stage) -> Path:
    """The file of instructions the model gets in ``stage``, after ``system.md``."""
    return STAGES_DIR / f"{stage.value.lower()}.md"
