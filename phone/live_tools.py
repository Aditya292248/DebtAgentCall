"""The call tools on a live call: one caller turn's ``CallContext`` and registry.

``phone/webhook.py`` builds a ``LiveTools`` for every turn the model answers,
from the call's saved state (stage, verified, disclosed), the debtor and the
number we called. The model gets ``registry()``: the six tools from
``harness/call_tools.py`` and nothing else, so no ``delegate`` or ``fan_out``
and no database lookup by name. Every call goes through ``CallTools``, which
runs the compliance gate first and then ``PolicyApprover``; both are audited.

Before DISCUSS the tools are not offered to the model (no schemas), as before.
A tool the model asks for anyway still runs through the gate, so
``lookup_account`` comes back as ``error: blocked`` rather than an account.
The state the gate reads is the server's, never anything the model said.

The backend (store, audit writer, flags, plan policy) is opened only when the
model first calls a tool, so a turn without tools never touches the database.
Every row a tool saves (plan, callback, dispute, escalation) is committed before
the tool returns, so the model only hears "approved" or "booked" once it is
durable. If opening the backend, saving, auditing or the final commit fails,
``failure`` says why and the webhook speaks a fixed line and hangs up instead of
the model's reply: nothing it said can promise what wasn't saved.
``before_first_save`` runs before the first row is written; the webhook uses it
to mark the webhook as started, so a retry after a crash never saves it twice.

A saved row is not enough: the reply must also match what code decided.
``check_commitment`` drops a sentence that says something was approved, agreed
or booked unless this turn saved an approved plan or a callback. When a plan
was only saved as pending, the webhook says ``PENDING_LINE`` instead.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any

from compliance import Contact, Decision, Flag, allow, block
from harness.call_state import CallState, Debtor
from harness.call_tools import (
    ESCALATED,
    AuditWriter,
    CallContext,
    CallStore,
    CallTools,
    MemoryCallStore,
    PaymentPlan,
    PgCallStore,
    PlanPolicy,
    audit_to_conn,
    load_plan_policy,
)
from harness.tools import Tool, ToolRegistry

log = logging.getLogger(__name__)

COMMITMENT_RULE = "unbacked_commitment"
# Words that tell the caller something is settled. Negated forms ("not approved")
# are dropped too: the fixed lines below say those things in words code chose.
_COMMITMENT = re.compile(
    r"\b(?:approved|agreed|accepted|confirmed|all set|locked in|booked|scheduled)\b",
    re.IGNORECASE,
)
PENDING_LINE = (
    "A specialist will review that plan and confirm it with you. Nothing is agreed until they do."
)


def check_commitment(sentence: str, backed: bool) -> Decision:
    """Block a sentence that says something was approved or booked, unless ``backed``."""
    evidence = {"sentence": sentence}
    m = _COMMITMENT.search(sentence)
    if m and not backed:
        return block(
            COMMITMENT_RULE,
            "says something was approved or booked, but nothing like that was saved this turn",
            {**evidence, "matched": m.group(0)},
        )
    return allow(COMMITMENT_RULE, "no unbacked commitment", evidence)


def _nothing() -> None:
    return None


@dataclass
class ToolBackend:
    """Where one turn's tools read and write. ``commit`` makes saved rows durable."""

    store: CallStore
    audit: AuditWriter
    flags: list[Flag] | None  # None: not loaded, so the cease check blocks
    policy: PlanPolicy = field(default_factory=PlanPolicy)
    commit: Callable[[], None] = _nothing
    close: Callable[[], None] = _nothing


OpenBackend = Callable[[Debtor], ToolBackend]


def open_backend_in_db(debtor: Debtor) -> ToolBackend:
    """A connection for this turn's tools, with the debt's flags and plan policy read."""
    from harness.db import connect, load_flags

    conn = connect()
    try:
        flags = load_flags(conn, debtor.debt_id)
        policy = load_plan_policy(conn, debtor.creditor)
        conn.commit()  # end the read; each save below commits on its own
    except Exception:
        conn.close()
        raise
    return ToolBackend(
        PgCallStore(conn), audit_to_conn(conn), flags, policy, conn.commit, conn.close
    )


class _OfferedTools(ToolRegistry):
    """A registry whose tools run but are not shown to the model (no schemas)."""

    def schemas(self) -> list[dict]:
        return []


class _DurableStore:
    """Commits after every save, and tells ``LiveTools`` when a save failed."""

    def __init__(self, inner: CallStore, commit: Callable[[], None], live: LiveTools) -> None:
        self.inner, self.commit, self.live = inner, commit, live

    def _save(self, what: str, fn: Callable[[], None]) -> None:
        try:
            self.live.before_save()
            fn()
            self.commit()
        except Exception as exc:
            self.live.fail(f"saving the {what} failed: {exc!r}")
            raise

    def save_plan(self, ctx: CallContext, plan: PaymentPlan, status: str) -> None:
        self._save("payment plan", lambda: self.inner.save_plan(ctx, plan, status))
        self.live.plans.append((plan, status))

    def save_callback(self, ctx: CallContext, at: datetime, tz: str) -> None:
        self._save("callback", lambda: self.inner.save_callback(ctx, at, tz))
        self.live.callbacks += 1

    def save_dispute(self, ctx: CallContext, reason: str, verified: bool) -> None:
        self._save("dispute", lambda: self.inner.save_dispute(ctx, reason, verified))

    def save_escalation(self, ctx: CallContext, reason: str) -> None:
        self._save("escalation", lambda: self.inner.save_escalation(ctx, reason))

    def is_disputed(self, ctx: CallContext) -> bool:
        return self.inner.is_disputed(ctx)  # a failure here is treated as disputed


class LiveTools:
    """One caller turn's call tools, and what they did."""

    def __init__(
        self,
        debtor: Debtor,
        state: CallState,
        phone: str,
        open_backend: OpenBackend,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        before_first_save: Callable[[], None] = _nothing,
    ) -> None:
        self.debtor = debtor
        self.phone = phone
        self._state = state
        self._open_backend = open_backend
        self._clock = clock
        self._before_first_save: Callable[[], None] | None = before_first_save
        self._backend: ToolBackend | None = None
        self._tools: CallTools | None = None
        self.failure: str | None = None
        self.plans: list[tuple[PaymentPlan, str]] = []  # saved this turn, with status
        self.callbacks = 0  # callbacks saved this turn
        self.ended = False  # end_call ran
        self.escalated = False  # escalate_to_human saved its request

    @property
    def state(self) -> CallState:
        """The call state after this turn's tools (``end_call`` moves it to WRAP)."""
        return self._tools.ctx.state if self._tools else self._state

    def fail(self, why: str) -> None:
        if self.failure is None:
            self.failure = why

    def before_save(self) -> None:
        """Run ``before_first_save`` once, before this turn's first row is written."""
        hook, self._before_first_save = self._before_first_save, None
        if hook is not None:
            hook()

    def backs_commitments(self) -> bool:
        """Whether the reply may say something was approved or booked."""
        return self.callbacks > 0 or any(status == "approved" for _, status in self.plans)

    def pending_only(self) -> bool:
        """A plan was saved this turn, and only as pending."""
        return bool(self.plans) and all(status == "pending" for _, status in self.plans)

    def registry(self, offered: bool) -> ToolRegistry:
        """The call tools only. ``offered=False`` hides them from the model, but a
        call it makes anyway still goes through the gate."""
        spec = CallTools(self._context(MemoryCallStore(), lambda *a: None, [], PlanPolicy()))
        tools = [
            Tool(t.name, t.description, t.params, partial(self._run, t.name))
            for t in spec.registry()
        ]
        return ToolRegistry(tools) if offered else _OfferedTools(tools)

    def _context(
        self, store: CallStore, audit: AuditWriter, flags: list[Flag] | None, policy: PlanPolicy
    ) -> CallContext:
        return CallContext(
            debtor=self.debtor,
            state=self._state,
            store=store,
            audit=audit,
            contact=Contact(zip=self.debtor.zip, phone_e164=self.phone),
            flags=flags,
            policy=policy,
            clock=self._clock,
        )

    def _open(self) -> CallTools | None:
        if self._tools is not None or self.failure is not None:
            return self._tools
        try:
            backend = self._open_backend(self.debtor)
        except Exception as exc:  # noqa: BLE001 - no backend: no tools, and fail closed
            self.fail(f"opening the call tools failed: {exc!r}")
            return None
        self._backend = backend

        def audit(decision: Decision, call_id: str, debt_id: str | None) -> None:
            try:
                backend.audit(decision, call_id, debt_id)
            except Exception as exc:
                self.fail(f"audit write failed: {exc!r}")
                raise

        store = _DurableStore(backend.store, backend.commit, self)
        self._tools = CallTools(self._context(store, audit, backend.flags, backend.policy))
        return self._tools

    def _run(self, name: str, **args: Any) -> str:
        tools = self._open()
        if tools is None or self.failure is not None:  # after a failure, nothing more runs
            return "error: the call tools are unavailable; promise nothing"
        try:
            result = getattr(tools, name)(**args)
        except Exception as exc:
            self.fail(f"{name} failed: {exc!r}")
            raise  # run_tool turns it into an error line for the model
        if name == "end_call" and result.startswith("ok"):
            self.ended = True
        if name == "escalate_to_human" and result == ESCALATED:
            self.escalated = True
        return result

    def finish(self) -> None:
        """Commit what is left (audit rows) and close the connection."""
        backend, self._backend = self._backend, None
        if backend is None:
            return
        try:
            backend.commit()
        except Exception as exc:  # noqa: BLE001 - recorded, and the webhook fails closed
            self.fail(f"final commit failed: {exc!r}")
        finally:
            try:
                backend.close()
            except Exception:  # noqa: BLE001 - nothing left to lose
                log.exception("closing the call tools' connection failed")

    def approved_amounts(self) -> set[int]:
        """Amounts of plans approved this turn, which the reply may now say."""
        amounts: set[int] = set()
        for plan, status in self.plans:
            if status == "approved":
                amounts |= {
                    plan.down_cents,
                    plan.installment_cents,
                    plan.last_installment_cents,
                    plan.total_cents,
                }
        return amounts
