"""Stop triggers: spot what the caller says that legally ends the call.

What it checks: each kind fires on a few phrasings, with the right flag,
close line and escalation. Lines that must NOT fire ("I don't want to stop
paying", "the bank is not my favourite") stay quiet, and look-alikes that
must fire ("please stop calling me at work") do. A lawyer, a bankruptcy or a
death in what the caller says fires nothing: those rules are not handled in
this version. Several triggers in one line come back in priority order.
``combine`` is a union. All offline, no model or database.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from compliance.triggers import CLOSES, FLAGS, PRIORITY, Trigger, combine, detect

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"


def _kinds(text: str) -> list[str]:
    return [t.kind for t in detect(text)]


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("cease", "Stop calling me."),
        ("cease", "Don't call me again."),
        ("cease", "Never call this number again!"),
        ("cease", "Take me off your list."),
        ("cease", "Please remove my number from your calling list."),
        ("dispute", "That's not my debt."),
        ("dispute", "I don't owe you anything."),
        ("dispute", "I dispute this."),
        ("recording_objection", "Don't record this call."),
        ("recording_objection", "I don't consent to recording."),
        ("recording_objection", "I do not want to be recorded."),
        ("wrong_number", "You have the wrong number."),
        ("wrong_number", "There's no one here by that name."),
        ("wrong_number", "Sam doesn't live here."),
    ],
)
def test_each_kind_fires(kind: str, text: str) -> None:
    assert kind in _kinds(text)


def test_ignores_case_and_curly_apostrophes() -> None:
    assert _kinds("STOP CALLING ME") == ["cease"]
    assert _kinds("Don’t call me again") == ["cease"]
    assert _kinds("i  dont   owe this") == ["dispute"]


def test_trigger_fields() -> None:
    (t,) = detect("Stop calling me.")
    assert t == Trigger(
        kind="cease",
        matched="Stop calling",
        flag="cease",
        close=CLOSES["cease"],
        escalate=False,
    )


@pytest.mark.parametrize(
    ("kind", "flag", "escalate"),
    [
        ("cease", "cease", False),
        ("dispute", None, True),
        ("recording_objection", None, False),
        ("wrong_number", None, False),
    ],
)
def test_flag_and_escalate_per_kind(kind: str, flag: str | None, escalate: bool) -> None:
    samples = {
        "cease": "stop calling",
        "dispute": "not my debt",
        "recording_objection": "don't record me",
        "wrong_number": "wrong number",
    }
    (t,) = detect(samples[kind])
    assert (t.kind, t.flag, t.escalate) == (kind, flag, escalate)


def test_flags_are_known_flag_kinds() -> None:
    # Read straight from the check constraint on flags.kind, so a rename in SQL
    # fails here instead of at the first insert.
    sql = (MIGRATIONS / "20261103130000_ledger_and_flags.sql").read_text(encoding="utf-8")
    m = re.search(r"check \(kind in \(([^)]*)\)\)", sql)
    assert m, "flags.kind check constraint not found"
    known = set(re.findall(r"'(\w+)'", m.group(1)))
    assert "cease" in known
    assert set(FLAGS) == set(PRIORITY) == set(CLOSES)
    assert {f for f in FLAGS.values() if f is not None} <= known


def test_closes_never_name_the_debt() -> None:
    # A close may be said to the wrong person, so no debt words, no money.
    for kind in PRIORITY:
        close = CLOSES[kind].lower()
        for word in ("debt", "owe", "balance", "$", "collect", "payment"):
            assert word not in close, (kind, word)


# --- everyday phrasings ----------------------------------------------------
# How people actually say it. Edit a pattern and this table shows what moved.


@pytest.mark.parametrize(
    ("text", "kinds"),
    [
        ("I don't want any more calls.", ["cease"]),
        ("I don't want you calling me anymore.", ["cease"]),
        ("Quit phoning me.", ["cease"]),
        ("I revoke my consent.", ["cease"]),
        ("That's not mine.", ["dispute"]),
        ("I never owed anything.", ["dispute"]),
        ("I don't recognize this debt.", ["dispute"]),
        ("You've got the wrong guy.", ["wrong_number"]),
        ("I don't know anyone named Sam.", ["wrong_number"]),
        ("I'm asking you not to call me.", ["cease"]),
        ("I want this to stop, no more contact.", ["cease"]),
        ("Stop with the calls.", ["cease"]),
        ("Lose my number.", ["cease"]),
        ("I object to being recorded.", ["recording_objection"]),
        ("I paid that off years ago.", ["dispute"]),
        ("Nobody named Sam lives here.", ["wrong_number"]),
        ("Wrong #", ["wrong_number"]),
        # Must stay quiet.
        ("The deadline has passed.", []),
        ("She passed me the phone.", []),
        ("He passed the exam.", []),
        ("My son passed his driving test.", []),
        ("I don't recognize your number, who is this?", []),
        ("I recognize the debt.", []),
        ("I don't want any problems.", []),
        ("Is this recorded?", []),
        ("I'll call you back.", []),
        ("Can I call you back later?", []),
        ("I won't stop paying.", []),
        ("No records of that.", []),
    ],
)
def test_everyday_phrasings(text: str, kinds: list[str]) -> None:
    assert _kinds(text) == kinds


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Don't contact my mother.", "cease"),
        ("I don't text much.", "cease"),
        ("I'm not disputing it, I just can't pay.", "dispute"),
        ("No, I'm not recording anything.", "recording_objection"),
    ],
)
def test_accepted_false_alarms(text: str, kind: str) -> None:
    # Known and accepted: these end the call politely though they shouldn't.
    # Catching too much is the safe side; a later classifier may narrow them.
    assert kind in _kinds(text)


# --- must NOT fire ---------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I don't want to stop paying.",
        "The bank is not my favourite.",
        "Yes, this is Sam. How can I help?",
        "Can we set up a payment plan?",
        "What's my balance?",
        "I paid my phone bill already.",
        "Call me back after five.",
    ],
)
def test_ordinary_lines_do_not_fire(text: str) -> None:
    assert detect(text) == []


# --- look-alikes that MUST fire --------------------------------------------


def test_stop_calling_me_at_work_fires_cease() -> None:
    assert _kinds("Please stop calling me at work.") == ["cease"]


@pytest.mark.parametrize(
    "text",
    [
        "I have a lawyer.",
        "Talk to my attorney about it.",
        "I'm represented by counsel.",
        "I filed for bankruptcy in March.",
        "We're in Chapter 13.",
        "He passed away last month.",
        "She died in June.",
        "My father is deceased.",
    ],
)
def test_lawyer_bankruptcy_and_death_fire_nothing(text: str) -> None:
    # Negative case: these rules were removed, so the call just goes on.
    assert detect(text) == []


def test_removed_kinds_are_gone_from_every_table() -> None:
    for kind in ("attorney", "bankruptcy", "deceased"):
        assert kind not in PRIORITY and kind not in FLAGS and kind not in CLOSES


def test_stop_calling_still_fires_when_a_lawyer_is_mentioned() -> None:
    assert _kinds("Stop calling me, my lawyer says so.") == ["cease"]


# --- several at once -------------------------------------------------------


def test_several_triggers_come_back_in_priority_order() -> None:
    text = "Wrong number, and this isn't my debt. Stop calling me."
    assert _kinds(text) == ["cease", "dispute", "wrong_number"]


def test_priority_order_does_not_depend_on_word_order() -> None:
    a = _kinds("Wrong number, and don't record this.")
    b = _kinds("Don't record this, and wrong number.")
    assert a == b == ["recording_objection", "wrong_number"]


def test_each_kind_at_most_once() -> None:
    assert _kinds("Stop calling. Stop calling. Don't call me.") == ["cease"]


# --- combine ---------------------------------------------------------------


def test_combine_is_a_union_in_priority_order() -> None:
    regex = detect("stop calling me")
    classifier = [Trigger("dispute", "(classifier)", None, CLOSES["dispute"], True)]
    assert [t.kind for t in combine(regex, classifier)] == ["cease", "dispute"]


def test_combine_keeps_the_regex_hit_when_both_fire() -> None:
    regex = detect("stop calling me")
    classifier = [Trigger("cease", "(classifier)", "cease", CLOSES["cease"], False)]
    (t,) = combine(regex, classifier)
    assert t.matched == "stop calling"


def test_combine_drops_unknown_kinds_and_handles_empty() -> None:
    assert combine([], []) == []
    assert combine([], [Trigger("made_up", "x", None, "", False)]) == []
