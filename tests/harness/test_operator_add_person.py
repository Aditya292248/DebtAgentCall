"""Adding a person by chatting with the operator agent.

What it checks:
  - ``add_person`` never writes. Missing details come back as ``needs_more`` with
    what to ask next and open no approval; wrong ones (a future or ambiguous date of
    birth, a bad phone or ZIP) come back as ``needs_fix`` with a plain reason. Neither
    touches the database (these tests give a ``connect`` that fails if it is used).
  - Against the real local database (``db``), with made-up people removed afterwards:
    a ready person opens an approval and writes nothing until approved; one
    approval click writes the person and a second writes nothing; a denial writes
    nothing.
  - A person approved with no consent is saved, the answer says calls stay blocked,
    and the pre-call consent check does block them. With consent, the source and
    date are saved and the consent check allows.
  - A duplicate phone or account number is refused when asked and again when
    approved, with nothing partly written.
  - A correction replaces the open approval; a scripted conversation moves from
    missing details to an approval to a saved person.
  - Phone numbers are masked in everything the tool and the approval say, and the
    card shows exactly the consent text the model passed. The prompt forbids
    inventing consent.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from compliance import check_before_call
from harness.db import connect, load_call_facts
from harness.memory import JsonlSession
from harness.operator import DEFAULT_OPERATOR_PROMPT_PATH, Operator
from harness.people import AccountTaken
from harness.people import add_person as people_add
from model.fake import fake, tool_call, tool_reply
from phone.client import FakePhoneClient

WHEN = datetime(2026, 11, 5, 19, 0, tzinfo=UTC)
CONSENT = {"consent_source": "signed paper form, made up for tests", "consent_date": "2026-10-01"}


def no_database():
    raise AssertionError("this path must not touch the database")


def add(**fields: str) -> list:
    """A scripted model turn: call add_person, then say something."""
    return [tool_reply(tool_call("add_person", **fields)), "ok"]


def make(tmp_path: Path, script: list, *, connect_fn=no_database) -> Operator:
    session = JsonlSession.for_call(tmp_path / "chats", "chat-add")
    return Operator(
        session,
        provider=fake(scripted=script),
        connect=connect_fn,
        phone=FakePhoneClient(),
        clock=lambda: WHEN,
    )


def result(turn, index: int = -1) -> dict:
    return json.loads(turn.tool_calls[index]["result"])


class Person:
    """Made-up details with a phone number of its own, typed the way a person types it."""

    def __init__(self) -> None:
        tail = str(uuid.uuid4().int)[:6]
        self.digits = f"2135{tail}"
        self.e164 = "+1" + self.digits
        self.typed = f"({self.digits[:3]}) {self.digits[3:6]}-{self.digits[6:]}"
        self.tag = uuid.uuid4().hex[:8].upper()

    def fields(self, **over: str) -> dict[str, str]:
        base = {
            "first_name": "Sam",
            "last_name": f"Lee-{self.tag}",
            "date_of_birth": "March 3 85",
            "address": "12 Elm Street, Boston",
            "zip": "02115",
            "phone": self.typed,
            "creditor": "Acme Credit",
            "amount": "$480",
        }
        base.update(over)
        return {k: v for k, v in base.items() if v != ""}


@pytest.fixture
def people_made() -> Iterator[list[str]]:
    """Phone numbers (E.164) of people a test may have added; their debtors go afterwards."""
    phones: list[str] = []
    yield phones
    with connect() as conn:
        conn.execute(
            "delete from public.debtor where id in (select debtor_id from public.phone"
            " where e164 = any(%s))",
            (phones,),
        )


def rows_for(e164: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "select p.first_name, d.account_number, d.balance_cents, d.creditor, ph.consent_source,"
            " ph.consent_at from public.phone ph join public.debtor p on p.id = ph.debtor_id"
            " join public.debt d on d.debtor_id = p.id where ph.e164 = %s",
            (e164,),
        ).fetchall()


def rules(account: str) -> dict[str, bool]:
    with connect() as conn:
        facts = load_call_facts(account, conn)
    return {d.rule_id: d.allow for d in check_before_call(facts, WHEN)}


# --- offline: asking never writes -----------------------------------------------------


def test_a_missing_date_of_birth_is_needs_more_and_opens_nothing(tmp_path):
    """The negative case: no date of birth, so no approval and nothing written."""
    p = Person()
    op = make(tmp_path, add(**p.fields(date_of_birth="", **CONSENT)))

    turn = op.chat("add Sam Lee")

    body = result(turn)
    assert body["status"] == "needs_more"
    assert body["missing"] == ["date_of_birth"]
    assert body["ask_next"] == [{"fields": ["date_of_birth"], "about": ["date of birth"]}]
    assert turn.pending_approvals == []
    assert "Nothing has been saved" in body["note"]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("date_of_birth", "2031-05-01", "in the future"),
        ("date_of_birth", "3/4/85", "two ways"),
        ("phone", "555 0123", "phone number"),
        ("zip", "2115", "ZIP"),
    ],
)
def test_a_wrong_value_is_needs_fix_with_a_plain_reason(tmp_path, field, value, reason):
    p = Person()
    op = make(tmp_path, add(**p.fields(**{field: value}, **CONSENT)))

    turn = op.chat("add Sam Lee")

    body = result(turn)
    assert body["status"] == "needs_fix"
    (problem,) = [x for x in body["problems"] if x["field"] == field]
    assert reason in problem["reason"]
    assert turn.pending_approvals == []


def test_the_result_never_holds_a_whole_phone_number(tmp_path):
    p = Person()
    op = make(tmp_path, add(**p.fields(date_of_birth="")))
    turn = op.chat("add Sam Lee")
    text = turn.tool_calls[0]["result"]
    assert p.digits not in text and p.e164 not in text
    assert result(turn)["have"]["phone"] == "***-***-" + p.digits[-4:]


def test_the_prompt_forbids_inventing_consent_and_leading_with_a_checklist():
    prompt = DEFAULT_OPERATOR_PROMPT_PATH.read_text(encoding="utf-8").lower()
    assert "never make up a consent source or a date" in prompt
    assert "never say a person was added unless an approval result says so" in prompt
    assert "never list field names" in prompt
    assert "one or two at a time" in prompt


def test_add_person_arguments_are_all_optional(tmp_path):
    op = make(tmp_path, add())
    (tool,) = [t for t in op.tools() if t.name == "add_person"]
    assert tool.schema()["function"]["parameters"]["required"] == []
    assert "consent_source" in tool.params


def test_with_almost_nothing_it_still_answers(tmp_path):
    op = make(tmp_path, add())
    body = result(op.chat("add someone"))
    assert body["status"] == "needs_more"
    assert "first_name" in body["missing"]
    assert len(body["ask_next"]) <= 2


# --- against the database --------------------------------------------------------------


@pytest.mark.db
def test_a_ready_person_opens_an_approval_and_writes_nothing_until_approved(tmp_path, people_made):
    p = Person()
    people_made.append(p.e164)
    op = make(tmp_path, add(**p.fields(**CONSENT)), connect_fn=connect)

    turn = op.chat("add Sam Lee, owes Acme $480")

    body = result(turn)
    assert body["status"] == "ready"
    (pending,) = turn.pending_approvals
    assert pending.action == "add_person"
    assert pending.phone == "***-***-" + p.digits[-4:]
    assert pending.details["amount"] == "$480.00"
    assert pending.details["creditor"] == "Acme Credit"
    assert pending.details["account_generated"] is True
    # The card shows exactly the consent text that was passed, nothing added.
    assert pending.details["consent_line"] == f"{CONSENT['consent_source']}, given 2026-10-01"
    assert p.digits not in json.dumps(turn.tool_calls[0]["result"])
    assert rows_for(p.e164) == []  # nothing is saved before approval


@pytest.mark.db
def test_approving_saves_once_and_a_second_click_writes_nothing(tmp_path, people_made):
    p = Person()
    people_made.append(p.e164)
    op = make(tmp_path, add(**p.fields(**CONSENT)), connect_fn=connect)
    (pending,) = op.chat("add Sam Lee").pending_approvals

    done = op.decide(pending.id, True)

    assert done.reply.startswith(f"[approval {pending.id}] Saved Sam Lee-")
    assert p.digits not in done.reply and "***-***-" + p.digits[-4:] in done.reply
    ((first, account, cents, creditor, source, consent_at),) = rows_for(p.e164)
    assert (first, cents, creditor) == ("Sam", 48000, "Acme Credit")
    assert source == CONSENT["consent_source"] and consent_at.date().isoformat() == "2026-10-01"
    assert account in done.reply
    assert rules(account)["tcpa_consent"] is True
    with pytest.raises(LookupError):
        op.decide(pending.id, True)
    assert len(rows_for(p.e164)) == 1
    with connect() as conn:
        audit = conn.execute(
            "select reason, evidence->>'approval_id' from public.audit_event"
            " where kind = 'person' and evidence->>'approval_id' = %s",
            (pending.id,),
        ).fetchall()
    assert audit == [(f"added {account} from the operator chat", pending.id)]


@pytest.mark.db
def test_denying_writes_nothing(tmp_path, people_made):
    p = Person()
    people_made.append(p.e164)
    op = make(tmp_path, add(**p.fields(**CONSENT)), connect_fn=connect)
    (pending,) = op.chat("add Sam Lee").pending_approvals

    done = op.decide(pending.id, False)

    assert "Not saved" in done.reply
    assert rows_for(p.e164) == []


@pytest.mark.db
def test_a_person_with_no_consent_is_saved_but_calls_stay_blocked(tmp_path, people_made):
    p = Person()
    people_made.append(p.e164)
    op = make(tmp_path, add(**p.fields(consent_source="none")), connect_fn=connect)
    turn = op.chat("add Sam Lee, no permission")
    (pending,) = turn.pending_approvals
    assert "stay blocked" in pending.details["consent_line"]
    assert pending.details["consent_recorded"] is False

    done = op.decide(pending.id, True)

    assert "No consent is on file" in done.reply and "stay blocked" in done.reply
    ((_, account, _, _, source, consent_at),) = rows_for(p.e164)
    assert source is None and consent_at is None
    assert rules(account)["tcpa_consent"] is False


@pytest.mark.db
def test_consent_is_asked_about_and_never_defaulted(tmp_path, people_made):
    p = Person()
    people_made.append(p.e164)
    op = make(tmp_path, add(**p.fields()), connect_fn=connect)  # everything but consent

    turn = op.chat("add Sam Lee")

    assert result(turn)["status"] == "needs_more"
    assert result(turn)["missing"] == ["consent_source"]
    assert turn.pending_approvals == []
    assert rows_for(p.e164) == []


@pytest.mark.db
def test_a_duplicate_phone_is_refused_when_asked_and_when_approved(tmp_path, people_made):
    p = Person()
    people_made.append(p.e164)
    # asked: the number is already on file
    first = make(tmp_path, add(**p.fields(**CONSENT)), connect_fn=connect)
    (pending,) = first.chat("add Sam Lee").pending_approvals
    first.decide(pending.id, True)
    second = make(tmp_path / "again", add(**p.fields(**CONSENT)), connect_fn=connect)
    turn = second.chat("add Sam Lee again")
    body = result(turn)
    assert body["status"] == "needs_fix"
    assert "already on file" in body["problems"][0]["reason"]
    assert turn.pending_approvals == []

    # approved: someone else took the number between the card and the click
    q = Person()
    people_made.append(q.e164)
    third = make(tmp_path / "race", add(**q.fields(**CONSENT)), connect_fn=connect)
    (race,) = third.chat("add Casey").pending_approvals
    other = Person()
    people_made.append(other.e164)
    people_add(  # same number, another person, added elsewhere
        {
            "first_name": "Other", "last_name": other.tag, "dob": "1980-01-01",
            "address": "1 Main St", "zip": "02115", "state": "MA", "phone": q.e164,
            "line_type": "cell", "creditor": "Acme", "account_number": f"OT-{other.tag}",
            "balance_cents": 100,
        }
    )  # fmt: skip
    done = third.decide(race.id, True)
    assert "Not saved" in done.reply and "already on file" in done.reply
    assert [r[0] for r in rows_for(q.e164)] == ["Other"]  # only the other person: nothing partial
    with connect() as conn:
        refused = conn.execute(
            "select decision from public.audit_event where evidence->>'approval_id' = %s",
            (race.id,),
        ).fetchall()
    assert refused == [("block",)]


@pytest.mark.db
def test_a_duplicate_account_number_is_refused_with_nothing_written(tmp_path, people_made):
    p, q = Person(), Person()
    people_made.extend([p.e164, q.e164])
    account = f"DUP-{p.tag}"
    first = make(tmp_path, add(**p.fields(account_number=account, **CONSENT)), connect_fn=connect)
    first.decide(first.chat("add").pending_approvals[0].id, True)

    again = make(
        tmp_path / "b", add(**q.fields(account_number=account, **CONSENT)), connect_fn=connect
    )
    body = result(again.chat("add"))

    assert body["status"] == "needs_fix"
    assert body["problems"][0]["field"] == "account_number"
    assert rows_for(q.e164) == []
    with pytest.raises(AccountTaken):  # and the writer itself refuses it too
        people_add(
            {
                "first_name": "X", "last_name": "Y", "dob": "1980-01-01", "address": "1 Main St",
                "zip": "02115", "state": "MA", "phone": q.e164, "line_type": "cell",
                "creditor": "Acme", "account_number": account, "balance_cents": 100,
            }
        )  # fmt: skip
    assert rows_for(q.e164) == []


@pytest.mark.db
def test_a_correction_replaces_the_open_approval(tmp_path, people_made):
    p = Person()
    people_made.append(p.e164)
    script = [
        tool_reply(tool_call("add_person", **p.fields(amount="840", **CONSENT))),
        "Waiting for your approval.",
        tool_reply(tool_call("add_person", **p.fields(amount="480", **CONSENT))),
        "Updated: waiting for your approval.",
    ]
    op = make(tmp_path, script, connect_fn=connect)

    old = op.chat("add Sam Lee owes 840").pending_approvals[0]
    turn = op.chat("actually it's 480 not 840")

    (new,) = turn.pending_approvals
    assert new.id != old.id and new.details["amount"] == "$480.00"
    with pytest.raises(LookupError):
        op.decide(old.id, True)
    op.decide(new.id, True)
    ((_, _, cents, *_),) = rows_for(p.e164)
    assert cents == 48000


@pytest.mark.db
def test_a_conversation_from_missing_details_to_a_saved_person(tmp_path, people_made):
    p = Person()
    people_made.append(p.e164)
    script = [
        # message 1: name, amount, phone
        tool_reply(
            tool_call(
                "add_person",
                first_name="Sam",
                last_name=f"Lee-{p.tag}",
                amount="$480",
                phone=p.typed,
                creditor="Acme Credit",
            )  # fmt: skip
        ),
        "Thanks. What is Sam's date of birth, and the street address with ZIP?",
        # message 2: DOB and address
        tool_reply(
            tool_call(
                "add_person",
                first_name="Sam",
                last_name=f"Lee-{p.tag}",
                amount="$480",
                phone=p.typed,
                creditor="Acme Credit",
                date_of_birth="March 3 85",
                address="12 Elm Street",
                zip="02115",
            )  # fmt: skip
        ),
        "Got it. Do you have permission to call this number?",
        # message 3: consent
        tool_reply(tool_call("add_person", **p.fields(**CONSENT))),
        "I'm asking you to approve adding Sam Lee. Nothing is saved yet.",
    ]
    op = make(tmp_path, script, connect_fn=connect)

    one = op.chat("add Sam Lee, owes Acme Credit $480, phone " + p.typed)
    assert result(one)["status"] == "needs_more"
    assert result(one)["missing"] == ["date_of_birth", "address", "zip", "state", "consent_source"]
    assert result(one)["ask_next"][0]["fields"] == ["date_of_birth"]
    assert one.pending_approvals == []

    two = op.chat("born March 3 85, lives at 12 Elm Street, 02115")
    assert result(two)["status"] == "needs_more"
    assert result(two)["missing"] == ["consent_source"]
    assert result(two)["have"]["state"] == "MA"
    assert two.pending_approvals == []
    assert rows_for(p.e164) == []

    three = op.chat("yes, signed form on Oct 1")
    assert result(three)["status"] == "ready"
    (pending,) = three.pending_approvals
    assert rows_for(p.e164) == []

    op.decide(pending.id, True)
    assert len(rows_for(p.e164)) == 1
