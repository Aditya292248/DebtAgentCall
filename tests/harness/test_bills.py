"""Bill upload: the model reads a bill into a draft, and a person confirms it.

What it checks:
  - Uploading a bill saves no debtor, phone or debt: only a draft and an audit
    row. Only confirm saves them, once.
  - Confirm refuses a phone number already on file for a debtor, since a live
    call finds its debtor by the number and could load the wrong person.
  - Confirm checks the person's fields again: a balance that isn't a positive
    whole number of cents, or a phone that isn't E.164, is rejected (422) and
    nothing is saved.
  - A debtor saved from a bill has no consent on file, so the pre-call checks
    block calls to them (``tcpa_consent``).
  - The model is shown the bill (an image, or a PDF as a file) and its reply is
    normalised and checked in code: the printed balance becomes cents, a US
    phone becomes E.164, and missing, unsure or bad fields are flagged. Date of
    birth and line type are never on a bill, so they are always flagged.
  - Files that aren't a PNG, JPEG, WebP or PDF are refused before anything is
    stored or read.
  - The operator's ``read_bill`` shows a draft with the phone masked and saves
    nothing; an operator without bills keeps its four tools.
  - Against the real local database (``db``): the same, through
    ``PostgresBillDb``, the ``bill_draft`` table and the audit log.

The bill is a synthetic 1x1 PNG made in this file; the "vision model" is
``FakeProvider``. Every name, number and address is made up.
"""

from __future__ import annotations

import json
import struct
import uuid
import zlib
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from compliance import CallFacts, PhoneConsent, check_before_call, may_call
from harness.bills import (
    Bills,
    LocalBillStore,
    MemoryBillDb,
    PostgresBillDb,
    cents_from_printed,
    draft_fields,
    e164_from_printed,
    validate,
)
from harness.memory import JsonlSession
from harness.operator import Operator
from model.fake import fake, tool_call, tool_reply

TODAY = date(2026, 11, 15)
WHEN = datetime(2026, 11, 16, 19, 0, tzinfo=UTC)  # 11:00 in Los Angeles


def synthetic_png() -> bytes:
    """A valid 1x1 white PNG: a stand-in for a photo of a bill."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\xff\xff")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


PNG = synthetic_png()
PDF = b"%PDF-1.4\n% synthetic bill\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"


def reply(account: str = "NB-20931", **overrides: object) -> str:
    """What a vision model reads off Riley Example's made-up bill."""
    fields: dict[str, object] = {
        "first_name": "Riley",
        "last_name": "Example",
        "address": "100 Test Street, Los Angeles",
        "zip": "90012",
        "state": "CA",
        "phone": "(213) 555-0199",
        "creditor": "Northbank Utilities",
        "balance": "$1,234.56",
        "account_reference": account,
        "statement_date": "2026-10-31",
    }
    unsure = overrides.pop("unsure", [])
    fields.update(overrides)
    return json.dumps({"fields": fields, "unsure": unsure})


def confirmed(draft_fields_: dict, **edits: object) -> dict:
    """The draft as a person finishes it: the two fields no bill has, plus ``edits``."""
    return {**draft_fields_, "dob": "1985-04-12", "line_type": "cell", **edits}


def make_bills(tmp_path, model_reply: str | None = None, db=None) -> Bills:
    return Bills(
        store=LocalBillStore(tmp_path / "bills"),
        db=db if db is not None else MemoryBillDb(),
        provider=fake(default=model_reply or reply()),
        today=lambda: TODAY,
    )


@pytest.fixture
def api(tmp_path, monkeypatch) -> Iterator[tuple[TestClient, Bills]]:
    import harness.api

    bills = make_bills(tmp_path)
    monkeypatch.setattr(harness.api, "make_bills", lambda: bills)
    yield TestClient(harness.api.app), bills


def upload(client: TestClient, data: bytes = PNG, kind: str = "image/png"):
    return client.post("/api/bills", files={"file": ("riley.png", data, kind)})


# --- the negative case: upload saves nothing --------------------------------------


def test_uploading_a_bill_creates_no_debt_until_confirm(api):
    client, bills = api
    db = bills.db
    assert isinstance(db, MemoryBillDb)

    resp = upload(client)
    assert resp.status_code == 200, resp.text
    draft = resp.json()
    assert draft["status"] == "draft"
    assert draft["fields"]["balance_cents"] == 123456
    # Only a draft: no debtor, phone or debt yet.
    assert (db.debtors, db.phones, db.debts) == ({}, [], {})
    assert [d.rule_id for d, _ in db.audit] == ["bill_upload"]

    resp = client.post(
        f"/api/bills/{draft['draft_id']}/confirm", json={"fields": confirmed(draft["fields"])}
    )
    assert resp.status_code == 200, resp.text
    saved = resp.json()
    assert list(db.debts) == [saved["debt_id"]]
    assert db.debts[saved["debt_id"]]["balance_cents"] == 123456
    assert db.debts[saved["debt_id"]]["account_number"] == "NB-20931"
    assert len(db.debtors) == len(db.phones) == 1
    assert [(d.rule_id, debt) for d, debt in db.audit] == [
        ("bill_upload", None),
        ("bill_confirm", saved["debt_id"]),
    ]
    assert client.get(f"/api/bills/{draft['draft_id']}").json()["status"] == "confirmed"


def test_a_draft_confirms_only_once(api):
    client, bills = api
    draft = upload(client).json()
    url = f"/api/bills/{draft['draft_id']}/confirm"
    body = {"fields": confirmed(draft["fields"])}
    assert client.post(url, json=body).status_code == 200
    assert client.post(url, json=body).status_code == 409
    assert len(bills.db.debts) == 1  # type: ignore[attr-defined]


def test_an_account_number_already_saved_is_not_saved_twice(api):
    client, bills = api
    first, second = upload(client).json(), upload(client).json()
    ok = client.post(
        f"/api/bills/{first['draft_id']}/confirm", json={"fields": confirmed(first["fields"])}
    )
    assert ok.status_code == 200
    resp = client.post(
        f"/api/bills/{second['draft_id']}/confirm", json={"fields": confirmed(second["fields"])}
    )
    assert resp.status_code == 409
    assert len(bills.db.debts) == 1  # type: ignore[attr-defined]


def test_a_phone_number_already_on_file_is_not_given_to_a_second_debtor(api):
    client, bills = api
    first, second = upload(client).json(), upload(client).json()
    ok = client.post(
        f"/api/bills/{first['draft_id']}/confirm", json={"fields": confirmed(first["fields"])}
    )
    assert ok.status_code == 200
    # Same number, different account: a call to the number could load either one.
    resp = client.post(
        f"/api/bills/{second['draft_id']}/confirm",
        json={"fields": confirmed(second["fields"], account_reference="NB-77777")},
    )
    assert resp.status_code == 409
    assert "phone number is already on file" in resp.json()["detail"]
    db = bills.db
    assert isinstance(db, MemoryBillDb)
    assert len(db.debts) == len(db.debtors) == len(db.phones) == 1
    assert client.get(f"/api/bills/{second['draft_id']}").json()["status"] == "draft"


def test_confirming_an_unknown_draft_is_a_404(api):
    client, _ = api
    good = confirmed(draft_fields(reply(), TODAY)[0])
    for draft_id in ("bill_" + "0" * 32, "../etc/passwd", "nope"):
        resp = client.post(f"/api/bills/{draft_id}/confirm", json={"fields": good})
        assert resp.status_code == 404, draft_id


# --- confirm checks the fields again ------------------------------------------------


@pytest.mark.parametrize("balance", ["1234.56", 1234.56, 0, -500, True, None, "123456"])
def test_confirm_rejects_a_bad_balance_and_saves_nothing(api, balance):
    client, bills = api
    draft = upload(client).json()
    resp = client.post(
        f"/api/bills/{draft['draft_id']}/confirm",
        json={"fields": confirmed(draft["fields"], balance_cents=balance)},
    )
    assert resp.status_code == 422
    assert "balance_cents" in resp.json()["detail"]["problems"]
    assert bills.db.debts == {}  # type: ignore[attr-defined]
    assert client.get(f"/api/bills/{draft['draft_id']}").json()["status"] == "draft"


@pytest.mark.parametrize(
    "phone", ["2135550199", "(213) 555-0199", "+1 213 555 0199", "+0123", "", None, 12135550199]
)
def test_confirm_rejects_a_phone_that_is_not_e164_and_saves_nothing(api, phone):
    client, bills = api
    draft = upload(client).json()
    resp = client.post(
        f"/api/bills/{draft['draft_id']}/confirm",
        json={"fields": confirmed(draft["fields"], phone=phone)},
    )
    assert resp.status_code == 422
    assert "phone" in resp.json()["detail"]["problems"]
    assert bills.db.phones == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("zip", "9001"),
        ("zip", "90012-12"),
        ("state", "California"),
        ("dob", None),
        ("dob", "2027-01-01"),
        ("line_type", "voip"),
        ("account_reference", "NB 20931; drop table"),
        ("first_name", "  "),
        ("statement_date", "last month"),
    ],
)
def test_confirm_rejects_other_bad_fields(name, value):
    fields = confirmed(draft_fields(reply(), TODAY)[0], **{name: value})
    assert name in validate(fields, TODAY)


def test_confirm_rejects_fields_it_does_not_know():
    fields = confirmed(draft_fields(reply(), TODAY)[0], consent_at="2026-01-01")
    assert "fields" in validate(fields, TODAY)


def test_good_fields_pass():
    assert validate(confirmed(draft_fields(reply(), TODAY)[0]), TODAY) == {}


# --- a bill is not consent ------------------------------------------------------------


def test_a_confirmed_debtor_without_consent_is_blocked_by_the_precall_checks(tmp_path):
    bills = make_bills(tmp_path)
    draft = bills.upload(PNG, "image/png")
    saved = bills.confirm(draft.id, confirmed(draft.fields))
    db = bills.db
    assert isinstance(db, MemoryBillDb)
    (phone,) = db.phones
    assert phone["consent_at"] is None
    facts = CallFacts(
        debt_id=saved.debt_id,
        zip=db.debtors[saved.debtor_id]["zip"],
        phone=PhoneConsent(e164=phone["e164"], line_type=phone["line_type"]),
        attempts=[],
        flags=[],
    )
    decisions = check_before_call(facts, WHEN)
    assert not may_call(decisions)
    blocking = [d.rule_id for d in decisions if not d.allow]
    assert blocking == ["tcpa_consent"]


# --- reading the bill ---------------------------------------------------------------


def test_the_model_is_shown_the_image_and_the_draft_is_normalised_in_code(tmp_path):
    bills = make_bills(tmp_path)
    draft = bills.upload(PNG, "image/png")
    assert bills.provider is not None and bills.provider.responder is not None
    (messages,) = bills.provider.responder.calls  # type: ignore[attr-defined]
    parts = messages[0]["content"]
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert draft.fields["balance_cents"] == 123456
    assert draft.fields["phone"] == "+12135550199"
    # Never on a bill, so always for the person to fill in.
    assert set(draft.flags) == {"dob", "line_type"}
    # The file is kept under the draft id and its hash, not the name it was sent with.
    assert draft.storage_key.startswith(draft.id + "/") and "riley" not in draft.storage_key
    assert LocalBillStore(tmp_path / "bills").get(draft.storage_key) == PNG


def test_a_pdf_is_sent_to_the_model_as_a_file(tmp_path):
    bills = make_bills(tmp_path)
    bills.upload(PDF, "application/pdf")
    (messages,) = bills.provider.responder.calls  # type: ignore[union-attr]
    part = messages[0]["content"][1]
    assert part["type"] == "file"
    assert part["file"]["file_data"].startswith("data:application/pdf;base64,")


def test_missing_unsure_and_unreadable_fields_are_flagged():
    fields, flags = draft_fields(
        reply(phone=None, balance="about twelve hundred", unsure=["creditor"]), TODAY
    )
    assert fields["phone"] is None and flags["phone"] == "missing from the bill"
    assert fields["balance_cents"] is None and "could not read" in flags["balance_cents"]
    assert flags["creditor"] == "the model was not sure it read this right"


def test_a_reply_that_is_not_json_still_gives_a_draft_with_everything_flagged(tmp_path):
    bills = make_bills(tmp_path, model_reply="I can't read this image, sorry.")
    draft = bills.upload(PNG, "image/png")
    assert all(v is None for v in draft.fields.values())
    assert {"balance_cents", "phone", "dob", "line_type"} <= set(draft.flags)
    assert bills.db.debts == {}  # type: ignore[attr-defined]


def test_a_reply_in_a_code_fence_is_read():
    fields, _ = draft_fields("```json\n" + reply() + "\n```", TODAY)
    assert fields["balance_cents"] == 123456


@pytest.mark.parametrize(
    ("printed", "cents"),
    [
        ("$1,234.56", 123456),
        ("1234.5", 123450),
        ("USD 80", 8000),
        ("$0.07", 7),
        ("1.234", None),
        ("-$5.00", None),
        ("12,34.5x", None),
        (1234, None),
    ],
)
def test_cents_from_printed(printed, cents):
    assert cents_from_printed(printed) == cents


@pytest.mark.parametrize(
    ("printed", "e164"),
    [
        ("(213) 555-0199", "+12135550199"),
        ("1-213-555-0199", "+12135550199"),
        ("+442079460000", "+442079460000"),
        ("555-0199", None),
        (None, None),
    ],
)
def test_e164_from_printed(printed, e164):
    assert e164_from_printed(printed) == e164


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (b"", "image/png"),
        (b"hello, not a bill", "text/plain"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
        (PNG, "application/pdf"),  # sent as one type, is another
        (b"\x89PNG\r\n\x1a\n" + b"0" * (10 * 1024 * 1024), "image/png"),  # too big
    ],
)
def test_files_that_are_not_bills_are_refused_before_anything_happens(api, tmp_path, data, kind):
    client, bills = api
    resp = upload(client, data, kind)
    assert resp.status_code == 415
    assert bills.db.drafts == {}  # type: ignore[attr-defined]
    assert bills.provider.responder.calls == []  # type: ignore[union-attr]
    assert not (tmp_path / "bills").exists()


# --- the operator can show a draft, never save one -------------------------------------


def test_read_bill_shows_the_draft_masked_and_saves_nothing(tmp_path):
    bills = make_bills(tmp_path)
    draft = bills.upload(PNG, "image/png")
    session = JsonlSession.for_call(tmp_path / "chats", "chat-1")
    operator = Operator(
        session,
        provider=fake(
            scripted=[tool_reply(tool_call("read_bill", draft_id=draft.id)), "Here it is."]
        ),
        connect=lambda: pytest.fail("read_bill must not touch the database"),
        bills=bills.db,
    )
    turn = operator.chat("show me the bill I just uploaded")
    (call,) = turn.tool_calls
    body = json.loads(call["result"])
    assert body["fields"]["phone"] == "***-***-0199"
    assert body["fields"]["balance_cents"] == 123456
    assert body["status"] == "draft"
    assert bills.db.debts == {}  # type: ignore[attr-defined]
    assert operator.read_bill("bill_" + "f" * 32).startswith("error:")
    assert operator.read_bill("../x").startswith("error:")


def test_an_operator_without_bills_has_no_read_bill(tmp_path):
    session = JsonlSession.for_call(tmp_path / "chats", "chat-1")
    operator = Operator(session, provider=fake(), connect=lambda: pytest.fail("no db"))
    assert "read_bill" not in [t.name for t in operator.tools()]


# --- against the real local database -------------------------------------------------


@pytest.fixture
def pg_bills(tmp_path) -> Iterator[tuple[Bills, str]]:
    from harness.db import connect

    account = "BILL-" + uuid.uuid4().hex[:10]
    bills = make_bills(tmp_path, reply(account=account), db=PostgresBillDb())
    yield bills, account
    with connect() as conn:
        conn.execute(
            "delete from public.debtor where id in "
            "(select debtor_id from public.debt where account_number = %s)",
            (account,),
        )
        conn.execute(
            "delete from public.bill_draft where fields->>'account_reference' = %s", (account,)
        )


def _count(sql: str, *params: object) -> int:
    from harness.db import connect

    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.db
def test_in_db_upload_saves_only_a_draft_and_confirm_saves_a_debtor_who_cannot_be_called(
    pg_bills,
):
    from harness.db import load_call_facts

    bills, account = pg_bills
    debts = "select count(*) from public.debt where account_number = %s"
    audit = "select count(*) from public.audit_event where kind = 'bill'"
    audit_before = _count(audit)

    draft = bills.upload(PNG, "image/png")
    assert _count(debts, account) == 0
    assert _count("select count(*) from public.bill_draft where id = %s", draft.id) == 1
    assert _count(audit) == audit_before + 1
    assert bills.get(draft.id) == draft

    saved = bills.confirm(draft.id, confirmed(draft.fields))
    assert _count(debts, account) == 1
    assert _count(audit) == audit_before + 2
    stored = bills.get(draft.id)
    assert stored is not None and stored.status == "confirmed" and stored.debt_id == saved.debt_id

    facts = load_call_facts(account)
    assert facts is not None and facts.phone is not None
    assert facts.phone.consent_at is None
    decisions = check_before_call(facts, WHEN)
    assert not may_call(decisions)
    assert "tcpa_consent" in [d.rule_id for d in decisions if not d.allow]


@pytest.mark.db
def test_in_db_a_bad_balance_saves_nothing_and_a_draft_confirms_once(pg_bills):
    from harness.bills import AlreadyConfirmed, InvalidFields

    bills, account = pg_bills
    draft = bills.upload(PNG, "image/png")
    with pytest.raises(InvalidFields):
        bills.confirm(draft.id, confirmed(draft.fields, balance_cents="1234.56"))
    assert _count("select count(*) from public.debt where account_number = %s", account) == 0
    bills.confirm(draft.id, confirmed(draft.fields))
    with pytest.raises(AlreadyConfirmed):
        bills.confirm(draft.id, confirmed(draft.fields))
    assert _count("select count(*) from public.debt where account_number = %s", account) == 1


@pytest.mark.db
def test_in_db_a_bill_with_a_seeded_debtors_phone_saves_nothing(pg_bills):
    from harness.bills import PhoneExists
    from harness.db import find_debtor_by_phone

    bills, account = pg_bills
    draft = bills.upload(PNG, "image/png")
    sam = "+12135550101"  # Sam Doe's number in the seed
    with pytest.raises(PhoneExists):
        bills.confirm(draft.id, confirmed(draft.fields, phone=sam))
    assert _count("select count(*) from public.debt where account_number = %s", account) == 0
    found = find_debtor_by_phone(sam)
    assert found is not None and (found.first_name, found.last_name) == ("Sam", "Doe")
