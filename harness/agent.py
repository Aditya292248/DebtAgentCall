"""The agent: it keeps the conversation, resends it every turn, and runs tools.

``Agent`` holds ``self.messages``, the whole conversation as a list of
``{"role", "content"}`` dicts. Each ``send()`` appends the user turn, sends the
full list to the model through ``model.chat``, and appends the reply. The model
itself is still stateless; memory is just us resending everything every turn.

The system prompt lives in a file (``instructions/system.md`` by default).
``_payload()`` loads it and puts it first as a ``system`` message, ahead of the
history. It is re-read on every call and never stored in ``self.messages``, so
editing the file takes effect on the next turn. On a call, the webhook also sets
``stage_prompt_path`` to the current stage's file (``instructions/stages/``),
which is added after it in the same system message.

If the model call fails, the user turn is taken back out, so the history never
ends on a user message with no reply.

Tools and the loop. ``send()`` stays one model call, now with the tool schemas
attached; it records whatever comes back, tool requests included, but runs
nothing. ``run()`` is the loop: it calls ``send()``, and while the model's last
reply asked for tools it runs them, appends the results as ``tool`` messages, and
calls ``send()`` again so the model can see them. It stops when the model
answers in plain text or ``max_steps`` model calls have been used up. If any
model call in the loop fails, the whole turn is taken back out, as with ``send()``.

Streaming. ``run_stream()`` is the same loop on ``model.chat_stream``: it
yields the reply text as the model writes it, and ``None`` at the end of each
model call, so a caller can finish a half sentence before tools run. Tools run
between model calls exactly as in ``run()``. A caller that stops reading
(``close()``) ends the turn there: what the model had written so far is kept as
a plain assistant message, with no tool requests, so the history stays valid;
the caller then rewrites it to what was really said.

Memory. Give the agent a ``Session`` (``harness/memory.py``) and every message
is saved the moment it joins the history (``_add``). A new ``Agent`` on the same
session starts from what was saved, so each webhook turn of a call sees the
turns before it. If the last turn died between a tool request and its result,
the missing result is filled in with a placeholder first. When a turn is taken
back out after a failure, the session records the shorter history too.

Skills. The agent looks in ``skills_dir`` (``skills/`` by default) and adds a
menu, each skill's name and description only, to the system prompt. When there
is at least one skill it also gets a ``load_skill`` tool. The folder is re-read
on every call, like the instructions file.

Compaction (``harness/compact.py``). Before a caller turn is added, the agent
checks how big the next model call would be: the ``prompt_tokens`` the server
reported last time plus an estimate for what came after, or an estimate of the
whole payload. Past ``COMPACT_AT`` of ``max_context_tokens`` it keeps the first
``keep_head`` and last ``keep_tail`` messages and swaps the middle for one
summary the model writes, and the session saves a ``compaction`` record. If the
summarizer fails, the history is left as it was and the turn goes ahead.

Adapted from codingHarness.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from harness.compact import SUMMARIZE_PROMPT, compact_messages, estimate_tokens, render
from harness.instructions import load_system_prompt
from harness.memory import Session, unanswered_tool_calls
from harness.skills import Skill, discover, load_skills, skill_tool, skills_prompt
from harness.tools import ToolRegistry, default_tools, run_tool
from model import LLMResponse, Provider, chat, chat_stream

DEFAULT_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent.parent / "instructions" / "system.md"
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
MAX_STEPS = 10  # model calls per run(); stops a confused model looping forever
MAX_CONTEXT_TOKENS = 16_000  # set to your model's context length
COMPACT_AT = 0.8  # compact once the next call passes this share of the limit
KEEP_HEAD = 2  # first messages kept word for word: how the call opened
KEEP_TAIL = 6  # recent messages kept word for word: what is being said now


class Agent:
    def __init__(
        self,
        model: str | None = None,
        provider: Provider | None = None,
        system_prompt_path: str | Path = DEFAULT_SYSTEM_PROMPT_PATH,
        tools: ToolRegistry | None = None,
        max_steps: int = MAX_STEPS,
        session: Session | None = None,
        skills_dir: str | Path | None = None,
        max_context_tokens: int = MAX_CONTEXT_TOKENS,
        keep_head: int = KEEP_HEAD,
        keep_tail: int = KEEP_TAIL,
        stage_prompt_path: str | Path | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must allow at least one model call")
        if max_context_tokens < 1 or keep_head < 1 or keep_tail < 1:
            raise ValueError("max_context_tokens, keep_head and keep_tail must be positive")
        self.model = model
        self.provider = provider
        self.system_prompt_path = Path(system_prompt_path)
        self.stage_prompt_path = Path(stage_prompt_path) if stage_prompt_path else None
        self.tools = default_tools() if tools is None else tools
        self.max_steps = max_steps
        # Looked up here, not as a default argument, so tests can point every agent
        # at an empty folder at once.
        self.skills_dir = Path(skills_dir) if skills_dir is not None else DEFAULT_SKILLS_DIR
        self.max_context_tokens = max_context_tokens
        self.keep_head = keep_head
        self.keep_tail = keep_tail
        self.compactions = 0
        self.compact_error: str | None = None  # why the last compaction failed, if it did
        self.messages: list[dict] = []
        # (prompt_tokens the server reported, how many messages that prompt held)
        self._measured: tuple[int, int] | None = None

        # Memory: start from what this call already said, then save everything new.
        self.session = session
        if session is not None:
            self.messages = session.load()
            for placeholder in unanswered_tool_calls(self.messages):
                self._add(placeholder)  # the last turn died mid-tool: close the gap

    def _add(self, message: dict) -> None:
        """Append to the history and, if there's a session, save it there too."""
        self.messages.append(message)
        if self.session is not None:
            self.session.append(message)

    def _undo_to(self, length: int) -> None:
        """Take back everything after the first ``length`` messages, saved copy included."""
        if len(self.messages) == length:
            return
        del self.messages[length:]
        self._measured = None  # the server's count was for the longer history
        if self.session is not None:
            self.session.replace(self.messages)

    def skills(self) -> list[Skill]:
        """Name and description of every skill in ``skills_dir``, read fresh."""
        return load_skills(discover(self.skills_dir))

    def _tools(self, skills: list[Skill]) -> ToolRegistry:
        """This call's tools: the registry, plus load_skill if there are skills."""
        if not skills or self.tools.get("load_skill"):
            return self.tools
        return ToolRegistry([*self.tools, skill_tool(skills)])

    def _payload(self, skills: list[Skill] | None = None) -> list[dict]:
        """What the model receives: system prompt + call stage instructions + skills menu
        (each if any), then history."""
        skills = self.skills() if skills is None else skills
        parts = [load_system_prompt(self.system_prompt_path)]
        if self.stage_prompt_path is not None:
            parts.append(load_system_prompt(self.stage_prompt_path))
        parts.append(skills_prompt(skills))
        system_prompt = "\n\n".join(p for p in parts if p)
        system = [{"role": "system", "content": system_prompt}] if system_prompt else []
        # A new list, so whatever the provider holds onto doesn't change under it later.
        return system + list(self.messages)

    def send(self, user_text: str | None = None) -> str:
        """One model call. Adds ``user_text`` first if given, then records the reply.
        Tool requests are recorded, not run: that's ``run()``'s job.
        """
        if user_text is not None:
            self._maybe_compact(user_text)
        return self._send(user_text)

    def _send(self, user_text: str | None) -> str:
        start = len(self.messages)
        if user_text is not None:
            self._add({"role": "user", "content": user_text})
        try:
            skills = self.skills()
            resp = chat(
                self._payload(skills),
                model=self.model,
                tools=self._tools(skills).schemas() or None,
                provider=self.provider,
            )
        except Exception:
            # Undo the user turn so a failed call leaves the history as it was.
            self._undo_to(start)
            raise
        self._record(resp)
        return resp.content

    def _record(self, resp: LLMResponse) -> None:
        """Add the model's reply (and any tool requests) to the history."""
        prompt_tokens = resp.usage.get("prompt_tokens")
        if isinstance(prompt_tokens, int) and prompt_tokens > 0:
            self._measured = (prompt_tokens, len(self.messages))
        if resp.tool_calls:
            # Every tool result must name the call it answers; fill in ids a server left out.
            calls = [{**c, "id": c.get("id") or f"call_{i}"} for i, c in enumerate(resp.tool_calls)]
            self._add({"role": "assistant", "content": resp.content, "tool_calls": calls})
        else:
            self._add({"role": "assistant", "content": resp.content})

    def _send_stream(self, user_text: str | None) -> Generator[str | None, None, None]:
        """One streamed model call: yields its text, then None once it is recorded."""
        start = len(self.messages)
        if user_text is not None:
            self._add({"role": "user", "content": user_text})
        written: list[str] = []
        recorded = False
        try:
            skills = self.skills()
            pieces = chat_stream(
                self._payload(skills),
                model=self.model,
                tools=self._tools(skills).schemas() or None,
                provider=self.provider,
            )
            try:
                for piece in pieces:
                    if isinstance(piece, LLMResponse):
                        self._record(piece)
                        recorded = True
                        break
                    written.append(piece)
                    yield piece
            finally:
                close = getattr(pieces, "close", None)
                if callable(close):
                    close()
        except GeneratorExit:
            # The caller stopped reading mid-reply: keep what was written, no tools.
            if not recorded:
                self._add({"role": "assistant", "content": "".join(written)})
            raise
        except Exception:
            self._undo_to(start)
            raise
        if not recorded:  # the stream ended without its summary: keep the text
            self._add({"role": "assistant", "content": "".join(written)})
        try:
            yield None
        except GeneratorExit:
            last = self.messages[-1]
            if last.get("tool_calls"):  # stopped before its tools ran: drop the requests
                self.messages[-1] = {"role": "assistant", "content": last.get("content") or ""}
                if self.session is not None:
                    self.session.replace(self.messages)
            raise

    def run_stream(self, user_text: str) -> Generator[str | None, None, None]:
        """``run()``, streamed: reply text as it comes, ``None`` after each model call."""
        self._maybe_compact(user_text)
        start = len(self.messages)
        try:
            yield from self._send_stream(user_text)
            steps = 1
            while calls := self.messages[-1].get("tool_calls"):
                self._run_tools(calls)
                if steps == self.max_steps:
                    yield f"stopped: used all {self.max_steps} steps without a final answer"
                    yield None
                    return
                yield from self._send_stream(None)
                steps += 1
        except Exception:
            # Undo the whole turn, as run() does. A close() (GeneratorExit) is not undone.
            self._undo_to(start)
            raise

    def run(self, user_text: str) -> str:
        """The agent loop: send, run the tools the model asked for, send again, until
        it answers in plain text or ``max_steps`` model calls are used up."""
        # Compact before marking where the turn starts, so an undo can't cut the summary.
        self._maybe_compact(user_text)
        start = len(self.messages)
        try:
            reply = self._send(user_text)
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
            self._undo_to(start)
            raise
        return reply

    def context_tokens(self) -> int:
        """Size of the next model call, tool schemas included.

        Exact for everything the server already counted on the last call, estimated
        for the rest. With no server count (fake provider, or a server that doesn't
        report usage) the whole payload is estimated.
        """
        if self._measured and len(self.messages) >= self._measured[1]:
            counted, n = self._measured
            return counted + estimate_tokens(self.messages[n:])
        skills = self.skills()
        return estimate_tokens(self._payload(skills), self._tools(skills).schemas() or None)

    def _maybe_compact(self, pending: str | None = None) -> None:
        """Compact if the next call, with ``pending`` caller text added, passes the line."""
        size = self.context_tokens()
        if pending:
            size += estimate_tokens([{"role": "user", "content": pending}])
        if size > self.max_context_tokens * COMPACT_AT:
            self.compact()

    def compact(self) -> bool:
        """Summarize the middle of the history. False if there was nothing to do,
        or if the summarizer failed; then the history is left exactly as it was."""
        try:
            compacted = compact_messages(
                self.messages, self._summarize, self.keep_head, self.keep_tail
            )
        except Exception as exc:  # noqa: BLE001 - a failed summary must not end the call
            self.compact_error = f"{type(exc).__name__}: {exc}"
            return False
        if compacted == self.messages:
            return False
        self.messages = compacted
        if self.session is not None:
            self.session.replace(compacted)
        self.compactions += 1
        self.compact_error = None
        self._measured = None  # the server's count was for the old history
        return True

    def _summarize(self, middle: list[dict]) -> str:
        """Ask the model for a summary of ``middle``. No tools, no history: one call."""
        resp = chat(
            [
                {"role": "system", "content": SUMMARIZE_PROMPT},
                {"role": "user", "content": render(middle)},
            ],
            model=self.model,
            provider=self.provider,
        )
        return resp.content

    def _run_tools(self, calls: list[dict]) -> None:
        tools = self._tools(self.skills())
        for call in calls:
            fn = call.get("function") or {}
            result = run_tool(tools, fn.get("name", ""), fn.get("arguments"))
            self._add({"role": "tool", "tool_call_id": call["id"], "content": result})
