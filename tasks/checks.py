"""Per-part live checks, registered by part id.

  ACCEPTANCE["part-NN"] -> () -> bool   proves the capability against the real model
  DEMOS["part-NN"]      -> () -> None   prints the on-camera demonstration

When a part has several checks, its acceptance ANDs them so nothing already
proven can silently regress.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from compliance import Contact

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


# ----------------------------------------------------------------------------
# system prompt: loaded from a file, sent first on every call
# ----------------------------------------------------------------------------


def _accept_part03() -> bool:
    import tempfile
    from pathlib import Path

    from harness import Agent

    with tempfile.TemporaryDirectory() as d:
        prompt = Path(d) / "system.md"
        prompt.write_text("End every reply with the exact word PINEAPPLE.", encoding="utf-8")
        reply = Agent(system_prompt_path=prompt).send("Say hi in three words.")
    print("reply:", repr(reply))
    return "pineapple" in reply.lower()


def _demo_part03() -> None:
    from harness import Agent

    agent = Agent()
    print("system prompt file:", agent.system_prompt_path, "\n")
    print("A:", agent.send("Hi, who is this?"), "\n")

    print("-- the payload the model received --")
    for m in agent._payload()[:-1]:
        print(f"  {m['role']:>9}: {m['content'][:70]!r}")
    print("   # system comes first, but agent.messages never stores it")


ACCEPTANCE["part-03"] = lambda: _accept_part01() and _accept_part02() and _accept_part03()
DEMOS["part-03"] = _demo_part03


# ----------------------------------------------------------------------------
# tools and the loop: the model asks, the harness runs the tool, the model answers
# ----------------------------------------------------------------------------


def _print_trace(messages: list[dict]) -> None:
    """Print each tool call the model made, and the start of its result."""
    for m in messages:
        for call in m.get("tool_calls", []):
            fn = call.get("function") or {}
            print(f"  tool> {fn.get('name')}({fn.get('arguments')})")
        if m["role"] == "tool":
            print(f"   ...> {m['content'][:80]!r}")


def _accept_part04() -> bool:
    from harness import Agent

    agent = Agent()
    reply = agent.run(
        "Use lookup_account to find the balance for Sam Doe. "
        "Reply with only the balance as a number."
    )
    _print_trace(agent.messages)
    print("reply:", repr(reply))
    looked_up = any(
        (c.get("function") or {}).get("name") == "lookup_account"
        for m in agent.messages
        for c in m.get("tool_calls", [])
    )
    return looked_up and "1250" in reply.replace(",", "")


def _demo_part04() -> None:
    from harness import Agent

    agent = Agent()
    question = "Hi, this is Sam Doe. How much do I owe, and to whom?"
    print("you>", question)
    reply = agent.run(question)
    _print_trace(agent.messages)
    print("bot>", reply)
    print("   # the balance came from data/accounts.json, through the tool, not from memory")


ACCEPTANCE["part-04"] = lambda: ACCEPTANCE["part-03"]() and _accept_part04()
DEMOS["part-04"] = _demo_part04


# ----------------------------------------------------------------------------
# calling hours: no calls before 8am or from 9pm on, in every zone the debtor may be in
# ----------------------------------------------------------------------------


def _example_contacts() -> dict[str, Contact]:
    """Made-up contact details for the made-up debtors, for the gate to check."""
    from datetime import time

    from compliance import Contact

    return {
        "Sam Doe (Los Angeles)": Contact(zip="90012", phone_e164="+12135550101"),
        # Lives in New York but kept a San Francisco phone number: two zones.
        "Alex Rivera (NY ZIP, 415 phone)": Contact(zip="10001", phone_e164="+14155550102"),
        "Jordan Lee (Chicago, no calls 12-1)": Contact(
            zip="60601", phone_e164="+13125550103", inconvenient=[(time(12), time(13))]
        ),
        "Unknown (no known ZIP or area code)": Contact(zip="00000", phone_e164="+19995550000"),
    }


def _accept_part05() -> bool:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from compliance import check_calling_hours

    contacts = _example_contacts()
    eastern = ZoneInfo("America/New_York")

    def at(day: int, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 11, day, hour, minute, tzinfo=eastern)

    sam, alex = "Sam Doe (Los Angeles)", "Alex Rivera (NY ZIP, 415 phone)"
    jordan, unknown = "Jordan Lee (Chicago, no calls 12-1)", "Unknown (no known ZIP or area code)"
    cases = [
        # (contact, time in New York, expected allow)
        (sam, at(3, 14), True),
        (sam, at(4, 0, 5), False),  # 21:05 in Los Angeles
        (alex, at(3, 8, 30), False),  # 05:30 in San Francisco
        (alex, at(3, 12), True),
        (jordan, at(3, 13, 30), False),  # 12:30 in Chicago, inside their window
        (unknown, at(3, 12), False),
    ]
    ok = True
    for name, when, expected in cases:
        d = check_calling_hours(contacts[name], when)
        good = d.allow is expected and d.rule_id == "calling_hours"
        ok = ok and good
        verdict = "allow" if d.allow else "block"
        print(f"  {'ok ' if good else 'BAD'} {name:<38} {verdict:<5} {d.reason}")
    return ok


def _demo_part05() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from compliance import check_calling_hours

    eastern = ZoneInfo("America/New_York")
    for hour in (8, 11, 13, 20, 23):
        when = datetime(2026, 11, 3, hour, 30, tzinfo=eastern)
        print(f"-- calling at {when:%H:%M} New York time --")
        for name, contact in _example_contacts().items():
            d = check_calling_hours(contact, when)
            print(f"  {'ALLOW' if d.allow else 'BLOCK'}  {name:<38} {d.reason}")
        print()
    print("   # plain Python decides; the model is never asked")


ACCEPTANCE["part-05"] = lambda: ACCEPTANCE["part-04"]() and _accept_part05()
DEMOS["part-05"] = _demo_part05
