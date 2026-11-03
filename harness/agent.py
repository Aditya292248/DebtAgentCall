"""The agent: it keeps the conversation and resends it every turn.

``Agent`` holds ``self.messages``, the whole conversation as a list of
``{"role", "content"}`` dicts. Each ``send()`` appends the user turn, sends the
full list to the model through ``model.chat``, and appends the reply. The model
itself is still stateless; memory is just us resending everything every turn.

If the model call fails, the user turn is taken back out, so the history never
ends on a user message with no reply.

Adapted from codingHarness.
"""

from __future__ import annotations

from model import Provider, chat


class Agent:
    def __init__(self, model: str | None = None, provider: Provider | None = None) -> None:
        self.model = model
        self.provider = provider
        self.messages: list[dict] = []

    def send(self, user_text: str) -> str:
        """Add the user turn, call the model with the full history, record the reply."""
        self.messages.append({"role": "user", "content": user_text})
        try:
            resp = chat(
                # A copy, so whatever the provider holds onto doesn't change under it later.
                list(self.messages),
                model=self.model,
                provider=self.provider,
            )
        except Exception:
            # Undo the user turn so a failed call leaves the history as it was.
            self.messages.pop()
            raise
        self.messages.append({"role": "assistant", "content": resp.content})
        return resp.content
