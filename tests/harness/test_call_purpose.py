"""The call's purpose: after the disclosure the agent says why it called.

We placed the call, so the disclosure turn no longer ends with "How can I help
you today?". Code adds a purpose line built from the debt's database row: the
creditor and balance, formatted the way ``lookup_account`` gives them, and a
payment question. The model never writes that line.

What it checks:
  - The negative case: after a spoken identity check passes, the disclosure turn
    has no "How can I help" and names the creditor and balance exactly as stored.
  - A disputed, paused or closed debt gets a neutral line: no amount, no ask.
  - A debt row that can't be read, or doesn't match, gets no amount and the call
    ends; the model is not called.
  - Nothing about the debt is said before verification, and the row is not even
    read until then (the existing guarantee, re-checked on the new line).
  - A scripted model ``end_call`` on "Thank you for your time." hangs up.
  - The discuss prompt gives the model a job and the way to close.
  - On the local database (rolled back): the purpose line's balance is the one
    owed now, after a recorded payment, the same one the call tools are given.
All offline, over the signed simulator, with the fake model and made-up people.
"""

from __future__ import annotations

import json
import secrets
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compliance import SpeechState, check_sentence, split_sentences
from harness import Agent
from harness.call_state import Debtor, DebtRow, Stage, money
from harness.call_tools import MemoryCallStore
from harness.memory import JsonlSession
from harness.stages import (
    DISCLOSURE,
    PURPOSE,
    PURPOSE_ON_HOLD,
    PURPOSE_UNREAD,
    instructions_path,
    purpose_line,
)
from model import FakeProvider, Provider
from model.fake import tool_call, tool_reply
from phone.live_tools import ToolBackend
from phone.webhook import create_app
from sim.agentphone_sim import Script, run_script

SECRET = "whsec_test_only"
PHONE = "+16175550142"  # synthetic 555 number
MORGAN = Debtor(
    debtor_id="00000000-0000-4000-8000-0000000000b9",
    debt_id="00000000-0000-4000-8000-00000000b029",
    first_name="Morgan",
    last_name="Ellis",
    dob=date(1982, 3, 7),
    zip="30309",
    creditor="Lakeside Appliance Co",
    balance_cents=73450,
)
STORED = DebtRow(MORGAN.creditor, MORGAN.balance_cents, "active")
SPOKEN_VERIFY = [
    "Hello?",
    "Yes, this is Morgan.",
    "March seventh nineteen eighty two",
    "three oh three oh nine",
]
# A model that tries to say the debt whenever it gets the chance.
LEAKY = "Your balance with Lakeside Appliance Co is $734.50. Want to pay?"


class Call:
    """The webhook in-process, played by the signed simulator, with one made-up debtor."""

    def __init__(self, tmp_path: Path, row=STORED, scripted=None) -> None:  # type: ignore[no-untyped-def]
        self.responder = FakeProvider(scripted=scripted, default=LEAKY)
        self.folder = tmp_path / "sessions"
        self.reads = 0
        self.row = row
        provider = Provider(base_url="fake://local", model="fake", responder=self.responder)
        app = create_app(
            secret=SECRET,
            agent_factory=lambda session: Agent(provider=provider, session=session),
            open_session=lambda cid: JsonlSession.for_call(self.folder, cid),
            find_debtor={PHONE: MORGAN}.get,
            read_debt=self._read,
            audit=lambda decision, call_id, debt_id: None,
            record_stop=lambda stop: None,
            open_backend=lambda debtor: ToolBackend(
                store=MemoryCallStore(), audit=lambda d, c, i: None, flags=[]
            ),
        )
        self.client = TestClient(app)

    def _read(self, debtor: Debtor) -> DebtRow | None:
        self.reads += 1
        if isinstance(self.row, Exception):
            raise self.row
        return self.row

    def play(self, *lines: str) -> list[str]:
        turns = run_script(
            Script("call_purpose", list(lines), from_number=PHONE), self.client, SECRET
        )
        return [reply for _, reply in turns]

    def last_state(self) -> dict:
        (path,) = self.folder.glob("*.jsonl")
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [r["state"] for r in records if r["type"] == "state"][-1]


def _disclosure() -> str:
    return DISCLOSURE.format(first_name="Morgan")


# --- the negative case: the agent no longer hands the call over ------------------


def test_disclosure_turn_states_the_purpose_with_the_stored_amount(tmp_path: Path) -> None:
    call = Call(tmp_path)
    replies = call.play(*SPOKEN_VERIFY)
    said = replies[-1]

    assert said.startswith(_disclosure())  # the mini-Miranda, in the verifying turn
    assert "how can i help" not in said.lower()
    purpose = said[len(_disclosure()) :]
    assert MORGAN.creditor in purpose
    # Exactly as stored, formatted the way lookup_account gives it ("734.50").
    assert f"${money(STORED.balance_cents)}" in purpose and "$734.50" in purpose
    assert said == f"{_disclosure()} {PURPOSE.format(creditor=MORGAN.creditor, balance='$734.50')}"
    assert call.last_state()["stage"] == Stage.DISCUSS and call.last_state()["verified"]
    assert call.responder.calls == []  # code said all of it; no model turn
    assert call.reads == 1


def test_purpose_line_passes_the_sentence_filter() -> None:
    line, ends = purpose_line(MORGAN, STORED)
    assert not ends
    speech = SpeechState(
        verified=True,
        disclosed=True,
        known_amounts_cents={MORGAN.balance_cents},
        creditor=MORGAN.creditor,
    )
    for sentence in split_sentences(line):
        assert check_sentence(sentence, speech).allow, sentence


# --- disputed or paused: no payment ask -------------------------------------------


@pytest.mark.parametrize("status", ["disputed", "paused", "closed"])
def test_debt_on_hold_gets_the_neutral_line(tmp_path: Path, status: str) -> None:
    call = Call(tmp_path, row=DebtRow(MORGAN.creditor, MORGAN.balance_cents, status))
    said = call.play(*SPOKEN_VERIFY)[-1]

    assert said == f"{_disclosure()} {PURPOSE_ON_HOLD.format(creditor=MORGAN.creditor)}"
    purpose = said[len(_disclosure()) :]
    assert "$" not in purpose and "734" not in purpose
    assert "pay it" not in purpose and "plan" not in purpose and "?" not in purpose
    assert call.last_state()["stage"] == Stage.DISCUSS


def test_zero_balance_asks_for_nothing() -> None:
    debtor = Debtor(**{**MORGAN.__dict__, "balance_cents": 0})
    line, ends = purpose_line(debtor, DebtRow(MORGAN.creditor, 0, "active"))
    assert line == PURPOSE_ON_HOLD.format(creditor=MORGAN.creditor) and not ends


# --- the row can't be read: fail closed ---------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        RuntimeError("database went away"),
        None,  # no such row
        DebtRow(MORGAN.creditor, 99900, "active"),  # balance changed since the call began
        DebtRow("Someone Else LLC", MORGAN.balance_cents, "active"),
    ],
)
def test_unreadable_debt_says_no_amount_and_ends(tmp_path: Path, row) -> None:  # type: ignore[no-untyped-def]
    call = Call(tmp_path, row=row)
    replies = call.play(*SPOKEN_VERIFY, "How much do I owe?")

    assert len(replies) == len(SPOKEN_VERIFY)  # hung up; the last line was never answered
    said = replies[-1]
    assert said == f"{_disclosure()} {PURPOSE_UNREAD}"
    assert "$" not in said and "734" not in said and "999" not in said
    assert call.last_state()["stage"] == Stage.WRAP
    assert call.responder.calls == []  # the model never got to fill in a line


# --- nothing about the debt before verification -------------------------------------


def test_nothing_about_the_debt_before_verification(tmp_path: Path) -> None:
    call = Call(tmp_path)
    replies = call.play(
        "Hello?",
        "Who is this?",  # the model answers; its amount is dropped
        "Yes, this is Morgan.",
        "Why do you need that?",  # the model again, still unverified
        *SPOKEN_VERIFY[2:],
    )
    before = replies[:-1]
    assert not any("$" in r or MORGAN.creditor in r or "balance" in r for r in before)
    assert replies[-1].startswith(_disclosure()) and "$734.50" in replies[-1]


def test_a_failed_check_never_reads_or_says_the_debt(tmp_path: Path) -> None:
    call = Call(tmp_path)
    replies = call.play(
        "Hello?", "Yes, speaking.", "January first nineteen ninety", "nine nine nine nine nine",
        "February second nineteen ninety one", "one one one one one",
    )  # fmt: skip
    assert call.reads == 0
    assert not any("$" in r or MORGAN.creditor in r or "debt" in r for r in replies)
    assert call.last_state()["stage"] == Stage.WRAP


# --- the close ---------------------------------------------------------------------


def test_model_end_call_on_thank_you_hangs_up(tmp_path: Path) -> None:
    end = tool_call("end_call", reason="caller said thank you to finish")
    call = Call(tmp_path, scripted=[tool_reply(end), "You're welcome. Goodbye."])
    replies = call.play(*SPOKEN_VERIFY, "Thank you for your time.", "Hello? Still there?")

    assert replies[-1] == "You're welcome. Goodbye."
    assert len(replies) == len(SPOKEN_VERIFY) + 1  # hangup: the next line was never sent
    assert call.last_state()["stage"] == Stage.WRAP


def test_discuss_prompt_gives_the_model_a_job() -> None:
    text = instructions_path(Stage.DISCUSS).read_text(encoding="utf-8")
    lowered = text.lower()
    assert "you placed this call" in lowered
    for tool in ("offer_payment_plan", "schedule_callback", "log_dispute", "end_call"):
        assert tool in text
    assert "never ask" in lowered and "how can i help" in lowered  # named only to forbid it
    for rule in ("threaten", "lawyer", "government", "isn't true"):
        assert rule in lowered


# --- the real local database, rolled back ----------------------------------------


@pytest.mark.db
def test_purpose_line_uses_the_balance_owed_after_payments() -> None:
    from harness.db import connect, find_debtor_by_phone, read_debt_row

    tag = secrets.token_hex(4)
    phone = "+1555" + str(int(tag, 16) % 10_000_000).zfill(7)
    conn = connect()
    try:  # one transaction, rolled back: nothing made up here is ever committed
        (person,) = conn.execute(
            "insert into public.debtor (first_name, last_name, dob, address, zip, state)"
            " values ('Testpurpose', 'Ellis', '1980-01-01', '1 Made Up Way', '00000', 'CA')"
            " returning id",
        ).fetchone()
        conn.execute(
            "insert into public.phone (debtor_id, e164, line_type) values (%s, %s, 'cell')",
            (person, phone),
        )
        (debt_id,) = conn.execute(
            "insert into public.debt (debtor_id, account_number, creditor, balance_cents,"
            " opened_on) values (%s, %s, 'Made Up Store', 10000, '2026-10-01')"
            " returning id::text",
            (person, f"TST-{tag}"),
        ).fetchone()
        conn.execute(
            "insert into public.payment (debt_id, amount_cents, paid_on, method, recorded_by)"
            " values (%s, 2500, '2026-11-01', 'cash', 'test')",
            (debt_id,),
        )
        debtor = find_debtor_by_phone(phone, conn)
        row = read_debt_row(debt_id, conn)
    finally:
        conn.rollback()
        conn.close()

    assert debtor is not None and row is not None
    assert row.balance_cents == debtor.balance_cents == 7500  # 100.00 less 25.00 paid
    line, ends = purpose_line(debtor, row)
    assert not ends and "$75.00" in line and "$100.00" not in line
