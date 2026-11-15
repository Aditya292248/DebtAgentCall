"""Bill upload: read a bill into a draft, and save nothing until a person confirms it.

A wrong amount read off a bill would become a false statement on a call, so the
model only ever produces a **draft**. Nothing goes into ``debtor``, ``phone`` or
``debt`` until a person has looked at every field and confirmed it.

``Bills.upload(data, content_type)``:
  1. checks the file is a PNG, JPEG, WebP or PDF (by its first bytes, not only
     the type the browser sent) and no bigger than ``MAX_BYTES``,
  2. stores it through a ``BillStore`` (a private Supabase Storage bucket named
     ``bills``, or a local folder for tests and offline use), under a key made
     from the draft id and the file's hash, never the uploaded file name,
  3. asks the vision model (through ``model.chat``, so ``FakeProvider`` works)
     for the fields as printed on the bill,
  4. normalises them in code (the balance as printed becomes integer cents; a
     US phone number becomes E.164) and checks them with ``validate``,
  5. saves the draft with a ``flags`` map of every field that is missing, that
     the model said it was unsure of, or that failed a check, plus an audit row.

``Bills.confirm(draft_id, fields)`` takes the fields as the person edited them
and checks them again with ``validate``, strictly this time: the balance must
be a positive integer number of cents, the phone must already be E.164, and so
on. Only then does it save the debtor, the phone and the debt, mark the draft
confirmed, and write an audit row, all in one transaction. A draft confirms
once.

A bill is not consent to be called. The phone is saved with no ``consent_at``,
so ``compliance.check_consent`` blocks every call to it until consent is
recorded some other way. A bill also has no date of birth (which the call's
identity check needs) or line type (cell or landline, which the TCPA rules
need), so the draft always flags those two for the person to fill in.

Two backends hold drafts and do the saving: ``PostgresBillDb`` (the
``bill_draft`` table, migration ``*_bill_draft.sql``) and ``MemoryBillDb``,
which keeps everything in dicts so tests and the demo run without a database.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

import httpx
import psycopg

from compliance.decision import Decision, allow
from model.client import chat
from model.provider import Provider, _load_dotenv

MAX_BYTES = 10 * 1024 * 1024
MAX_BALANCE_CENTS = 100_000_000_00  # one hundred million dollars: anything above is a misread
BUCKET = "bills"

# What a file starts with, for each type we take.
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/webp": (b"RIFF",),  # and b"WEBP" at offset 8, checked below
    "application/pdf": (b"%PDF-",),
}
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "application/pdf": "pdf"}

# Every field a confirmed bill has, in the order a form shows them.
FIELDS = (
    "first_name",
    "last_name",
    "address",
    "zip",
    "state",
    "phone",
    "line_type",
    "dob",
    "creditor",
    "balance_cents",
    "account_reference",
    "statement_date",
)
# Never printed on a bill: the person always has to add these.
NOT_ON_A_BILL = {"dob": "not on a bill; enter the date of birth", "line_type": "not on a bill"}
OPTIONAL = {"statement_date"}

_E164 = re.compile(r"\+[1-9][0-9]{1,14}")
_ZIP = re.compile(r"[0-9]{5}(-[0-9]{4})?")
_STATE = re.compile(r"[A-Z]{2}")
_ACCOUNT = re.compile(r"[A-Za-z0-9-]{1,40}")  # what the operator tools accept as an id
DRAFT_ID = re.compile(r"bill_[0-9a-f]{32}")
_MONEY = re.compile(r"[0-9]+(\.[0-9]{1,2})?")

EXTRACT_PROMPT = """\
Read this bill and reply with one JSON object, nothing else:
{"fields": {"first_name": ..., "last_name": ..., "address": ..., "zip": ...,
 "state": ..., "phone": ..., "creditor": ..., "balance": ...,
 "account_reference": ..., "statement_date": ...},
 "unsure": [names of fields you are not sure you read right]}
Copy each value exactly as printed. "balance" is the amount due as printed,
such as "$1,234.56". "state" is the two-letter code. "statement_date" is
YYYY-MM-DD. Use null for anything that is not on the bill. Do not guess."""


class BillRejected(ValueError):
    """The upload is not a bill file we take (type, size or contents)."""


class InvalidFields(ValueError):
    """The fields given to confirm failed a check; ``problems`` says which and why."""

    def __init__(self, problems: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in problems.items()))
        self.problems = problems


class DraftNotFound(LookupError):
    pass


class AlreadyConfirmed(RuntimeError):
    pass


class AccountExists(RuntimeError):
    """A debt with this account number is already saved."""


@dataclass(frozen=True)
class Draft:
    id: str
    storage_key: str
    content_type: str
    sha256: str
    fields: dict[str, Any]  # every name in FIELDS; None when unknown
    flags: dict[str, str]  # field -> why a person must look at it
    status: str = "draft"  # or "confirmed"
    debt_id: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "draft_id": self.id,
            "status": self.status,
            "fields": dict(self.fields),
            "flags": dict(self.flags),
            "debt_id": self.debt_id,
        }


@dataclass(frozen=True)
class Saved:
    draft_id: str
    debtor_id: str
    debt_id: str
    account_number: str


# --- checks --------------------------------------------------------------------


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return " ".join(value.split())
    return None


def _iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def validate(fields: dict[str, Any], today: date | None = None) -> dict[str, str]:
    """Every problem with ``fields`` as a person confirms them; empty means they can be saved.

    Strict on purpose: nothing here converts or guesses. The balance must be an
    ``int`` (not a float, a bool or "12.50"), and the phone must already be E.164.
    """
    today = today or datetime.now(UTC).date()
    problems: dict[str, str] = {}
    for name in ("first_name", "last_name", "address", "creditor"):
        if _text(fields.get(name)) is None:
            problems[name] = "missing"

    zip_code = fields.get("zip")
    if not isinstance(zip_code, str) or not _ZIP.fullmatch(zip_code):
        problems["zip"] = "must be a 5-digit ZIP, or ZIP+4 like 90012-1234"
    state = fields.get("state")
    if not isinstance(state, str) or not _STATE.fullmatch(state):
        problems["state"] = "must be a two-letter state code like CA"
    phone = fields.get("phone")
    if not isinstance(phone, str) or not _E164.fullmatch(phone):
        problems["phone"] = "must be E.164, like +12135550123"
    if fields.get("line_type") not in ("cell", "landline"):
        problems["line_type"] = "must be cell or landline"

    dob = _iso_date(fields.get("dob"))
    if dob is None:
        problems["dob"] = "must be a date, YYYY-MM-DD"
    elif dob >= today or dob.year < 1900:
        problems["dob"] = "must be a real date of birth in the past"

    balance = fields.get("balance_cents")
    if type(balance) is not int:
        problems["balance_cents"] = "must be a whole number of cents"
    elif not 0 < balance <= MAX_BALANCE_CENTS:
        problems["balance_cents"] = "must be more than 0 and at most $100,000,000.00"

    account = fields.get("account_reference")
    if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
        problems["account_reference"] = "must be 1-40 letters, digits or dashes"

    statement = fields.get("statement_date")
    if statement is not None:
        when = _iso_date(statement)
        if when is None:
            problems["statement_date"] = "must be a date, YYYY-MM-DD, or empty"
        elif when > today:
            problems["statement_date"] = "is in the future"

    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        problems["fields"] = f"unknown fields: {', '.join(unknown)}"
    return problems


def cents_from_printed(value: Any) -> int | None:
    """``"$1,234.56"`` -> 123456. None for anything that isn't plainly an amount."""
    if not isinstance(value, str):  # a bare number could be dollars or cents: ask the person
        return None
    text = value.strip().replace(",", "").replace(" ", "")
    if text.startswith("USD"):
        text = text[3:]
    text = text.removeprefix("$")
    if not _MONEY.fullmatch(text):
        return None
    try:
        return int(Decimal(text) * 100)
    except InvalidOperation:
        return None


def e164_from_printed(value: Any) -> str | None:
    """A US number as printed (``(213) 555-0123``) in E.164, or None if it isn't one."""
    if not isinstance(value, str):
        return None
    if _E164.fullmatch(value.strip()):
        return value.strip()
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def _model_json(text: str) -> dict[str, Any] | None:
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(body)
    except ValueError:
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(body[start : end + 1])
        except ValueError:
            return None
    return parsed if isinstance(parsed, dict) else None


def draft_fields(reply: str, today: date | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """What the model read, normalised in code, and the flags for every field to look at."""
    parsed = _model_json(reply)
    fields: dict[str, Any] = dict.fromkeys(FIELDS)
    flags: dict[str, str] = {}
    if parsed is None:
        flags = {name: "the model's reply could not be read; enter it" for name in FIELDS}
        flags.update(NOT_ON_A_BILL)
        return fields, flags
    raw = parsed.get("fields") if isinstance(parsed.get("fields"), dict) else {}
    assert isinstance(raw, dict)
    listed = parsed.get("unsure")
    unsure = {u for u in listed if isinstance(u, str)} if isinstance(listed, list) else set()

    for name in ("first_name", "last_name", "address", "creditor"):
        fields[name] = _text(raw.get(name))
    zip_code = _text(raw.get("zip"))
    fields["zip"] = zip_code
    state = _text(raw.get("state"))
    fields["state"] = state.upper() if state else None
    fields["phone"] = e164_from_printed(raw.get("phone")) or _text(raw.get("phone"))
    fields["balance_cents"] = cents_from_printed(raw.get("balance"))
    fields["account_reference"] = _text(raw.get("account_reference"))
    fields["statement_date"] = _text(raw.get("statement_date"))

    for name, problem in validate(fields, today).items():
        if name in NOT_ON_A_BILL:
            flags[name] = NOT_ON_A_BILL[name]
        elif fields.get(name) is None and not (name == "balance_cents" and raw.get("balance")):
            flags[name] = "missing from the bill"
        elif name == "balance_cents":
            flags[name] = f"could not read {raw.get('balance')!r} as an amount"
        else:
            flags[name] = problem
    if fields["statement_date"] is None:
        flags.setdefault("statement_date", "missing from the bill (optional)")
    model_name = {"balance": "balance_cents"}
    for name in unsure:
        key = model_name.get(name, name)
        if key in FIELDS:
            flags.setdefault(key, "the model was not sure it read this right")
    return fields, flags


def sniff(data: bytes, content_type: str | None) -> str:
    """The file's type from its first bytes; raises ``BillRejected`` for anything else."""
    if not data:
        raise BillRejected("the file is empty")
    if len(data) > MAX_BYTES:
        raise BillRejected(f"the file is bigger than {MAX_BYTES // (1024 * 1024)} MB")
    for kind, starts in _MAGIC.items():
        if data.startswith(starts) and (kind != "image/webp" or data[8:12] == b"WEBP"):
            sent = (content_type or "").split(";")[0].strip().lower()
            if sent not in ("", "application/octet-stream", kind):
                raise BillRejected(f"the file is {kind} but was sent as {sent}")
            return kind
    raise BillRejected("only PNG, JPEG, WebP or PDF bills are taken")


def vision_messages(data: bytes, kind: str) -> list[dict]:
    """The one user message that shows the model the bill."""
    url = f"data:{kind};base64,{base64.b64encode(data).decode('ascii')}"
    if kind == "application/pdf":
        part: dict = {"type": "file", "file": {"filename": "bill.pdf", "file_data": url}}
    else:
        part = {"type": "image_url", "image_url": {"url": url}}
    return [{"role": "user", "content": [{"type": "text", "text": EXTRACT_PROMPT}, part]}]


# --- where the file goes ---------------------------------------------------------


class BillStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...


class LocalBillStore:
    """Bills in a folder on disk, for tests and for running without Supabase."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"bad storage key {key!r}")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()


class SupabaseBillStore:
    """The private ``bills`` bucket in Supabase Storage, written with the service key."""

    def __init__(self, url: str, service_key: str, bucket: str = BUCKET) -> None:
        self.base = f"{url.rstrip('/')}/storage/v1/object/{bucket}"
        self.headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}

    def put(self, key: str, data: bytes, content_type: str) -> None:
        resp = httpx.post(
            f"{self.base}/{key}",
            content=data,
            headers={**self.headers, "Content-Type": content_type, "x-upsert": "false"},
            timeout=30,
        )
        resp.raise_for_status()

    def get(self, key: str) -> bytes:
        resp = httpx.get(f"{self.base}/{key}", headers=self.headers, timeout=30)
        resp.raise_for_status()
        return resp.content


def store_from_env() -> BillStore:
    """Supabase Storage when ``SUPABASE_URL`` and ``SUPABASE_SERVICE_ROLE_KEY`` are set,
    else the folder in ``BILLS_DIR`` (default ``.bills/``)."""
    _load_dotenv()
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if url and key:
        return SupabaseBillStore(url, key)
    return LocalBillStore(os.environ.get("BILLS_DIR") or ".bills")


# --- where drafts live, and the one place that saves -----------------------------


class BillDb(Protocol):
    def add_draft(self, draft: Draft, decision: Decision) -> None:
        """Keep the draft and its audit row. Saves no debtor, phone or debt."""

    def get_draft(self, draft_id: str) -> Draft | None: ...

    def confirm(self, draft_id: str, fields: dict[str, Any], decision: Decision) -> Saved:
        """Save debtor, phone (no consent) and debt, mark the draft confirmed and
        write the audit row, all or nothing. Raises ``DraftNotFound``,
        ``AlreadyConfirmed`` or ``AccountExists``."""


INSERT_DRAFT_SQL = """
    insert into public.bill_draft (id, storage_key, content_type, sha256, fields, flags)
    values (%s, %s, %s, %s, %s, %s)
"""
GET_DRAFT_SQL = """
    select id, storage_key, content_type, sha256, fields, flags, status, debt_id::text
    from public.bill_draft where id = %s
"""
LOCK_DRAFT_SQL = "select status from public.bill_draft where id = %s for update"
ACCOUNT_TAKEN_SQL = "select 1 from public.debt where account_number = %s"
INSERT_DEBTOR_SQL = """
    insert into public.debtor (first_name, last_name, dob, address, zip, state)
    values (%s, %s, %s, %s, %s, %s) returning id::text
"""
# consent_at and consent_source stay null: a bill is not consent to be called.
INSERT_PHONE_SQL = """
    insert into public.phone (debtor_id, e164, line_type) values (%s, %s, %s)
"""
INSERT_DEBT_SQL = """
    insert into public.debt (debtor_id, account_number, creditor, balance_cents)
    values (%s, %s, %s, %s) returning id::text
"""
CONFIRM_DRAFT_SQL = """
    update public.bill_draft
    set status = 'confirmed', confirmed_at = now(), confirmed_fields = %s, debt_id = %s
    where id = %s
"""


class PostgresBillDb:
    def __init__(self, connect: Callable[[], psycopg.Connection] | None = None) -> None:
        if connect is None:
            from harness.db import connect as connect_db

            connect = connect_db
        self._connect = connect

    def add_draft(self, draft: Draft, decision: Decision) -> None:
        from psycopg.types.json import Jsonb

        from compliance.audit import record

        with self._connect() as conn:
            conn.execute(
                INSERT_DRAFT_SQL,
                (
                    draft.id,
                    draft.storage_key,
                    draft.content_type,
                    draft.sha256,
                    Jsonb(draft.fields),
                    Jsonb(draft.flags),
                ),
            )
            record(conn, decision, kind="bill")

    def get_draft(self, draft_id: str) -> Draft | None:
        with self._connect() as conn:
            row = conn.execute(GET_DRAFT_SQL, (draft_id,)).fetchone()
        if row is None:
            return None
        did, key, kind, sha, fields, flags, status, debt_id = row
        return Draft(did, key, kind, sha, dict(fields), dict(flags), status, debt_id)

    def confirm(self, draft_id: str, fields: dict[str, Any], decision: Decision) -> Saved:
        account = fields["account_reference"]
        with self._connect() as conn:
            row = conn.execute(LOCK_DRAFT_SQL, (draft_id,)).fetchone()
            if row is None:
                raise DraftNotFound(draft_id)
            if row[0] != "draft":
                raise AlreadyConfirmed(draft_id)
            if conn.execute(ACCOUNT_TAKEN_SQL, (account,)).fetchone():
                raise AccountExists(account)
            try:
                return self._save(conn, draft_id, fields, decision)
            except psycopg.errors.UniqueViolation as exc:  # saved by someone else just now
                raise AccountExists(account) from exc

    @staticmethod
    def _save(
        conn: psycopg.Connection, draft_id: str, fields: dict[str, Any], decision: Decision
    ) -> Saved:
        from psycopg.types.json import Jsonb

        from compliance.audit import record

        debtor_row = conn.execute(
            INSERT_DEBTOR_SQL,
            (
                _text(fields["first_name"]),
                _text(fields["last_name"]),
                date.fromisoformat(fields["dob"]),
                _text(fields["address"]),
                fields["zip"],
                fields["state"],
            ),
        ).fetchone()
        assert debtor_row is not None
        debtor_id = debtor_row[0]
        conn.execute(INSERT_PHONE_SQL, (debtor_id, fields["phone"], fields["line_type"]))
        account = fields["account_reference"]
        debt_row = conn.execute(
            INSERT_DEBT_SQL,
            (debtor_id, account, _text(fields["creditor"]), fields["balance_cents"]),
        ).fetchone()
        assert debt_row is not None
        debt_id = debt_row[0]
        conn.execute(CONFIRM_DRAFT_SQL, (Jsonb(fields), debt_id, draft_id))
        record(conn, decision, kind="bill", debt_id=debt_id)
        return Saved(draft_id, debtor_id, debt_id, account)


@dataclass
class MemoryBillDb:
    """Drafts and saved rows in dicts: the offline backend for tests and the demo."""

    drafts: dict[str, Draft] = field(default_factory=dict)
    debtors: dict[str, dict[str, Any]] = field(default_factory=dict)
    phones: list[dict[str, Any]] = field(default_factory=list)
    debts: dict[str, dict[str, Any]] = field(default_factory=dict)
    audit: list[tuple[Decision, str | None]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_draft(self, draft: Draft, decision: Decision) -> None:
        with self._lock:
            self.drafts[draft.id] = draft
            self.audit.append((decision, None))

    def get_draft(self, draft_id: str) -> Draft | None:
        return self.drafts.get(draft_id)

    def confirm(self, draft_id: str, fields: dict[str, Any], decision: Decision) -> Saved:
        with self._lock:
            draft = self.drafts.get(draft_id)
            if draft is None:
                raise DraftNotFound(draft_id)
            if draft.status != "draft":
                raise AlreadyConfirmed(draft_id)
            account = fields["account_reference"]
            if any(d["account_number"] == account for d in self.debts.values()):
                raise AccountExists(account)
            debtor_id, debt_id = str(uuid.uuid4()), str(uuid.uuid4())
            self.debtors[debtor_id] = {
                k: fields[k] for k in ("first_name", "last_name", "dob", "address", "zip", "state")
            }
            self.phones.append(
                {
                    "debtor_id": debtor_id,
                    "e164": fields["phone"],
                    "line_type": fields["line_type"],
                    "consent_at": None,
                }
            )
            self.debts[debt_id] = {
                "debtor_id": debtor_id,
                "account_number": account,
                "creditor": fields["creditor"],
                "balance_cents": fields["balance_cents"],
            }
            self.drafts[draft_id] = replace(draft, status="confirmed", debt_id=debt_id)
            self.audit.append((decision, debt_id))
        return Saved(draft_id, debtor_id, debt_id, account)


# --- the service -----------------------------------------------------------------


class Bills:
    """Upload a bill into a draft; confirm a draft into a debtor, phone and debt."""

    def __init__(
        self,
        *,
        store: BillStore,
        db: BillDb,
        provider: Provider | None = None,
        model: str | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.store = store
        self.db = db
        self.provider = provider
        self.model = model
        self._today = today or (lambda: datetime.now(UTC).date())

    def upload(self, data: bytes, content_type: str | None) -> Draft:
        kind = sniff(data, content_type)
        draft_id = "bill_" + uuid.uuid4().hex
        sha = hashlib.sha256(data).hexdigest()
        key = f"{draft_id}/{sha}.{_EXT[kind]}"
        self.store.put(key, data, kind)
        reply = chat(
            vision_messages(data, kind), model=self.model, provider=self.provider, max_tokens=1024
        )
        fields, flags = draft_fields(reply.content, self._today())
        draft = Draft(draft_id, key, kind, sha, fields, flags)
        decision = allow(
            "bill_upload",
            "bill read into a draft; nothing is saved until a person confirms it",
            {"draft_id": draft_id, "sha256": sha, "content_type": kind, "flagged": sorted(flags)},
        )
        self.db.add_draft(draft, decision)
        return draft

    def get(self, draft_id: str) -> Draft | None:
        if not DRAFT_ID.fullmatch(draft_id):
            return None
        return self.db.get_draft(draft_id)

    def confirm(self, draft_id: str, fields: dict[str, Any]) -> Saved:
        """Save the person's fields, after checking them again. Raises ``InvalidFields``
        before touching anything if a check fails."""
        problems = validate(fields, self._today())
        if problems:
            raise InvalidFields(problems)
        draft = self.get(draft_id)
        if draft is None:
            raise DraftNotFound(draft_id)
        clean = {name: fields.get(name) for name in FIELDS}
        edited = sorted(n for n in FIELDS if clean[n] != draft.fields.get(n))
        decision = allow(
            "bill_confirm",
            "a person confirmed the bill's fields; the phone has no consent on file",
            {"draft_id": draft_id, "account_number": clean["account_reference"], "edited": edited},
        )
        return self.db.confirm(draft_id, clean, decision)


def bills_from_env() -> Bills:
    return Bills(store=store_from_env(), db=PostgresBillDb())
