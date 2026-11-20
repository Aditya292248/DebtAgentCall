"""Stop triggers: spot what the caller says that legally ends the call.

``detect(caller_text)`` runs on what the caller said, before the model sees it.
Each hit is a ``Trigger``: which kind it is, the words that matched, which
flag to set, the fixed polite line to close with, and whether a person must
look at it (escalate).

| kind                  | caller says, for example              | flag         | escalate |
| --------------------- | ------------------------------------- | ------------ | -------- |
| `cease`               | "stop calling", "take me off your list" | `cease`    | no       |
| `dispute`             | "that's not my debt", "I don't owe"   | none         | yes      |
| `recording_objection` | "don't record this"                   | none         | no       |
| `wrong_number`        | "wrong number", "no one by that name" | none         | no       |

Choices:
  - ``cease`` sets the ``cease`` flag, not ``opt_out_phone``. "Stop calling"
    may mean this number only, but ``cease`` stops every call and
    ``check_cease`` blocks on either, so the stricter one is the safe guess.
    It also counts as revoking robocall consent (TCPA), so the webhook sets
    ``phone.revoked_at`` on the number it called too.
  - ``dispute`` sets no flag. A dispute is not a ``flags.kind``; it is the
    ``disputed`` status of the debt, which the webhook sets, so the dialer
    (which only calls ``active`` debts) stops until someone reviews it. Only
    after the person is verified: before that the webhook changes nothing and
    says a neutral goodbye, since a stranger's "it isn't mine" is no dispute.
  - ``wrong_number`` and ``recording_objection`` set no flag either: no flag
    kind fits, and neither one stops future calls to the right person.

Matching ignores case and works on whole words. It is deliberately eager: a
false alarm only ends the call politely, a miss can break the law. So
"don't call me" fires even inside "please don't call me at work".

Several triggers can fire at once. They come back in ``PRIORITY`` order, so
the first one's ``close`` is the line to say.

``combine(regex_hits, classifier_hits)`` merges these hits with those of a
later small classifier: a trigger fires if either one finds it.

The webhook (``phone/webhook.py``) runs ``detect`` on every caller turn
before the model, saves the flag, sets ``phone.revoked_at`` for ``cease``,
writes an audit row and ends the call with the first hit's ``close``. This
module itself writes nothing. This is not legal advice.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Trigger:
    kind: str
    matched: str  # the caller's words that fired it
    flag: str | None  # the flags.kind to set, or None
    close: str  # the fixed line the agent says before hanging up
    escalate: bool  # a person must follow up


# Most important first: what the caller asks for, then a mixed-up call.
PRIORITY: tuple[str, ...] = (
    "cease",
    "dispute",
    "recording_objection",
    "wrong_number",
)

_NOT = r"(?:don'?t|do not|never)"
_CALL = r"(?:call|calling|calls|contact|contacting|phone|phoning|ring|ringing|text|texting)"

_PATTERNS: dict[str, list[str]] = {
    "cease": [
        rf"\b(?:stop|quit|cease)\s+(?:all\s+)?{_CALL}\b",
        rf"\b{_NOT}\s+(?:ever\s+)?{_CALL}\b",
        rf"\bnot to (?:ever )?{_CALL}\b",
        r"\bno more contact\b",
        r"\bstop with (?:the|these|all the) calls\b",
        r"\blose my (?:number|name)\b",
        r"\b(?:take|remove|put)\s+(?:me|my (?:name|number))\s+(?:off|from|on)\s+"
        r"(?:your|the|this)\s+(?:\w+\s+)?list\b",
        r"\bdo[- ]not[- ]call list\b",
        r"\b(?:no|any) ?more (?:calls|calling)\b",
        r"\b(?:don'?t|do not) want (?:any |further |these )?(?:calls|calling)\b",
        rf"\b(?:don'?t|do not) want (?:you|anyone|anybody|y'all) (?:to )?(?:keep )?{_CALL}\b",
        r"\bleave me alone\b",
        r"\b(?:stop|quit) (?:bothering|harassing|contacting) me\b",
        r"\b(?:revoke|withdraw)\b[^.?!]{0,20}\bconsent\b",
        r"\b(?:opt|opting) out\b",
        r"\bunsubscribe\b",
    ],
    "dispute": [
        r"\bnot (?:my|our) (?:debt|bill|account|loan|card|balance)\b",
        r"(?:\bisn'?t|\bis not|\bain'?t|'s not|\bwasn'?t|\bwas not) (?:mine|my debt)\b",
        r"\b(?:i|we)\s+(?:don'?t|do not|never|didn'?t|did not)\s+owed?\b",
        # Needs a debt word: "I don't recognize your number" is just a greeting.
        r"\b(?:don'?t|do not|didn'?t|did not) recogni[sz]e (?:this|that|the|these|those|any)"
        r" (?:debts?|accounts?|charges?|bills?|balances?|amounts?|loans?|cards?|company"
        r"|creditor)\b",
        r"\bowe (?:you |them |anybody |anyone )?nothing\b",
        r"\bdisput(?:e|es|ed|ing)\b",
        r"\bnever had (?:an?|this|that|any) (?:account|loan|card)\b",
        r"\balready paid (?:this|that|it|off)\b",
        r"\bpaid (?:this|that|it) off\b",
    ],
    "recording_objection": [
        r"\bobject(?:s|ing)?\b[^.?!]{0,20}\brecord(?:ed|ing)?\b",
        r"\b(?:don'?t|do not|stop|quit|no)\s+record(?:ing)?\b",
        r"\b(?:not|don'?t|do not)\b[^.?!]{0,30}\brecord(?:ed|ing)?\b",
        r"\bturn off (?:the )?record(?:ing|er)\b",
    ],
    "wrong_number": [
        r"\bwrong (?:number|person|house|guy|man|woman|lady|people)\b",
        r"\bwrong #",
        r"\bno ?(?:one|body) (?:here )?(?:named|called) \w+ (?:lives|is) here\b",
        r"\b(?:don'?t|do not) know (?:any ?one|anybody|someone|a) (?:named|called|by)\b",
        r"\bno ?(?:one|body) (?:here )?(?:by|with|named|called) (?:that|the|this) name\b",
        r"\b(?:doesn'?t|does not|don'?t|do not) live here\b",
        r"\bnever heard of (?:him|her|them|that person)\b",
        r"\bthere'?s no \w+ here\b",
    ],
}

_COMPILED: dict[str, list[re.Pattern[str]]] = {
    kind: [re.compile(p, re.IGNORECASE) for p in patterns] for kind, patterns in _PATTERNS.items()
}

FLAGS: dict[str, str | None] = {
    "cease": "cease",
    "dispute": None,
    "recording_objection": None,
    "wrong_number": None,
}

_ESCALATE = frozenset({"dispute"})

# Fixed closing lines. None of them names the debt, the amount or the
# creditor, so they are safe to say even to the wrong person.
CLOSES: dict[str, str] = {
    "cease": "Understood. We'll stop calling you. Goodbye.",
    "dispute": "Thank you. I've noted that you dispute this, and someone will follow up "
    "in writing. Goodbye.",
    "recording_objection": "Understood. I'll end the call here, and we can reach you by mail "
    "instead. Goodbye.",
    "wrong_number": "Sorry to have bothered you. Goodbye.",
}


def _normalize(text: str) -> str:
    """Straight apostrophes and single spaces, so "don’t" and "don't" match alike."""
    text = text.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip()


def _trigger(kind: str, matched: str) -> Trigger:
    return Trigger(
        kind=kind,
        matched=matched,
        flag=FLAGS[kind],
        close=CLOSES[kind],
        escalate=kind in _ESCALATE,
    )


def detect(caller_text: str) -> list[Trigger]:
    """Every trigger in what the caller said, in ``PRIORITY`` order. Empty if none."""
    text = _normalize(caller_text)
    hits: list[Trigger] = []
    for kind in PRIORITY:
        for pattern in _COMPILED[kind]:
            m = pattern.search(text)
            if m:
                hits.append(_trigger(kind, m.group(0)))
                break
    return hits


def combine(regex_hits: Iterable[Trigger], classifier_hits: Iterable[Trigger]) -> list[Trigger]:
    """Union of both lists, one per kind, in ``PRIORITY`` order.

    A kind fires if either side found it. When both did, the regex hit is kept,
    since its ``matched`` words are exact. A kind not in ``PRIORITY`` is dropped:
    there is no close line for it.
    """
    by_kind: dict[str, Trigger] = {}
    for t in [*regex_hits, *classifier_hits]:
        by_kind.setdefault(t.kind, t)
    return [by_kind[kind] for kind in PRIORITY if kind in by_kind]
