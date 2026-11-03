# DebtAgentCall

A debt-collection voice agent that puts compliance first, built one small step at a time.

Right now the project can send messages to a language model and get a reply back, and it has an agent that remembers the conversation, follows a system prompt kept in a file, and can look up an account with a tool. The call flow and the compliance rules come later, each in its own step.

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

## Credits

The `model/` and `harness/` code is adapted from codingHarness, keeping only what each step needs.
