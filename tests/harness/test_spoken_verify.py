"""Spoken answers: the identity check works with what speech-to-text really sends.

On a phone call, numbers arrive as words ("zero two one one five") and one
sentence can arrive as several turns ("Yeah. This is", then the rest).

What it checks:
  - ``find_dob`` and ``find_zip`` read spoken dates and ZIP codes, mixed with
    digits, and still read every digit form they read before.
  - Ambiguous or impossible spoken dates ("one fifteen nineteen ninety", day 32,
    month 13), short or long digit runs and years in the future are no answer.
  - Over the signed simulator: a spoken right answer verifies, and the debt is
    only spoken of after that.
  - The negative cases: a spoken wrong date of birth does not verify and counts
    as one attempt; a spoken wrong ZIP does not verify; a fragment in GREETING
    or VERIFY gets a fixed re-ask with no model call and no attempt counted; the
    model saying "you're verified" changes nothing.
All offline, with the fake model. Only made-up people, dates and ZIP codes.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compliance import SpeechState, check_sentence
from harness import Agent
from harness.call_state import CallState, Debtor, Stage
from harness.stages import (
    ASK_ZIP,
    DISCLOSURE,
    GREETING,
    GREETING_REASK,
    PURPOSE,
    REASK_DOB,
    REASK_ZIP,
    VERIFY_ASK,
    VERIFY_FAILED,
    VERIFY_REASK,
    VERIFY_RETRY,
    find_dob,
    find_zip,
    next_stage,
)
from model import FakeProvider, Provider
from phone.webhook import create_app, debt_as_found
from sim.agentphone_sim import Script, run_script

SECRET = "whsec_test_only"
PHONE = "+12135550188"  # synthetic 555 number
ALEX = Debtor(
    debtor_id="00000000-0000-4000-8000-0000000000aa",
    debt_id="00000000-0000-4000-8000-00000000a0aa",
    first_name="Alex",
    last_name="Rivera",
    dob=date(1990, 1, 15),
    zip="02115",
    creditor="Example Outfitters",
    balance_cents=42000,
)
NAMES = {"full_name": "Alex Rivera", "first_name": "Alex"}
CLAIM = "Great, you're verified now. Your balance is $420.00."
TO_VERIFY = ["Hello?", "Yes, speaking."]
FIXED_REASKS = [GREETING_REASK.format(**NAMES), VERIFY_REASK, REASK_DOB, REASK_ZIP]


class Call:
    """The webhook in-process, played by the signed simulator, with one fake debtor."""

    def __init__(self, tmp_path: Path, reply: str = CLAIM) -> None:
        self.responder = FakeProvider(default=reply)
        self.folder = tmp_path / "sessions"
        provider = Provider(base_url="fake://local", model="fake", responder=self.responder)
        app = create_app(
            secret=SECRET,
            agent_factory=lambda session: Agent(provider=provider, session=session),
            open_session=self._open,
            find_debtor={PHONE: ALEX}.get,
            read_debt=debt_as_found,
            audit=lambda decision, call_id, debt_id: None,
        )
        self.client = TestClient(app)

    def _open(self, call_id: str):
        from harness.memory import JsonlSession

        return JsonlSession.for_call(self.folder, call_id)

    def play(self, *lines: str) -> list[str]:
        turns = run_script(Script("part28", list(lines), from_number=PHONE), self.client, SECRET)
        return [reply for _, reply in turns]

    def states(self) -> list[dict]:
        (path,) = self.folder.glob("*.jsonl")
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [r["state"] for r in records if r["type"] == "state"]

    def last(self) -> dict:
        return self.states()[-1]


# --- find_dob / find_zip: spoken forms ------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        "fifteenth January nineteen ninety",
        "January fifteenth nineteen ninety",
        "the fifteenth of January nineteen ninety",
        "January the fifteenth, nineteen ninety",
        "January fifteen nineteen ninety",
        "15th January nineteen ninety",
        "fifteenth of January 1990",
        "Fifteenth January, nineteen ninety.",
    ],
)
def test_spoken_dates_are_read(said: str) -> None:
    assert find_dob(said)[0] == date(1990, 1, 15)


@pytest.mark.parametrize(
    "said, expected",
    [
        ("Twenty second October two thousand four", date(2004, 10, 22)),
        ("twenty-second of October, two thousand and four", date(2004, 10, 22)),
        ("October twenty second twenty oh four", date(2004, 10, 22)),
        ("the first of March two thousand", date(2000, 3, 1)),
        ("March thirty first nineteen eighty five", date(1985, 3, 31)),
        ("December ninth two thousand twelve", date(2012, 12, 9)),
        ("Sept 3rd nineteen oh five", date(1905, 9, 3)),
    ],
)
def test_more_spoken_dates(said: str, expected: date) -> None:
    assert find_dob(said)[0] == expected


@pytest.mark.parametrize(
    "said",
    [
        "zero two one one five",
        "oh two one one five",
        "O two one one five",
        "zero, two, one, one, five",
        "zero two one one and five",
        "my zip is 0 2 1 1 5",
        "02115",
        "02115-1234",
    ],
)
def test_spoken_zips_are_read(said: str) -> None:
    assert find_zip(said) == "02115"


def test_date_and_zip_in_one_spoken_turn() -> None:
    dob, rest = find_dob("It's January fifteenth nineteen ninety, and zero two one one five.")
    assert dob == date(1990, 1, 15)
    assert find_zip(rest) == "02115"
    dob, rest = find_dob("fifteenth January 1990 oh two one one five")
    assert (dob, find_zip(rest)) == (date(1990, 1, 15), "02115")


@pytest.mark.parametrize(
    "said, dob, zip_code",
    [
        # The year's last word could be the ZIP's first digit: the ZIP's five digits decide.
        ("January fifteenth nineteen ninety, nine oh two one oh", date(1990, 1, 15), "90210"),
        ("January fifteenth nineteen ninety nine oh two one oh", date(1990, 1, 15), "90210"),
        ("January fifteenth nineteen ninety. Nine zero two one zero.", date(1990, 1, 15), "90210"),
        (
            "January fifteenth nineteen ninety nine zero two one one five",
            date(1999, 1, 15),
            "02115",
        ),
        ("January fifteenth nineteen ninety nine", date(1999, 1, 15), None),
        ("March first nineteen eighty, six oh six oh one", date(1980, 3, 1), "60601"),
        ("the first of March two thousand, nine oh two one oh", date(2000, 3, 1), "90210"),
        ("the first of March two thousand, four", date(2004, 3, 1), None),
        (
            "the first of March two thousand twenty one oh two one one five",
            date(2021, 3, 1),
            "02115",
        ),
    ],
)
def test_a_zip_said_right_after_the_year(said: str, dob: date, zip_code: str | None) -> None:
    got, rest = find_dob(said)
    assert (got, find_zip(rest)) == (dob, zip_code)


@pytest.mark.parametrize(
    "said",
    [
        "January fifteenth nineteen ninety nine oh two",  # 1999, or 1990 and half a ZIP
        "January fifteenth nineteen ninety one one one five",
    ],
)
def test_a_year_that_might_end_in_a_zip_digit_is_no_answer(said: str) -> None:
    assert find_dob(said)[0] is None


def test_right_answer_with_a_zip_starting_nine_verifies() -> None:
    debtor = replace(ALEX, zip="90210")
    state, fixed = next_stage(
        _verifying(), "January fifteenth nineteen ninety, nine oh two one oh", debtor
    )
    assert state.verified and fixed == DISCLOSURE.format(**NAMES)
    state, fixed = next_stage(
        _verifying(), "January fifteenth nineteen ninety nine oh two one oh", debtor
    )
    assert state.verified and state.verify_attempts == 0


@pytest.mark.parametrize(
    "said",
    [
        "April 12, 1985 and 90012",
        "12th of April 1985, my zip code is 90012",
        "4/12/1985 90012",
        "1985-04-12 90012",
        "4/12/85 90012",
    ],
)
def test_digit_forms_still_work(said: str) -> None:
    dob, rest = find_dob(said)
    assert dob == date(1985, 4, 12) and find_zip(rest) == "90012"


@pytest.mark.parametrize(
    "said",
    [
        "one fifteen nineteen ninety",  # no month name: could be read many ways
        "thirty second January nineteen ninety",  # day 32
        "January thirty second nineteen ninety",
        "fifteenth of the thirteenth nineteen ninety",  # month 13
        "February thirtieth nineteen ninety",  # not a real date
        "January fifteenth ninety",  # not a whole year
        "January fifteenth",  # no year
        "January fifteenth twenty fifty",  # in the future
        "January fifteenth eighteen ninety",  # before 1900
        "nineteen ninety",
    ],
)
def test_ambiguous_or_impossible_dates_are_no_answer(said: str) -> None:
    assert find_dob(said)[0] is None


@pytest.mark.parametrize(
    "said",
    ["zero two one one", "zero two one one five five", "two one one five", "twenty one fifteen"],
)
def test_wrong_length_digit_runs_are_no_zip(said: str) -> None:
    assert find_zip(said) is None


# --- next_stage: what each kind of turn does -----------------------------------


def _verifying() -> CallState:
    return CallState(call_id="c", debtor_id="d", debt_id="x", stage=Stage.VERIFY, turns=2)


def test_spoken_wrong_dob_counts_as_one_attempt_and_does_not_verify() -> None:
    state, fixed = next_stage(
        _verifying(), "sixteenth January nineteen ninety, zero two one one five", ALEX
    )
    assert not state.verified and state.stage == Stage.VERIFY
    assert (state.verify_attempts, fixed) == (1, VERIFY_RETRY)


def test_a_half_answer_then_a_fragment_asks_only_for_the_rest() -> None:
    state, fixed = next_stage(_verifying(), "January fifteenth nineteen ninety", ALEX)
    assert fixed == ASK_ZIP
    state, fixed = next_stage(state, "zero two one", ALEX)  # cut off mid-ZIP
    assert (fixed, state.verify_attempts) == (REASK_ZIP, 0)
    state, fixed = next_stage(_verifying(), "zero two one one five", ALEX)
    state, fixed = next_stage(state, "um", ALEX)
    assert (fixed, state.verify_attempts) == (REASK_DOB, 0)


def test_questions_still_go_to_the_model() -> None:
    for question in ("Why do you need that?", "What is this for?", "Why? I told you who I am."):
        assert next_stage(_verifying(), question, ALEX)[1] is None, question
    greeting = CallState(call_id="c", debtor_id="d", debt_id="x", stage=Stage.GREETING, turns=1)
    assert next_stage(greeting, "Who is this?", ALEX)[1] is None


@pytest.mark.parametrize("said", ["Yes I am.", "Yeah, this is Alex", "Yes, it's me", "Speaking."])
def test_a_finished_yes_is_not_taken_for_a_fragment(said: str) -> None:
    greeting = CallState(call_id="c", debtor_id="d", debt_id="x", stage=Stage.GREETING, turns=1)
    assert next_stage(greeting, said, ALEX) == (
        CallState(call_id="c", debtor_id="d", debt_id="x", stage=Stage.VERIFY, turns=2),
        VERIFY_ASK,
    )


def test_reask_lines_claim_nothing_and_pass_the_filter() -> None:
    strict = SpeechState(creditor=ALEX.creditor, known_amounts_cents={ALEX.balance_cents})
    for line in FIXED_REASKS:
        assert "verif" not in line.lower() and "confirm" not in line.lower()
        assert check_sentence(line, strict).allow, line


# --- over the signed simulator ---------------------------------------------------


def test_spoken_right_answers_verify(tmp_path: Path) -> None:
    call = Call(tmp_path)
    replies = call.play(
        *TO_VERIFY, "the fifteenth of January nineteen ninety", "oh two, one, one, five"
    )
    purpose = PURPOSE.format(creditor=ALEX.creditor, balance="$420.00")
    disclosure = f"{DISCLOSURE.format(**NAMES)} {purpose}"
    assert replies == [GREETING.format(**NAMES), VERIFY_ASK, ASK_ZIP, disclosure]
    assert call.last()["verified"] and call.last()["stage"] == "DISCUSS"
    assert call.responder.calls == []


def test_spoken_wrong_dob_does_not_verify_and_is_an_attempt(tmp_path: Path) -> None:
    call = Call(tmp_path)
    replies = call.play(*TO_VERIFY, "January sixteenth nineteen ninety, zero two one one five")
    assert replies[-1] == VERIFY_RETRY
    state = call.last()
    assert (state["stage"], state["verified"], state["verify_attempts"]) == ("VERIFY", False, 1)
    assert call.responder.calls == []


def test_spoken_wrong_zip_does_not_verify(tmp_path: Path) -> None:
    call = Call(tmp_path)
    replies = call.play(
        *TO_VERIFY,
        "January fifteenth nineteen ninety, zero two one one six",
        "fifteenth January nineteen ninety, oh two one one six",
    )
    assert replies[-2:] == [VERIFY_RETRY, VERIFY_FAILED]
    state = call.last()
    assert (state["stage"], state["verified"], state["disclosed"]) == ("WRAP", False, False)
    assert call.responder.calls == []


@pytest.mark.parametrize("fragment", ["yeah this is", "Yeah. This is", "could", "um"])
def test_fragment_in_greeting_gets_a_fixed_reask(tmp_path: Path, fragment: str) -> None:
    call = Call(tmp_path)
    replies = call.play("Hello?", fragment)
    assert replies[-1] == GREETING_REASK.format(**NAMES)
    assert call.last()["stage"] == "GREETING"
    assert call.responder.calls == []


@pytest.mark.parametrize(
    "fragment", ["yeah this is", "could", "zero two one", "one fifteen nineteen ninety", "hold on"]
)
def test_fragment_in_verify_gets_a_fixed_reask_and_is_no_attempt(
    tmp_path: Path, fragment: str
) -> None:
    call = Call(tmp_path)
    replies = call.play(*TO_VERIFY, fragment, fragment)
    assert replies[-2:] == [VERIFY_REASK, VERIFY_REASK]
    state = call.last()
    assert (state["stage"], state["verified"], state["verify_attempts"]) == ("VERIFY", False, 0)
    assert call.responder.calls == []


def test_model_saying_verified_changes_nothing(tmp_path: Path) -> None:
    call = Call(tmp_path, reply=CLAIM)
    replies = call.play(*TO_VERIFY, "Why do you need that?", "Who is this?", "yeah this is")
    assert len(call.responder.calls) == 2  # only the two questions reached the model
    for reply in replies:
        assert "$420" not in reply
    assert replies[-1] == VERIFY_REASK
    state = call.last()
    assert (state["stage"], state["verified"], state["disclosed"]) == ("VERIFY", False, False)
    assert state["verify_attempts"] == 0
