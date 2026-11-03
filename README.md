# DebtAgentCall

A debt-collection voice agent that puts compliance first, built one small step at a time.

Right now the project does one thing: it sends messages to a language model and gets a reply back. The agent, the call flow and the compliance rules come later, each in its own step.

## What is here

```
model/
  client.py             chat(messages, ...) -> LLMResponse, the one function every model call goes through
  provider.py           Provider (base URL, model, API key) and LLMResponse
  openai_compatible.py  the HTTP call to an OpenAI-style /chat/completions endpoint
  fake.py               a fake provider, so tests run offline and give the same answer every time
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

## Commands

| Command                   | Needs a model? | What it does                                                    |
| ------------------------- | -------------- | --------------------------------------------------------------- |
| `uv run verify`           | No             | Format check, lint, mypy, pytest and an import check            |
| `uv run pytest -q`        | No             | Only the tests                                                  |
| `uv run accept part-01`   | Yes            | Asks the real model two simple questions and checks the answers |
| `uv run demo part-01`     | Yes            | Asks the model for a short, polite greeting to "Sam Doe"        |

## Credits

The `model/` code is adapted from codingHarness, keeping only what a single model call needs.
