"""Call stages: verify who answered and say the disclosure before any debt talk.

What it checks: a whole call through the simulator goes greeting, verify,
disclosure, discuss, and the model is only called in the stages where code
has no fixed line. The call state is saved in the session and survives a
reopen. And the refusals:
  - a wrong date of birth twice never reaches DISCUSS, and no reply mentions
    the debt
  - when a third party answers, no reply mentions the debt, the balance, the
    creditor or any amount, even with a model that tries to say them
  - a model that says the balance during VERIFY has that sentence dropped,
    and an ``utterance_blocked`` audit row is written (``db`` test)
  - the disclosure always comes before the first amount
  - an unknown phone number gets a fixed goodbye and no model call
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compliance import SpeechState, check_sentence, split_sentences
from harness import Agent
from harness.call_state import CallState, Debtor, Stage, load_state, save_state
from harness.memory import JsonlSession
from harness.stages import (
    ASK_DOB,
    ASK_ZIP,
    DISCLOSURE,
    GREETING,
    LOCATION_ASK,
    THIRD_PARTY_CLOSE,
    VERIFY_ASK,
    VERIFY_FAILED,
    VERIFY_RETRY,
    WRONG_NUMBER_CLOSE,
    find_dob,
    find_zip,
    instructions_path,
    next_stage,
)
from model import FakeProvider, Provider
from phone import signed_headers
from phone.webhook import CANT_CONTINUE, SAFE_LINE, WEBHOOK_PATH, audit_to_db, create_app
from sim.agentphone_sim import Script, load_script, run_script

SECRET = "whsec_test_only"

# The same people as supabase/seed.sql, so these tests need no database.
SAM = Debtor(
    debtor_id="00000000-0000-4000-8000-000000000001",
    debt_id="00000000-0000-4000-8000-00000000a001",
    first_name="Sam",
    last_name="Doe",
    dob=date(1985, 4, 12),
    zip="90012",
    creditor="Northwind Furniture",
    balance_cents=125000,
)
JORDAN = Debtor(
    debtor_id="00000000-0000-4000-8000-000000000003",
    debt_id="00000000-0000-4000-8000-00000000a003",
    first_name="Jordan",
    last_name="Lee",
    dob=date(1978, 1, 5),
    zip="60601",
    creditor="Maple Street Gym",
    balance_cents=8999,
)
DEBTORS = {"+12135550101": SAM, "+13125550103": JORDAN}

# A model that always tries to talk about the debt, whatever stage it's in.
LEAKY = "Sure. Your balance with Northwind Furniture is $1,250.00. Anything else?"
DEBT_TALK = re.compile(
    r"\b(?:debts?|balance|owe[sd]?|collect\w*|payment|northwind|maple street)\b|\$|\d+\.\d\d",
    re.IGNORECASE,
)


class Call:
    """The webhook in-process, with fake debtors, a list for the audit log and JSONL sessions."""

    def __init__(
        self, tmp_path: Path, reply: str = LEAKY, audit=None, skills_dir=None, slow=0.0
    ) -> None:
        self.responder = FakeProvider(default=reply)
        self.audited: list = []
        self.folder = tmp_path / "sessions"
        provider = Provider(base_url="fake://local", model="fake", responder=self.responder)
        app = create_app(
            secret=SECRET,
            agent_factory=lambda session: Agent(
                provider=provider, session=session, skills_dir=skills_dir
            ),
            open_session=lambda call_id: self._open(call_id, slow),
            find_debtor=DEBTORS.get,
            audit=audit or (lambda decision, call_id, debt_id: self.audited.append(decision)),
        )
        self.client = TestClient(app)

    def _open(self, call_id: str, slow: float) -> JsonlSession:
        session = JsonlSession.for_call(self.folder, call_id)
        time.sleep(slow)  # lets a test hold two turns of one call open at once
        return session

    def play(self, script: Script | str) -> list[tuple[str, str]]:
        return run_script(
            load_script(script) if isinstance(script, str) else script, self.client, SECRET
        )

    def turn(self, call_id: str, text: str, to: str = "+12135550101") -> dict:
        body = json.dumps(
            {
                "event": "agent.message",
                "channel": "voice",
                "data": {"callId": call_id, "transcript": text, "to": to, "direction": "outbound"},
            }
        ).encode()
        headers = signed_headers(
            body, SECRET, time.time(), f"wh_{uuid.uuid4().hex}", "agent.message"
        )
        resp = self.client.post(WEBHOOK_PATH, content=body, headers=headers)
        assert resp.status_code == 200
        return resp.json()

    def stages(self) -> list[Stage]:
        """The stage after each turn, from the one call's saved state records."""
        (path,) = self.folder.glob("*.jsonl")
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [Stage(r["state"]["stage"]) for r in records if r["type"] == "state"]


def _amount_free(reply: str) -> bool:
    return DEBT_TALK.search(reply) is None


# --- the happy path ------------------------------------------------------------


def test_greeting_verify_disclose_discuss(tmp_path: Path) -> None:
    call = Call(tmp_path)
    turns = call.play("sam_verified")
    replies = [reply for _, reply in turns]
    assert replies[0] == GREETING.format(full_name="Sam Doe")
    assert replies[1] == VERIFY_ASK
    assert replies[2] == DISCLOSURE.format(first_name="Sam")
    assert replies[3] == LEAKY  # verified and disclosed: the amount on file may be said
    assert call.stages() == [
        Stage.GREETING,
        Stage.VERIFY,
        Stage.DISCUSS,
        Stage.DISCUSS,
        Stage.DISCUSS,
    ]
    assert call.audited == []

    # The model only ran for the two DISCUSS turns, with system.md and discuss.md.
    assert len(call.responder.calls) == 2
    system = call.responder.calls[0][0]
    assert system["role"] == "system"
    assert "phone agent" in system["content"]
    assert instructions_path(Stage.DISCUSS).read_text(encoding="utf-8").strip() in system["content"]
    # It saw the fixed lines too, so it knows the disclosure was said.
    assert {"role": "assistant", "content": replies[2]} in call.responder.calls[0]
    assert call.responder.kwargs[0].get("tools")  # lookup_account is offered now


def test_model_is_not_called_in_fixed_stages(tmp_path: Path) -> None:
    call = Call(tmp_path)
    call.play(Script("fixed_only", ["Hello?", "Yes, speaking.", "4/12/1985, 90012"]))
    assert call.stages() == [Stage.GREETING, Stage.VERIFY, Stage.DISCUSS]
    assert call.responder.calls == []


def test_call_state_survives_a_reopen(tmp_path: Path) -> None:
    session = JsonlSession.for_call(tmp_path, "call_1")
    state = CallState.new("call_1", SAM)
    save_state(session, state)
    moved = CallState(**{**state.__dict__, "stage": Stage.VERIFY, "turns": 2})
    save_state(session, moved)
    assert load_state(JsonlSession.for_call(tmp_path, "call_1")) == moved
    assert JsonlSession.for_call(tmp_path, "call_1").load() == []  # states aren't messages


@pytest.mark.db
def test_call_state_survives_in_postgres() -> None:
    from harness.db import connect
    from harness.memory import PostgresSession

    call_id = f"test_{uuid.uuid4().hex[:8]}_state"
    try:
        save_state(PostgresSession.for_call(call_id), CallState.new(call_id, SAM))
        assert load_state(PostgresSession.for_call(call_id)) == CallState.new(call_id, SAM)
    finally:
        with connect() as conn:
            conn.execute("delete from public.call_message where call_id = %s", (call_id,))


# --- negative: wrong identity -------------------------------------------------


def test_wrong_dob_twice_never_reaches_discuss(tmp_path: Path) -> None:
    call = Call(tmp_path)
    turns = call.play("sam_wrong_dob")
    replies = [reply for _, reply in turns]
    assert replies[2] == VERIFY_RETRY
    assert replies[-1] == VERIFY_FAILED
    assert len(turns) == 4  # the close hung up; "How much do I owe?" was never answered
    assert Stage.DISCUSS not in call.stages()
    assert call.stages()[-1] == Stage.WRAP
    for reply in replies:
        assert _amount_free(reply), reply
        assert check_sentence(reply, SpeechState(creditor=SAM.creditor)).allow, reply
    assert call.responder.calls == []


# --- negative: a third party answers -------------------------------------------


def test_third_party_hears_nothing_about_the_debt(tmp_path: Path) -> None:
    call = Call(tmp_path, reply="It's about the debt of $89.99 owed to Maple Street Gym.")
    turns = call.play("jordan_not_here")
    replies = [reply for _, reply in turns]
    assert replies[1] == LOCATION_ASK.format(full_name="Jordan Lee")
    assert replies[2] == SAFE_LINE  # the model tried; every sentence was dropped
    assert replies[3] == THIRD_PARTY_CLOSE
    assert call.stages() == [Stage.GREETING, Stage.THIRD_PARTY, Stage.THIRD_PARTY, Stage.WRAP]
    for reply in replies:
        assert _amount_free(reply), reply
    assert [d.rule_id for d in call.audited] == ["third_party_disclosure"]
    assert call.responder.kwargs[0].get("tools") is None  # no account lookups for them


def test_wrong_number_closes_without_the_model(tmp_path: Path) -> None:
    call = Call(tmp_path)
    turns = call.play("wrong_number")
    assert turns[-1][1] == WRONG_NUMBER_CLOSE
    assert call.stages()[-1] == Stage.WRAP
    assert call.responder.calls == []


def test_unknown_number_gets_a_fixed_goodbye(tmp_path: Path) -> None:
    call = Call(tmp_path)
    reply = call.turn("call_x", "Hello?", to="+19995550199")
    assert reply == {"text": CANT_CONTINUE, "hangup": True}
    assert call.responder.calls == []


# --- negative: the model says the balance too early ----------------------------


def _ask_why_during_verify(call: Call, call_id: str) -> dict:
    call.turn(call_id, "Hello?")
    call.turn(call_id, "Yes, speaking.")
    return call.turn(call_id, "Why do you need that?")  # no date, no ZIP: the model answers


def test_balance_said_during_verify_is_dropped(tmp_path: Path) -> None:
    call = Call(tmp_path, reply="I understand. Your balance is $1,250.00. What is your ZIP code?")
    reply = _ask_why_during_verify(call, "call_v")
    assert reply == {"text": "I understand. What is your ZIP code?"}
    assert [(d.rule_id, d.evidence["sentence"]) for d in call.audited] == [
        ("third_party_disclosure", "Your balance is $1,250.00.")
    ]
    # The verify instructions were in front of the model, and it had no tools.
    system = call.responder.calls[0][0]["content"]
    assert instructions_path(Stage.VERIFY).read_text(encoding="utf-8").strip() in system
    assert call.responder.kwargs[0].get("tools") is None
    # The saved history holds what the caller heard, not the blocked sentence.
    saved = JsonlSession.for_call(call.folder, "call_v").load()
    assert saved[-1] == {"role": "assistant", "content": "I understand. What is your ZIP code?"}


def test_no_skills_before_discuss(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "hardship.md").write_text(
        "---\nname: hardship\ndescription: caller lost their job\n---\nOffer a plan.\n",
        encoding="utf-8",
    )
    call = Call(tmp_path, reply="Could you tell me your ZIP code?", skills_dir=skills)
    _ask_why_during_verify(call, "call_s")
    assert call.responder.kwargs[0].get("tools") is None  # no load_skill in VERIFY
    assert "hardship" not in call.responder.calls[0][0]["content"]
    call.turn("call_s", "4/12/1985, 90012")  # verified: the disclosure, no model
    call.turn("call_s", "I lost my job.")  # DISCUSS: now the skills are offered
    offered = {t["function"]["name"] for t in call.responder.kwargs[1]["tools"]}
    assert "load_skill" in offered
    assert "hardship" in call.responder.calls[1][0]["content"]


@pytest.mark.db
def test_blocked_sentence_is_written_to_the_audit_log(tmp_path: Path) -> None:
    from harness.db import connect

    call = Call(
        tmp_path, reply="Your balance is $1,250.00. Could I get your ZIP code?", audit=audit_to_db
    )
    call_id = f"test_{uuid.uuid4().hex[:8]}_audit"
    reply = _ask_why_during_verify(call, call_id)
    assert reply == {"text": "Could I get your ZIP code?"}
    with connect() as conn:
        rows = conn.execute(
            "select kind, rule_id, decision, debt_id::text, evidence->>'sentence' "
            "from public.audit_event where call_id = %s",
            (call_id,),
        ).fetchall()
    assert rows == [
        (
            "utterance_blocked",
            "third_party_disclosure",
            "block",
            SAM.debt_id,
            "Your balance is $1,250.00.",
        )
    ]


# --- negative: the disclosure comes first ---------------------------------------


def test_disclosure_comes_before_the_first_amount(tmp_path: Path) -> None:
    call = Call(tmp_path)  # the model tries to say the amount every time it runs
    script = Script(
        "pushy",
        [
            "Hello?",
            "Who is this?",  # model in GREETING: its amount is dropped
            "Yes, this is Sam Doe.",
            "Why do you need that?",  # model in VERIFY: dropped again
            "1985-04-12, zip 90012",
            "How much?",
        ],
        from_number="+12135550101",
    )
    replies = [reply for _, reply in call.play(script)]
    with_amount = [i for i, r in enumerate(replies) if "$" in r]
    disclosed_at = replies.index(DISCLOSURE.format(first_name="Sam"))
    assert with_amount and min(with_amount) > disclosed_at
    assert len(call.audited) == 2  # the balance sentence from each early model turn
    assert {d.rule_id for d in call.audited} == {"third_party_disclosure"}


# --- the pieces -------------------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        "April 12, 1985 and 90012",
        "12th of April 1985, my zip code is 90012",
        "4/12/1985 90012",
        "04-12-1985, ZIP 90012-1234",
        "1985-04-12 90012",
        "Apr. 12th 1985, 90012",
        "4/12/85 90012",
        "April 12, '85, zip 90012",
    ],
)
def test_dob_and_zip_are_read_in_common_formats(said: str) -> None:
    dob, rest = find_dob(said)
    assert dob == date(1985, 4, 12)
    assert find_zip(rest) == "90012"


def _verifying() -> CallState:
    return CallState(call_id="c", debtor_id="d", debt_id="x", stage=Stage.VERIFY, turns=2)


def test_date_of_birth_and_zip_can_come_in_two_turns() -> None:
    state, fixed = next_stage(_verifying(), "90012", SAM)
    assert (state.stage, state.verify_attempts, fixed) == (Stage.VERIFY, 0, ASK_DOB)
    state, fixed = next_stage(state, "April 12, 1985", SAM)
    assert state.verified and state.stage == Stage.DISCUSS
    assert fixed == DISCLOSURE.format(first_name="Sam")


def test_a_wrong_half_answer_is_one_attempt_and_gives_no_hint() -> None:
    state, fixed = next_stage(_verifying(), "My birthday is 1/1/1990", SAM)
    assert fixed == ASK_ZIP  # not "that's wrong": a third party learns nothing yet
    state, fixed = next_stage(state, "90012", SAM)
    assert (state.verify_attempts, fixed) == (1, VERIFY_RETRY)
    assert state.dob_ok is None and state.zip_ok is None  # start the next try fresh


def test_two_turns_of_one_call_at_once_still_count_both_wrong_answers(tmp_path: Path) -> None:
    import threading

    call = Call(tmp_path)
    call.turn("call_p", "Hello?")
    call.turn("call_p", "Yes, speaking.")
    call = Call(tmp_path, slow=0.2)  # same folder; without a lock both turns read one state
    replies: list[dict] = []
    threads = [
        threading.Thread(target=lambda t=t: replies.append(call.turn("call_p", t)))
        for t in ("1/1/1990, 90012", "2/2/1991, 90012")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r["text"] for r in replies) == sorted([VERIFY_RETRY, VERIFY_FAILED])
    assert load_state(JsonlSession.for_call(call.folder, "call_p")).verify_attempts == 2


def test_a_yes_with_a_no_in_it_is_not_a_yes() -> None:
    state = CallState.new("c", SAM)
    state, _ = next_stage(state, "Hello?", SAM)
    for unclear in ["Yes, but I'm not Sam.", "Yeah no.", "Who's asking?"]:
        moved, fixed = next_stage(state, unclear, SAM)
        assert moved.stage in (Stage.GREETING, Stage.THIRD_PARTY), unclear
        assert moved.stage != Stage.VERIFY
    assert next_stage(state, "This is Sam.", SAM)[0].stage == Stage.VERIFY


def test_disclose_refuses_if_not_verified() -> None:
    state = CallState(call_id="c", debtor_id="d", debt_id="x", stage=Stage.DISCLOSE, turns=3)
    moved, fixed = next_stage(state, "anything", SAM)
    assert moved.stage == Stage.WRAP and not moved.disclosed
    assert fixed == VERIFY_FAILED


def test_fixed_lines_before_verify_pass_the_filter() -> None:
    strict = SpeechState(creditor=SAM.creditor, known_amounts_cents={SAM.balance_cents})
    lines = [
        GREETING.format(full_name="Sam Doe"),
        VERIFY_ASK,
        VERIFY_RETRY,
        ASK_DOB,
        ASK_ZIP,
        VERIFY_FAILED,
        LOCATION_ASK.format(full_name="Sam Doe"),
        THIRD_PARTY_CLOSE,
        WRONG_NUMBER_CLOSE,
        SAFE_LINE,
        CANT_CONTINUE,
    ]
    for line in lines:
        for sentence in split_sentences(line):
            assert check_sentence(sentence, strict).allow, sentence
