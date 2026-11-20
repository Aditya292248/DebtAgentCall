"""Memory: each call's conversation, saved as it happens, so the next turn sees it.

AgentPhone sends every caller turn as its own webhook, and each webhook builds a
new ``Agent``. A ``Session`` is how that agent finds what was said earlier on the
same call. It is a list of records, only ever added to:

    {"type": "session", "id": "call_abc", "created": "..."}
    {"type": "message", "message": {"role": "user", "content": "Hello?"}}
    {"type": "message", "message": {"role": "assistant", "content": "", "tool_calls": [...]}}
    {"type": "message", "message": {"role": "tool", "tool_call_id": "call_1", "content": "..."}}
    {"type": "compaction", "messages": [... the whole history from here on ...]}
    {"type": "state", "state": {"stage": "VERIFY", "verified": false, ...}}

``load()`` replays the records: messages are appended, and a compaction swaps
in the history it holds. ``state`` records hold the call's ``CallState``
(``harness/call_state.py``); ``last_state()`` returns the newest one, and
``load()`` skips them. Nothing is ever rewritten, so every message ever sent
stays on record, even after a compaction replaced it.

There are two places to keep the records, with the same behaviour:
  - ``JsonlSession``: one JSON line per record in a file. Used by the tests and
    the demo, so they run with no database.
  - ``PostgresSession``: one row per record in the ``call_message`` table. The
    webhook uses this one by default.

Loading is forgiving where a crash can leave damage and strict elsewhere:
  - a half-written last JSONL line is ignored (the process died mid-write), and
    the next write cuts it off first, so it never ends up in the middle
  - a broken line anywhere else is an error: that's corruption, not a crash
  - a tool request whose result never got saved (the turn died mid-tool) gets a
    result with outcome ``unknown`` from ``unanswered_tool_calls``, because the
    model API rejects a tool request with no answer and it is not known whether
    the tool ran

Adapted from codingHarness.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from harness.tool_outcome import INTERRUPTED, tool_message

if TYPE_CHECKING:
    import psycopg

KINDS = ("session", "message", "compaction", "state")
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")


class Session:
    """One call's saved history. Subclasses say where the records are kept."""

    call_id: str

    def append(self, message: dict) -> None:
        """Record one message the moment it joins the history."""
        self._write("message", {"type": "message", "message": message})

    def replace(self, messages: list[dict]) -> None:
        """Record that the history is now ``messages`` (after a compaction or an undo)."""
        self._write("compaction", {"type": "compaction", "messages": messages})

    def put_state(self, state: dict) -> None:
        """Record the call's state; the newest one wins."""
        self._write("state", {"type": "state", "state": state})

    def last_state(self) -> dict | None:
        """The newest state record, or None if the call has none yet."""
        states = [r["state"] for r in self._records() if r.get("type") == "state"]
        return states[-1] if states else None

    def header(self) -> dict:
        """The first record: the call id and when the session was created."""
        records = self._records()
        first = records[0] if records else {}
        return first if first.get("type") == "session" else {}

    def load(self) -> list[dict]:
        """Replay the records into the current history, message by message."""
        messages: list[dict] = []
        for record in self._records():
            kind = record.get("type")
            if kind == "message":
                messages.append(record["message"])
            elif kind == "compaction":
                messages = list(record["messages"])
        return messages

    def _start(self) -> None:
        """Write the header record if this call has nothing saved yet."""
        if not self._records():
            created = datetime.now(UTC).isoformat(timespec="seconds")
            self._write("session", {"type": "session", "id": self.call_id, "created": created})

    def _write(self, kind: str, record: dict) -> None:
        raise NotImplementedError

    def _records(self) -> list[dict]:
        raise NotImplementedError


class JsonlSession(Session):
    """A call's history as a JSONL file: one record per line."""

    def __init__(self, path: str | Path, call_id: str | None = None) -> None:
        self.path = Path(path)
        self.call_id = call_id or self.path.stem

    @classmethod
    def for_call(cls, root: str | Path, call_id: str) -> JsonlSession:
        """The session file for ``call_id`` under ``root``, created if it's new."""
        session = cls(Path(root) / f"{_file_name(call_id)}.jsonl", call_id)
        session.path.parent.mkdir(parents=True, exist_ok=True)
        session._start()
        return session

    def _write(self, kind: str, record: dict) -> None:
        self._drop_half_line()
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()

    def _drop_half_line(self) -> None:
        """Every record ends in a newline, so a file that doesn't was cut off mid-write.
        Trim back to the last full line, so the next record starts cleanly."""
        if not self.path.exists():
            return
        data = self.path.read_bytes()
        if data and not data.endswith(b"\n"):
            with self.path.open("r+b") as f:
                f.truncate(data.rfind(b"\n") + 1)

    def _records(self) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        records: list[dict] = []
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                if number == len(lines):
                    break  # half-written last line from a crash: drop it
                raise ValueError(f"{self.path}:{number}: corrupt session line ({exc})") from exc
        return records


class PostgresSession(Session):
    """A call's history as rows of the ``call_message`` table, in ``seq`` order.

    Each write opens its own connection and commits, so a turn that dies later
    still leaves everything before it saved.
    """

    def __init__(
        self, call_id: str, connect: Callable[[], psycopg.Connection] | None = None
    ) -> None:
        if connect is None:
            from harness.db import connect as connect_to_db

            connect = connect_to_db
        self.call_id = call_id
        self._connect = connect

    @classmethod
    def for_call(
        cls, call_id: str, connect: Callable[[], psycopg.Connection] | None = None
    ) -> PostgresSession:
        """The session for ``call_id``, with its header row written if it's new."""
        session = cls(call_id, connect)
        session._start()
        return session

    def _write(self, kind: str, record: dict) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            conn.execute(
                "insert into public.call_message (call_id, kind, body) values (%s, %s, %s)",
                (self.call_id, kind, Jsonb(record)),
            )

    def _records(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "select body from public.call_message where call_id = %s order by seq",
                (self.call_id,),
            ).fetchall()
        return [body for (body,) in rows]


def unanswered_tool_calls(messages: list[dict]) -> list[dict]:
    """An ``unknown`` result for each tool request that never got one, in request order.

    A crash between the request and its saved result is the unknown case: the tool
    may have run, so it is neither retried nor taken for a success.
    """
    answered = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
    return [
        {"role": "tool", "tool_call_id": call["id"], "content": tool_message(INTERRUPTED)}
        for m in messages
        for call in m.get("tool_calls") or []
        if call.get("id") not in answered
    ]


def _file_name(call_id: str) -> str:
    """``call_id`` if it is safe as a file name, else a hash of it, so an id like
    ``../x`` can't write outside the sessions folder."""
    if _SAFE_ID.fullmatch(call_id):
        return call_id
    return "id-" + hashlib.sha256(call_id.encode()).hexdigest()[:32]
