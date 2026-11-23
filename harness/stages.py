"""Call stages: the required order of every call, decided in code.

``next_stage(state, caller_text, debtor)`` is a pure function. It looks at
where the call is and what the caller just said, and returns the new state plus
either a fixed line for code to speak, or ``None``, which means "let the model
answer, with this stage's instructions".

    GREETING     First turn: ask for the person by full name, nothing else.
                 "Yes, speaking" -> VERIFY. "He's not here" -> THIRD_PARTY.
                 "Wrong number" -> WRAP. For questions and fragments, the model
                 responds in the current stage.
    VERIFY       Ask for date of birth and ZIP and compare them with the
                 database here, in code (FDCPA 805(b): no debt talk with anyone
                 but the debtor). They may come in one turn or two. A match
                 -> DISCLOSE. Two full answers that don't match -> WRAP, with a
                 polite close and nothing about a debt. Answers may be spoken
                 in words ("the fifteenth of January nineteen ninety", "zero
                 two one one five"). A turn with no answer in it is not an
                 attempt. The model responds using the remaining field names.
    DISCLOSE     Fixed text: recording notice, "automated assistant", and the
                 mini-Miranda (FDCPA 807(11)). Then DISCUSS. It never waits for
                 the caller, so it is said in the same turn the match happened.
                 The webhook adds ``purpose_line`` after it: why we called, with
                 the creditor and balance from the debt's database row, so the
                 call does not open with "How can I help you?".
    DISCUSS      The model talks; every sentence still goes through the filter.
    THIRD_PARTY  Fixed question: a good time to reach the person. Their answer
                 gets a fixed goodbye -> WRAP. A question back goes to the model.
    WRAP         The call is ending. Fixed closes also hang up.
    STOPPED      Set by the stop triggers, which the webhook runs before this
                 function (``compliance/triggers.py``). Fixed close only.

The text matching is kept small and cautious: when unsure, the call does not
move forward. A spoken date that could be read two ways, or is not a real date,
is no answer at all, never a match. A "yes" with a "no" or "not" in it is not a
yes. The full list of stop phrases ("stop calling", a dispute) is
``compliance/triggers.py``. Dates and ZIP codes are read by ``harness/spoken.py``.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from compliance.triggers import CLOSES
from harness.call_state import CallState, Debtor, DebtRow, Stage, money
from harness.spoken import find_dob, find_zip, requested_months  # also imported from here by tests

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
    "obtained will be used for that purpose."
)
# Said right after the disclosure, in the same turn. We placed the call, so we say
# why; the creditor and balance come from the database row, never from the model.
# No "today" or "now": the sentence filter's overshadowing rule would block that.
PURPOSE = (
    "I'm calling about your account with {creditor}, which has a balance of {balance}. "
    "Would you like to pay it in full, or would a payment plan work better for you?"
)
# A disputed, paused or closed debt: no amount and no payment ask.
PURPOSE_ON_HOLD = (
    "I'm calling about your account with {creditor}. I'm not asking for a payment on this call."
)
# The debt row could not be read, or doesn't match what this call was set up with:
# no amount, nothing for the model to fill in, and the call ends.
PURPOSE_UNREAD = (
    "I'm sorry, I can't bring up your account details right now, so I'll end the call "
    "here. Thank you for your time. Goodbye."
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
        if not _CUT_OFF.search(text) and said_yes(text, debtor):
            return replace(state, stage=Stage.VERIFY), VERIFY_ASK
        return state, None

    if state.stage == Stage.VERIFY:
        dob, rest = find_dob(text)
        zip_code = find_zip(rest)
        if dob is None and zip_code is None:  # no answer, so not an attempt either
            return replace(state, verification_prompts=state.verification_prompts + 1), None
        # Keep only whether each answer matched (never the answer itself), so the
        # caller can give the two in separate turns.
        state = replace(state, verification_prompts=0)
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

    if (
        state.stage == Stage.DISCUSS
        and state.verified
        and state.disclosed
        and state.plan_discussion
    ):
        months = requested_months(text)
        if months is not None:
            # Remember an explicit numeric constraint for the payment tool. The model
            # chooses what to say using the conversation, even on the first request.
            state = replace(state, requested_months=months)
    return state, None  # DISCUSS and WRAP: the model answers


def _disclose(state: CallState, names: dict[str, str]) -> tuple[CallState, str]:
    """Say the disclosure, then move on to talking about the debt."""
    if not state.verified:  # can't happen through next_stage; refuse rather than leak
        return replace(state, stage=Stage.WRAP), VERIFY_FAILED
    return replace(state, disclosed=True, stage=Stage.DISCUSS), DISCLOSURE.format(**names)


def purpose_line(debtor: Debtor, row: DebtRow | None) -> tuple[str, bool]:
    """What to say after the disclosure, and whether the call ends there.

    ``row`` is the debt read again from the database (None: the read failed). The
    amount is said only if the row agrees with what the call tools and the sentence
    filter know (``debtor``), so the caller never hears a balance they can't repeat.
    """
    if (
        row is None
        or not isinstance(row.balance_cents, int)
        or not row.creditor.strip()
        or (row.creditor, row.balance_cents) != (debtor.creditor, debtor.balance_cents)
    ):
        return PURPOSE_UNREAD, True
    if row.status != "active" or row.balance_cents <= 0:
        return PURPOSE_ON_HOLD.format(creditor=row.creditor), False
    return PURPOSE.format(creditor=row.creditor, balance=f"${money(row.balance_cents)}"), False


def instructions_path(stage: Stage) -> Path:
    """The file of instructions the model gets in ``stage``, after ``system.md``."""
    return STAGES_DIR / f"{stage.value.lower()}.md"
