# DebtAgentCall

[![CI](https://github.com/Aditya292248/DebtAgentCall/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Aditya292248/DebtAgentCall/actions/workflows/ci.yml)

A debt-collection voice agent that puts compliance first, built one small step at a time.

Right now the project can send messages to a language model and get a reply back, and it has an agent that remembers the conversation, follows a system prompt kept in a file, and can look up an account with a tool. The made-up debtors live in a local Postgres database run by Supabase. The pre-call compliance rules (calling hours, the 7-in-7 call limit, consent, stop requests, do-not-call) are here as plain Python checks; they are not wired into the agent yet. Every decision they make can be written to an append-only audit log that the database won't let anyone edit. There is a webhook that AgentPhone can call during a phone call, and a fake caller to test it without a phone. Each call's conversation is saved as it happens, so every turn of a call sees the turns before it. A first read-only web page shows the debtors with their pre-call result and the audit log. A dialer checks each debt right before dialing, writes the call to the ledger first, and places it once. The call flow and the other rules come later, each in its own step.

## What is here

```
model/
  client.py             chat(messages, ...) -> LLMResponse, the one function every model call goes through
  provider.py           Provider (base URL, model, API key) and LLMResponse
  openai_compatible.py  the HTTP call to an OpenAI-style /chat/completions endpoint
  fake.py               a fake provider, so tests run offline and give the same answer every time
harness/
  agent.py              Agent, which keeps the conversation and resends it every turn
  instructions.py       load_system_prompt(path), which reads the system prompt from a file
  tools.py              Tool, ToolRegistry, run_tool() and the first tool, lookup_account
  tool_outcome.py       ToolOutcome: the one outcome every tool call ends in, and the text the model reads
  db.py                 connect(), find_account(name) and the pre-call loaders, which read Postgres
  precall.py            check_and_record(conn, account, when): run the pre-call checks, log every decision
  memory.py             Session, JsonlSession and PostgresSession: each call's saved history
  compact.py            compact_messages(): summarize the middle of a long call to stay in the context window
  call_state.py         CallState, Stage and Debtor: where a call is in its required steps
  stages.py             next_stage(state, caller_text, debtor): the call's order, decided in code
  api.py                `uv run api`: a small read-only web API for the UI (pre-call decisions, chain check)
  subagents.py          run_subagent, fan_out and the delegate / fan_out tools (after calls only)
  review.py             review_transcript(transcript, account): reviewer subagents check a finished call
  skills.py             Skill, discover(), skills_prompt() and the load_skill tool
  payments.py           the payment ledger: parsing, current balance checks, history and the on-time report
  operator_reads.py     the operator chat's account reads, notes, callbacks and stops (capped, phones masked)
compliance/
  decision.py           Decision (allow, rule_id, reason, evidence), the answer every rule gives
  timezones.py          zones_for(zip, phone), the time zones a debtor might be in
  hours.py              Contact and check_calling_hours(contact, when)
  attempts.py           Attempt and check_call_frequency, the 7-calls-in-7-days limit
  consent.py            PhoneConsent and check_consent
  flags.py              Flag and the checks for stop requests and do-not-call
  precall.py            CallFacts, check_before_call(facts, when) and may_call(decisions)
  audit.py              record(conn, decision) and verify_chain(conn), the audit log's hash chain
  utterance.py          SpeechState, check_sentence(sentence, state) and split_sentences(text)
phone/
  signing.py            sign() and verify(), the HMAC check on every AgentPhone webhook
  webhook.py            create_app(), the FastAPI app with POST /webhooks/agentphone
  live_tools.py         the call tools for one live turn: CallContext from the saved state, gate, approver
  replies.py            say(), say_and_hangup() and transfer(), the replies AgentPhone understands
  client.py             PhoneClient, AgentPhoneClient (make_call, never retried), FakePhoneClient, CallError
dialer/
  service.py            dial_one(conn, account, when, phone): check, record, then call once; dial_queue for a list
  cli.py                `uv run dialer`: --dry-run shows who would be called; --confirm places calls
sim/
  agentphone_sim.py     a fake AgentPhone that plays a scripted caller against the webhook
  scripts/              the caller scripts, as YAML
instructions/
  system.md             the system prompt the agent sends first on every call
  subagent.md           the short prompt every subagent starts with
  stages/               what the model is told in each call stage, added after system.md
skills/                 short playbooks for hard conversations (hardship, angry caller, ...)
supabase/
  config.toml           local Supabase settings (database on port 54322)
  migrations/           the debtor, phone, debt, call_attempt, flags, audit_event, call_message, payment and debt_note tables
  seed.sql              made-up debtors, with call history and flags for each pre-call check
tasks/
  verify.py             offline checks: format, lint, types, tests, import
  accept.py             live check against a real model
  demo.py               a short live demo
  checks.py             the live checks and demos the two scripts above run
  audit_verify.py       `uv run audit-verify`, which checks every hash in the audit log
  serve.py              `uv run serve`, which runs the webhook on this machine
  latency_report.py     `uv run latency-report`, which reports how long live caller turns took on our side (see docs/latency.md)
web/                    the browser UI (Vite, React, TypeScript, Tailwind, shadcn/ui), read-only for now
tests/
  <area>/               offline tests by feature, in the same folders as the code
```

The rest of the code will only use `chat` and `Provider`. That means you can switch from LM Studio to Ollama, OpenRouter or OpenAI without changing any calling code.

## Setup

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
cp .env.example .env
```

Then edit `.env` to point at your model server. Any server that speaks the OpenAI chat format works:

| Variable       | What it is                          | Default                    |
| -------------- | ----------------------------------- | -------------------------- |
| `LLM_BASE_URL` | Server URL, ending in `/v1`         | `http://localhost:1234/v1` |
| `LLM_MODEL`    | Model name the server knows         | `google/gemma-3-12b`       |
| `LLM_API_KEY`  | API key (local servers accept any)  | `lm-studio`                |
| `AGENTPHONE_WEBHOOK_SECRET` | The secret AgentPhone signs webhooks with | none (every webhook gets a 401) |

Real environment variables take priority over `.env`.

## Use it

```python
from model import chat

reply = chat([{"role": "user", "content": "Say hello in one sentence."}])
print(reply.content)
```

To test without a model server, use the fake provider:

```python
from model import chat, fake

reply = chat([{"role": "user", "content": "hi"}], provider=fake(default="hello"))
assert reply.content == "hello"
```

If `LLM_BASE_URL` is empty or missing `http://` / `https://`, `chat` raises a `ValueError` that says so, before it sends any request.

## The agent

The model forgets everything between calls. `Agent` fixes that by keeping the whole conversation in `agent.messages` and sending all of it every turn:

```python
from harness import Agent

agent = Agent()
agent.send("My name is Sam Doe.")
print(agent.send("What is my name?"))  # the model can answer, because turn 1 was resent
print(agent.messages)  # user, assistant, user, assistant
```

`Agent` takes the same optional `provider` and `model` as `chat`, so `Agent(provider=fake(...))` works offline. If the model call fails, the user message is taken back out of `agent.messages`, so the history never ends on a message with no reply.

## The system prompt

The agent's standing instructions live in `instructions/system.md`. On every call, `Agent` reads that file with `load_system_prompt()` and sends its text first, as a `system` message, ahead of the conversation. The prompt is not stored in `agent.messages`, so if you edit the file, the next turn uses the new text.

To use a different file, pass its path:

```python
agent = Agent(system_prompt_path="my_prompt.md")
```

If the file is missing or empty, no system message is sent and the agent still works.

## Tools and the loop

The model can't look anything up by itself. It can only ask. `Agent` sends it a list of tools with every call, and the model can reply with a tool request instead of text, such as "call `lookup_account` with name=Sam Doe".

`agent.run(text)` is the loop that handles those requests:

1. Send the conversation to the model.
2. If the reply asks for tools, run each one and add its result to the conversation as a `tool` message.
3. Send again, so the model can see the results. Repeat until it answers in plain text.

```python
from harness import Agent

agent = Agent()
print(agent.run("I'm Sam Doe. How much do I owe?"))  # the balance comes from the database
```

There is one tool so far, `lookup_account(name)`. It reads the database (see below) and returns the matching account, or `error: no account found for '...'`.

A bad tool request never crashes the loop. An unknown tool, broken JSON, a missing or extra argument, or an error inside the tool all come back to the model as an `error: ...` message, so it can read what went wrong. Every tool request gets a result, and every result has one outcome (below).

If the model keeps asking for tools, `run()` stops after `max_steps` model calls (10 by default) and returns `stopped: used all N steps without a final answer`. `send()` still makes exactly one call and runs no tools.

### Tool outcomes

Every tool call ends in exactly one outcome (`harness/tool_outcome.py`), and the model is told which on the first line of the tool message: `outcome: success`, then the text.

| Outcome | Meaning |
| --- | --- |
| `success` | The tool ran and returned a result. |
| `error` | It ran and failed cleanly: bad arguments, an unknown tool, a result that isn't text, or an `error:` result of its own. A tool that raises is `error` only if it is safe to retry. |
| `timeout` | A tool that is safe to retry did not finish within its `timeout_seconds`. |
| `denied` | Not allowed: a compliance gate block, or the approver said no. Retrying won't change it. |
| `aborted` | The turn was cancelled (`Agent.abort()`) before the tool started, so it never ran. |
| `not_started` | An earlier call in the same batch ended `unknown` or was aborted, so this one was held back. |
| `unknown` | Nobody can tell whether it ran: a crash between the request and its saved result, an exception from a tool that may have written, or a timeout of a tool with side effects. Never retried automatically. |

A `Tool` is `safe_to_retry=False` unless it only reads. Today that is `lookup_account` (both versions), `load_skill` and the operator's read tools (`find_debtor`, `check_call`, `call_status`, `payment_history`, `on_time_report`, `list_debtors`, `account_summary`, `call_history`, `next_allowed_call`, `portfolio_summary`, `audit_for`). `run_tool_result()` gives the outcome; `run_tool()` returns the same text as before. `Agent(on_tool_outcome=...)` is called once per tool call, for logging.

On a live call an `unknown` outcome is treated like a failed save: the fixed line and a hangup, and a person checks. In the operator chat, an approved payment, note, callback or dial that fails part-way is "not confirmed": it is never marked done and never run a second time.

## The database

The made-up debtors live in Postgres, run locally by [Supabase](https://supabase.com/docs/guides/local-development). You need the Supabase CLI and Docker Desktop (running).

```bash
supabase start
```

This starts a local database on port 54322 and creates the tables in `supabase/migrations/`. The first time, it also loads `supabase/seed.sql`. To wipe the database and load the seed again:

```bash
supabase db reset
```

There are three tables:

- `debtor`: name, date of birth, address, ZIP and state
- `phone`: the debtor's numbers, whether each is a cell or a landline, and when consent was given or taken back
- `debt`: the creditor, the balance it was placed with in cents (`balance_cents`), the day it was opened (`opened_on`), and a status (`active`, `disputed`, `paused` or `closed`)

`balance_cents` never changes after a payment. The balance owed now is `public.current_balance_cents(debt)`, which subtracts payments and adds back reversals (see [Payments](#payments)). `lookup_account`, the live call and the web list all read that function.

The app connects with `DATABASE_URL` (see `.env.example`). It defaults to the local database, `postgresql://postgres:postgres@127.0.0.1:54322/postgres`. Row level security is on for every table, with only a read rule for signed-in Supabase users. The app's own direct connection is not limited by it.

`harness/db.py` has `find_account(name)`, which `lookup_account` uses. It matches the full name, ignoring case and extra spaces. If the database is down, the tool returns an `error: lookup_account failed: ...` message instead of crashing.

The tests in `tests/harness/test_account_lookup_db.py` that need the database are marked `db`. They run against the real local database. When it isn't running, they are skipped with "run `supabase start`", so `uv run verify` still works offline. CI (`.github/workflows/ci.yml`) starts the database with `supabase db start` and sets `REQUIRE_DATABASE=1`, which turns that skip into a failure, so the database tests always run there. A `db` test that writes against a debt uses the `scratch_sam` fixture (`tests/conftest.py`): its own debtor and debt with fresh ids, deleted afterwards, so it passes on a fresh or a used database and leaves the seed rows alone.

```bash
uv run pytest -m db -v
```

```bash
uv run demo part-06
```

The demo prints the debt table, then looks up Sam Doe and a name that isn't there.

## Calling hours

The law (FDCPA 805(a)(1), Reg F 1006.6(b)(1)(i)) says a collector shouldn't call before 8am or after 9pm in the debtor's local time. That rule lives in plain Python, not in the prompt, so the model can't talk its way past it.

```python
from datetime import datetime, timezone
from compliance import Contact, check_calling_hours

sam = Contact(zip="90012", phone_e164="+12135550101")
d = check_calling_hours(sam, datetime.now(timezone.utc))
print(d.allow, d.reason)  # e.g. False  outside 08:00-21:00: 21:05 America/Los_Angeles
```

How it decides:

- It finds every time zone the debtor might be in, from the ZIP code and from the phone's area code. People move and keep their number, so if the two disagree, both zones count.
- The call is allowed only if it is from 08:00 up to 21:00 in every one of those zones. 21:00 itself is blocked.
- If neither the ZIP nor the area code is known, we don't know their local time, so the call is blocked.
- Any time the debtor said is bad for them (`Contact.inconvenient`, such as 12:00-13:00) is blocked too, in every zone.

`when` must include a time zone, or the check raises a `ValueError`. It can be a future time, so a later tool that books a callback can use the same check. Every rule returns a `Decision` with `allow`, `rule_id` (here `"calling_hours"`), a short `reason`, and the `evidence` it looked at.

The ZIP and area-code tables in `compliance/timezones.py` are small and hand-made; a real data source comes later, and so do state rules that are stricter than the federal hours. This is not legal advice.

## Pre-call checks

Before any call, `check_before_call(facts, when)` runs every check and returns all their decisions. `may_call(decisions)` is True only if every one allows. Like calling hours, each check is plain Python that looks at facts from the database, never at the model's words.

| Rule id          | Blocks the call when                                                                 |
| ---------------- | ------------------------------------------------------------------------------------ |
| `calling_hours`  | it is outside 08:00-21:00 in any zone the debtor may be in (see above)                |
| `call_frequency` | this debt already had 7 calls in the last 7 days, answered or not, or a conversation with the debtor in the last 7 days (Reg F 1006.14(b)(2)) |
| `tcpa_consent`   | the number has no consent on file, or the consent was revoked                         |
| `cease`          | the debtor asked us to stop (`cease`), or to stop calling this number (`opt_out_phone`) |
| `dnc`            | the number is on a do-not-call list                                                   |

Missing data blocks too: no phone record, no call history or no flags loaded each turn into a blocked decision.

Attorney representation, bankruptcy and deceased-person handling are not handled in this version. They are legal requirements for real collection calls; see "Not handled in this version" in [docs/compliance.md](docs/compliance.md).

```python
from datetime import datetime, timezone
from compliance import check_before_call, may_call
from harness.db import load_call_facts

decisions = check_before_call(load_call_facts("ACC-1004"), datetime.now(timezone.utc))
print(may_call(decisions), [d.reason for d in decisions if not d.allow])
```

The facts come from two new tables: `call_attempt` (one row per call placed) and `flags` (stop requests, do-not-call and the rest). The seed adds one made-up debtor for each way a call can be blocked. `harness/db.py` only loads; the checks in `compliance/` do all the deciding, so they can be tested without a database.

```bash
uv run demo part-07
```

The demo checks every made-up debtor at a fixed time (2026-11-05, 14:00 in New York), so the seeded call history always gives the same answers. This is not legal advice.

## Test numbers

`TEST_NUMBERS` (in the environment or `.env`; unset by default) is a comma-separated list of E.164 numbers, for the repo owner's own phone only, so real test calls can be placed outside calling hours and past the call limit. A listed number skips the `calling_hours` and `call_frequency` rules (the 7-in-7 limit and the pause after a conversation) and nothing else: consent, cease, DNC and debt status still run and still block, and every attempt is still recorded. Every skip shows as an allow with the reason `skipped: test number (TEST_NUMBERS)` and, when the decision is recorded, as one `test_number_exemption` row per skipped rule in the audit log. A malformed entry is ignored and logged; there are no wildcards or prefixes. The dialer, the operator agent and `/api/precall` share the same check, and the phone webhook and live-call rules are not affected.

```bash
uv run demo part-27
```

## The audit log

Regulators can ask why a call was or wasn't placed, so every check's decision is kept in the `audit_event` table: when, which debt, which rule, allow or block, the reason and the evidence. `check_and_record` runs the pre-call checks and writes one row per decision, allowed and blocked alike:

```python
from datetime import datetime, timezone
from compliance import may_call
from harness.db import connect
from harness.precall import check_and_record

with connect() as conn:
    decisions = check_and_record(conn, "ACC-1007", datetime.now(timezone.utc))
    print(may_call(decisions))  # False: Morgan asked us to stop, and that row is in the log
```

The log can only grow. The database itself enforces this, so it doesn't depend on our code behaving:

- `update`, `delete` and `truncate` are refused by triggers, and revoked from every role, the app's own login included. A correction is a new row.
- Each row stores the hash of the row before it (`prev_hash`) and its own `hash`, which covers that link plus all of its fields. The insert trigger fills these in (and the row number and time) under a lock, so two inserts at once can't fork the chain.
- Row level security is on, with read access for signed-in users only.

`uv run audit-verify` recomputes every hash in Python and exits 1 with the problems if a row was changed, or removed from before the newest one. If someone with full database access switches the triggers off and edits an old row, that row's hash stops matching, and the check catches it.

What the chain can't prove on its own: the hash method is public and uses no secret key, so someone with full database access who also recomputes every later hash leaves a chain that checks out. Cutting rows off the end does the same. Both are caught only by comparing against a copy of the newest `seq` and `hash` kept outside the database, which is planned for later (a regular export to Supabase Storage). Until then, treat the log as protection against mistakes and casual edits, not against someone who controls the database.

```bash
uv run audit-verify
```

If Windows blocks the generated `audit-verify.exe`, `uv run python -m tasks.audit_verify` runs the same check.

`compliance/audit.py` explains exactly how a hash is made, so anyone can check the log with their own code. The tests in `tests/compliance/test_audit_log.py` run inside transactions that are rolled back, so they never leave rows behind.

```bash
uv run demo part-08
```

The demo runs the pre-call checks for every made-up debtor, prints the rows it added and then the `audit-verify` result. These rows are kept; `supabase db reset` clears them.

## The phone webhook

[AgentPhone](https://docs.agentphone.ai) handles the speech on a call. It turns what the caller says into text, sends that text to our webhook, and speaks the text we send back. So our server only ever deals with text:

```
caller speaks -> AgentPhone (speech to text) -> POST /webhooks/agentphone -> Agent.run -> {"text": reply} -> AgentPhone (text to speech)
```

Every request is checked before anything else happens:

- AgentPhone signs each webhook with a shared secret (`AGENTPHONE_WEBHOOK_SECRET`). The signature is an HMAC-SHA256 over `"{timestamp}.{raw body}"`. A wrong or missing signature gets a 401, and the model is never called. If the secret isn't set at all, every request gets a 401.
- A request whose timestamp is more than five minutes off is refused too, so an old request can't be replayed.
- Each webhook has an `X-Webhook-ID`. If the same id comes again, it gets the same reply as the first time, and the model isn't asked twice.

Then the event decides what happens. `agent.message` on the voice channel runs one turn of the agent with the caller's words and replies `{"text": ...}`. `agent.call_ended` is recorded by `phone/call_end.py`: the attempt's outcome and its `connected` and `conversation_with_debtor` flags (a conversation only if the caller spoke and was verified), the call state marked ended, an audit row and a call summary; a call we did not place is audited and still answered 200. `uv run reconcile` does the same for attempts whose end never arrived. Any other event gets `{}`. `phone/replies.py` also has `say_and_hangup()` and `transfer()`, for ending the call or handing it to a person. On a staged call the model gets the call tools from `harness/call_tools.py` (built per turn by `phone/live_tools.py`): each tool call passes the compliance gate and then `PolicyApprover`, saved rows are committed before the reply, `end_call` hangs up, and `escalate_to_human` transfers only when `SUPERVISOR_NUMBER` is set.

Each turn builds a new agent, but on that call's saved session, so it remembers what was said earlier in the same call (see [Sessions](#sessions)).

To run it:

```bash
uv run serve
```

It listens on `http://127.0.0.1:8000/webhooks/agentphone` (`--host` and `--port` change that). There is no real AgentPhone account set up yet, so the way to try it is the fake caller in `sim/`. It reads a script of caller lines from `sim/scripts/`, sends each one as a correctly signed webhook, prints the replies and ends with `agent.call_ended`. With the server running and the same secret set in both terminals:

```bash
uv run python -m sim.agentphone_sim sam_balance --url http://127.0.0.1:8000
```

The tests use the same simulator in-process, through FastAPI's `TestClient`, with the fake model.

```bash
uv run demo part-09
```

The demo plays every script in `sim/scripts/` through the webhook in-process. It uses the real model if `LLM_BASE_URL` is set, and otherwise the fake one.

## Sessions

AgentPhone sends each caller turn as its own webhook, and each webhook builds a new `Agent`. To remember the call, every message is saved the moment it is added, under the call's `data.callId`. The next turn's agent loads that call's messages first, so the model sees the whole call so far. Two different calls never share messages.

A session is a list of records that are only ever added to: a `session` header, then one `message` record per message, and a `compaction` record whenever the history is replaced by a shorter one. Loading replays them in order. There are two places to keep them, with the same behaviour:

- `PostgresSession` keeps them as rows of the `call_message` table. The webhook uses this by default.
- `JsonlSession` keeps them as lines in a file. The tests and the demos use it, so they need no database.

```python
from harness import Agent
from harness.memory import JsonlSession

session = JsonlSession.for_call("sessions", "call_123")
Agent(session=session).run("Hi, this is Sam Doe.")
Agent(session=JsonlSession.for_call("sessions", "call_123")).run("What name did I give?")
```

A crash can leave a few kinds of damage, and each one is handled:

- If the process died while writing a line, that half line is skipped on load and cut off before the next write. A broken line anywhere else is treated as corruption and raises an error.
- If a turn died after the model asked for a tool but before the result was saved, the next load adds a result with the outcome `unknown`, since the tool may have run. Without a result, the model API would reject the history.
- If a model call fails, the turn is taken back out of the saved history too, so a retry doesn't send the caller's words twice.

The `call_message` table needs the new migration. Run `supabase migration up` on an existing local database, or `supabase db reset` to rebuild it.

```bash
uv run demo part-10
```

The demo plays Sam Doe's call through the webhook. With the fake model, each reply says how many earlier messages of the call the model was sent.

## Compaction

A model can only read so much at once (its context window). A long call would sooner or later go past it, so before each caller turn the agent checks how big the next model call would be. Past 80% of `max_context_tokens`, it keeps the first `keep_head` messages (how the call opened) and the last `keep_tail` (what is being said now) word for word, and asks the model to summarize everything in between into one message. The system prompt isn't in the history at all, so it is never summarized.

```
keep_head=2, keep_tail=3:
[hello] [hi] [a] [b] [c] [d] [e] [f] [g]  ->  [hello] [hi] [summary of a..d] [e] [f] [g]
```

- **Tool calls stay whole.** The cut points move a message or two so a tool request is never split from its result. The model API would reject a history like that.
- **A second compaction** finds the first summary in its middle and folds it into the new one, so there is only ever one summary.
- **It is saved.** The session gets a `compaction` record, so the next webhook's agent loads the shorter history. The old messages stay on record.
- **If the summary fails** (the model errors), the history is left exactly as it was and the caller still gets an answer. `agent.compact_error` says what went wrong.
- **Sizes** are the token count the server reported for the last call, plus about 4 characters per token for anything newer.

Compliance never depends on the summary. Whether the person was verified, whether the debt collector disclosure was said, and whether they asked us to stop are kept by code outside the messages, and the summary message says it is not a record of them. A summary can drop or garble anything, so nothing should read those facts back out of the history.

```bash
uv run demo part-16
```

The demo plays a long made-up call with a small 300-token limit. It compacts twice, the second time folding in the first summary, and the agent can still answer a question about something said in the summarized middle.

## The sentence filter

Once a sentence is spoken on a call, it can't be taken back. So every sentence the agent wants to say goes through `check_sentence(sentence, state)` first, and a blocked one is never sent. `state` is a `SpeechState`: whether the person is verified, whether the debt collector disclosure was said, the amounts on file, the creditor's name, whether legal action is authorized, whether the debt is time-barred, and whether we are in the validation period.

| Rule id                  | Blocks a sentence that                                                         |
| ------------------------ | ------------------------------------------------------------------------------ |
| `third_party_disclosure` | mentions the debt, the creditor or money before the person is verified (FDCPA 805(b)) |
| `mini_miranda_first`     | does the same after verifying but before the disclosure (FDCPA 807(11))        |
| `false_identity`         | claims to be, or call from, a lawyer, court, the IRS or government (807)        |
| `threat`                 | threatens arrest, jail or police, or legal action that isn't authorized (806, 807) |
| `time_barred`            | mentions suing or garnishing on a time-barred debt (Reg F 1006.26(b))          |
| `profanity`              | swears                                                                         |
| `amount_mismatch`        | says an amount that isn't on file, or talks money without a checkable amount   |
| `overshadowing`          | pushes to pay now ("must pay today", "final notice") during the validation period |

```python
from compliance import SpeechState, check_sentence

state = SpeechState(verified=False, known_amounts_cents={124000}, creditor="Acme Bank")
print(check_sentence("Your balance is $1,240.", state).rule_id)  # third_party_disclosure
```

Words match whole words, ignoring case, and the lists sit at the top of `compliance/utterance.py`. So "indebted" and "Owen" don't trip the debt check, but "debtor" does, because it is listed. "Sue" is only a threat as a verb ("we will sue", "sued"), so a person named Sue can be greeted. The filter doesn't understand "not": "we will not sue you" is blocked too. When unsure, it blocks. `split_sentences(text)` cuts model output into sentences without breaking "$1,240.50" or "Mr. Doe"; the call stages below use it on every model reply.

```bash
uv run demo part-11
```

The demo prints allow or block, with the rule, for a list of sample sentences at each stage of a call. This is not legal advice.

## Call stages

Every call goes in the same order, and code, not the model, decides when to move on:

| Stage         | What happens                                                                        | Model? |
| ------------- | ----------------------------------------------------------------------------------- | ------ |
| `GREETING`    | "May I please speak with Sam Doe?" Nothing about a debt.                              | Only for unclear answers |
| `VERIFY`      | Asks for date of birth and ZIP, and compares them with the database in code (FDCPA 805(b)). They can come in one turn or two; two wrong answers end the call politely | Only for questions like "why?" |
| `DISCLOSE`    | Fixed text: the call is recorded, "you're speaking with an automated assistant", and the mini-Miranda (FDCPA 807(11)) | Never |
| `DISCUSS`     | The model talks about the debt, with its tools                                       | Yes |
| `THIRD_PARTY` | Someone else answered: asks only for a good time to reach the person, then says goodbye | Only for questions back |
| `WRAP`        | The call is ending                                                                   | Yes, to say goodbye |
| `STOPPED`     | A stop trigger fired (a later change): fixed close only                              | Never |

`next_stage(state, caller_text, debtor)` in `harness/stages.py` is the whole decision, as a plain function. It returns the new state and either a fixed line to speak or `None`, meaning "let the model answer". The state (`CallState` in `harness/call_state.py`) is saved as a `state` record in the call's session, next to its messages, so it survives a restart. When the model does answer, it gets `instructions/system.md` plus the stage's file from `instructions/stages/`, no tools until the disclosure was said, and every sentence of its reply goes through the sentence filter. Blocked sentences are dropped, written to the audit log as `utterance_blocked`, and if nothing is left the caller hears "Sorry, could you say that again?". The webhook finds who a call is for from the number it dialed; an unknown number gets a fixed goodbye.

The word matching is kept small and careful: a "yes" with a "no" or "not" in it is not a yes, and a date that can't be read doesn't move the call forward.

```bash
uv run demo part-13
```

The demo plays three calls (verified, wrong date of birth, someone else answers) and prints the stage next to each line. With the fake model, which always tries to say the balance, you can see the filter drop it before verification. This is not legal advice.

## The UI

A page in the browser with two areas: the debtor list on the left and the agent chat filling the rest. It is light by default; the moon button in the top bar switches to dark, and the choice is remembered.

![The UI: debtors on the left, the agent chat on the right](docs/ui-part1.png)
<!-- screenshot placeholder: add docs/ui-part1.png -->

- **Top bar:** "Check at" picks the time every check is run for, so you can see who would be blocked at, say, 9:30pm. "Now" goes back to the current time. "Overview" and "Compliance log" open the side panel.
- **Left, debtors:** every made-up debt with its name, balance, account and creditor, and a small status dot with "Ready to call" or "Can't call". Hover a "Can't call" to see the blocking rules and reasons. Tabs show All, Ready or Can't call, and the search box matches name, account, creditor or state.
- **Main, agent chat:** type a request such as `call Sam Doe`. Each tool the agent ran shows as a collapsed line you can open for its arguments and result. A call waits on an approval card: Approve only shows when a recent check passes, Deny always does, and the server runs every check again before it dials. Nothing dials from the browser. A payment or reversal waits on its own card that shows the debtor, amount, date (in the browser's time zone, which the chat sends) and the balance before and after; nothing is written until Approve.
- **Side panel:** opens from the right over the page. Click a debtor to see all the checks for them, passed and blocked. "Overview" shows how many debts are ready or blocked, the total owed, and how many debts each rule blocks. "Compliance log" shows the `audit_event` rows, newest first (rule, allow or block, reason, account and time), with a "Verified" or "Log was changed" mark and a "Can't call only" filter.

Nothing on the page is made up in the browser: every number and badge comes from the database or from the Python checks, so it changes when the data does.

Where the data comes from:

- Debtors, phones, debts and the audit rows are read straight from Supabase by the browser, with the anon key. The end of `supabase/seed.sql` lets `anon` read those four tables, and nothing else: no writes, and no other tables. It is in the seed rather than a migration on purpose: the seed only runs on a local `supabase db reset`, so `supabase db push` never gives a hosted project with real data this open read access. If your local database predates it, run `supabase db reset`.
- The badges and the chain check come from the Python code itself, through `harness/api.py` (`GET /api/precall?at=...` and `GET /api/audit/verify`), so the page never repeats the rules in JavaScript. Looking at the page doesn't write to the audit log.

To run it, start the database, the API and the web app, each in its own terminal:

```bash
npx supabase start
```

```bash
uv run api
```

```bash
cd web && npm install && npm run dev
```

Before the first run, copy `web/.env.example` to `web/.env.local` and paste in the anon key that `npx supabase status` prints. Then open http://localhost:5173. The dev server forwards `/api` to the API on port 8000. If Windows blocks `api.exe`, `uv run python -m harness.api` starts the same thing.

You need Node.js 18 or newer for `web/`. `npm run build` and `npm run lint` check it. The API's tests are in `tests/harness/test_api.py`; one checks that Morgan Patel, who asked us to stop, comes back blocked by `cease`.

```bash
uv run demo part-12
```

The demo prints what the API answers at 2pm New York time and at 9:30pm Los Angeles time, then the chain check.

## Subagents

A subagent is a fresh `Agent` with its own short prompt (`instructions/subagent.md`) and an empty history. It gets one task and only its final reply comes back. `run_subagent(task)` runs one; `fan_out([task, ...])` runs up to 8 at once (4 in parallel) and returns the replies in task order. `delegate_tool()` and `fan_out_tool()` let a model do the same.

- A subagent never gets `delegate` or `fan_out` itself, so it can't start more subagents.
- A subagent that crashes comes back as an `error: subagent failed: ...` string. The others keep going.
- More than 8 tasks is refused, not cut short, so no task is silently dropped.
- Live calls never get these tools: a subagent is a whole extra model conversation, too slow for someone waiting on the phone. `LIVE_CALL_EXCLUDED` names them and a test checks that the webhook's agent doesn't offer them.

The first use is after a call. `review_transcript(transcript, account)` in `harness/review.py` starts one reviewer per area, in parallel: was the debt collector disclosure given, were there threats or false statements, do the amounts match the account, and was the debt mentioned to anyone before they were verified. Each reviewer answers `OK: ...` or `FLAG: ...`, which becomes a `Finding(area, ok, note)`. An answer that is neither counts as a flag.

This is a second check only. The rules are enforced by code before and during the call; a reviewer can miss things or be wrong, so a finding is something for a person to look at, never a decision.

```bash
uv run demo part-15
```

The demo reviews a made-up call where the agent threatens arrest. With the fake model (no `LLM_BASE_URL`), the threats reviewer flags it and the other three pass.

## Stop triggers

Some things a caller says end the call by law, whatever the model thinks. So `detect(caller_text)` in `compliance/triggers.py` reads each caller line **before** the model sees it, and returns a list of `Trigger(kind, matched, flag, close, escalate)`: what kind it is, the words that matched, which flag to set, the fixed polite line to close with, and whether a person must follow up.

| Kind                  | Caller says, for example                 | Flag         | Escalate | Rule                     |
| --------------------- | ---------------------------------------- | ------------ | -------- | ------------------------ |
| `cease`               | "stop calling", "take me off your list"  | `cease`      | no       | FDCPA 805(c), TCPA revocation |
| `dispute`             | "not my debt", "I don't owe"             | none         | yes      | FDCPA 809                |
| `recording_objection` | "don't record", "I don't consent to recording" | none   | no       | all-party recording states |
| `wrong_number`        | "wrong number", "no one by that name"    | none         | no       | no third-party disclosure |

- **Eager on purpose.** A false alarm only ends the call politely; a miss can break the law. So "please don't call me at work" fires. Words match whole words, ignoring case.
- **Not too eager.** "I don't want to stop paying", "the bank is not my favourite" and "I have a lawyer" fire nothing. "Please stop calling me at work" fires `cease`.
- **Known false alarms, accepted:** "Don't contact my mother" and "I don't text much" fire `cease`, "I'm not disputing it" fires `dispute`, and "No, I'm not recording anything" fires `recording_objection`. Tests pin these down, so a change is a choice, not an accident.
- **Several at once** come back in a fixed order (stop calling, dispute, recording, wrong number), so the first one's close is the line to say. No close mentions the debt, so it is safe to say to the wrong person.
- **Flags:** `cease` sets the stricter `cease` flag, not `opt_out_phone`. A dispute sets no flag: it is the debt's `disputed` status, set later by `log_dispute`.
- `combine(regex_hits, classifier_hits)` merges these with a later small classifier's hits: a trigger fires if either finds it.

Nothing is wired into calls yet, and nothing is written to the database.

```bash
uv run demo part-14
```

The demo prints the triggers, flags and close line for sample caller lines, including the ones that must not fire. This is not legal advice.

## Skills

A skill is a short playbook for a hard moment on a call, kept as a Markdown file in `skills/<name>/SKILL.md`. Five ship with the repo: `hardship`, `angry-caller`, `confused-caller`, `payment-plan-talk` and `callback-request`.

- **Only the menu is always sent.** The agent adds a `## Skills` section to the system prompt with each skill's name and one-line description. When the conversation matches one, the model calls `load_skill` with its name and gets the full playbook back.
- **Read fresh every call**, like the system prompt, so editing or adding a skill takes effect on the next turn.
- **No skills, no tool.** If `skills/` is empty or missing, there is no menu and no `load_skill` tool. An unknown name comes back as an `error: ...` string.
- **Guidance only.** Each playbook starts with "Rules are enforced in code, not here." The legal must-stops, the sentence filter and the pre-call checks are code and run whether or not a skill is loaded. A test runs every line of every playbook through the sentence filter, so no pressure or threat language can slip into one.

The required steps of a call (greeting, verifying, the disclosure) are not skills; they come with the call stages.

```bash
uv run demo part-17
```

The demo prints the skills menu, then plays a caller who lost their job: the model loads the `hardship` playbook before answering.

## Account questions from the chat

The operator chat answers questions about accounts from the database, through read-only tools in `harness/operator_reads.py`:

- `list_debtors(sort, filter, limit)`: sort by balance, last payment, status or name; filter by `state:XX`, `status:...`, `ready` or `blocked` (ready and blocked come from the dialer's own dry run). At most 25 rows, whatever limit is asked for, and the answer says when it was cut short.
- `account_summary`, `call_history` (attempts, outcomes and the 7-in-7 count as the rule sees it), `portfolio_summary(period)` and `audit_for(debtor_id, limit)` (at most 50 rows). Phone numbers are masked to the last four digits everywhere, including inside audit evidence.
- `next_allowed_call(debtor_id)`: steps forward 15 minutes at a time through `compliance.check_before_call` with the facts the dialer loads, for up to 14 days, and confirms the time it finds with `dialer.service.plan_queue`. It gives the time in the operator's zone too (`next_allowed_local`), so the model doesn't convert it. A block that waiting can't clear (cease, consent, a disputed debt) gives no time at all. It only answers a question; the dialer still runs every check when a call is approved.

Three tools write:

- `add_note(debtor_id, text)` and `schedule_callback(debtor_id, when)` open an approval card like a call does. Nothing is written until Approve. A callback is refused, both when asked and on Approve, if the pre-call checks would block a call at that moment (calling hours, a cease, and the rest). Notes go in `debt_note`, which is append-only like `payment`. Each approved write has an audit row.
- `record_stop(debtor_id, kind)` (cease or dispute) writes at once with an audit row (`kind = stop`). It needs no approval because a stop can only mean fewer calls, but for that reason it takes only an exact account id, never a name: there is no card to catch a wrong match.

There is no chat tool to remove a stop flag, record consent, edit a balance or delete anything; a test checks the tool names, the code, and that running every tool leaves a stop in place.

```bash
uv run demo operator-reads
```

## Payments

The operator chat can record what a debtor paid and answer questions about it. "Testpay Ledger paid 20 yesterday" becomes a `record_payment` request, which writes nothing: code finds the debt (an account number, or a name with exactly one match), reads the amount and the day ("yesterday" is the operator's yesterday, in the time zone the browser sends, else `OPERATOR_TZ`, else New York), checks them, and opens an approval card. Only Approve writes the row.

- **Refused in code, before any card:** an amount of zero or less, more than the current balance, or that doesn't parse; a day in the future or before the debt's `opened_on`; an unknown debtor or a name that matches more than one debt.
- **Checked again on Approve,** inside one transaction with the debt row locked, against the balance as it is then. So two cards that each fit the balance can't both be approved if together they overpay: the second is refused. The payment row and its audit event (`kind = payment`) commit together; a refusal at that point writes no payment and is audited as a `block`.
- **The ledger is append-only.** `public.payment` refuses `update`, `delete` and `truncate`, with those privileges revoked, like `audit_event`. A correction is `reverse_payment(payment_id, reason)`, which also needs approval and adds a reversing row; a payment can be reversed once. The table's insert trigger checks the balance, the dates and the reversal again, so a write that skips the Python checks is refused too.
- **One balance everywhere.** `debt.balance_cents` stays the amount the debt was placed with. `public.current_balance_cents(debt)` = `balance_cents` - payments + reversals is the only place the current balance is computed. `find_debtor_by_phone` reads it on each call turn, so the call tools, `lookup_account` and the sentence filter's known amount all reflect a payment on the next turn, and the old amount is blocked as `amount_mismatch`.
- **Read-only questions:** `payment_history(debtor_id, since)` ("last 3 days" is today and the two days before, in the operator's time zone) gives the rows and their total. `on_time_report()` compares each debt's newest approved or confirmed `payment_plan` with its due dates: the down payment is due the day the plan was made, installment `k` is due `k` weeks, `2k` weeks or `k` months later; payments dated on or after the plan's start fill the installments in order; each one due so far is on time, late (covered only by a later payment) or missed (not covered yet), with no grace period. Debts without a plan are listed apart.

Debts that existed before the payments migration get the day it ran as `opened_on`, so a backdated payment on one of them is refused until a person sets the real date. The seed sets it for the made-up debts. On an existing local database run `supabase migration up` (never against a hosted project).

```bash
uv run demo part-31
```

## Bill upload

A bill (PNG, JPEG, WebP or PDF) can be read into a draft, and nothing is saved until a person confirms it. A wrong amount read off a bill would become a false statement on a call, so the model only reads.

- `POST /api/bills` (multipart, field `file`) checks the file type from its first bytes, stores the file (the private `bills` bucket when `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are set, else the folder in `BILLS_DIR`, default `.bills/`), has the vision model read the fields and returns a draft. Code turns the printed balance into cents and a US number into E.164, and flags every field that is missing, unsure or fails a check. Date of birth and line type are never on a bill, so they are always flagged. No debtor, phone or debt row is written, only the draft (`bill_draft`) and an audit row (`kind = bill`).
- `POST /api/bills/{id}/confirm` takes `{fields}` as the person edited them and checks them again, strictly: the balance must be a positive whole number of cents, the phone must already be E.164, and the ZIP, state, date of birth and line type must be valid (422 with each problem otherwise). It refuses (409) a draft already confirmed, an account number already saved, and a phone number already on file, since a call finds its debtor by the number. Then it saves the debtor, phone and debt with an audit row in one transaction.
- A bill is not consent to be called. The phone is saved with no consent, so `tcpa_consent` blocks every call to it until consent is recorded.
- The operator chat's `read_bill(draft_id)` shows a draft with the phone masked. It never saves.

The web UI has no upload entry point yet; the API is ready for one.

```bash
uv run demo bills
```

## Dialer: the one safe way every call is placed

`dial_one(conn, account_number, when, phone)` in `dialer/service.py` is the only code that asks AgentPhone to place a call. `dial_queue(debtors, when, phone, conn)` runs it for a list of debts in a fixed order (the one whose last call is oldest goes first), and a later `place_call` tool for an operator agent will call it for one debt. Each time, for that debt:

1. **Check and write it down, in one transaction.** Lock the debt's row, run every pre-call check and record each decision in the audit log. The debt must also be `active`: a disputed, paused or closed debt is blocked, even when you name it. If they all allow, add a `call_attempt` row (`placing`) and commit.
2. **Blocked:** nobody is dialed, and the result keeps the reasons.
3. **Dial once.** `make_call` is called one time. The AgentPhone call id goes on the attempt (`placed`).
4. **If AgentPhone says no** (429, 409 or 402), the attempt is marked `throttled`, `concurrency_limit`, `number_busy` or `no_funds`. **If we don't hear back** (a timeout, a 5xx), it is marked `placed_unknown`, because the phone may have rung. Either way we never try again by ourselves: AgentPhone has no idempotency key, so a retry could ring the person twice.

- **The decision to call is always code.** There is no model in the dialer, not even to order the queue.
- **Why the lock:** two runners working on the same debt would otherwise both count "6 calls this week" and both place the 8th. With the row lock, the second one waits, then counts 7 and is blocked.
- **Every attempt counts toward 7-in-7,** refused ones too. That can waste one, but it never under-counts a call that rang.
- **The queue stops early** when AgentPhone's answer means every call would fail (no funds, too many calls, our number busy, throttled, or no answer at all). Otherwise each remaining debt would lose one of its 7 attempts to a call that never rang. The result lists the debts it didn't try.
- **If saving the call id fails** after AgentPhone placed the call, the id is logged as an error and the run stops; the attempt stays `placing`, so it still counts.
- **Not done yet:** nothing marks a call as answered or as a conversation with the debtor afterwards. That needs the after-call step, so for now the "no calls for 7 days after a conversation" rule only sees conversations already in the ledger.
- `max_parallel` (default 1) works on several debts at once, each with its own database connection.
- **Recording:** there is no recording-consent decision yet, so the dialer leaves AgentPhone's default. A TODO in `dial_one` marks where `disableRecording` goes.
- `AgentPhoneClient` uses the `agentphone` SDK (a project dependency, installed by `uv sync`) and `AGENTPHONE_API_KEY`, `AGENTPHONE_AGENT_ID` and `AGENTPHONE_FROM_NUMBER_ID` (see `.env.example`). It sends no system prompt, so calls run in webhook mode, and it turns the SDK's own retries off.

```bash
uv run dialer --dry-run
```

The dry run prints who would be called and why each of the others is blocked, and writes nothing. Without `--dry-run`, the command refuses to start unless `AGENTPHONE_API_KEY` is set **and** `--confirm` is passed. `--when` works only with `--dry-run`: a real call rings now, so it is checked and logged at now. `uv run demo part-18` is the dry run on the seed data at a fixed time. This is not legal advice.

## Commands

| Command                   | Needs a model? | What it does                                                    |
| ------------------------- | -------------- | --------------------------------------------------------------- |
| `uv run verify`           | No             | Format check, lint, mypy, pytest and an import check            |
| `uv run pytest -q`        | No             | Only the tests                                                  |
| `uv run accept part-01`   | Yes            | Asks the real model two simple questions and checks the answers |
| `uv run demo part-01`     | Yes            | Asks the model for a short, polite greeting to "Sam Doe"        |
| `uv run accept part-02`   | Yes            | Checks that the agent remembers a name from the turn before     |
| `uv run demo part-02`     | Yes            | A two-turn chat where the model remembers "Sam Doe"             |
| `uv run accept part-03`   | Yes            | Checks that the model follows a system prompt from a file       |
| `uv run demo part-03`     | Yes            | One turn with the system prompt, then shows what was sent       |
| `uv run accept part-04`   | Yes            | Checks that the model looks up Sam Doe's balance with the tool  |
| `uv run demo part-04`     | Yes            | Sam Doe asks what they owe; shows the tool call and the answer  |
| `uv run accept part-05`   | Yes            | Runs the earlier checks, then the calling-hours gate on examples |
| `uv run demo part-05`     | No             | Prints allow/block for the example debtors at several times     |
| `uv run accept part-06`   | Yes            | Runs the earlier checks, then looks up Sam Doe in the database  |
| `uv run demo part-06`     | No (needs DB)  | Prints the debt table and two lookups, one for an unknown name  |
| `uv run accept part-07`   | Yes            | Runs the earlier checks, then the pre-call checks on every seeded debtor |
| `uv run demo part-07`     | No (needs DB)  | Prints every pre-call decision for each made-up debtor          |
| `uv run accept part-08`   | Yes            | Runs the earlier checks, then records every debtor's decisions and checks the chain |
| `uv run demo part-08`     | No (needs DB)  | Records the pre-call decisions, prints the audit rows and the chain check |
| `uv run audit-verify`     | No (needs DB)  | Recomputes every audit hash; exits 1 if any row was changed or removed |
| `uv run accept part-09`   | Yes            | Runs the earlier checks, then plays Sam Doe's call through the webhook and checks the balance |
| `uv run demo part-09`     | No             | Plays every caller script through the webhook (real model if set, else the fake) |
| `uv run accept part-10`   | Yes            | Runs the earlier checks, then checks the agent remembers a name from an earlier turn, and that another call doesn't |
| `uv run demo part-10`     | No             | Plays a call through the webhook; each reply shows how much of the call the model saw |
| `uv run accept part-11`   | Yes            | Runs the earlier checks, then the sentence filter on sample sentences |
| `uv run demo part-11`     | No             | Prints allow/block for sample sentences at each stage of a call |
| `uv run latency-report --since 1h` | Local DB | Shows median and p95 time to the first substantive voice reply, grouped by fixed and model turns. The hold line is counted separately. |
| `uv run serve`            | Yes            | Runs the webhook on `127.0.0.1:8000` for AgentPhone to call       |
| `uv run accept part-12`   | Yes            | Runs the earlier checks, then the UI API's tests against the database |
| `uv run demo part-12`     | No (needs DB)  | Prints the API's pre-call answers at two times, and the chain check |
| `uv run api`              | No (needs DB)  | Starts the read-only API for the UI on http://127.0.0.1:8000    |
| `uv run accept part-14`   | Yes            | Runs the earlier checks, then the stop triggers on sample caller lines |
| `uv run demo part-14`     | No             | Prints the stop triggers, flags and close line for sample caller lines |
| `uv run accept part-15`   | Yes            | Runs the earlier checks, then has reviewer subagents flag the threat in a sample call |
| `uv run demo part-15`     | No             | Reviews a sample call with four reviewer subagents in parallel (real model if set, else the fake) |
| `uv run accept part-16`   | Yes            | Runs the earlier checks, then a long call that must compact and still remember a detail from the summarized middle |
| `uv run demo part-16`     | No             | Plays a long call with a small limit and shows each compaction (real model if set, else the fake) |
| `uv run accept part-17`   | Yes            | Runs the earlier checks, then checks the model loads the hardship skill for a caller who lost their job |
| `uv run demo part-17`     | No             | Prints the skills menu and a call where the model loads a playbook (real model if set, else the fake) |
| `uv run accept part-18`   | Yes            | Runs the earlier checks, then a dialer dry run over every seeded debtor |
| `uv run demo part-18`     | No (needs DB)  | A dialer dry run on the seed: who would be called, and why the rest are blocked |
| `uv run reconcile --dry-run` | No (needs DB and AgentPhone settings) | Attempts older than 30 minutes with no end recorded, and what would be recorded for each; writes nothing |
| `uv run dialer --dry-run` | No (needs DB) | Who would be called now, and why the others are blocked; writes nothing |
| `uv run accept part-13`   | Yes (needs DB) | Runs the earlier checks, then plays three calls and checks the disclosure comes before any amount, and nothing leaks otherwise |
| `uv run demo part-13`     | No             | Plays three calls through the call stages and prints the stage next to each line |
| `npm run dev` (in `web/`) | No (needs DB and API) | Starts the UI on http://localhost:5173                   |

## Credits

The `model/` and `harness/` code is adapted from codingHarness, keeping only what each step needs.
