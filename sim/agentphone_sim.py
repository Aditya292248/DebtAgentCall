"""A fake AgentPhone: plays a scripted caller against our webhook.

A script is a YAML file with the caller's lines (see ``sim/scripts/``). For each
line the simulator sends the same signed ``agent.message`` webhook AgentPhone
would send, and reads back the agent's reply. It finishes with
``agent.call_ended``, like a real call, and returns the conversation as
``(caller line, agent reply)`` pairs.

It can talk to the app in-process through FastAPI's ``TestClient`` (what the
tests and the demo do) or to a running server:

    uv run python -m sim.agentphone_sim sim/scripts/sam_balance.yaml --url http://127.0.0.1:8000

The server and the simulator must use the same ``AGENTPHONE_WEBHOOK_SECRET``.

A reply may be one JSON object or a stream (``application/x-ndjson``): one
object per line, ``"interim": true`` on all but the last. ``chunks_of`` reads
either into a list of chunks, and ``combine`` joins them into the one reply a
caller heard (the texts in order, with the last chunk's ``hangup``/``action``).
``post_timed`` sends one webhook straight to the ASGI app and notes when each
chunk left it, which ``TestClient`` can't show (it waits for the whole body).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from phone.signing import signed_headers
from phone.webhook import SECRET_ENV, WEBHOOK_PATH

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"


@dataclass(frozen=True)
class Script:
    name: str
    lines: list[str]
    from_number: str = "+12135550101"
    to_number: str = "+15555550100"


class SimError(RuntimeError):
    """The webhook answered something other than 200."""


def load_script(path: str | Path) -> Script:
    """Read a caller script. ``path`` may also be a bare name from ``sim/scripts``."""
    p = Path(path)
    if not p.exists():
        p = SCRIPTS_DIR / f"{path}.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    lines = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
        raise ValueError(f"{p}: 'lines' must be a list of strings")
    return Script(
        name=str(data.get("name", p.stem)),
        lines=lines,
        from_number=str(data.get("from", Script.from_number)),
        to_number=str(data.get("to", Script.to_number)),
    )


NDJSON = "application/x-ndjson"


def chunks_of(resp: httpx.Response) -> list[dict]:
    """The reply as chunks: one for a plain JSON reply, one per line for NDJSON."""
    if resp.headers.get("content-type", "").startswith(NDJSON):
        return [_object(json.loads(line)) for line in resp.text.splitlines() if line.strip()]
    return [_object(resp.json())]


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}  # AgentPhone ignores anything else


def combine(chunks: list[dict]) -> dict:
    """The whole reply: every chunk's text in order, and the last chunk's other fields."""
    if not chunks:
        return {}
    texts = [c["text"] for c in chunks if isinstance(c.get("text"), str) and c["text"]]
    whole = {k: v for k, v in chunks[-1].items() if k not in ("text", "interim")}
    if texts or "text" in chunks[-1]:
        whole = {"text": " ".join(texts), **whole}
    return whole


def reply_of(resp: httpx.Response) -> dict:
    """``combine(chunks_of(resp))``: what the caller heard this turn, as one reply."""
    return combine(chunks_of(resp))


def post_timed(
    app: Any, body: bytes, headers: dict[str, str], path: str = WEBHOOK_PATH
) -> tuple[int, list[tuple[float, dict]]]:
    """POST ``body`` to the ASGI ``app`` and return the status and each chunk with the
    seconds from sending the request to that chunk leaving the app."""
    return asyncio.run(_post_timed(app, body, headers, path))


async def _post_timed(
    app: Any, body: bytes, headers: dict[str, str], path: str
) -> tuple[int, list[tuple[float, dict]]]:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    sent = False
    status = 0
    chunks: list[tuple[float, dict]] = []
    pending = b""
    started = time.perf_counter()

    async def receive() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)  # the caller never disconnects mid-turn
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        nonlocal status, pending
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            pending += message.get("body", b"")
            at = time.perf_counter() - started
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if line.strip():
                    chunks.append((at, _object(json.loads(line))))
            if not message.get("more_body") and pending.strip():
                chunks.append((at, _object(json.loads(pending))))  # a plain JSON reply
                pending = b""

    await app(scope, receive, send)
    return status, chunks


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def run_script(
    script: Script,
    client: httpx.Client,
    secret: str,
    *,
    path: str = WEBHOOK_PATH,
    clock: Callable[[], float] = time.time,
    replies: list[list[dict]] | None = None,
) -> list[tuple[str, str]]:
    """Play ``script`` against the webhook at ``client`` and return the conversation.

    ``client`` is an ``httpx.Client`` with a ``base_url``; FastAPI's ``TestClient``
    is one too. Raises ``SimError`` if the webhook refuses a request. Pass a list
    as ``replies`` to get every turn's chunks as they were sent.
    """
    call_id = f"call_sim_{uuid.uuid4().hex[:12]}"
    started = clock()
    history: list[dict] = []
    turns: list[tuple[str, str]] = []

    def post(event: str, envelope: dict) -> dict:
        body = json.dumps(envelope).encode()
        headers = signed_headers(body, secret, clock(), f"wh_{uuid.uuid4().hex}", event)
        resp = client.post(path, content=body, headers=headers)
        if resp.status_code != 200:
            raise SimError(f"{event} got {resp.status_code}: {resp.text[:200]}")
        chunks = chunks_of(resp)
        if replies is not None and event == "agent.message":
            replies.append(chunks)
        return combine(chunks)

    for line in script.lines:
        answer = post(
            "agent.message",
            {
                "event": "agent.message",
                "channel": "voice",
                "timestamp": _iso(clock()),
                "agentId": "agent_sim",
                "data": {
                    "callId": call_id,
                    "transcript": line,
                    "confidence": 0.95,
                    "status": "in-progress",
                    "from": script.to_number,  # we placed the call
                    "to": script.from_number,
                    "direction": "outbound",
                },
                "conversationState": {},
                "recentHistory": list(history),
            },
        )
        reply = str(answer.get("text", ""))
        history += [{"role": "user", "content": line}, {"role": "agent", "content": reply}]
        turns.append((line, reply))
        if answer.get("hangup") or answer.get("action") == "transfer":
            break

    ended = clock()
    post(
        "agent.call_ended",
        {
            "event": "agent.call_ended",
            "channel": "voice",
            "timestamp": _iso(ended),
            "agentId": "agent_sim",
            "data": {
                "callId": call_id,
                "startedAt": _iso(started),
                "endedAt": _iso(ended),
                "durationSeconds": int(ended - started),
                "disconnectionReason": "user_hangup",
                "transcript": history,
            },
        },
    )
    return turns


def print_conversation(turns: list[tuple[str, str]]) -> None:
    for caller, agent in turns:
        print(f"caller> {caller}")
        print(f" agent> {agent}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Play a caller script against the webhook.")
    parser.add_argument("script", help="a YAML file, or a name from sim/scripts")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="where the server runs")
    args = parser.parse_args(argv)
    secret = os.environ.get(SECRET_ENV)
    if not secret:
        print(f"set {SECRET_ENV} to the same secret the server uses")
        return 2
    with httpx.Client(base_url=args.url, timeout=60) as client:
        print_conversation(run_script(load_script(args.script), client, secret))
    return 0


if __name__ == "__main__":
    sys.exit(main())
