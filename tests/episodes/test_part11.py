"""Sentence filter: block what the agent may not say.

What it checks: ``check_sentence`` allows a greeting before verification and a
correct amount after the disclosure, and blocks each forbidden kind of
sentence with its own ``rule_id``: debt words before verification or before
the disclosure, a wrong amount, threats, legal action on a time-barred debt,
claiming to be a lawyer, profanity and pressure during the validation period.
Word-boundary cases pin down what does not misfire ("Sue" the name,
"indebted", "Owen"). ``split_sentences`` cuts text without breaking amounts
or "Mr.". All offline, no model or database.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from compliance import SpeechState, check_sentence, split_sentences
from compliance.utterance import amounts_in_cents

BEFORE_VERIFY = SpeechState(
    verified=False,
    disclosed=False,
    known_amounts_cents={124000, 12400},
    creditor="Acme Bank",
    legal_action_authorized=False,
    time_barred=False,
    in_validation_period=False,
)
VERIFIED = replace(BEFORE_VERIFY, verified=True)
DISCLOSED = replace(VERIFIED, disclosed=True)
IN_VALIDATION = replace(DISCLOSED, in_validation_period=True)


def _rule(sentence: str, state: SpeechState) -> str | None:
    """The rule that blocks the sentence, or None if it is allowed."""
    d = check_sentence(sentence, state)
    return None if d.allow else d.rule_id


# ---------------------------------------------------------------- allowed


def test_greeting_before_verification_is_allowed() -> None:
    d = check_sentence("Hi, this is Maya. May I speak with Sam Doe, please?", BEFORE_VERIFY)
    assert d.allow and d.rule_id == "utterance"


def test_correct_amount_after_disclosure_is_allowed() -> None:
    assert _rule("Your balance with Acme Bank is $1,240.", DISCLOSED) is None
    assert _rule("Your balance is 1240 dollars.", DISCLOSED) is None
    assert _rule("A first payment of $124.00 would work.", DISCLOSED) is None


def test_legal_action_allowed_when_authorized_and_not_time_barred() -> None:
    state = replace(DISCLOSED, legal_action_authorized=True)
    assert _rule("The creditor may file a lawsuit on this account.", state) is None


# ---------------------------------------------------------------- negative


def test_balance_before_verification_is_third_party_disclosure() -> None:
    d = check_sentence("Your balance is $1,240.", BEFORE_VERIFY)
    assert not d.allow
    assert d.rule_id == "third_party_disclosure"
    assert d.evidence["sentence"] == "Your balance is $1,240."


@pytest.mark.parametrize(
    "sentence",
    [
        "I'm calling about a debt.",
        "This is about your account with Acme Bank.",
        "It's about $1,240.",
        "This is a collection call.",
        "Is the debtor home?",
    ],
)
def test_debt_topics_before_verification_are_blocked(sentence: str) -> None:
    assert _rule(sentence, BEFORE_VERIFY) == "third_party_disclosure"


def test_debt_after_verify_before_disclosure_is_blocked() -> None:
    assert _rule("Thanks Sam, I'm calling about a debt.", VERIFIED) == "mini_miranda_first"
    assert _rule("Your balance is $1,240.", VERIFIED) == "mini_miranda_first"


def test_wrong_amount_is_amount_mismatch() -> None:
    d = check_sentence("Your balance is $1,250.", DISCLOSED)
    assert not d.allow and d.rule_id == "amount_mismatch"
    assert _rule("You owe 1300 dollars.", DISCLOSED) == "amount_mismatch"
    assert _rule("That's $1,240.50 today.", DISCLOSED) == "amount_mismatch"
    # One right amount doesn't excuse a wrong one in the same sentence.
    assert _rule("It was $1,240, now $1,500.", DISCLOSED) == "amount_mismatch"


def test_money_without_a_checkable_amount_is_blocked() -> None:
    assert _rule("You owe a few hundred dollars.", DISCLOSED) == "amount_mismatch"


def test_garnish_wages_is_a_threat() -> None:
    assert _rule("We will garnish your wages.", DISCLOSED) == "threat"


@pytest.mark.parametrize(
    "sentence",
    [
        "You could be arrested.",
        "We'll send the police.",
        "You could go to jail for this.",
    ],
)
def test_criminal_threats_are_blocked_even_when_legal_action_is_authorized(sentence: str) -> None:
    state = replace(DISCLOSED, legal_action_authorized=True)
    assert _rule(sentence, state) == "threat"


def test_lawsuit_without_authorization_is_a_threat() -> None:
    assert _rule("We will sue you.", DISCLOSED) == "threat"
    assert _rule("We're going to sue.", DISCLOSED) == "threat"
    assert _rule("We could seize your car.", DISCLOSED) == "threat"


def test_lawsuit_on_time_barred_debt_is_blocked() -> None:
    authorized_but_old = replace(DISCLOSED, legal_action_authorized=True, time_barred=True)
    d = check_sentence("We may file a lawsuit.", authorized_but_old)
    assert not d.allow and d.rule_id == "time_barred"
    # Without authorization it is blocked as a threat first.
    assert _rule("We may file a lawsuit.", replace(DISCLOSED, time_barred=True)) == "threat"


@pytest.mark.parametrize(
    "sentence",
    [
        "I'm an attorney.",
        "I’m a lawyer for the creditor.",
        "This is the IRS.",
        "I'm calling from the county court.",
        "We are with the government.",
    ],
)
def test_false_identity_is_blocked(sentence: str) -> None:
    assert _rule(sentence, DISCLOSED) == "false_identity"


def test_asking_about_the_debtors_attorney_is_allowed() -> None:
    assert _rule("Could you share your attorney's name and phone number?", DISCLOSED) is None


def test_profanity_is_blocked() -> None:
    assert _rule("What the hell are you doing?", DISCLOSED) == "profanity"
    assert _rule("Hello, how are you?", DISCLOSED) is None


@pytest.mark.parametrize(
    "sentence",
    [
        "You must pay today.",
        "Please pay immediately.",
        "This is your final notice.",
        "You need to pay it in full now.",
    ],
)
def test_pressure_during_validation_period_is_overshadowing(sentence: str) -> None:
    assert _rule(sentence, IN_VALIDATION) == "overshadowing"


def test_same_pressure_after_validation_period_is_allowed() -> None:
    assert _rule("Please pay immediately.", DISCLOSED) is None


# ---------------------------------------------------------------- word boundaries


def test_sue_the_name_is_not_a_threat() -> None:
    assert _rule("May I speak with Sue?", BEFORE_VERIFY) is None
    assert _rule("Thanks, Sue. Have a good day.", DISCLOSED) is None
    # The verb still is.
    assert _rule("We will sue.", DISCLOSED) == "threat"
    assert _rule("They sued him last year.", DISCLOSED) == "threat"


def test_debt_words_match_whole_words_only() -> None:
    # "indebted" and "Owen" contain debt words but are not them.
    assert _rule("I'm indebted to you for your patience.", BEFORE_VERIFY) is None
    assert _rule("Is Owen there?", BEFORE_VERIFY) is None
    # "debtor" is a debt word in its own right, so it is listed and blocked.
    assert _rule("Are you the debtor?", BEFORE_VERIFY) == "third_party_disclosure"


def test_matching_ignores_case() -> None:
    assert _rule("YOUR BALANCE IS DUE.", BEFORE_VERIFY) == "third_party_disclosure"
    assert _rule("this is about acme bank.", BEFORE_VERIFY) == "third_party_disclosure"


def test_default_state_is_the_strictest() -> None:
    assert _rule("Your balance is $1,240.", SpeechState()) == "third_party_disclosure"
    assert _rule("Hello, is this Sam?", SpeechState()) is None


# ---------------------------------------------------------------- parsing


def test_amounts_are_parsed_to_cents() -> None:
    assert amounts_in_cents("$1,240") == [124000]
    assert amounts_in_cents("$1,240.50 or 1240 dollars and 5 cents") == [124050, 124005]
    assert amounts_in_cents("$12.5") == [1250]
    assert amounts_in_cents("account ending 1240") == []


def test_split_sentences() -> None:
    text = "Hi Mr. Doe. Your balance is $1,240.50! Can you pay today? Thanks"
    assert split_sentences(text) == [
        "Hi Mr. Doe.",
        "Your balance is $1,240.50!",
        "Can you pay today?",
        "Thanks",
    ]
    assert split_sentences("One.\nTwo") == ["One.", "Two"]
    assert split_sentences("   ") == []
