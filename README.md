# DebtAgentCall

A debt-collection voice agent that puts compliance first, built one small step at a time.

Right now the project can send messages to a language model and get a reply back, and it has an agent that remembers the conversation, follows a system prompt kept in a file, and can look up an account with a tool. The first compliance rule, calling hours, is here as a plain Python check; it is not wired into the agent yet. The call flow and the other rules come later, each in its own step.

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
compliance/
  decision.py           Decision (allow, rule_id, reason, evidence), the answer every rule gives
  timezones.py          zones_for(zip, phone), the time zones a debtor might be in
  hours.py              Contact and check_calling_hours(contact, when)
instructions/
  system.md             the system prompt the agent sends first on every call
data/
  accounts.json         a few made-up accounts that lookup_account reads
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
print(agent.run("I'm Sam Doe. How much do I owe?"))  # the balance comes from data/accounts.json
```

There is one tool so far, `lookup_account(name)`. It reads `data/accounts.json`, a small file of made-up accounts, and returns the matching one, or says that none was found.

A bad tool request never crashes the loop. An unknown tool, broken JSON, a missing or extra argument, or an error inside the tool all come back to the model as an `error: ...` message, so it can try again. Every tool request gets a result.

If the model keeps asking for tools, `run()` stops after `max_steps` model calls (10 by default) and returns `stopped: used all N steps without a final answer`. `send()` still makes exactly one call and runs no tools.

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

## Credits

The `model/` and `harness/` code is adapted from codingHarness, keeping only what each step needs.
