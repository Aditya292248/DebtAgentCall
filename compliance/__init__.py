"""``compliance/`` — the legal rules, as plain Python checks.

Each rule is a function the harness calls before it acts. The model can't talk
its way past one, because the check never reads the model's words: it looks at
the facts (the time, the debtor's location, the call history, the flags) and
returns a ``Decision``. ``check_before_call`` runs all the pre-call checks.

Nothing here is wired into the agent yet.
"""

from __future__ import annotations

from compliance.attempts import Attempt, check_call_frequency
from compliance.consent import PhoneConsent, check_consent
from compliance.decision import Decision, allow, block
from compliance.flags import Flag, check_cease, check_dnc
from compliance.hours import Contact, check_calling_hours
from compliance.precall import CallFacts, check_before_call, may_call
from compliance.timezones import zones_for
from compliance.utterance import SpeechState, check_sentence, split_sentences

__all__ = [
    "Attempt",
    "CallFacts",
    "Contact",
    "Decision",
    "Flag",
    "PhoneConsent",
    "SpeechState",
    "allow",
    "block",
    "check_before_call",
    "check_call_frequency",
    "check_calling_hours",
    "check_cease",
    "check_consent",
    "check_dnc",
    "check_sentence",
    "may_call",
    "split_sentences",
    "zones_for",
]
