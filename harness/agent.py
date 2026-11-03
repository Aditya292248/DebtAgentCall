"""The agent: it keeps the conversation, resends it every turn, and runs tools.

``Agent`` holds ``self.messages``, the whole conversation as a list of
``{"role", "content"}`` dicts. Each ``send()`` appends the user turn, sends the
full list to the model through ``model.chat``, and appends the reply. The model
itself is still stateless; memory is just us resending everything every turn.

The system prompt lives in a file (``instructions/system.md`` by default).
``_payload()`` loads it and puts it first as a ``system`` message, ahead of the
history. It is re-read on every call and never stored in ``self.messages``, so
editing the file takes effect on the next turn.

If the model call fails, the user turn is taken back out, so the history never
ends on a user message with no reply.

Tools and the loop. ``send()`` stays one model call, now with the tool schemas
attached; it records whatever comes back, tool requests included, but runs
nothing. ``run()`` is the loop: it calls ``send()``, and while the model's last
reply asked for tools it runs them, appends the results as ``tool`` messages, and
calls ``send()`` again so the model can see them. It stops when the model
answers in plain text or ``max_steps`` model calls have been used up. If any
model call in the loop fails, the whole turn is taken back out, as with ``send()``.

Adapted from codingHarness.
"""

from __future__ import annotations

from pathlib import Path

from harness.instructions import load_system_prompt
from harness.tools import ToolRegistry, default_tools, run_tool
from model import Provider, chat

DEFAULT_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent.parent / "instructions" / "system.md"
MAX_STEPS = 10  # model calls per run(); stops a confused model looping forever


class Agent:
    def __init__(
        self,
        model: str | None = None,
        provider: Provider | None = None,
        system_prompt_path: str | Path = DEFAULT_SYSTEM_PROMPT_PATH,
        tools: ToolRegistry | None = None,
        max_steps: int = MAX_STEPS,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must allow at least one model call")
        self.model = model
        self.provider = provider
        self.system_prompt_path = Path(system_prompt_path)
        self.tools = default_tools() if tools is None else tools
        self.max_steps = max_steps
        self.messages: list[dict] = []

    def _payload(self) -> list[dict]:
        """What the model receives: the system prompt (if any), then the history."""
        system_prompt = load_system_prompt(self.system_prompt_path)
        system = [{"role": "system", "content": system_prompt}] if system_prompt else []
        # A new list, so whatever the provider holds onto doesn't change under it later.
        return system + list(self.messages)

    def send(self, user_text: str | None = None) -> str:
        """One model call. Adds ``user_text`` first if given, then records the reply.
        Tool requests are recorded, not run: that's ``run()``'s job.
        """
        if user_text is not None:
            self.messages.append({"role": "user", "content": user_text})
        try:
            resp = chat(
                self._payload(),
                model=self.model,
                tools=self.tools.schemas() or None,
                provider=self.provider,
            )
        except Exception:
            # Undo the user turn so a failed call leaves the history as it was.
            if user_text is not None:
                self.messages.pop()
            raise
        if resp.tool_calls:
            # Every tool result must name the call it answers; fill in ids a server left out.
            calls = [{**c, "id": c.get("id") or f"call_{i}"} for i, c in enumerate(resp.tool_calls)]
            self.messages.append(
                {"role": "assistant", "content": resp.content, "tool_calls": calls}
            )
        else:
            self.messages.append({"role": "assistant", "content": resp.content})
        return resp.content

    def run(self, user_text: str) -> str:
        """The agent loop: send, run the tools the model asked for, send again, until
        it answers in plain text or ``max_steps`` model calls are used up."""
        start = len(self.messages)
        try:
            reply = self.send(user_text)
            steps = 1
            while calls := self.messages[-1].get("tool_calls"):
                # Answer every request, even on the last step, so the history stays valid:
                # an assistant tool call with no matching tool result is an API error.
                self._run_tools(calls)
                if steps == self.max_steps:
                    return f"stopped: used all {self.max_steps} steps without a final answer"
                reply = self.send()
                steps += 1
        except Exception:
            # Undo the whole turn, so a retry doesn't send the question twice.
            del self.messages[start:]
            raise
        return reply

    def _run_tools(self, calls: list[dict]) -> None:
        for call in calls:
            fn = call.get("function") or {}
            result = run_tool(self.tools, fn.get("name", ""), fn.get("arguments"))
            self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
