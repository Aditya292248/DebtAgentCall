"""Add a person (debtor, phone and debt) and archive or restore one, from the web UI.

``add_person`` checks the fields in code, then writes the debtor, phone and debt
in one transaction, with one audit row. It reuses ``harness.bills.validate``, the
check a confirmed bill goes through, and adds what a typed form needs: a state
that is a real code, a name length, and the optional consent record.

Consent is never assumed. A person added with no consent source and date is
saved, but ``check_consent`` keeps blocking calls to them until consent is
recorded some other way. Both consent fields or neither: one alone is refused.

``archive_person`` and ``restore_person`` set and clear ``debtor.archived_at``.
Nothing is deleted: debts, payments, call attempts and audit rows stay, because
the records are kept for three years (Reg F, 12 CFR 1006.100). An archived
person is blocked by ``compliance.archive.check_archived`` in the shared
pre-call checks, so the dialer and the operator chat can't call them.

A phone number belongs to one debtor (a live call finds its debtor by the number
dialed) and an account number to one debt. Both are checked under a lock, in the
same transaction as the insert, so two adds can't both pass; a refusal writes
nothing. The message names the account number of the existing record and nothing
else about that person.

``bills.confirm`` could call ``save_new_person`` below instead of its own insert,
so the two paths share one set of checks and one writer.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import psycopg

from compliance.decision import allow
from harness.bills import (
    ACCOUNT_TAKEN_SQL,
    INSERT_DEBT_SQL,
    INSERT_DEBTOR_SQL,
    INSERT_PHONE_SQL,
    LOCK_PHONE_SQL,
    PHONE_TAKEN_SQL,
    _iso_date,
    _text,
    validate,
)

US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC AS GU MP PR VI".split()
)
MAX_NAME = 80
MAX_TEXT = 200
FORM_FIELDS = (
    "first_name",
    "last_name",
    "dob",
    "address",
    "zip",
    "state",
    "phone",
    "line_type",
    "creditor",
    "account_number",
    "balance_cents",
    "consent_source",
    "consent_date",
)
ACTOR = "operator"  # the web UI has no sign-in yet; the audit row says it was the operator

ACCOUNT_OF_PHONE_SQL = """
    select d.account_number
    from public.phone ph join public.debt d on d.debtor_id = ph.debtor_id
    where ph.e164 = %s order by d.account_number limit 1
"""
LOCK_ACCOUNT_SQL = "select pg_advisory_xact_lock(hashtextextended('person_account:' || %s, 0))"
INSERT_PHONE_WITH_CONSENT_SQL = """
    insert into public.phone (debtor_id, e164, line_type, consent_source, consent_at)
    values (%s, %s, %s, %s, %s)
"""
PERSON_SQL = """
    select p.id::text, d.id::text, p.first_name || ' ' || p.last_name, p.archived_at
    from public.debt d join public.debtor p on p.id = d.debtor_id
    where d.account_number = %s
    for update of p
"""
PHONE_OF_SQL = "select e164 from public.phone where debtor_id = %s order by e164 limit 1"
ARCHIVE_SQL = "update public.debtor set archived_at = now() where id = %s"
RESTORE_SQL = "update public.debtor set archived_at = null where id = %s"


class InvalidPerson(ValueError):
    """The fields failed a check; ``problems`` maps each field to why."""

    def __init__(self, problems: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in problems.items()))
        self.problems = problems


class AccountTaken(RuntimeError):
    """A debt with this account number already exists."""


class PhoneTaken(RuntimeError):
    """This number is on file; the argument is the account number of that record."""


class PersonNotFound(LookupError):
    pass


class WrongArchiveState(RuntimeError):
    """Archiving someone already archived, or restoring someone who is not."""


@dataclass(frozen=True)
class PersonRef:
    account_number: str
    name: str
    phone_masked: str | None
    archived: bool
    consent_recorded: bool | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "account_number": self.account_number,
            "name": self.name,
            "phone": self.phone_masked,
            "archived": self.archived,
        }
        if self.consent_recorded is not None:
            out["consent_recorded"] = self.consent_recorded
        return out


def mask_phone(e164: str | None) -> str | None:
    """Only the last four digits, the way the operator chat shows a number."""
    return "***-***-" + e164[-4:] if e164 else None


# --- checks ----------------------------------------------------------------------


def check_person(fields: dict[str, Any], today: date | None = None) -> dict[str, str]:
    """Every problem with a new person's fields, by form field name; empty means valid."""
    today = today or datetime.now(UTC).date()
    unknown = sorted(set(fields) - set(FORM_FIELDS))
    if unknown:
        return {"fields": f"unknown fields: {', '.join(unknown)}"}
    shared = {
        "first_name": fields.get("first_name"),
        "last_name": fields.get("last_name"),
        "address": fields.get("address"),
        "zip": fields.get("zip"),
        "state": fields.get("state"),
        "phone": fields.get("phone"),
        "line_type": fields.get("line_type"),
        "dob": fields.get("dob"),
        "creditor": fields.get("creditor"),
        "balance_cents": fields.get("balance_cents"),
        "account_reference": fields.get("account_number"),
    }
    problems = {k: "is needed" if v == "missing" else v for k, v in validate(shared, today).items()}
    if "account_reference" in problems:
        problems["account_number"] = problems.pop("account_reference")
    for name in ("first_name", "last_name"):
        value = _text(fields.get(name))
        if value is not None and len(value) > MAX_NAME:
            problems[name] = f"must be at most {MAX_NAME} characters"
    for name in ("address", "creditor"):
        value = _text(fields.get(name))
        if value is not None and len(value) > MAX_TEXT:
            problems[name] = f"must be at most {MAX_TEXT} characters"
    state = fields.get("state")
    phone = fields.get("phone")
    if "phone" not in problems and not (isinstance(phone, str) and len(phone) >= 9):
        problems["phone"] = "must be E.164 with the country code, like +12135550123"
    if "state" not in problems and state not in US_STATES:
        problems["state"] = "is not a US state or territory code"

    source, when = _consent(fields)
    if source is None and when is not None:
        problems["consent_source"] = "is needed when a consent date is given"
    if when is None and source is not None:
        problems["consent_date"] = "is needed when a consent source is given"
    if source is not None and len(source) > MAX_TEXT:
        problems["consent_source"] = f"must be at most {MAX_TEXT} characters"
    raw_date = fields.get("consent_date")
    if _text(raw_date) is not None:
        parsed = _iso_date(raw_date)
        if parsed is None:
            problems["consent_date"] = "must be a date, YYYY-MM-DD"
        elif parsed > today or parsed.year < 1990:
            problems["consent_date"] = "must be a real date that is not in the future"
    return problems


def _consent(fields: dict[str, Any]) -> tuple[str | None, date | None]:
    """The consent source text and date as given; None for what is empty or not a date."""
    return _text(fields.get("consent_source")), _iso_date(fields.get("consent_date"))


# --- writes ----------------------------------------------------------------------


def save_new_person(conn: psycopg.Connection, fields: dict[str, Any]) -> tuple[str, str, str]:
    """Insert debtor, phone and debt on ``conn``; returns (debtor_id, debt_id, account).

    ``fields`` must already have passed ``check_person``. Takes the locks that keep
    a number and an account number unique, and raises ``PhoneTaken`` or
    ``AccountTaken`` before writing anything. The caller commits.
    """
    account = fields["account_number"]
    phone = fields["phone"]
    conn.execute(LOCK_ACCOUNT_SQL, (account,))
    if conn.execute(ACCOUNT_TAKEN_SQL, (account,)).fetchone():
        raise AccountTaken(account)
    conn.execute(LOCK_PHONE_SQL, (phone,))
    if conn.execute(PHONE_TAKEN_SQL, (phone,)).fetchone():
        row = conn.execute(ACCOUNT_OF_PHONE_SQL, (phone,)).fetchone()
        raise PhoneTaken(row[0] if row else "an existing record")
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
    source, when = _consent(fields)
    if source is not None and when is not None:
        consent_at = datetime(when.year, when.month, when.day, tzinfo=UTC)
        conn.execute(
            INSERT_PHONE_WITH_CONSENT_SQL,
            (debtor_id, phone, fields["line_type"], source, consent_at),
        )
    else:  # no consent: the TCPA check keeps blocking this number
        conn.execute(INSERT_PHONE_SQL, (debtor_id, phone, fields["line_type"]))
    debt_row = conn.execute(
        INSERT_DEBT_SQL,
        (debtor_id, account, _text(fields["creditor"]), fields["balance_cents"]),
    ).fetchone()
    assert debt_row is not None
    return debtor_id, debt_row[0], account


def _audit(conn: psycopg.Connection, rule: str, reason: str, debt_id: str, **evidence: Any) -> None:
    from compliance.audit import record

    record(
        conn,
        allow(rule, reason, {"actor": ACTOR, **evidence}),
        kind="person",
        debt_id=debt_id,
    )


def add_person(
    fields: dict[str, Any],
    connect: Callable[[], AbstractContextManager[psycopg.Connection]] | None = None,
    *,
    via: str = "the web UI",
    evidence: dict[str, Any] | None = None,
) -> PersonRef:
    """Validate, then save the person, phone and debt and one audit row, or nothing.

    ``via`` says where the request came from in the audit reason, and ``evidence``
    adds to the audit row (the operator chat adds its approval id)."""
    problems = check_person(fields)
    if problems:
        raise InvalidPerson(problems)
    if connect is None:
        from harness.db import connect as connect_db

        connect = connect_db
    with connect() as conn:  # one transaction: commits on success, rolls back on any error
        _debtor_id, debt_id, account = save_new_person(conn, fields)
        consent = _consent(fields)[0] is not None
        _audit(
            conn,
            "person_added",
            f"added {account} from {via}",
            debt_id,
            account_number=account,
            phone_last4=fields["phone"][-4:],
            consent_recorded=consent,
            **(evidence or {}),
        )
    return PersonRef(
        account_number=account,
        name=f"{_text(fields['first_name'])} {_text(fields['last_name'])}",
        phone_masked=mask_phone(fields["phone"]),
        archived=False,
        consent_recorded=consent,
    )


def _set_archived(
    account: str,
    archive: bool,
    connect: Callable[[], psycopg.Connection] | None,
) -> PersonRef:
    if connect is None:
        from harness.db import connect as connect_db

        connect = connect_db
    with connect() as conn:
        row = conn.execute(PERSON_SQL, (account,)).fetchone()
        if row is None:
            raise PersonNotFound(account)
        debtor_id, debt_id, name, archived_at = row
        # Wait for a dial that holds this debt's lock, so it can't pass its checks and
        # place a call after the archive: the dialer takes the same lock first.
        conn.execute("select 1 from public.debt where id = %s for update", (debt_id,))
        if archive and archived_at is not None:
            raise WrongArchiveState(f"{account} is already archived")
        if not archive and archived_at is None:
            raise WrongArchiveState(f"{account} is not archived")
        conn.execute(ARCHIVE_SQL if archive else RESTORE_SQL, (debtor_id,))
        _audit(
            conn,
            "person_archived" if archive else "person_restored",
            f"{'archived' if archive else 'restored'} {account} from the web UI",
            debt_id,
            account_number=account,
        )
        phone = conn.execute(PHONE_OF_SQL, (debtor_id,)).fetchone()
    return PersonRef(account, name, mask_phone(phone[0] if phone else None), archived=archive)


def archive_person(
    account: str, connect: Callable[[], psycopg.Connection] | None = None
) -> PersonRef:
    """Archive, never delete: the person leaves the list and is blocked from calls."""
    return _set_archived(account, True, connect)


def restore_person(
    account: str, connect: Callable[[], psycopg.Connection] | None = None
) -> PersonRef:
    """Bring an archived person back. Their consent, flags and history are as they were."""
    return _set_archived(account, False, connect)
