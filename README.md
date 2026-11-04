# DebtAgentCall

A debt-collection voice agent that puts compliance first, built one small step at a time.

Right now the project can send messages to a language model and get a reply back, and it has an agent that remembers the conversation, follows a system prompt kept in a file, and can look up an account with a tool. The made-up debtors live in a local Postgres database run by Supabase. The pre-call compliance rules (calling hours, the 7-in-7 call limit, consent, stop requests, do-not-call, lawyers, bankruptcy and death) are here as plain Python checks; they are not wired into the agent yet. The call flow and the other rules come later, each in its own step.

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
  db.py                 connect(), find_account(name) and the pre-call loaders, which read Postgres
compliance/
  decision.py           Decision (allow, rule_id, reason, evidence), the answer every rule gives
  timezones.py          zones_for(zip, phone), the time zones a debtor might be in
  hours.py              Contact and check_calling_hours(contact, when)
  attempts.py           Attempt and check_call_frequency, the 7-calls-in-7-days limit
  consent.py            PhoneConsent and check_consent
  flags.py              Flag and the checks for stop requests, do-not-call, lawyers, bankruptcy, death
  precall.py            CallFacts, check_before_call(facts, when) and may_call(decisions)
instructions/
  system.md             the system prompt the agent sends first on every call
supabase/
  config.toml           local Supabase settings (database on port 54322)
  migrations/           the debtor, phone, debt, call_attempt and flags tables
  seed.sql              made-up debtors, with call history and flags for each pre-call check
tasks/
  verify.py             offline checks: format, lint, types, tests, import
  accept.py             live check against a real model
  demo.py               a short live demo
  checks.py             the live checks and demos the two scripts above run
tests/
  episodes/             offline tests that use the fake provider
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

A bad tool request never crashes the loop. An unknown tool, broken JSON, a missing or extra argument, or an error inside the tool all come back to the model as an `error: ...` message, so it can try again. Every tool request gets a result.

If the model keeps asking for tools, `run()` stops after `max_steps` model calls (10 by default) and returns `stopped: used all N steps without a final answer`. `send()` still makes exactly one call and runs no tools.

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
- `debt`: the creditor, the balance in cents, and a status (`active`, `disputed`, `paused` or `closed`)

The app connects with `DATABASE_URL` (see `.env.example`). It defaults to the local database, `postgresql://postgres:postgres@127.0.0.1:54322/postgres`. Row level security is on for every table, with only a read rule for signed-in Supabase users. The app's own direct connection is not limited by it.

`harness/db.py` has `find_account(name)`, which `lookup_account` uses. It matches the full name, ignoring case and extra spaces. If the database is down, the tool returns an `error: lookup_account failed: ...` message instead of crashing.

The tests in `tests/episodes/test_part06.py` that need the database are marked `db`. They run against the real local database. When it isn't running, they are skipped with "run `supabase start`", so `uv run verify` still works offline.

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
| `attorney`       | a lawyer represents the debtor on this debt                                           |
| `bankruptcy`     | the debtor filed for bankruptcy                                                       |
| `deceased`       | the debtor has died                                                                   |

Missing data blocks too: no phone record, no call history or no flags loaded each turn into a blocked decision.

```python
from datetime import datetime, timezone
from compliance import check_before_call, may_call
from harness.db import load_call_facts

decisions = check_before_call(load_call_facts("ACC-1004"), datetime.now(timezone.utc))
print(may_call(decisions), [d.reason for d in decisions if not d.allow])
```

The facts come from two new tables: `call_attempt` (one row per call placed) and `flags` (stop requests, lawyers, bankruptcy and the rest). The seed adds one made-up debtor for each way a call can be blocked. `harness/db.py` only loads; the checks in `compliance/` do all the deciding, so they can be tested without a database.

```bash
uv run demo part-07
```

The demo checks every made-up debtor at a fixed time (2026-11-05, 14:00 in New York), so the seeded call history always gives the same answers. This is not legal advice.

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

## Credits

The `model/` and `harness/` code is adapted from codingHarness, keeping only what each step needs.
