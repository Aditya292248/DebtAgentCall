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
    from fastapi.testclient import TestClient

    from compliance import Contact
    from model import Provider

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
    print("   # the balance came from the database, through the tool, not from memory")


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


# ----------------------------------------------------------------------------
# supabase: the made-up debtors live in a real Postgres database
# ----------------------------------------------------------------------------


def _accept_part06() -> bool:
    from harness.db import database_url, find_account
    from harness.tools import lookup_account

    print("database:", database_url())
    sam = find_account("Sam Doe")
    print("Sam Doe:", sam)
    unknown = lookup_account("Nobody Here")
    print("unknown:", repr(unknown))
    return (
        sam is not None
        and sam["balance"] == "1250.00"
        and unknown.startswith("error: no account found")
    )


def _demo_part06() -> None:
    from harness.db import connect, database_url
    from harness.tools import lookup_account

    print("database:", database_url(), "\n")
    print("-- what is in the debt table --")
    with connect() as conn:
        rows = conn.execute(
            "select d.account_number, p.first_name || ' ' || p.last_name, d.creditor, "
            "d.balance_cents, d.status from public.debt d "
            "join public.debtor p on p.id = d.debtor_id order by d.account_number"
        ).fetchall()
    for number, name, creditor, cents, status in rows:
        print(f"  {number}  {name:<12} {creditor:<20} {cents:>7} cents  {status}")
    print()
    for name in ("Sam Doe", "Nobody Here"):
        print(f"lookup_account({name!r})")
        print("  ->", lookup_account(name))
    print("   # the tool reads Postgres now; an unknown name is an error string, not a crash")


ACCEPTANCE["part-06"] = lambda: ACCEPTANCE["part-05"]() and _accept_part06()
DEMOS["part-06"] = _demo_part06


# ----------------------------------------------------------------------------
# pre-call checks: call limits, consent, stop requests, lawyers, bankruptcy
# ----------------------------------------------------------------------------

# A fixed time so the seeded call history always gives the same answers:
# 2026-11-05 14:00 in New York, 11:00 in Los Angeles.
_PART07_WHEN = "2026-11-05T19:00:00+00:00"

# account -> (who, the rules expected to block at _PART07_WHEN)
_PART07_EXPECTED: dict[str, tuple[str, list[str]]] = {
    "ACC-1001": ("Sam Doe", []),
    "ACC-1002": ("Alex Rivera", []),
    "ACC-1003": ("Jordan Lee", ["call_frequency"]),
    "ACC-1004": ("Casey Morgan", ["call_frequency"]),
    "ACC-1005": ("Riley Chen", ["tcpa_consent"]),
    "ACC-1006": ("Taylor Brooks", ["tcpa_consent"]),
    "ACC-1007": ("Morgan Patel", ["cease"]),
    "ACC-1008": ("Parker Reed", ["cease"]),
    "ACC-1009": ("Avery Ortiz", ["dnc"]),
    "ACC-1010": ("Robin Hale", ["attorney"]),
    "ACC-1011": ("Drew Kim", ["bankruptcy"]),
    "ACC-1012": ("Quinn Walsh", ["deceased"]),
}


def _accept_part07() -> bool:
    from datetime import datetime

    from compliance import check_before_call, may_call
    from harness.db import load_call_facts

    when = datetime.fromisoformat(_PART07_WHEN)
    ok = True
    for account, (who, expected) in _PART07_EXPECTED.items():
        decisions = check_before_call(load_call_facts(account), when)
        blocked = [d.rule_id for d in decisions if not d.allow]
        good = blocked == expected and may_call(decisions) is (not expected)
        ok = ok and good
        print(f"  {'ok ' if good else 'BAD'} {account} {who:<14} blocked by {blocked or 'nothing'}")
    return ok


def _demo_part07() -> None:
    from datetime import datetime

    from compliance import check_before_call, may_call
    from harness.db import load_call_facts

    when = datetime.fromisoformat(_PART07_WHEN)
    print(f"checking every made-up debtor for a call at {when:%Y-%m-%d %H:%M} UTC\n")
    for account, (who, _) in _PART07_EXPECTED.items():
        decisions = check_before_call(load_call_facts(account), when)
        print(f"{account} {who}: {'MAY CALL' if may_call(decisions) else 'DO NOT CALL'}")
        for d in decisions:
            print(f"  {'allow' if d.allow else 'BLOCK'}  {d.rule_id:<14} {d.reason}")
        print()
    print("   # one BLOCK is enough to stop the call; the model is never asked")


ACCEPTANCE["part-07"] = lambda: ACCEPTANCE["part-06"]() and _accept_part07()
DEMOS["part-07"] = _demo_part07


# ----------------------------------------------------------------------------
# audit log: every pre-call decision, in a hash chain nobody can edit
# ----------------------------------------------------------------------------


def _accept_part08() -> bool:
    from datetime import datetime

    from compliance.audit import verify_chain
    from harness.db import connect
    from harness.precall import check_and_record

    when = datetime.fromisoformat(_PART07_WHEN)
    ok = True
    # Rolled back, so checking doesn't add rows to the real log.
    with connect() as conn, conn.transaction(force_rollback=True):
        for account, (who, expected) in _PART07_EXPECTED.items():
            before = conn.execute("select coalesce(max(seq), 0) from public.audit_event")
            start = before.fetchone()
            decisions = check_and_record(conn, account, when)
            recorded = conn.execute(
                "select rule_id from public.audit_event where seq > %s and decision = 'block' "
                "order by seq",
                start,
            ).fetchall()
            good = len(decisions) == 8 and [r[0] for r in recorded] == expected
            ok = ok and good
            print(f"  {'ok ' if good else 'BAD'} {account} {who:<14} recorded blocks {expected}")
        problems = verify_chain(conn)
    print("chain problems:", problems or "none")
    return ok and not problems


def _demo_part08() -> None:
    from datetime import datetime

    from compliance import may_call
    from compliance.audit import rows
    from harness.db import connect
    from harness.precall import check_and_record
    from tasks.audit_verify import run

    when = datetime.fromisoformat(_PART07_WHEN)
    with connect() as conn:
        start_row = conn.execute("select coalesce(max(seq), 0) from public.audit_event").fetchone()
        start = start_row[0] if start_row else 0
        for account, (who, _) in _PART07_EXPECTED.items():
            decisions = check_and_record(conn, account, when)
            print(f"{account} {who:<14} {'MAY CALL' if may_call(decisions) else 'DO NOT CALL'}")
        conn.commit()

        print(f"\n-- the audit rows this run added (from seq {start + 1}) --")
        for r in rows(conn, after_seq=start):
            if r["decision"] == "block" or r["rule_id"] == "calling_hours":
                print(
                    f"  {r['seq']:>4} {r['decision']:<5} {r['rule_id']:<14} "
                    f"{r['reason'][:44]:<44} {r['prev_hash'][:8]} -> {r['hash'][:8]}"
                )
        print("  (allows other than calling_hours are recorded too, just not shown)\n")
        print("$ uv run audit-verify")
        run(conn)
    print("   # each hash covers the row before it; try `update public.audit_event ...` and see")


ACCEPTANCE["part-08"] = lambda: ACCEPTANCE["part-07"]() and _accept_part08()
DEMOS["part-08"] = _demo_part08


# ----------------------------------------------------------------------------
# webhook: AgentPhone sends the caller's words, we answer with text
# ----------------------------------------------------------------------------


def _sim_client(provider: Provider | None = None) -> tuple[TestClient, str]:
    """The webhook app in-process, with a one-off secret the simulator shares."""
    import secrets
    import warnings

    with warnings.catch_warnings():  # starlette's notice about httpx, not ours to fix
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    from phone.webhook import create_app

    secret = secrets.token_hex(16)
    return TestClient(create_app(provider=provider, secret=secret)), secret


def _accept_part09() -> bool:
    from sim.agentphone_sim import load_script, run_script

    client, secret = _sim_client()
    turns = run_script(load_script("sam_balance"), client, secret)
    for caller, agent in turns:
        print(f"  caller> {caller}\n   agent> {agent}")
    balance_reply = turns[2][1].replace(",", "")
    return all(reply.strip() for _, reply in turns) and "1250" in balance_reply


def _llm_configured() -> bool:
    import os

    from model.provider import _load_dotenv

    _load_dotenv()
    return bool(os.environ.get("LLM_BASE_URL"))


def _demo_fake_reply(messages: list[dict]) -> str:
    """Stand-in answers when no model is set up, so the demo still runs."""
    said = messages[-1]["content"].lower()
    if "owe" in said:
        return "I can help with that. Let me look up your account."
    if "wrong number" in said or "nobody" in said:
        return "Sorry to bother you. I'll update our records. Goodbye."
    if "bye" in said or "call me back" in said:
        return "Thank you for your time. Goodbye."
    if "good time" in said:
        return "No problem. When would be a better time to call?"
    if "this is" in said:
        return "Thank you. What can I help you with today?"
    return "Hello, may I please speak with the account holder?"


def _demo_part09() -> None:
    from model import fake
    from sim.agentphone_sim import SCRIPTS_DIR, load_script, print_conversation, run_script

    real = _llm_configured()
    provider = None if real else fake(scripted=_demo_fake_reply)
    print("model:", "the real one from LLM_BASE_URL" if real else "fake (set LLM_BASE_URL)", "\n")
    client, secret = _sim_client(provider)
    for path in sorted(SCRIPTS_DIR.glob("*.yaml")):
        script = load_script(path)
        print(f"-- {script.name} --")
        print_conversation(run_script(script, client, secret))
        print()
    print("   # every line went through a signed webhook; a bad signature gets a 401")


ACCEPTANCE["part-09"] = lambda: ACCEPTANCE["part-08"]() and _accept_part09()
DEMOS["part-09"] = _demo_part09
