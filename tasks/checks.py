"""Per-part live checks, registered by part id.

  ACCEPTANCE["part-NN"] -> () -> bool   proves the capability against the real model
  DEMOS["part-NN"]      -> () -> None   prints the on-camera demonstration

When a part has several checks, its acceptance ANDs them so nothing already
proven can silently regress.
"""

from __future__ import annotations

from collections.abc import Callable

ACCEPTANCE: dict[str, Callable[[], bool]] = {}
DEMOS: dict[str, Callable[[], None]] = {}


# ----------------------------------------------------------------------------
# model seam: call the model and get a reply
# ----------------------------------------------------------------------------


def _accept_part01_reply() -> bool:
    from model import chat

    reply = chat([{"role": "user", "content": "What is 2 + 2? Reply with only the number."}])
    print("model replied:", repr(reply.content))
    return "4" in reply.content


def _accept_part01_provider() -> bool:
    from model import Provider, chat

    provider = Provider.from_env()
    print("provider:", provider.base_url, provider.model)
    reply = chat(
        [{"role": "user", "content": "Reply with exactly one word: PLUGGABLE"}],
        provider=provider,
    )
    print("reply:", repr(reply.content))
    return "pluggable" in reply.content.lower()


def _accept_part01() -> bool:
    return _accept_part01_reply() and _accept_part01_provider()


def _demo_part01() -> None:
    from model import Provider, chat

    provider = Provider.from_env()
    print("provider:", provider.base_url, provider.model, "\n")

    prompt = (
        "Write one short, polite greeting to a person named Sam Doe, "
        "as the opening line of a phone call. One sentence only."
    )
    print("Q:", prompt)
    print("A:", chat([{"role": "user", "content": prompt}], provider=provider).content)
    print("   # one call in, one reply out: no history, no tools yet")


ACCEPTANCE["part-01"] = _accept_part01
DEMOS["part-01"] = _demo_part01


# ----------------------------------------------------------------------------
# agent: keep the conversation and resend it every turn
# ----------------------------------------------------------------------------


def _accept_part02() -> bool:
    from harness import Agent

    agent = Agent()
    agent.send("My name is Sam Doe. Just say OK.")
    reply = agent.send("What is my name? Reply with only the name.")
    print("reply:", repr(reply))
    print("history length:", len(agent.messages))
    return "sam doe" in reply.lower() and len(agent.messages) == 4


def _demo_part02() -> None:
    from harness import Agent

    agent = Agent()
    print("A1:", agent.send("My name is Sam Doe. Remember it."))
    print("A2:", agent.send("What is my name?"))
    print("   # it remembers: send() resent the earlier turns\n")

    print("-- what the model actually received on the last call --")
    for m in agent.messages[:-1]:
        print(f"  {m['role']:>9}: {m['content'][:70]}")


ACCEPTANCE["part-02"] = lambda: _accept_part01() and _accept_part02()
DEMOS["part-02"] = _demo_part02
