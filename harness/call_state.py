"""Call state: where each call is in its required steps, saved between turns.

Every call has to go in this order:

    GREETING -> VERIFY -> DISCLOSE -> DISCUSS -> WRAP

with two side exits, ``THIRD_PARTY`` (someone else answered) and ``STOPPED``
(the person asked us to stop; set by the in-call triggers, which come later).
``harness/stages.py`` decides each move. This file only holds the facts and
saves them.

Where it is saved: as a ``state`` record in the call's own session, next to its
messages (``Session.put_state`` / ``Session.last_state``). The newest record
wins. That is the simplest thing that survives a restart: the session is
already opened on every turn and written before the reply goes out, so there is
no second table to keep in step, and the old records show how the call moved
through its stages.

``Debtor`` is what the stages compare against: the person and the one debt the
call is about, read from the database by phone number.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum

from harness.memory import Session


class Stage(StrEnum):
    GREETING = "GREETING"  # ask for the person by name, say nothing else
    VERIFY = "VERIFY"  # date of birth and ZIP, compared in code
    DISCLOSE = "DISCLOSE"  # recording notice, AI notice, mini-Miranda: fixed text
    DISCUSS = "DISCUSS"  # the model talks about the debt
    WRAP = "WRAP"  # the call is ending
    THIRD_PARTY = "THIRD_PARTY"  # someone else: ask for a good time, nothing more
    STOPPED = "STOPPED"  # a stop trigger fired: fixed close only


@dataclass(frozen=True)
class Debtor:
    """The person and the debt a call is about, as the database has them."""

    debtor_id: str
    debt_id: str
    first_name: str
    last_name: str
    dob: date
    zip: str
    creditor: str
    balance_cents: int

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


@dataclass(frozen=True)
class CallState:
    call_id: str
    debtor_id: str
    debt_id: str
    stage: Stage = Stage.GREETING
    verified: bool = False
    disclosed: bool = False
    verify_attempts: int = 0
    turns: int = 0  # caller turns handled so far; 0 means we haven't greeted yet
    # During VERIFY: whether the date of birth / ZIP given so far matched (None =
    # not given yet). Only the yes/no is kept, never what the caller said.
    dob_ok: bool | None = None
    zip_ok: bool | None = None

    @classmethod
    def new(cls, call_id: str, debtor: Debtor) -> CallState:
        return cls(call_id=call_id, debtor_id=debtor.debtor_id, debt_id=debtor.debt_id)

    def to_dict(self) -> dict:
        return {**asdict(self), "stage": self.stage.value}

    @classmethod
    def from_dict(cls, data: dict) -> CallState:
        return cls(
            call_id=str(data["call_id"]),
            debtor_id=str(data["debtor_id"]),
            debt_id=str(data["debt_id"]),
            stage=Stage(data["stage"]),
            verified=bool(data["verified"]),
            disclosed=bool(data["disclosed"]),
            verify_attempts=int(data["verify_attempts"]),
            turns=int(data.get("turns", 0)),
            dob_ok=data.get("dob_ok"),
            zip_ok=data.get("zip_ok"),
        )


def load_state(session: Session) -> CallState | None:
    """The call's newest saved state, or None if nothing was saved yet."""
    data = session.last_state()
    return CallState.from_dict(data) if data else None


def save_state(session: Session, state: CallState) -> None:
    session.put_state(state.to_dict())
