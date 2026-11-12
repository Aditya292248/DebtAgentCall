"""The webhook AgentPhone calls during a phone call.

AgentPhone does the speech: it turns what the caller says into text, POSTs it
here, and speaks whatever text we answer with. Each request goes through:

1. Read the raw body, before any JSON parsing, and check the signature and
   timestamp with ``phone.signing.verify``. Anything that fails gets a 401 and
   never reaches the model.
2. If this ``X-Webhook-ID`` was seen before, send back the same reply as the
   first time without calling the model again.
3. Act on the event:
   - ``agent.message`` on the voice channel: one turn of ``Agent.run`` with the
     caller's words, answered as ``{"text": reply}``.
   - ``agent.call_ended``: accepted with ``{}``. Nothing is stored yet.
   - anything else: ``{}``.

Each turn uses a fresh ``Agent``, so the agent doesn't yet remember earlier
turns of the same call. Keeping memory per call comes next.

Run it with ``uv run serve``. Tests build their own app with ``create_app``,
passing a fake model and a test secret.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from harness import Agent
from model import Provider
from model.provider import _load_dotenv
from phone.replies import say
from phone.signing import ID_HEADER, verify

WEBHOOK_PATH = "/webhooks/agentphone"
SECRET_ENV = "AGENTPHONE_WEBHOOK_SECRET"
DIDNT_CATCH = "Sorry, I didn't catch that. Could you say it again?"
MAX_REMEMBERED_IDS = 10_000  # enough to spot repeats, without growing forever


class _SeenIds:
    """The reply sent for each recent webhook id, oldest dropped first."""

    def __init__(self, limit: int = MAX_REMEMBERED_IDS) -> None:
        self.limit = limit
        self._replies: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, webhook_id: str) -> dict | None:
        with self._lock:
            return self._replies.get(webhook_id)

    def remember(self, webhook_id: str, reply: dict) -> None:
        with self._lock:
            self._replies[webhook_id] = reply
            while len(self._replies) > self.limit:
                self._replies.popitem(last=False)


def create_app(
    *,
    provider: Provider | None = None,
    secret: str | None = None,
    agent_factory: Callable[[], Agent] | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """The webhook app.

    ``secret`` defaults to ``AGENTPHONE_WEBHOOK_SECRET`` (``.env`` is a fallback).
    Without one every request is refused, so a missing setting fails closed.
    ``provider`` is the model to use (the real one from the environment if not
    given), and ``agent_factory`` replaces how each turn's ``Agent`` is built.
    """
    if secret is None:
        _load_dotenv()
        secret = os.environ.get(SECRET_ENV) or None
    make_agent = agent_factory or (lambda: Agent(provider=provider))
    seen = _SeenIds()
    app = FastAPI(title="DebtAgentCall webhook")
    app.state.seen = seen

    @app.post(WEBHOOK_PATH)
    async def agentphone(request: Request) -> JSONResponse:
        raw = await request.body()  # the exact bytes that were signed
        problem = verify(raw, request.headers, secret, clock())
        if problem:
            return JSONResponse({"error": problem}, status_code=401)

        webhook_id = request.headers.get(ID_HEADER)
        if webhook_id and (earlier := seen.get(webhook_id)) is not None:
            return JSONResponse(earlier)

        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            return JSONResponse({"error": "body is not valid JSON"}, status_code=400)
        if not isinstance(envelope, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

        reply = await _handle(envelope, make_agent)
        if webhook_id:
            seen.remember(webhook_id, reply)
        return JSONResponse(reply)

    return app


async def _handle(envelope: dict, make_agent: Callable[[], Agent]) -> dict:
    """The reply for one verified webhook."""
    event = envelope.get("event")
    data = envelope.get("data") or {}
    if event == "agent.message" and envelope.get("channel") == "voice":
        transcript = data.get("transcript") if isinstance(data, dict) else None
        if not isinstance(transcript, str) or not transcript.strip():
            return say(DIDNT_CATCH)
        agent = make_agent()
        # Agent.run is blocking (it waits on the model), so keep it off the event loop.
        reply = await run_in_threadpool(agent.run, transcript)
        return say(reply)
    # agent.call_ended is accepted; storing the call comes later. Unknown events too.
    return {}
