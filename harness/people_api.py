"""The people endpoints: add a person, archive one, restore one.

``POST /api/people`` takes the form's fields (see ``harness.people.FORM_FIELDS``),
checks them in code and saves the debtor, phone and debt in one transaction with
an audit row. 422 names each bad field; 409 means the account number or the phone
number is already on file and nothing was written. Consent is only recorded when
``consent_source`` and ``consent_date`` are both given.

``POST /api/people/{account}/archive`` and ``/restore`` set and clear
``archived_at``. There is no endpoint that deletes anything, and none that sets
consent on its own. Every response masks the phone to its last four digits.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException

from harness.people import (
    AccountTaken,
    InvalidPerson,
    PersonNotFound,
    PhoneTaken,
    WrongArchiveState,
    add_person,
    archive_person,
    restore_person,
)

router = APIRouter()
_ACCOUNT = re.compile(r"[A-Za-z0-9-]{1,40}")


def _account(value: str) -> str:
    if not _ACCOUNT.fullmatch(value):
        raise HTTPException(404, f"no account {value!r}")
    return value


@router.post("/api/people", status_code=201)
def create_person(body: dict[str, Any]) -> dict[str, Any]:
    try:
        person = add_person(body)
    except InvalidPerson as exc:
        raise HTTPException(422, {"problems": exc.problems}) from exc
    except AccountTaken as exc:
        raise HTTPException(
            409, {"field": "account_number", "message": f"account number {exc} is already on file"}
        ) from exc
    except PhoneTaken as exc:
        raise HTTPException(
            409,
            {
                "field": "phone",
                "message": f"this phone number is already on file under account {exc}",
            },
        ) from exc
    return person.as_json()


@router.post("/api/people/{account}/archive")
def archive(account: str) -> dict[str, Any]:
    try:
        return archive_person(_account(account)).as_json()
    except PersonNotFound as exc:
        raise HTTPException(404, f"no account {account!r}") from exc
    except WrongArchiveState as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/people/{account}/restore")
def restore(account: str) -> dict[str, Any]:
    try:
        return restore_person(_account(account)).as_json()
    except PersonNotFound as exc:
        raise HTTPException(404, f"no account {account!r}") from exc
    except WrongArchiveState as exc:
        raise HTTPException(409, str(exc)) from exc
