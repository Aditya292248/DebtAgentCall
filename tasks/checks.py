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

    from compliance import Contact, SpeechState
    from harness import Agent
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
    import tempfile
    import warnings
    from functools import partial

    with warnings.catch_warnings():  # starlette's notice about httpx, not ours to fix
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    from harness.memory import JsonlSession
    from phone.webhook import create_app

    # Sessions go to a temp folder, so this needs no database.
    folder = tempfile.mkdtemp(prefix="debtagent-sessions-")
    open_session = partial(JsonlSession.for_call, folder)
    secret = secrets.token_hex(16)
    # The bare webhook these earlier parts were built on; call stages are part-13.
    app = create_app(provider=provider, secret=secret, open_session=open_session, stages=False)
    return TestClient(app), secret


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


# ----------------------------------------------------------------------------
# sessions: each call remembers its earlier turns between webhooks
# ----------------------------------------------------------------------------


def _sim_turn(client: TestClient, secret: str, call_id: str, text: str) -> str:
    """One signed caller turn on ``call_id``; the agent's reply."""
    import json
    import time
    import uuid

    from phone.signing import signed_headers
    from phone.webhook import WEBHOOK_PATH

    body = json.dumps(
        {
            "event": "agent.message",
            "channel": "voice",
            "data": {"callId": call_id, "transcript": text, "direction": "outbound"},
        }
    ).encode()
    headers = signed_headers(body, secret, time.time(), f"wh_{uuid.uuid4().hex}", "agent.message")
    return str(client.post(WEBHOOK_PATH, content=body, headers=headers).json().get("text", ""))


def _accept_part10() -> bool:
    import uuid

    client, secret = _sim_client()
    call_id = f"call_accept_{uuid.uuid4().hex[:8]}"
    first = _sim_turn(client, secret, call_id, "Hi, this is Sam Doe speaking.")
    second = _sim_turn(client, secret, call_id, "Sorry, what name did I just give you?")
    other = _sim_turn(client, secret, f"{call_id}_other", "What name did I just give you?")
    print(f"  call 1> {first}\n  call 1> {second}\n  call 2> {other}")
    return "sam" in second.lower() and "sam" not in other.lower()


def _demo_seen(messages: list[dict]) -> str:
    """Fake answer that says how much of the call it was sent."""
    history = [m for m in messages if m.get("role") != "system"]
    return f"(the model was sent {len(history) - 1} earlier messages from this call)"


def _demo_part10() -> None:
    from model import fake
    from sim.agentphone_sim import load_script, print_conversation, run_script

    real = _llm_configured()
    provider = None if real else fake(scripted=_demo_seen)
    print("model:", "the real one from LLM_BASE_URL" if real else "fake (set LLM_BASE_URL)", "\n")
    client, secret = _sim_client(provider)
    print_conversation(run_script(load_script("sam_balance"), client, secret))
    print("\n   # each line was its own webhook; the saved session carried the call between them")


ACCEPTANCE["part-10"] = lambda: ACCEPTANCE["part-09"]() and _accept_part10()
DEMOS["part-10"] = _demo_part10


# ----------------------------------------------------------------------------
# sentence filter: every sentence is checked before the agent may say it
# ----------------------------------------------------------------------------


def _part11_samples() -> list[tuple[str, str, str | None]]:
    """(stage, sentence, the rule expected to block it or None)."""
    return [
        ("before verify", "Hi, this is Maya. May I speak with Sam Doe?", None),
        ("before verify", "May I speak with Sue?", None),
        ("before verify", "Your balance is $1,240.", "third_party_disclosure"),
        ("before verify", "I'm calling from Acme Bank.", "third_party_disclosure"),
        ("verified", "Thanks Sam, I'm calling about a debt.", "mini_miranda_first"),
        ("disclosed", "Your balance with Acme Bank is $1,240.", None),
        ("disclosed", "Your balance is $1,250.", "amount_mismatch"),
        ("disclosed", "We will garnish your wages.", "threat"),
        ("disclosed", "You could be arrested.", "threat"),
        ("disclosed", "I'm an attorney.", "false_identity"),
        ("disclosed", "What the hell is going on?", "profanity"),
        ("time-barred", "We may file a lawsuit.", "time_barred"),
        ("validation", "You must pay today.", "overshadowing"),
        ("validation", "You can dispute this within 30 days.", None),
    ]


def _part11_states() -> dict[str, SpeechState]:
    from dataclasses import replace

    from compliance import SpeechState

    base = SpeechState(
        known_amounts_cents={124000},
        creditor="Acme Bank",
        in_validation_period=False,
    )
    disclosed = replace(base, verified=True, disclosed=True)
    return {
        "before verify": base,
        "verified": replace(base, verified=True),
        "disclosed": disclosed,
        "time-barred": replace(disclosed, legal_action_authorized=True, time_barred=True),
        "validation": replace(disclosed, in_validation_period=True),
    }


def _accept_part11_filter() -> bool:
    from compliance import check_sentence

    states = _part11_states()
    ok = True
    for stage, sentence, expected in _part11_samples():
        d = check_sentence(sentence, states[stage])
        got = None if d.allow else d.rule_id
        good = got == expected
        ok = ok and good
        print(f"  {'ok ' if good else 'BAD'} {stage:<13} {got or 'allow':<22} {sentence}")
    return ok


def _demo_part11() -> None:
    from compliance import check_sentence, split_sentences

    states = _part11_states()
    print("on file: $1,240.00 owed to Acme Bank\n")
    for stage, sentence, _ in _part11_samples():
        d = check_sentence(sentence, states[stage])
        verdict = "allow" if d.allow else f"BLOCK {d.rule_id}"
        print(f"  [{stage:<13}] {verdict:<28} {sentence}")
    print()
    reply = "Thanks, Sam. Your balance is $1,240. We will sue you if you don't pay."
    print("one model reply, split and checked sentence by sentence:")
    for sentence in split_sentences(reply):
        d = check_sentence(sentence, states["disclosed"])
        print(f"  {'allow' if d.allow else 'BLOCK ' + d.rule_id:<28} {sentence}")
    print("   # a blocked sentence is never spoken; the model is never asked")


ACCEPTANCE["part-11"] = lambda: ACCEPTANCE["part-09"]() and _accept_part11_filter()
DEMOS["part-11"] = _demo_part11


# ----------------------------------------------------------------------------
# UI: the debtor list with pre-call badges, and the audit log, in the browser
# ----------------------------------------------------------------------------


def _accept_part12() -> bool:
    from fastapi.testclient import TestClient

    from harness.api import app

    # Calls the API directly rather than running pytest: without a database the
    # pytest `db` tests are skipped and pytest still exits 0, which would pass here.
    client = TestClient(app)
    res = client.get("/api/precall", params={"at": _PART07_WHEN})
    if res.status_code != 200:
        print("GET /api/precall failed:", res.status_code, res.json().get("detail"))
        return False
    ok = True
    for debt in res.json()["debts"]:
        expected = _PART07_EXPECTED.get(debt["account_number"])
        if expected is None:
            continue
        blocked = [d["rule_id"] for d in debt["decisions"] if not d["allow"]]
        good = blocked == expected[1] and debt["may_call"] is (not expected[1])
        ok = ok and good
        print(f"  {'ok ' if good else 'BAD'} {debt['account_number']} {debt['name']:<14} {blocked}")
    seen = {d["account_number"] for d in res.json()["debts"]}
    missing = sorted(set(_PART07_EXPECTED) - seen)
    if missing:
        print("missing from the API:", missing)
    chain = client.get("/api/audit/verify").json()
    print("chain:", chain)
    return ok and not missing and chain.get("ok") is True


def _demo_part12() -> None:
    from fastapi.testclient import TestClient

    from harness.api import app

    client = TestClient(app)
    for label, at in (
        ("2pm New York", _PART07_WHEN),
        ("9:30pm Los Angeles", "2026-11-06T05:30:00+00:00"),
    ):
        print(f"-- GET /api/precall?at={at}  ({label}) --")
        for debt in client.get("/api/precall", params={"at": at}).json()["debts"]:
            blocks = [f"{d['rule_id']}: {d['reason']}" for d in debt["decisions"] if not d["allow"]]
            badge = "ready" if debt["may_call"] else "; ".join(blocks)
            print(f"  {debt['account_number']} {debt['name']:<14} {badge}")
        print()
    print("-- GET /api/audit/verify --")
    print(" ", client.get("/api/audit/verify").json())
    print("\n   # the same answers the UI shows: `uv run api`, then `npm run dev` in web/")


ACCEPTANCE["part-12"] = lambda: ACCEPTANCE["part-11"]() and _accept_part10() and _accept_part12()
DEMOS["part-12"] = _demo_part12


# ----------------------------------------------------------------------------
# subagents: after a call, reviewers read the transcript in parallel
# ----------------------------------------------------------------------------

_PART15_ACCOUNT = {
    "account_id": "ACC-1001",
    "name": "Sam Doe",
    "creditor": "Acme Bank",
    "balance": "1250.00",
    "currency": "USD",
}
# A made-up call that breaks one rule: the agent threatens arrest at the end.
_PART15_CALL = [
    ("Hello?", "Hi, this is Maya. May I speak with Sam Doe?"),
    (
        "Yes, this is Sam.",
        "Thanks, Sam. I'm a debt collector trying to collect a debt, and any information "
        "I get will be used for that purpose.",
    ),
    ("How much do I owe?", "Your balance with Acme Bank is $1,250.00."),
    ("I can't pay that right now.", "If you don't pay this week, we will have you arrested."),
]


def _demo_review_reply(messages: list[dict]) -> str:
    """Stand-in reviewer answers when no model is set up, so the demo still runs."""
    task = messages[-1]["content"]
    if "Area: threats" in task:
        return 'FLAG: the agent said "we will have you arrested"'
    return "OK: nothing wrong in this area"


def _accept_part15() -> bool:
    from harness.review import review_transcript

    findings = {f.area: f for f in review_transcript(_PART15_CALL, _PART15_ACCOUNT)}
    for f in findings.values():
        print(f"  {'ok  ' if f.ok else 'FLAG'} {f.area:<12} {f.note}")
    return not findings["threats"].ok and findings["amounts"].ok


def _demo_part15() -> None:
    import time

    from harness.review import format_transcript, review_transcript
    from model import fake

    real = _llm_configured()
    provider = None if real else fake(scripted=_demo_review_reply)
    print("model:", "the real one from LLM_BASE_URL" if real else "fake (set LLM_BASE_URL)", "\n")
    print("-- the call --")
    print(format_transcript(_PART15_CALL), "\n")
    print("-- one reviewer subagent per area, all at once --")
    start = time.perf_counter()
    findings = review_transcript(_PART15_CALL, _PART15_ACCOUNT, provider=provider)
    for f in findings:
        print(f"  {'ok  ' if f.ok else 'FLAG'} {f.area:<12} {f.note}")
    print(f"  ({len(findings)} reviewers in {time.perf_counter() - start:.1f}s)")
    print("\n   # a second check after the call, for a person to look at; never on a live call")


ACCEPTANCE["part-15"] = lambda: ACCEPTANCE["part-12"]() and _accept_part15()
DEMOS["part-15"] = _demo_part15


# ----------------------------------------------------------------------------
# stop triggers: what the caller says that legally ends the call
# ----------------------------------------------------------------------------


def _part14_samples() -> list[tuple[str, list[str]]]:
    """(caller line, the trigger kinds expected, in priority order)."""
    return [
        ("Yes, this is Sam. What's this about?", []),
        ("I don't want to stop paying, I just need more time.", []),
        ("The bank is not my favourite.", []),
        ("Please stop calling me at work.", ["cease"]),
        ("I filed chapter 7 last year.", ["bankruptcy"]),
        ("You'll have to talk to my lawyer.", ["attorney"]),
        ("My lawyer friend said this is fine.", ["attorney"]),
        ("That's not my debt, I don't owe anything.", ["dispute"]),
        ("I don't consent to recording.", ["recording_objection"]),
        ("Sorry, wrong number.", ["wrong_number"]),
        ("He passed away in June.", ["deceased"]),
        ("Stop calling, I have an attorney and I dispute this.", ["attorney", "cease", "dispute"]),
    ]


def _accept_part14_triggers() -> bool:
    from compliance.triggers import detect

    ok = True
    for line, expected in _part14_samples():
        got = [t.kind for t in detect(line)]
        good = got == expected
        ok = ok and good
        print(f"  {'ok ' if good else 'BAD'} {', '.join(got) or 'none':<32} {line}")
    return ok


def _demo_part14() -> None:
    from compliance.triggers import detect

    for line, _ in _part14_samples():
        print(f'caller: "{line}"')
        hits = detect(line)
        if not hits:
            print("  no trigger: the model gets the line as usual")
        for t in hits:
            extra = f"flag={t.flag or '-'}{'  escalate' if t.escalate else ''}"
            print(f"  {t.kind:<20} matched {t.matched!r:<22} {extra}")
        if hits:
            print(f"  agent closes with: {hits[0].close}")
        print()
    print("   # checked before the model sees the line; not wired into calls yet")


ACCEPTANCE["part-14"] = lambda: ACCEPTANCE["part-12"]() and _accept_part14_triggers()
DEMOS["part-14"] = _demo_part14


# ----------------------------------------------------------------------------
# skills: playbooks the agent loads for hard conversations
# ----------------------------------------------------------------------------

_PART17_LINE = "I lost my job last month and I just can't keep up right now."


def _part17_agent(provider: Provider | None = None) -> Agent:
    from harness import Agent
    from harness.agent import DEFAULT_SKILLS_DIR
    from harness.tools import ToolRegistry

    # Only load_skill, so the run shows the skill and nothing else.
    return Agent(provider=provider, tools=ToolRegistry(), skills_dir=DEFAULT_SKILLS_DIR)


def _loaded_skills(messages: list[dict]) -> list[str]:
    import json

    names: list[str] = []
    for m in messages:
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            if fn.get("name") == "load_skill":
                args = fn.get("arguments") or {}
                args = json.loads(args) if isinstance(args, str) else args
                names.append(str(args.get("name")))
    return names


def _accept_part17() -> bool:
    agent = _part17_agent()
    print("  skills:", ", ".join(s.name for s in agent.skills()))
    reply = agent.run(_PART17_LINE)
    loaded = _loaded_skills(agent.messages)
    print(f"  caller: {_PART17_LINE}\n  loaded: {loaded or 'none'}\n  agent: {reply}")
    return "hardship" in loaded


def _demo_part17() -> None:
    from harness.skills import skills_prompt
    from model import fake
    from model.fake import Reply, tool_call, tool_reply

    real = _llm_configured()
    script: list[Reply] = [
        tool_reply(tool_call("load_skill", name="hardship")),
        "I'm sorry to hear that.",
    ]
    agent = _part17_agent(None if real else fake(scripted=script))
    print("model:", "the real one from LLM_BASE_URL" if real else "fake (set LLM_BASE_URL)", "\n")
    print("-- the menu added to the system prompt --")
    print(skills_prompt(agent.skills()), "\n")
    print(f'caller: "{_PART17_LINE}"')
    reply = agent.run(_PART17_LINE)
    for m in agent.messages:
        if m["role"] == "tool":
            first = m["content"].splitlines()[0]
            print(f"  load_skill -> {first} ...")
    print(f"  loaded: {_loaded_skills(agent.messages) or 'none'}")
    print(f"agent: {reply}")
    print("\n   # skills are guidance only; the checks in code still run on every line")


ACCEPTANCE["part-17"] = lambda: ACCEPTANCE["part-15"]() and _accept_part17()
DEMOS["part-17"] = _demo_part17


# ----------------------------------------------------------------------------
# dialer: check, record, then place each call once
# ----------------------------------------------------------------------------


def _accept_part18() -> bool:
    """The dry run over the seed blocks exactly the debts the pre-call checks block."""
    from datetime import datetime

    from dialer.service import plan_queue
    from harness.db import connect

    when = datetime.fromisoformat(_PART07_WHEN)
    with connect() as conn:
        result = plan_queue(list(_PART07_EXPECTED), when, conn)
        conn.rollback()
    ok = True
    for o in result.outcomes:
        expected = _PART07_EXPECTED[o.account_number][1]
        got = [d.rule_id for d in o.decisions if not d.allow]
        good = got == expected
        ok = ok and good
        print(f"  {'ok ' if good else 'BAD'} {o.account_number} blocked by {got or 'nothing'}")
    return ok


def _demo_part18() -> None:
    from dialer.cli import main

    print("$ uv run dialer --dry-run --when", _PART07_WHEN, "\n")
    main(["--dry-run", "--when", _PART07_WHEN])
    print("\n   # a dry run: nothing written, nobody dialed; code decides who is called")


ACCEPTANCE["part-18"] = lambda: (
    ACCEPTANCE["part-17"]() and _accept_part14_triggers() and _accept_part18()
)
DEMOS["part-18"] = _demo_part18


# ----------------------------------------------------------------------------
# compaction: a long call is summarized in the middle to stay inside the window
# ----------------------------------------------------------------------------

# A made-up long call. The colour is said in the middle, so after compaction the
# model only knows it from the summary; the name is in the head, kept word for word.
_PART16_CALLER = [
    "Hi, this is Sam Doe.",
    "I've had a long week, work has been busy and the kids have been sick.",
    "By the way, my favourite colour is teal, my old bank card was teal too.",
    "I moved house last month, so post has been going to the wrong place.",
    "I usually get paid on the first and the fifteenth of the month.",
    "My car needed new tyres too, so money has been tight lately.",
    "What colour did I say was my favourite?",
]
_PART16_LIMIT = 300  # tokens; small on purpose so a short call needs compacting


def _part16_agent(provider: Provider | None, root: str) -> Agent:
    from pathlib import Path

    from harness import Agent
    from harness.memory import JsonlSession
    from harness.tools import ToolRegistry

    no_skills = Path(root) / "no-skills"  # keeps the skills menu out of the small budget
    no_skills.mkdir(exist_ok=True)
    return Agent(
        provider=provider,
        tools=ToolRegistry(),
        skills_dir=no_skills,
        session=JsonlSession.for_call(root, "call_long"),
        max_context_tokens=_PART16_LIMIT,
        keep_head=2,
        keep_tail=2,
    )


def _accept_part16() -> bool:
    import tempfile

    from harness.memory import JsonlSession

    with tempfile.TemporaryDirectory() as root:
        agent = _part16_agent(None, root)
        reply = ""
        for line in _PART16_CALLER:
            reply = agent.run(line)
        reloaded = JsonlSession.for_call(root, "call_long").load()
    print(f"  compactions: {agent.compactions}  last reply: {reply!r}")
    return agent.compactions >= 1 and "teal" in reply.lower() and reloaded == agent.messages


def _demo_compact_reply(messages: list[dict]) -> str:
    """Stand-in answers when no model is set up, so the demo still runs."""
    from harness.compact import SUMMARIZE_PROMPT, SUMMARY_HEADER

    if messages[0].get("content") == SUMMARIZE_PROMPT:
        text = messages[1]["content"]
        return (
            "Sam mentioned: "
            + ("favourite colour is teal; " if "teal" in text else "")
            + ("money is tight." if "money" in text else "a busy week.")
        )
    history = [m for m in messages if m["role"] != "system"]
    summary = next((m["content"] for m in history if m["content"].startswith(SUMMARY_HEADER)), "")
    if "what colour" in history[-1]["content"].lower():
        return "You said teal." if "teal" in summary else "I'm not sure, sorry."
    return "I understand, thank you for telling me."


def _demo_part16() -> None:
    import tempfile

    from harness.compact import SUMMARY_HEADER
    from harness.memory import JsonlSession
    from model import fake

    real = _llm_configured()
    provider = None if real else fake(scripted=_demo_compact_reply)
    print("model:", "the real one from LLM_BASE_URL" if real else "fake (set LLM_BASE_URL)")
    print(
        f"limit: {_PART16_LIMIT} tokens, compact past 80%, keep 2 at the start and 2 at the end\n"
    )
    with tempfile.TemporaryDirectory() as root:
        agent = _part16_agent(provider, root)
        for line in _PART16_CALLER:
            before, done = agent.context_tokens(), agent.compactions
            reply = agent.run(line)
            if agent.compactions > done:
                print(f"  -- compacted: ~{before} tokens of history became one summary --")
            print(f"  caller> {line}\n  agent>  {reply}   (~{agent.context_tokens()} tokens)")
        print("\n-- what the model is sent now --")
        for m in agent.messages:
            text = m["content"].replace(SUMMARY_HEADER + "\n", "[summary] ")
            print(f"  {m['role']:<9} {text[:110]}")
        records = JsonlSession.for_call(root, "call_long")._records()
        print("\nsaved records:", ", ".join(r["type"] for r in records))
    print(
        "\n   # verification, disclosure and stop requests are kept by code, never by the summary"
    )


ACCEPTANCE["part-16"] = lambda: ACCEPTANCE["part-10"]() and _accept_part16()
DEMOS["part-16"] = _demo_part16


# ----------------------------------------------------------------------------
# call stages: verify who answered, say the disclosure, then talk about the debt
# ----------------------------------------------------------------------------

_PART13_SCRIPTS = ("sam_verified", "sam_wrong_dob", "jordan_not_here")
# A fake model that tries to say the balance every time, to show what the filter drops.
_PART13_LEAKY = "Sure. Your balance with Northwind Furniture is $1,250.00. Anything else?"


def _part13_debtors() -> dict:
    """Sam and Jordan as supabase/seed.sql has them, so the demo needs no database."""
    from datetime import date

    from harness.call_state import Debtor

    return {
        "+12135550101": Debtor(
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-00000000a001",
            "Sam",
            "Doe",
            date(1985, 4, 12),
            "90012",
            "Northwind Furniture",
            125000,
        ),
        "+13125550103": Debtor(
            "00000000-0000-4000-8000-000000000003",
            "00000000-0000-4000-8000-00000000a003",
            "Jordan",
            "Lee",
            date(1978, 1, 5),
            "60601",
            "Maple Street Gym",
            8999,
        ),
    }


def _staged_call(script_name: str, provider: Provider | None, *, use_db: bool) -> tuple:
    """Play one script through the webhook with call stages on.

    Returns ``(turns, stages, blocked)``: the conversation, the stage after each
    turn (read back from the saved session), and the sentences the filter dropped.
    """
    import json
    import secrets
    import tempfile
    from functools import partial
    from pathlib import Path

    from fastapi.testclient import TestClient

    from harness.call_state import Stage
    from harness.memory import JsonlSession
    from phone.webhook import audit_to_db, create_app, find_debtor_in_db
    from sim.agentphone_sim import load_script, run_script

    folder = Path(tempfile.mkdtemp(prefix="debtagent-stages-"))
    blocked: list[str] = []

    def audit(decision, call_id, debt_id) -> None:  # type: ignore[no-untyped-def]
        blocked.append(f"{decision.rule_id}: {decision.evidence.get('sentence')}")
        if use_db:
            audit_to_db(decision, call_id, debt_id)

    secret = secrets.token_hex(16)
    app = create_app(
        provider=provider,
        secret=secret,
        open_session=partial(JsonlSession.for_call, folder),
        find_debtor=find_debtor_in_db if use_db else _part13_debtors().get,
        audit=audit,
    )
    turns = run_script(load_script(script_name), TestClient(app), secret)
    (path,) = folder.glob("*.jsonl")
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    stages = [Stage(r["state"]["stage"]) for r in records if r["type"] == "state"]
    # DISCLOSE lasts one turn, inside the turn that verified: label that line with it.
    stages = [
        Stage.DISCLOSE if reply.startswith("Thank you, ") and "debt collector" in reply else stage
        for (_, reply), stage in zip(turns, stages, strict=False)
    ]
    return turns, stages, blocked


def _accept_part13() -> bool:
    import re

    from harness.stages import DISCLOSURE

    debt_talk = re.compile(r"\b(?:debts?|balance|owe[sd]?|collect\w*)\b|\$", re.IGNORECASE)
    ok = True
    for name in _PART13_SCRIPTS:
        turns, stages, blocked = _staged_call(name, None, use_db=True)
        replies = [reply for _, reply in turns]
        for (caller, agent), stage in zip(turns, stages, strict=False):
            print(f"  [{stage.value:<11}] caller> {caller}\n  {'':<13}  agent> {agent}")
        for line in blocked:
            print(f"  (dropped) {line}")
        if name == "sam_verified":
            said = DISCLOSURE.format(first_name="Sam")
            first_amount = next((i for i, r in enumerate(replies) if "$" in r), None)
            good = (
                said in replies
                and first_amount is not None
                and first_amount > replies.index(said)
                and "1,250" in replies[first_amount]
            )
        else:
            good = "DISCUSS" not in stages and not any(debt_talk.search(r) for r in replies)
        print(f"  {name}: {'ok' if good else 'BAD'}\n")
        ok = ok and good
    return ok


def _demo_part13() -> None:
    from model import fake

    real = _llm_configured()
    provider = None if real else fake(default=_PART13_LEAKY)
    print(
        "model:",
        "the real one from LLM_BASE_URL" if real else "fake, and it always says the balance",
    )
    print()
    for name in _PART13_SCRIPTS:
        turns, stages, blocked = _staged_call(name, provider, use_db=False)
        print(f"-- {name} --")
        for (caller, agent), stage in zip(turns, stages, strict=False):
            print(f"  [{stage.value:<11}] caller> {caller}")
            print(f"  {'':<13}  agent> {agent}")
        for line in blocked:
            print(f"  (dropped before it was said) {line}")
        print()
    print("   # greeting, identity check and disclosure are fixed lines from code; the model")
    print("   # only speaks after them, and every sentence it writes is checked first")


ACCEPTANCE["part-13"] = lambda: (
    ACCEPTANCE["part-15"]() and _accept_part14_triggers() and _accept_part13()
)
DEMOS["part-13"] = _demo_part13


# ----------------------------------------------------------------------------
# operator chat: "call Sam Doe" asks for approval; the checks still decide
# ----------------------------------------------------------------------------

_PART20_NIGHT = "2026-11-05T12:00:00+00:00"  # 04:00 in Los Angeles


def _part20_operator(root: str, at: str, provider: Provider | None = None):
    from datetime import datetime

    from harness.memory import JsonlSession
    from harness.operator import Operator
    from phone.client import FakePhoneClient

    phone = FakePhoneClient()
    when = datetime.fromisoformat(at)
    operator = Operator(
        JsonlSession.for_call(root, "chat-demo"),
        provider=provider,
        phone=phone,
        clock=lambda: when,
    )
    return operator, phone


def _accept_part20() -> bool:
    """The real model asks to call Sam Doe; nothing dials until approval, and an
    approval outside calling hours still dials nobody."""
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        operator, phone = _part20_operator(root, _PART07_WHEN)
        turn = operator.chat("Please call Sam Doe.")
        print("tools:", [c["name"] for c in turn.tool_calls])
        print("reply:", turn.reply)
        asked = [a for a in turn.pending_approvals if a.debtor_id == "ACC-1001"]
        if not asked or phone.calls:
            print("BAD: expected one pending approval for ACC-1001 and no dial")
            return False
        night, night_phone = _part20_operator(root, _PART20_NIGHT)
        result = night.decide(asked[0].id, approve=True)
        print("after approval at night:", result.reply)
        return not night_phone.calls and bool(result.dial and result.dial["blocked"])


def _demo_part20() -> None:
    import tempfile

    from model import fake
    from model.fake import tool_call, tool_reply

    real = _llm_configured()
    provider = (
        None
        if real
        else fake(
            scripted=[
                tool_reply(tool_call("find_debtor", "c1", query="Sam Doe")),
                tool_reply(tool_call("check_call", "c2", debtor_id="ACC-1001")),
                tool_reply(tool_call("place_call", "c3", debtor_id="ACC-1001")),
                "Sam Doe (ACC-1001) passes every check. The call is waiting for your approval; "
                "nothing has been dialed.",
            ]
        )
    )
    print("model:", "the real one from LLM_BASE_URL" if real else "fake (set LLM_BASE_URL)\n")
    with tempfile.TemporaryDirectory() as root:
        operator, phone = _part20_operator(root, _PART07_WHEN, provider)
        print("you>   call Sam Doe   (at 11:00 in Los Angeles)")
        turn = operator.chat("call Sam Doe")
        for c in turn.tool_calls:
            print(f"  tool  {c['name']}({c['arguments']}) -> {str(c['result'])[:100]}")
        print("agent>", turn.reply)
        print("dials so far:", len(phone.calls))
        # The person comes back to approve hours later, at 04:00 in Los Angeles.
        later, later_phone = _part20_operator(root, _PART20_NIGHT, provider)
        for approval in turn.pending_approvals:
            print(f"\n[approve {approval.id} for {approval.debtor_id}] (at 04:00 in Los Angeles)")
            print("agent>", later.decide(approval.id, approve=True).reply)
        print("dials so far:", len(phone.calls) + len(later_phone.calls))
    print("\n   # the model asks, the person approves, the checks decide: nothing rang at 04:00")


ACCEPTANCE["part-20"] = lambda: (
    ACCEPTANCE["part-18"]()
    and ACCEPTANCE["part-16"]()
    and ACCEPTANCE["part-13"]()
    and _accept_part20()
)
DEMOS["part-20"] = _demo_part20


# call tools: what the agent may do during a call, gated and then approved
# ----------------------------------------------------------------------------


def _part21_ctx(stage: str) -> tuple:
    """A call with Sam in ``stage`` ("verify" or "discuss"), its store and its audit list."""
    from dataclasses import replace
    from datetime import UTC, datetime

    from compliance import Contact
    from harness.call_state import CallState, Stage
    from harness.call_tools import CallContext, MemoryCallStore

    sam = _part13_debtors()["+12135550101"]
    state = CallState.new("demo-part21", sam)
    if stage == "discuss":
        state = replace(state, stage=Stage.DISCUSS, verified=True, disclosed=True)
    else:
        state = replace(state, stage=Stage.VERIFY)
    audited: list = []
    store = MemoryCallStore()
    ctx = CallContext(
        debtor=sam,
        state=state,
        store=store,
        audit=lambda d, call_id, debt_id: audited.append(d),
        contact=Contact(zip=sam.zip, phone_e164="+12135550101"),
        flags=[],
        clock=lambda: datetime(2026, 11, 16, 18, 0, tzinfo=UTC),
    )
    return ctx, store, audited


def _accept_part21() -> bool:
    from harness import Agent
    from harness.call_tools import call_tools
    from harness.tools import run_tool

    # Without the model: no account data before verification.
    ctx, _, _ = _part21_ctx("verify")
    early = run_tool(call_tools(ctx), "lookup_account", "{}")
    print("lookup before verification:", early)
    good = early.startswith("error: blocked") and "1250" not in early

    # With the real model: a plan outside the policy ends up pending, never approved.
    ctx, store, audited = _part21_ctx("discuss")
    agent = Agent(tools=call_tools(ctx))
    reply = agent.run(
        "The person is verified. They say: I can put fifty dollars down and pay the rest "
        "monthly over 18 months. Use offer_payment_plan for exactly that plan, then tell "
        "them the result in one sentence."
    )
    print("agent:", reply)
    for d in audited:
        print(f"  audit: {d.rule_id} {'allow' if d.allow else 'block'}: {d.reason}")
    statuses = [r["status"] for r in store.rows if r["kind"] == "payment_plan"]
    print("plans saved:", statuses)
    return good and bool(statuses) and set(statuses) == {"pending"}


def _demo_part21() -> None:
    from harness.call_tools import call_tools
    from harness.tools import run_tool

    def show(ctx, name: str, **args: str) -> None:  # type: ignore[no-untyped-def]
        print(f"  {name}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
        print(f"    -> {run_tool(call_tools(ctx), name, args)}")

    print("-- still verifying who answered --")
    ctx, _, _ = _part21_ctx("verify")
    show(ctx, "lookup_account")
    print("\n-- verified and disclosed --")
    ctx, store, audited = _part21_ctx("discuss")
    show(ctx, "lookup_account")
    show(ctx, "offer_payment_plan", down_cents="25000", installments="10", frequency="monthly")
    show(ctx, "offer_payment_plan", down_cents="5000", installments="18", frequency="monthly")
    show(ctx, "schedule_callback", datetime="2026-11-17T07:30", tz="America/Los_Angeles")
    show(ctx, "schedule_callback", datetime="2026-11-17T10:30", tz="America/Los_Angeles")
    show(ctx, "log_dispute", reason="I already paid this")
    show(ctx, "offer_payment_plan", down_cents="25000", installments="10", frequency="monthly")
    print("\n  saved:", [f"{r['kind']}:{r.get('status', '')}".rstrip(":") for r in store.rows])
    print(f"  audit rows: {len(audited)} ({sum(not d.allow for d in audited)} blocked or denied)")
    print("\n   # the gate checks stage, dispute, cease and calling hours first; the approver")
    print("   # then checks the plan policy. These tools are not on live calls yet.")


ACCEPTANCE["part-21"] = _accept_part21
DEMOS["part-21"] = _demo_part21


# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# live checks: stop triggers before the model, the sentence filter after it
# ----------------------------------------------------------------------------


def _part19_call(script_name: str, reply: str | None) -> tuple:
    """Play one script with stages on; stops are kept in a list, never written.

    ``reply`` is what a fake model says; None means a model that must not be
    called at all. Returns ``(turns, stops, blocked, model_calls)``.
    """
    import secrets
    import tempfile
    from functools import partial
    from pathlib import Path

    from fastapi.testclient import TestClient

    from harness.memory import JsonlSession
    from model import fake
    from phone.webhook import create_app
    from sim.agentphone_sim import load_script, run_script

    model_calls: list[str] = []

    def must_not_run(messages: list) -> str:
        model_calls.append(str(messages[-1].get("content")))
        return "the model should not have been called"

    provider = fake(default=reply) if reply is not None else fake(scripted=must_not_run)
    stops: list = []
    blocked: list = []
    secret = secrets.token_hex(16)
    app = create_app(
        provider=provider,
        secret=secret,
        open_session=partial(
            JsonlSession.for_call, Path(tempfile.mkdtemp(prefix="debtagent-p19-"))
        ),
        find_debtor=_part13_debtors().get,
        audit=lambda decision, call_id, debt_id: blocked.append(decision),
        record_stop=stops.append,
    )
    turns = run_script(load_script(script_name), TestClient(app), secret)
    return turns, stops, blocked, model_calls


def _accept_part19() -> bool:
    from compliance.triggers import CLOSES

    turns, stops, _, model_calls = _part19_call("sam_stop_calling", None)
    for caller, agent in turns:
        print(f"  caller> {caller}\n   agent> {agent}")
    stopped = (
        turns[-1][1] == CLOSES["cease"]
        and not model_calls
        and [s.trigger.flag for s in stops] == ["cease"]
    )
    print(f"  stop calling: {'ok' if stopped else 'BAD'} (model calls: {len(model_calls)})")

    turns, _, blocked, _ = _part19_call("sam_balance", _PART13_LEAKY)
    replies = [reply for _, reply in turns]
    filtered = bool(blocked) and not any("$" in r or "balance" in r.lower() for r in replies)
    print(
        f"  balance before verify dropped: {'ok' if filtered else 'BAD'} ({len(blocked)} blocked)"
    )
    return stopped and filtered


def _demo_part19() -> None:
    print("-- the caller says stop: the model is never asked --")
    turns, stops, _, model_calls = _part19_call("sam_stop_calling", None)
    for caller, agent in turns:
        print(f"  caller> {caller}\n   agent> {agent}")
    for s in stops:
        print(f"  saved: flag={s.trigger.flag} stage={s.stage} matched={s.trigger.matched!r}")
    print(f"  model calls: {len(model_calls)}\n")
    print("-- a model that says the balance before anyone is verified --")
    turns, _, blocked, _ = _part19_call("sam_balance", _PART13_LEAKY)
    for caller, agent in turns:
        print(f"  caller> {caller}\n   agent> {agent}")
    for d in blocked:
        print(f"  (dropped before it was said, {d.rule_id}) {d.evidence.get('sentence')}")
    print("\n   # code ends the call on a stop request and checks every sentence the model writes")


ACCEPTANCE["part-19"] = lambda: ACCEPTANCE["part-13"]() and _accept_part19()
DEMOS["part-19"] = _demo_part19


# ----------------------------------------------------------------------------
# chat panel in the web UI: the real API, a fake model, a fake phone
# ----------------------------------------------------------------------------

_PART24_CALL = r"^\s*(?:please\s+)?call\s+(.+?)[.!?\s]*$"


def _part24_fake_model(messages: list[dict]):
    """A stand-in operator model: "call <name>" -> find, check, ask to place the call.

    It asks to place the call even when the check blocks it, so the page shows a
    blocked approval card (no Approve button) as well as an allowed one.
    """
    import json
    import re

    from model.fake import tool_call, tool_reply

    last = messages[-1]
    call_id = f"demo_{len(messages)}"
    if last.get("role") != "tool":
        found = re.match(_PART24_CALL, str(last.get("content") or ""), re.IGNORECASE)
        if not found:
            return 'This is the fake model. Try "call Sam Doe" or "call Casey Morgan".'
        return tool_reply(tool_call("find_debtor", call_id, query=found.group(1)))
    asked = {
        c["id"]: c["function"]["name"]
        for m in messages
        if m.get("role") == "assistant"
        for c in m.get("tool_calls") or []
    }
    tool = asked.get(last.get("tool_call_id"))
    body = str(last.get("content") or "")
    if body.startswith("error"):
        return f"That didn't work: {body}"
    data = json.loads(body)
    if tool == "find_debtor":
        first = data["matches"][0]
        return tool_reply(tool_call("check_call", call_id, debtor_id=first["id"]))
    if tool == "check_call":
        return tool_reply(tool_call("place_call", call_id, debtor_id=data["debtor_id"]))
    if tool == "place_call":
        return (
            f"I asked to call {data['name']} ({data['debtor_id']}). Nothing has been dialed: "
            "approve or deny it in the card below."
        )
    return "Done."


def _demo_part24() -> None:
    """Serve the real chat API with a fake model and a fake phone, for ``npm run dev``."""
    import os
    import tempfile

    import uvicorn

    from harness import api
    from harness.memory import JsonlSession
    from harness.operator import Operator
    from model import fake
    from phone.client import FakePhoneClient

    real = _llm_configured()
    provider = None if real else fake(scripted=_part24_fake_model)
    root = tempfile.mkdtemp(prefix="chat-ui-demo-")
    phone = FakePhoneClient()

    def make_operator(session_id: str) -> Operator:
        # Chat history in a temp folder instead of the session table; the tools
        # still read the local Supabase, and approved calls go to the fake phone.
        return Operator(
            JsonlSession.for_call(root, f"chat-{session_id}"), provider=provider, phone=phone
        )

    api.make_operator = make_operator
    port = int(os.environ.get("API_PORT", "8000"))
    print("model:", "the real one from LLM_BASE_URL" if real else "fake (set LLM_BASE_URL)")
    print("phone: fake, so an approved call rings nobody")
    print(f"chat history: {root}")
    print(
        "\nTo use it:\n"
        "  1. npx supabase start, with the synthetic seed loaded (debtors, phones)\n"
        "  2. this terminal: uv run demo part-24   (serves /api on "
        f"http://127.0.0.1:{port})\n"
        "  3. another one: cd web && npm run dev, then open http://localhost:5173\n"
        '  4. type "call Sam Doe" in the chat on the right; the approval card shows the\n'
        "     masked phone and the check. During calling hours it can be approved; at\n"
        '     night, or for a debtor the checks block (try "call Casey Morgan"), the\n'
        "     card has only Deny.\n"
        "Ctrl+C stops the server.\n"
    )
    uvicorn.run(api.app, host="127.0.0.1", port=port)


DEMOS["part-24"] = _demo_part24


# ----------------------------------------------------------------------------
# call tools on live calls: the gate and the approver behind every tool call
# ----------------------------------------------------------------------------

_PART22_TO_DISCUSS = ["Hello?", "Yes, this is Sam Doe speaking.", "April 12, 1985, and 90012."]


def _part22_call(lines: list[str], provider: Provider | None) -> tuple:
    """Play ``lines`` through the signed simulator against the live webhook.

    ``provider`` None means the real model. The tools write to memory, not the
    database. Returns ``(turns, store, decisions, tool_results)``.
    """
    import secrets
    import tempfile
    from functools import partial
    from pathlib import Path

    from fastapi.testclient import TestClient

    from harness.call_tools import MemoryCallStore
    from harness.memory import JsonlSession
    from phone.live_tools import ToolBackend
    from phone.webhook import create_app
    from sim.agentphone_sim import Script, run_script

    store = MemoryCallStore()
    decisions: list = []
    folder = Path(tempfile.mkdtemp(prefix="debtagent-p22-"))
    secret = secrets.token_hex(16)
    app = create_app(
        provider=provider,
        secret=secret,
        open_session=partial(JsonlSession.for_call, folder),
        find_debtor=_part13_debtors().get,
        audit=lambda decision, call_id, debt_id: None,
        record_stop=lambda stop: None,
        open_backend=lambda debtor: ToolBackend(
            store, lambda d, call_id, debt_id: decisions.append(d), flags=[]
        ),
        supervisor_number="",
    )
    turns = run_script(Script("part22", lines), TestClient(app), secret)
    results = [
        m["content"]
        for path in folder.glob("*.jsonl")
        for m in JsonlSession(path).load()
        if m.get("role") == "tool"
    ]
    return turns, store, decisions, results


def _part22_lookup_model() -> Provider:
    """A fake model that calls lookup_account, then reads out what it got."""
    from model import fake
    from model.fake import tool_call, tool_reply

    def reply(messages: list):  # type: ignore[no-untyped-def]
        last = messages[-1]
        if last.get("role") == "tool":
            return f"Here is what I found. {last['content']}"
        return tool_reply(tool_call("lookup_account"))

    return fake(scripted=reply)


def _accept_part22() -> bool:
    # Before verification: the lookup is blocked by the gate, no balance is said.
    lines = _PART22_TO_DISCUSS[:2] + ["Why do you need that? How much do I owe?"]
    turns, _, _, results = _part22_call(lines, _part22_lookup_model())
    spoken = turns[-1][1]
    early = bool(results) and results[0].startswith("error: blocked") and "1,250" not in spoken
    print(f"  lookup before verification: {'ok' if early else 'BAD'} ({results[:1]})")

    # The real model, verified: a plan outside the policy is saved as pending only.
    ask = "Set up a plan for me: fifty dollars down and the rest monthly over eighteen months."
    turns, store, decisions, _ = _part22_call([*_PART22_TO_DISCUSS, ask], None)
    for caller, agent in turns[3:]:
        print(f"  caller> {caller}\n   agent> {agent}")
    statuses = [r["status"] for r in store.rows if r["kind"] == "payment_plan"]
    print(f"  plans saved: {statuses}; decisions: {[(d.rule_id, d.allow) for d in decisions]}")
    pending = bool(statuses) and set(statuses) == {"pending"}
    return early and pending


def _demo_part22() -> None:
    from model import fake
    from model.fake import tool_call, tool_reply

    print("-- not verified yet: the model calls lookup_account anyway --")
    lines = _PART22_TO_DISCUSS[:2] + ["Why do you need that? How much do I owe?"]
    turns, _, decisions, results = _part22_call(lines, _part22_lookup_model())
    for caller, agent in turns:
        print(f"  caller> {caller}\n   agent> {agent}")
    print(f"  tool result: {results[0]}")
    for d in decisions:
        print(f"  audit: {d.rule_id} {'allow' if d.allow else 'block'}: {d.reason}")

    for label, down, count in (("inside", "25000", "10"), ("outside", "5000", "18")):
        print(f"\n-- verified: a plan {label} the policy --")
        plan = tool_call(
            "offer_payment_plan", down_cents=down, installments=count, frequency="monthly"
        )
        model = fake(scripted=[tool_reply(plan), "Thanks, I have noted that plan."])
        turns, store, decisions, results = _part22_call(
            [*_PART22_TO_DISCUSS, "Can we set up a plan?"], model
        )
        print(f"  caller> {turns[-1][0]}\n   agent> {turns[-1][1]}")
        print(f"  tool result: {results[0][:90]}")
        print(f"  saved: {[r['status'] for r in store.rows]}")

    print("\n-- the model ends the call --")
    model = fake(scripted=[tool_reply(tool_call("end_call", reason="done")), "Goodbye."])
    turns, _, _, _ = _part22_call([*_PART22_TO_DISCUSS, "That's all.", "Hello?"], model)
    print(f"  caller> {turns[-1][0]}\n   agent> {turns[-1][1]} (hung up; 'Hello?' never sent)")
    print("\n   # every tool call on a real call passes the gate, then the approver, and")
    print("   # anything it saves is committed before the caller hears about it")


ACCEPTANCE["part-22"] = lambda: ACCEPTANCE["part-19"]() and _accept_part22()
DEMOS["part-22"] = _demo_part22
