"""The operator: the chat agent behind the text box where a person types "call Sam Doe".

``Operator`` wraps the same ``Agent`` the calls use, with its own system prompt
(``instructions/operator.md``) and four tools:

  - ``find_debtor(query)``: debts whose debtor's name or phone number matches,
    each as ``{id, name, phone}`` with the phone masked to its last four digits.
    The ``id`` is the debt's account number; it names one debt and its debtor,
    and it is what the other tools take as ``debtor_id``.
  - ``check_call(debtor_id)``: the pre-call checks as they stand right now
    (``dialer.service.plan_queue``, the dialer's own dry run): allow or block,
    with each rule id and reason. It only reads, like ``GET /api/precall``.
  - ``place_call(debtor_id)``: does **not** dial. It looks the debt up and
    records a pending approval that names the debtor and their masked phone, so
    the person approves who will be called, not just an id the model chose. An
    unknown id opens nothing, and nor do arguments that don't parse (``run_tool``
    answers those with an error).
  - ``call_status(call_id)``: one ``call_attempt`` row, found by its own id or
    by the AgentPhone call id.

Only ``decide(approval_id, approve)`` dials, and only for an approval that is
still pending. It is called by code (``POST /api/chat/approve``), never by the
model. First it makes sure AgentPhone is set up, so a missing key fails
before any attempt is recorded (and leaves the approval pending). Then the
decision is written to the history, so a second
click on the same approval finds nothing pending. Then it goes through
``dialer.service.dial_one``, which locks the debt, runs and records every
pre-call check again, records the attempt, and dials at most once. A debt the
checks block is not dialed, approval or not. The model never decides whether a
call is allowed: the person approves, the checks allow.

Pending approvals are not kept anywhere new: they are read back from the chat
history in the session store. A ``place_call`` result opens one; a message
that starts with ``[approval <id>]`` closes it. Only code writes tool results,
so only code can open one. Anyone (the model, or text typed in the box) could
write a closing marker, but closing one only ever means fewer calls.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from harness.agent import Agent
from harness.memory import Session
from harness.tools import Tool, ToolRegistry
from model import Provider
from phone.client import PhoneClient

DEFAULT_OPERATOR_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "instructions" / "operator.md"
)
# The operator gets no skills menu: the call skills are for the phone agent.
NO_SKILLS_DIR = Path(__file__).resolve().parent / "_operator_has_no_skills"
NEEDS_APPROVAL = "needs_approval"
MAX_MATCHES = 10
_ACCOUNT = re.compile(r"[A-Za-z0-9-]{1,40}")
_MARKER = re.compile(r"^\[approval (apr_[0-9a-f]{16})\]")

FIND_SQL = """
    select d.account_number, p.first_name || ' ' || p.last_name, ph.e164
    from public.debt d
    join public.debtor p on p.id = d.debtor_id
    left join lateral (
        select e164 from public.phone where debtor_id = p.id order by e164 limit 1
    ) ph on true
    where lower(p.first_name || ' ' || p.last_name) like %s
       or (%s <> '' and exists (
            select 1 from public.phone x
            where x.debtor_id = p.id and x.e164 like %s))
    order by d.account_number
    limit %s
"""

LOOKUP_SQL = """
    select p.first_name || ' ' || p.last_name, ph.e164
    from public.debt d
    join public.debtor p on p.id = d.debtor_id
    left join lateral (
        select e164 from public.phone where debtor_id = p.id order by e164 limit 1
    ) ph on true
    where d.account_number = %s
"""

STATUS_SQL = """
    select a.id::text, d.account_number, a.outcome, a.placed_at, a.connected,
           a.agentphone_call_id
    from public.call_attempt a
    join public.debt d on d.id = a.debt_id
    where a.id::text = %s or a.agentphone_call_id = %s
    order by a.placed_at desc
    limit 1
"""


def mask_phone(e164: str | None) -> str | None:
    """Only the last four digits, so the chat never shows a whole number."""
    if not e164:
        return None
    return "***-***-" + e164[-4:]


class PhoneUnavailable(RuntimeError):
    """AgentPhone isn't set up (keys or SDK missing), so no call can be placed."""


@dataclass(frozen=True)
class Approval:
    id: str
    debtor_id: str
    name: str
    phone: str | None  # masked
    tool_call_id: str


@dataclass
class ChatTurn:
    """What one chat message (or one approve/deny) produced, for the API."""

    reply: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_approvals: list[Approval] = field(default_factory=list)
    dial: dict[str, Any] | None = None  # what decide() did, if it ran


def pending_approvals(messages: list[dict]) -> list[Approval]:
    """Approvals opened by a ``place_call`` result and not yet closed, oldest first."""
    closed = set()
    for m in messages:
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            if match := _MARKER.match(m["content"]):
                closed.add(match.group(1))
    opened: list[Approval] = []
    for m in messages:
        if m.get("role") != "tool":
            continue
        try:
            body = json.loads(m.get("content") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(body, dict) and body.get("status") == NEEDS_APPROVAL:
            approval = Approval(
                id=str(body.get("approval_id")),
                debtor_id=str(body.get("debtor_id")),
                name=str(body.get("name")),
                phone=body.get("phone"),
                tool_call_id=m.get("tool_call_id", ""),
            )
            if approval.id not in closed:
                opened.append(approval)
    return opened


def _tool_calls_since(messages: list[dict], start: int) -> list[dict[str, Any]]:
    """Each tool request made in ``messages[start:]``, with the result it got."""
    results = {m.get("tool_call_id"): m.get("content") for m in messages if m.get("role") == "tool"}
    out = []
    for m in messages[start:]:
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            out.append(
                {
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments"),
                    "result": results.get(call.get("id")),
                }
            )
    return out


class _PhoneFromEnv:
    """AgentPhone, made from the environment only when a call is really placed, so
    chatting, and approving a call the checks then block, need no AgentPhone keys."""

    def __init__(self) -> None:
        self._client: PhoneClient | None = None

    def ready(self) -> None:
        """Build the client now; raise ``PhoneUnavailable`` if AgentPhone isn't set up."""
        if self._client is not None:
            return
        from phone.client import AgentPhoneClient

        try:
            client = AgentPhoneClient.from_env()
            client._client()  # imports the SDK
        except RuntimeError as exc:
            raise PhoneUnavailable(str(exc)) from exc
        self._client = client

    def make_call(
        self,
        to_number: str,
        *,
        disable_recording: bool | None = None,
        ring_timeout_seconds: int | None = None,
    ) -> str:
        self.ready()
        assert self._client is not None
        return self._client.make_call(
            to_number,
            disable_recording=disable_recording,
            ring_timeout_seconds=ring_timeout_seconds,
        )


class Operator:
    """One chat session's operator agent. Make a new one per request; the session keeps the rest.

    ``connect`` opens the database, ``phone`` places calls (by default AgentPhone,
    made when a call is approved), and ``clock`` says what time the checks run at.
    ``lookup(account_number)`` gives ``(name, e164)`` for a debt, or None; it
    reads the database unless a test passes its own.
    """

    def __init__(
        self,
        session: Session,
        *,
        provider: Provider | None = None,
        model: str | None = None,
        connect: Callable[[], psycopg.Connection] | None = None,
        phone: PhoneClient | None = None,
        clock: Callable[[], datetime] | None = None,
        lookup: Callable[[str], tuple[str, str | None] | None] | None = None,
        system_prompt_path: str | Path = DEFAULT_OPERATOR_PROMPT_PATH,
    ) -> None:
        if connect is None:
            from harness.db import connect as connect_db

            connect = connect_db
        self._connect = connect
        self._phone = phone or _PhoneFromEnv()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lookup = lookup or self._lookup_in_db
        self.session = session
        self.agent = Agent(
            model=model,
            provider=provider,
            system_prompt_path=system_prompt_path,
            tools=self.tools(),
            session=session,
            skills_dir=NO_SKILLS_DIR,
        )

    # --- the tools -----------------------------------------------------------

    def tools(self) -> ToolRegistry:
        debtor = "The id find_debtor returned, for example 'ACC-1001'."
        return ToolRegistry(
            [
                Tool(
                    "find_debtor",
                    "Search debtors by name or phone number. Returns each match's id, "
                    "name and masked phone number.",
                    {"query": "A name, part of a name, or phone digits."},
                    self.find_debtor,
                ),
                Tool(
                    "check_call",
                    "Run the pre-call checks for one debtor right now. Returns whether "
                    "a call is allowed and, for each rule, its id and reason.",
                    {"debtor_id": debtor},
                    self.check_call,
                ),
                Tool(
                    "place_call",
                    "Ask to call one debtor. This does not dial: the person must approve "
                    "it first, and the pre-call checks run again before any dial.",
                    {"debtor_id": debtor},
                    self.place_call,
                ),
                Tool(
                    "call_status",
                    "The status of a call attempt, by attempt id or call id.",
                    {"call_id": "The attempt id or call id from an earlier result."},
                    self.call_status,
                ),
            ]
        )

    def find_debtor(self, query: str) -> str:
        words = " ".join(query.split()).lower()
        digits = re.sub(r"\D", "", query)
        if not words:
            return "error: give a name or phone digits to search for"
        # A phone search needs a few digits, so "1" doesn't match every number.
        phone_like = f"%{digits}%" if len(digits) >= 4 else ""
        with self._connect() as conn:
            rows = conn.execute(
                FIND_SQL, (f"%{words}%", phone_like, phone_like, MAX_MATCHES)
            ).fetchall()
        matches = [
            {"id": account, "name": name, "phone": mask_phone(e164)} for account, name, e164 in rows
        ]
        if not matches:
            return f"error: no debtor matches {query!r}"
        return json.dumps({"matches": matches})

    def check_call(self, debtor_id: str) -> str:
        from dialer.service import plan_queue

        if not _ACCOUNT.fullmatch(debtor_id):
            return f"error: {debtor_id!r} is not a debtor id"
        when = self._clock()
        with self._connect() as conn:
            (outcome,) = plan_queue([debtor_id], when, conn).outcomes
            conn.rollback()  # the dry run only reads
        return json.dumps(
            {
                "debtor_id": debtor_id,
                "at": when.isoformat(),
                "decision": "block" if outcome.blocked else "allow",
                "rules": [
                    {"rule_id": d.rule_id, "allow": d.allow, "reason": d.reason}
                    for d in outcome.decisions
                ],
            }
        )

    def place_call(self, debtor_id: str) -> str:
        if not _ACCOUNT.fullmatch(debtor_id):
            return f"error: {debtor_id!r} is not a debtor id; nothing was dialed"
        found = self._lookup(debtor_id)
        if found is None:
            return f"error: no debtor {debtor_id!r}; nothing was dialed"
        name, e164 = found
        return json.dumps(
            {
                "status": NEEDS_APPROVAL,
                "approval_id": "apr_" + secrets.token_hex(8),
                "debtor_id": debtor_id,
                "name": name,
                "phone": mask_phone(e164),
                "note": "Nothing has been dialed. The person must approve this call, "
                "and the pre-call checks run again before dialing.",
            }
        )

    def _lookup_in_db(self, account_number: str) -> tuple[str, str | None] | None:
        with self._connect() as conn:
            row = conn.execute(LOOKUP_SQL, (account_number,)).fetchone()
        return (row[0], row[1]) if row else None

    def call_status(self, call_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(STATUS_SQL, (call_id, call_id)).fetchone()
        if row is None:
            return f"error: no call attempt {call_id!r}"
        attempt_id, account, outcome, placed_at, connected, agentphone_id = row
        return json.dumps(
            {
                "attempt_id": attempt_id,
                "debtor_id": account,
                "outcome": outcome,
                "placed_at": placed_at.isoformat(),
                "connected": connected,
                "call_id": agentphone_id,
            }
        )

    # --- chatting and approving ------------------------------------------------

    def pending(self) -> list[Approval]:
        return pending_approvals(self.agent.messages)

    def chat(self, message: str) -> ChatTurn:
        """One message from the person: the agent loop, then what it asked for."""
        start = len(self.agent.messages)
        reply = self.agent.run(message)
        return ChatTurn(
            reply=reply,
            tool_calls=_tool_calls_since(self.agent.messages, start),
            pending_approvals=self.pending(),
        )

    def decide(self, approval_id: str, approve: bool) -> ChatTurn:
        """Approve or deny a pending ``place_call``. Approving dials through ``dial_one``.

        Raises ``LookupError`` if ``approval_id`` is not pending (never opened,
        already decided, or closed some other way), and ``PhoneUnavailable`` if
        AgentPhone isn't set up; then nothing is recorded and it stays pending.
        """
        approval = next((a for a in self.pending() if a.id == approval_id), None)
        if approval is None:
            raise LookupError(f"no pending approval {approval_id!r}")
        if approve and isinstance(self._phone, _PhoneFromEnv):
            # Before any attempt is recorded: dial_one would take a setup error for
            # a call that may have rung, and count it against the debtor's limit.
            self._phone.ready()
        marker = f"[approval {approval.id}]"
        verb = "approved" if approve else "denied"
        # Close it before dialing, so a repeat of this request finds nothing to dial.
        self.agent._add(
            {"role": "user", "content": f"{marker} {verb}: place_call for {approval.debtor_id}"}
        )
        if not approve:
            reply = f"{marker} Not dialed: you denied the call to {approval.name}."
            self.agent._add({"role": "assistant", "content": reply})
            return ChatTurn(reply=reply, pending_approvals=self.pending())

        dial = self._dial(approval.debtor_id)
        if dial["blocked"]:
            reasons = "; ".join(dial["reasons"])
            reply = f"{marker} Not dialed: the checks block {approval.name} ({reasons})."
        elif dial["outcome"] == "placed":
            reply = f"{marker} Calling {approval.name}. Call id {dial['call_id']}."
        else:
            reply = (
                f"{marker} The call to {approval.name} came back {dial['outcome']}. "
                f"Attempt {dial['attempt_id']}."
            )
        self.agent._add({"role": "assistant", "content": reply})
        return ChatTurn(reply=reply, pending_approvals=self.pending(), dial=dial)

    def _dial(self, debtor_id: str) -> dict[str, Any]:
        from dialer.service import dial_one

        with self._connect() as conn:
            o = dial_one(conn, debtor_id, self._clock(), self._phone)
        return {
            "debtor_id": debtor_id,
            "blocked": o.blocked,
            "reasons": o.reasons,
            "outcome": o.outcome,
            "attempt_id": o.attempt_id,
            "call_id": o.call_id,
        }
