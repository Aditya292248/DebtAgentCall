"""The agent: it keeps the conversation and resends it every turn.

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

Adapted from codingHarness.
"""

from __future__ import annotations

from pathlib import Path

from harness.instructions import load_system_prompt
from model import Provider, chat

DEFAULT_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent.parent / "instructions" / "system.md"


class Agent:
    def __init__(
        self,
        model: str | None = None,
        provider: Provider | None = None,
        system_prompt_path: str | Path = DEFAULT_SYSTEM_PROMPT_PATH,
    ) -> None:
        self.model = model
        self.provider = provider
        self.system_prompt_path = Path(system_prompt_path)
        self.messages: list[dict] = []

    def _payload(self) -> list[dict]:
        """What the model receives: the system prompt (if any), then the history."""
        system_prompt = load_system_prompt(self.system_prompt_path)
        system = [{"role": "system", "content": system_prompt}] if system_prompt else []
        # A new list, so whatever the provider holds onto doesn't change under it later.
        return system + list(self.messages)

    def send(self, user_text: str) -> str:
        """Add the user turn, call the model with the payload, record the reply."""
        self.messages.append({"role": "user", "content": user_text})
        try:
            resp = chat(self._payload(), model=self.model, provider=self.provider)
        except Exception:
            # Undo the user turn so a failed call leaves the history as it was.
            self.messages.pop()
            raise
        self.messages.append({"role": "assistant", "content": resp.content})
        return resp.content
