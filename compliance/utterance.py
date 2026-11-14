"""The sentence filter: checks every sentence before the agent may speak it.

``check_sentence(sentence, state)`` returns a ``Decision``. A blocked sentence
must never be sent to the caller; the caller hears a safe fallback instead.
Like the other checks, this is plain Python: regexes over the text, plus a few
facts about the call in ``SpeechState``. The model can't argue with it.

Rules, checked in this order (the first one that blocks wins):

``third_party_disclosure`` (FDCPA 805(b))
    Before the person is verified, no debt words, no creditor name, no money.
``mini_miranda_first`` (FDCPA 807(11))
    After verifying, the same words stay blocked until the disclosure was said.
    The disclosure itself is fixed text spoken by the call script, not by the
    model, so it doesn't go through this filter.
``false_identity`` (FDCPA 807(1), 807(3))
    Claiming to be, or to call from, a lawyer, a court, the IRS or government.
``threat`` (FDCPA 806, 807(4), 807(5))
    Arrest, jail, police: always. Suing, garnishing, seizing: unless legal
    action is authorized for this debt.
``time_barred`` (Reg F 1006.26(b))
    Legal-action words on a time-barred debt, even when otherwise authorized.
``profanity`` (FDCPA 806(2))
    A short list of swear words.
``amount_mismatch`` (FDCPA 807(2)(A))
    Every amount spoken must be one we have on file, to the cent. Money words
    with no amount in digits ("a few hundred dollars") are blocked too, because
    they can't be checked.
``overshadowing`` (FDCPA 809(b), Reg F 1006.34)
    During the validation period, no pressure to pay now ("must pay today",
    "immediately", "final notice").

How words are matched: case-insensitive, whole words only. So ``debt`` does not
match inside "indebted", and ``owe`` does not match "Owen". Words that are
debt-related on their own ("debtor", "collector", "owed") are listed
explicitly. "Sue" is only treated as a threat in a verb phrase ("sue you",
"will sue", "suing", "sued"), so a person called Sue can still be greeted;
the cost is that an unusual phrasing of a threat to sue could slip past, which
the after-call review is meant to catch. The filter does not understand
negation: "we will not sue you" is blocked like "we will sue you". When unsure,
it blocks.

``split_sentences(text)`` cuts model output into sentences, so each one can be
checked before it is spoken. The webhook runs both on every model reply
(``phone/webhook.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from compliance.decision import Decision, allow, block

# ----------------------------------------------------------------------------
# The word lists. Each entry is a regex fragment; all are matched as whole
# words, ignoring case.
# ----------------------------------------------------------------------------

# Saying any of these tells a listener there is a debt.
DEBT_WORDS = [
    r"debts?",
    r"debtors?",
    r"collections?",
    r"collect(?:s|ed|ing)?",
    r"collectors?",
    r"balances?",
    r"owes?",
    r"owed",
    r"owing",
    r"payments?",
    r"past due",
    r"overdue",
    r"delinquent",
]

# Never allowed: consumer debt can't put anyone in jail.
CRIMINAL_THREATS = [
    r"arrest(?:s|ed|ing)?",
    r"jail",
    r"prison",
    r"police",
    r"criminal",
    r"warrant",
]

# Allowed only when legal action is authorized and the debt is not time-barred.
LEGAL_ACTION = [
    r"garnish(?:es|ed|ing|ment)?",
    r"seiz(?:e|es|ed|ing|ure)",
    r"repossess(?:es|ed|ing|ion)?",
    r"lawsuits?",
    r"legal action",
    r"take you to court",
    r"su(?:e|ed|ing)\s+(?:you|him|her|them|us)",
    r"(?:will|would|could|can|may|might|shall|going to|plan to|intend to)\s+sue",
    r"suing",
    r"sued",
]

# "I'm a lawyer", "calling from the court": pretending to be someone else.
_CLAIM = (
    r"(?:i\s+am|i'm|im|we\s+are|we're|this\s+is|it's|calling\s+from|calling\s+for|"
    r"on\s+behalf\s+of|here\s+from|work\s+for|working\s+for|with|from)"
)
_AUTHORITY = (
    r"(?:attorneys?|lawyers?|law\s+(?:office|firm)|government|irs|internal\s+revenue\s+service|"
    r"courts?|sheriff(?:'s\s+office)?|federal\s+agent|police)"
)
FALSE_IDENTITY = [
    rf"{_CLAIM}\s+(?:an?\s+|the\s+)?(?:(?:district|county|federal|state|local)\s+)?{_AUTHORITY}",
]

PROFANITY = [
    r"fuck\w*",
    r"shit\w*",
    r"bitch\w*",
    r"bastards?",
    r"assholes?",
    r"damn\w*",
    r"crap",
    r"piss\w*",
    r"hell",
]

# Pressure that could drown out the right to dispute during the validation period.
OVERSHADOWING = [
    r"immediately",
    r"right away",
    r"final notice",
    r"final warning",
    r"last chance",
    r"today only",
    r"act now",
    r"(?:must|have to|need to|got to)\s+pay",
    r"pay\s+(?:it\s+|this\s+|that\s+)?(?:in\s+full\s+)?(?:today|now|tonight)",
]

# Money: "$1,240", "$1,240.50", "1240 dollars", "1,240 dollars and 50 cents".
_NUMBER = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?"
_DOLLAR_SIGN = re.compile(r"\$\s?" + _NUMBER + r"(?!\d)")
_DOLLAR_WORD = re.compile(
    r"(?<![\d$.,])" + _NUMBER + r"\s*(?:dollars?|usd|bucks)\b(?:\s+and\s+(\d{1,2})\s+cents?\b)?",
    re.IGNORECASE,
)
_MONEY_WORD = re.compile(r"\$|\b(?:dollars?|usd|bucks|cents?)\b", re.IGNORECASE)


def _words(fragments: list[str]) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(fragments) + r")\b", re.IGNORECASE)


_DEBT_RE = _words(DEBT_WORDS)
_CRIMINAL_RE = _words(CRIMINAL_THREATS)
_LEGAL_RE = _words(LEGAL_ACTION)
_FALSE_ID_RE = _words(FALSE_IDENTITY)
_PROFANITY_RE = _words(PROFANITY)
_OVERSHADOW_RE = _words(OVERSHADOWING)


# ----------------------------------------------------------------------------
# What the filter knows about the call so far.
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SpeechState:
    """Facts about the call that decide what may be said.

    The defaults are the strictest: nobody verified, nothing disclosed, no
    amounts known, no legal action, and inside the validation period.
    """

    verified: bool = False
    disclosed: bool = False
    known_amounts_cents: set[int] = field(default_factory=set)
    creditor: str = ""
    legal_action_authorized: bool = False
    time_barred: bool = False
    in_validation_period: bool = True


def amounts_in_cents(sentence: str) -> list[int]:
    """Every dollar amount in the sentence, in cents, in the order spoken."""
    found: list[tuple[int, int]] = []
    for m in _DOLLAR_SIGN.finditer(sentence):
        found.append((m.start(), _cents(m.group(1), m.group(2))))
    for m in _DOLLAR_WORD.finditer(sentence):
        cents = _cents(m.group(1), m.group(2))
        if m.group(3):  # "and 5 cents" is 5 cents, not .50
            cents += int(m.group(3))
        found.append((m.start(), cents))
    return [cents for _, cents in sorted(found)]


def _cents(whole: str, decimals: str | None) -> int:
    """'1,240' and '5' -> 124050: a single decimal digit means tens of cents."""
    return int(whole.replace(",", "")) * 100 + int((decimals or "0").ljust(2, "0"))


def _debt_topic(sentence: str, state: SpeechState) -> str | None:
    """The first thing in the sentence that reveals a debt, or None."""
    if m := _DEBT_RE.search(sentence):
        return m.group(0)
    creditor = state.creditor.strip()
    if creditor and (m := re.search(rf"\b{re.escape(creditor)}\b", sentence, re.IGNORECASE)):
        return m.group(0)
    if m := _MONEY_WORD.search(sentence):
        return m.group(0)
    return None


def check_sentence(sentence: str, state: SpeechState) -> Decision:
    """Allow or block one sentence the agent is about to say."""
    evidence: dict[str, object] = {"sentence": sentence}
    # Models often write a curly apostrophe ("I’m"); match it like a straight one.
    sentence = sentence.replace("’", "'")

    def stop(rule_id: str, reason: str, matched: str) -> Decision:
        return block(rule_id, reason, {**evidence, "matched": matched})

    topic = _debt_topic(sentence, state)
    if topic is not None and not state.verified:
        return stop(
            "third_party_disclosure",
            f"mentions {topic!r} before the person is verified",
            topic,
        )
    if topic is not None and not state.disclosed:
        return stop(
            "mini_miranda_first",
            f"mentions {topic!r} before the debt collector disclosure",
            topic,
        )
    if m := _FALSE_ID_RE.search(sentence):
        return stop("false_identity", "claims to be a lawyer, court or government", m.group(0))
    if m := _CRIMINAL_RE.search(sentence):
        return stop("threat", "threatens arrest, jail or police", m.group(0))
    if m := _LEGAL_RE.search(sentence):
        if not state.legal_action_authorized:
            return stop("threat", "mentions legal action that is not authorized", m.group(0))
        if state.time_barred:
            return stop("time_barred", "mentions legal action on a time-barred debt", m.group(0))
    if m := _PROFANITY_RE.search(sentence):
        return stop("profanity", "uses profanity", m.group(0))

    amounts = amounts_in_cents(sentence)
    for cents in amounts:
        if cents not in state.known_amounts_cents:
            return stop(
                "amount_mismatch",
                f"${cents / 100:,.2f} is not an amount on file",
                f"{cents}",
            )
    if not amounts and (m := _MONEY_WORD.search(sentence)):
        return stop(
            "amount_mismatch", "talks about money without an amount we can check", m.group(0)
        )

    if state.in_validation_period and (m := _OVERSHADOW_RE.search(sentence)):
        return stop("overshadowing", "pressure to pay during the validation period", m.group(0))

    return allow("utterance", "passed every sentence check", evidence)


# ----------------------------------------------------------------------------
# Cutting text into sentences.
# ----------------------------------------------------------------------------

_ABBREVIATIONS = ("mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "inc", "co")
_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*(?=\s)|\n+")


def split_sentences(text: str) -> list[str]:
    """Cut text into sentences, keeping the end punctuation.

    A sentence ends at ``.``, ``!`` or ``?`` followed by a space, or at a line
    break. "$1,240.50" is not cut (no space after the dot), and neither is
    "Mr. Doe". Whatever comes after the last end mark is returned as the last
    item, so a streaming caller should hold it back until more text arrives.
    """
    sentences: list[str] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        before = text[start : m.start()].rsplit(maxsplit=1)
        last_word = before[-1].lower() if before else ""
        if m.group(0).startswith(".") and len(m.group(0)) == 1 and last_word in _ABBREVIATIONS:
            continue
        piece = text[start : m.end()].strip()
        if piece:
            sentences.append(piece)
        start = m.end()
    rest = text[start:].strip()
    if rest:
        sentences.append(rest)
    return sentences
