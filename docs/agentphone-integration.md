# Building on AgentPhone's webhook mode

Practical notes for anyone building a voice agent on [AgentPhone](https://docs.agentphone.ai) in webhook mode, drawn from what this project does and from AgentPhone's documentation. Nothing here is legal advice.

## How to read this page

Each statement is one of three kinds:

- **Documented** statements cite an AgentPhone documentation page. The pages were read on 2026-11-20 at `docs.agentphone.ai` (the index, `llms.txt`, lists them under `docs.agentphone.to`, which redirects to `.ai`). The OpenAPI file was only partly read, so the request schemas are taken from the guide and reference pages instead.
- **Project behavior** statements describe code in this repository, with the file to check.
- **Not confirmed** statements are things the project could not settle from the pages read or from testing. They are collected in [Not documented or not confirmed](#not-documented-or-not-confirmed). That a page does not say something is not evidence that the behavior does not exist.

An important limit: this project has been exercised against a signed fake caller (`sim/agentphone_sim.py`) and a fake phone client, not against recorded real calls. Where a section says "handled in this project", it means the code and tests cover it with synthetic input. It does not mean the behavior was measured on live calls.

Pages cited:

| Short name | Page |
| --- | --- |
| Webhooks | `docs.agentphone.ai/documentation/guides/webhooks` |
| Calls | `docs.agentphone.ai/documentation/guides/calls` |
| Agents | `docs.agentphone.ai/documentation/guides/agents` |
| Numbers | `docs.agentphone.ai/documentation/guides/phone-numbers` |
| Auth | `docs.agentphone.ai/authentication` |
| Python SDK | `docs.agentphone.ai/documentation/sdks/python` |
| Errors | `docs.agentphone.ai/documentation/reference/error-handling` |
| Create call | `docs.agentphone.ai/api-reference/calls/create-outbound-call-v-1-calls-post` |
| Best practices | `docs.agentphone.ai/documentation/reference/best-practices` |

## Setup that works

The sequence is: an API key, an agent in webhook mode, a number attached to it, a webhook URL for that agent, and a public address that reaches your server.

### 1. API key

Documented (Auth): create a key in the account settings and send it as `Authorization: Bearer <key>` to `https://api.agentphone.ai`. A webhook signing secret is a separate credential, returned when a webhook is saved.

The project reads the key from `AGENTPHONE_API_KEY`.

### 2. Agent in webhook mode

Documented (Agents): `POST /v1/agents` takes a required `name` and an optional `voiceMode`, which is `webhook` (the default) or `hosted`. In webhook mode every reply comes from your server. The guide's worked example uses `hosted`; a webhook-mode request is the same call with `voiceMode` set to `webhook` or left out. Illustrative, with a placeholder key (not run):

```bash
curl -s -X POST https://api.agentphone.ai/v1/agents \
  -H "Authorization: Bearer $AGENTPHONE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "collections-demo", "voiceMode": "webhook"}'
```

The response includes the agent `id`. The project reads it from `AGENTPHONE_AGENT_ID`. If the agent should be able to hand a call to a person, the agent also needs `transferNumber` (Calls, Agents).

### 3. Buy a number and attach it

Documented (Numbers, Agents): search with `GET /v1/numbers/available?areaCode=...`, buy with `POST /v1/numbers` (optionally with `agentId` to attach at once), or attach later with `POST /v1/agents/{agent_id}/numbers` and body `{"numberId": "..."}`. Illustrative (not run):

```bash
curl -s -X POST https://api.agentphone.ai/v1/numbers \
  -H "Authorization: Bearer $AGENTPHONE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"country": "US", "areaCode": "415", "agentId": "AGENT_ID"}'
```

The number's `id` is what the project reads from `AGENTPHONE_FROM_NUMBER_ID`. Buying a number can return 402 if the account has no funding (Numbers). A buying call spends money; check the account before running it.

### 4. Register the webhook URL for the agent

Documented (Webhooks): `POST /v1/agents/{agent_id}/webhook` with `url` (HTTPS), and optionally `contextLimit` (0 to 50) and `timeout` (5 to 120 seconds, default 30, applies to voice). The response carries the signing `secret`. If an agent has its own webhook, its events go only there; otherwise they go to the project-level webhook set with `POST /v1/webhooks`, never to both. Illustrative (not run):

```bash
curl -s -X POST https://api.agentphone.ai/v1/agents/AGENT_ID/webhook \
  -H "Authorization: Bearer $AGENTPHONE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://YOUR-TUNNEL-HOST/webhooks/agentphone", "timeout": 30}'
```

Copy `secret` from that response into `AGENTPHONE_WEBHOOK_SECRET` straight away. Whether the secret can be read again later is not confirmed, and the Webhooks guide says it can change on each create or update.

Project behavior: the route is `/webhooks/agentphone` (`WEBHOOK_PATH` in `phone/webhook.py`). The project does not register the webhook for you; this step is manual.

### 5. Environment variables the project reads

| Variable | Used by | Meaning |
| --- | --- | --- |
| `AGENTPHONE_WEBHOOK_SECRET` | `phone/webhook.py`, `tasks/serve.py` | Signing secret. Unset means every webhook gets a 401. |
| `AGENTPHONE_API_KEY` | `phone/client.py`, `dialer/cli.py` | Needed to place real calls. |
| `AGENTPHONE_AGENT_ID` | `phone/client.py` | The agent that places calls. |
| `AGENTPHONE_FROM_NUMBER_ID` | `phone/client.py` | The number calls come from. |
| `SUPERVISOR_NUMBER` | `phone/webhook.py` | When set, `escalate_to_human` also replies with a transfer. |
| `DATABASE_URL` | `harness/db.py` | Postgres. Defaults to the local Supabase database on port 54322. |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | `model/provider.py` | Any OpenAI-compatible chat endpoint. |

`.env.example` lists them. Real environment variables win over `.env`.

### 6. Run the server and a tunnel

Run the server (verified: starts and answers on the chosen port):

```bash
uv run serve --port 8000
```

It listens on `127.0.0.1` by default. AgentPhone needs an HTTPS address that reaches it, so a development tunnel is required. The project does not ship one. Either of these is illustrative (not run, and they are third-party tools):

```bash
cloudflared tunnel --url http://127.0.0.1:8000
ngrok http 8000
```

Save the tunnel's HTTPS address plus `/webhooks/agentphone` as the agent's webhook URL (step 4).

### 7. Check the server without AgentPhone

The simulator sends correctly signed requests, so the whole path can be exercised with no account. With the server running and the same secret in both terminals:

```bash
uv run python -m sim.agentphone_sim sam_balance --url http://127.0.0.1:8000
```

A signed request can also be sent by hand. This was run against a local server with a placeholder secret (`test-secret-not-real`) and an event the server ignores, so no database was touched. The signature comes from the project's own `sign()`:

```bash
BODY='{"event":"number.released","data":{}}'
TS=$(date +%s)
SIG=$(uv run python -c "import sys; from phone.signing import sign; print(sign(sys.argv[1].encode(), sys.argv[2], 'test-secret-not-real'))" "$BODY" "$TS")
curl -s -X POST http://127.0.0.1:8000/webhooks/agentphone \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: $SIG" -H "X-Webhook-Timestamp: $TS" \
  -H "X-Webhook-ID: demo-1" -d "$BODY"
```

The answer is `{}` with HTTP 200. Changing a byte of the body, or the secret, gives 401 with `{"error":"signature does not match"}`.

## The webhook contract as this project relies on it

### Events used

Documented (Webhooks): `agent.message` carries a caller turn; its `channel` is `voice` for a call, and `data` has `transcript`, `callId`, `from`, `to` and `direction`. `agent.call_ended` fires when a call ends and carries the whole transcript, `durationSeconds`, `startedAt`, `endedAt` and `disconnectionReason`. Other events exist (`agent.reaction`, `agent.contact_shared`, `number.released`).

Project behavior (`phone/webhook.py`, `_handle`): handles voice `agent.message` and `agent.call_ended`, and answers `{}` to anything else. `agent.call_ended` is parsed by `parse_ended` in `phone/call_end.py`.

### Headers and signature

Documented (Webhooks): `X-Webhook-Signature` is `sha256=<hex>`, an HMAC-SHA256 with the signing secret over the string `{timestamp}.{raw body}`. `X-Webhook-Timestamp` is Unix seconds, `X-Webhook-ID` is unique per delivery and the same across retries, and `X-Webhook-Event` names the event. The documented tolerance is 300 seconds.

Project behavior: `verify` in [`phone/signing.py`](../phone/signing.py) checks the raw body bytes before any parsing, refuses a missing secret, a missing or non-numeric timestamp, a timestamp more than 300 seconds away in either direction, and a mismatch (compared with `hmac.compare_digest`). The webhook returns 401 and never reaches the model. Tests: `tests/phone/test_webhook.py` (`test_bad_signature_is_401_and_the_model_is_not_called`, `test_body_changed_after_signing_is_401`, `test_timestamp_older_than_five_minutes_is_401`, `test_no_secret_configured_refuses_everything`).

The AgentPhone Python SDK also offers `verify_webhook` (agentphone 0.17.0 or later, Python SDK page). The project does not use it, so the check stays short and easy to test.

### Duplicates

Documented: `X-Webhook-ID` stays the same across retries. For a live voice turn there is no delayed retry queue; delayed retries are described for non-voice `agent.message` only, and `agent.call_ended` is attempted once (Webhooks).

Project behavior: the id and the reply that was sent are kept in memory (`_SeenIds`, 10,000 entries) and, on a staged call, saved with the call state under `REPLAY_KEY`. A repeat gets the saved reply without calling the model. Test: `test_duplicate_webhook_id_calls_the_model_once`, and `test_a_retry_gets_the_whole_reply_without_the_model` in `tests/phone/test_webhook_streaming.py`.

### Reply formats

Documented (Webhooks): a voice reply is a JSON object. Fields include `text`, `hangup` (end the call after speaking), `action` (`transfer` is a cold transfer and needs `transferNumber` on the agent, or `hangup`), `digits`, and `send_message`. A streamed reply uses `Content-Type: application/x-ndjson` with one object per line; lines with `"interim": true` keep the turn open and the last line, without `interim`, closes it. A non-object body is ignored and the caller hears silence.

Project behavior (`phone/replies.py`): `say`, `say_and_hangup`, `transfer`, `interim` and `ndjson_line` build these. `SentenceBuffer` cuts the model's output into whole sentences so each can be checked before it is sent. Only `text`, `hangup`, `action: transfer` and `interim` are used; the project does not use `digits` or `send_message`. The final NDJSON line carries `hangup` or `action` when the turn ends the call. The pages read show a final line with `text` only, so a final line carrying `hangup` or `action` follows the documented field list but is not shown as an example there. This is on the [not confirmed](#not-documented-or-not-confirmed) list.

### Timeout and the early fixed line

Documented (Webhooks): the voice timeout is 30 seconds by default and can be set from 5 to 120 seconds per webhook; on a timeout the turn is cancelled and the caller hears silence. The Best practices page advises keeping response time under 5 seconds.

Project behavior (`phone/webhook.py`): if no checked sentence is ready within `HOLD_AFTER` (2.5 seconds), the stream opens with the fixed line `HOLD_LINE` ("One moment while I check that.") and the turn carries on. A turn that finishes before then is one plain JSON reply, so a fast reply never says the hold line. Tests: `test_a_slow_model_gets_the_hold_line_first`, `test_a_quick_model_never_hears_the_hold_line`.

The project leaves the webhook `timeout` at its default; nothing in the code sets it.

## Dialing

### One dial, never retried

Documented (Errors): the API does not support customer-supplied idempotency keys, and a timeout or 5xx on `POST /v1/calls` may mean the call was placed, so retrying is advised against. The Python SDK's own retry policy is not stated in the pages read.

Project behavior:

- [`dialer/service.py`](../dialer/service.py) `dial_one` first locks the debt row, runs the pre-call checks and inserts a `call_attempt` row with outcome `placing`, and commits. Only then does it call `make_call`, exactly once. Two runners for the same debt cannot both pass a limit on stale facts.
- [`phone/client.py`](../phone/client.py) `AgentPhoneClient` creates the SDK with `max_retries=0` and never passes a `system_prompt`, so the call stays in webhook mode. It sends `disableRecording` and `ringTimeoutSeconds` through `extra_body` only when set, because the Python SDK page does not show those parameters. The dialer currently passes neither, so AgentPhone's defaults apply (a `TODO(recording consent)` in `dial_one` marks the spot).
- A refusal marks the attempt with that outcome. Anything else (a timeout, a dropped connection, a 5xx) becomes `placed_unknown`, and the project never tries again by itself.

### Error handling

| AgentPhone answer | Documented meaning (Calls, Errors) | `CallError.kind` | `call_attempt.outcome` |
| --- | --- | --- | --- |
| 402 | Insufficient funding | `no_funds` | `no_funds` |
| 409 | Number still activating (with `Retry-After`), or that number already has an active call | `busy` | `number_busy` |
| 429 with `Retry-After` | Endpoint throttle | `throttled` | `throttled` |
| 429 without `Retry-After` | Account concurrency cap (the cap value is not stated) | `concurrency` | `concurrency_limit` |
| other 4xx | Validation or similar | `other` | `failed` |
| 5xx, timeout, network | Unknown whether a call exists | not a `CallError` | `placed_unknown` |

`call_error_for` in `phone/client.py` does this mapping; `REFUSED_OUTCOME` in `dialer/service.py` maps kinds to outcomes. Tests: `tests/phone/test_client_errors.py`.

A limit in the project's reading: the SDK's `AgentPhoneError` keeps the status but no response headers, so a throttling 429 and a concurrency 429 can both read as `concurrency` through the SDK (noted in `_status_and_headers`). Both stop the queue, so the effect is the same.

`dial_queue` stops early on outcomes that affect every call (`STOP_OUTCOMES`: throttled, concurrency limit, number busy, no funds, placed unknown), so the remaining debts do not each lose an attempt from their call limit to a call that never rang.

### Calls that never report an end

Documented: `agent.call_ended` is attempted once with no automatic retry (Webhooks).

Project behavior: `record_call_end` in [`phone/call_end.py`](../phone/call_end.py) records the outcome from the event. `uv run reconcile` ([`dialer/reconcile.py`](../dialer/reconcile.py)) finds attempts placed more than 30 minutes ago with an AgentPhone call id and no end, reads each back with `GET /v1/calls/{id}` (a read, so it may be retried) and applies the same update. It leaves an attempt alone if the lookup fails or the call has not ended. Nothing runs it by itself; schedule it. Tests: `tests/phone/test_call_ended.py`, `tests/dialer/test_reconcile.py`.

## Handled in this project

These are choices the code makes because of how speech and the webhook behave. They are covered by tests on synthetic text. None comes from a recorded real call, so none should be read as a measurement of AgentPhone's accuracy.

**Spoken numbers.** The caller's words reach the webhook as text in `data.transcript`. Speech-to-text can write a date or ZIP code as digits or as words, so [`harness/spoken.py`](../harness/spoken.py) reads both: "zero two one one five" and "02115" are the same ZIP, and "the fifteenth of January nineteen ninety" is a date. An answer that can be read two ways gives no answer, never a guess. Tests: `tests/harness/test_spoken_numbers.py`, `tests/harness/test_spoken_verify.py`. Example text in those tests is made up.

**One sentence as several turns.** The project assumes a caller's sentence may arrive split across turns, such as a cut-off "Yeah. This is" followed by the rest. A turn with nothing usable in it gets a fixed re-ask and no model call (`GREETING_REASK`, `VERIFY_REASK`, `REASK_DOB`, `REASK_ZIP` in [`harness/stages.py`](../harness/stages.py)), and it does not count as a failed verification attempt. Tests: `test_fragment_in_greeting_gets_a_fixed_reask`, `test_fragment_in_verify_gets_a_fixed_reask_and_is_no_attempt` in `tests/harness/test_spoken_verify.py`.

**No opening line.** Documented (Calls): `initialGreeting` on `POST /v1/calls` is spoken when the recipient answers if provided, and an empty string makes the agent wait silently for the recipient to speak first. What happens when the field is omitted is not stated in the pages read, and neither is whether the agent's stored `beginMessage` is then used. The project never sends `initialGreeting`, so its first words are the fixed greeting line, spoken as the reply to the first `agent.message` turn (the recipient's "Hello?" or similar). If the recipient says nothing, the project has nothing to reply to. Whether AgentPhone sends a turn for silence is not confirmed.

## Not documented or not confirmed

Each item was not stated in the pages read on 2026-11-20 or could not be confirmed by testing. None is a claim that the behavior does not exist.

| Topic | Status |
| --- | --- |
| Voicemail or answering-machine detection | Not documented in the pages read on 2026-11-20. An agent field `voicemailMessage` is documented, but how a machine is detected is not stated. The project sets the `voicemail` outcome only from words in the disconnection reason after the call (`phone/call_end.py`). |
| Outbound concurrency cap | The 429 message format is documented; the number is not stated. Not confirmed. |
| Warm transfer | Not documented in the pages read on 2026-11-20. Only a cold transfer (`action: transfer`) is documented. What the caller hears and which number is dialed on the project's escalation path is not confirmed. |
| Toggling recording during a call | Not documented in the pages read on 2026-11-20. The documented per-call control is `disableRecording` at call creation; recording is also described as an account add-on. |
| Reading the webhook secret again | Not documented in the pages read on 2026-11-20. The guide says the secret can change on each create or update, without saying when. |
| A call-started event | Not documented in the pages read on 2026-11-20. The only call-level event named is `agent.call_ended`. |
| Full list of `disconnectionReason` values | Only `agent_hangup` appears as an example. The project matches words in the reason and never guesses from an unmatched one. |
| Final NDJSON line with `hangup` or `action` | The field list allows them on a reply; the examples read show a text-only final line. Not confirmed on a live call. |
| Omitted `initialGreeting` | Not documented in the pages read on 2026-11-20 (see above). |
| Whether the Python SDK accepts `disable_recording` and `ring_timeout_seconds` | Not documented in the Python SDK page. The project passes the REST names through `extra_body`; this path is tested with a fake, not against the live API. |
| Python SDK retry behavior | Not documented in the pages read on 2026-11-20. The project turns retries off with `max_retries=0`. |
| Pages not read | Quickstart, Troubleshooting, Usage and Billing, Sub-accounts and most API-reference pages. Some of the items above may be answered there. |

## Pitfalls

- **A stale tunnel address.** A development tunnel usually gets a new address on each start. The webhook URL saved for the agent then points nowhere. Re-save it (step 4). Per the Webhooks guide, saving can change the secret, so update `AGENTPHONE_WEBHOOK_SECRET` and restart the server too, or every webhook gets a 401 (`phone/signing.py` reports `signature does not match`).
- **The secret is not the API key.** They are separate credentials (Auth). Putting the API key in `AGENTPHONE_WEBHOOK_SECRET` produces 401s.
- **Per-agent versus project webhook.** If the agent has its own webhook, the project-level one never receives its events (Webhooks). Check which one you saved.
- **Running against the wrong database.** `DATABASE_URL` defaults to the local Supabase database (`harness/db.py`, `.env.example`). The webhook reads debtors, flags and call state from it and writes audit rows to it. A server pointed at a database that was reset or has other data will not find the dialed number, and an unknown number gets a fixed goodbye with no debt facts (`phone/webhook.py`). With no database reachable, `harness/db.py` reports `no database at <host>:<port>` and suggests `supabase start`.
- **Windows quoting for curl.** In Windows PowerShell 5.1, `curl` is an alias for `Invoke-WebRequest`, so call `curl.exe`. Passing inline JSON with `-d '{"a":1}'` also loses the double quotes, so the body no longer matches what was signed and the server answers 401 `signature does not match`. This was reproduced against a local server. Write the body to a file and send it byte for byte with `--data-binary "@body.json"`, signing that same file.
- **Signing the wrong bytes.** The signature covers the raw body. Parsing and re-serializing the JSON, or adding a trailing newline, breaks it (`test_body_changed_after_signing_is_401`).
- **Retrying a dial.** A timeout on `POST /v1/calls` does not mean no call was placed. Record the attempt first and do not retry (see Dialing).
- **Real calls cost money and ring real phones.** `uv run dialer --confirm` places them. Use `uv run dialer --dry-run` first. Use only numbers you own and have consented to call, and see [compliance.md](compliance.md) for what the code does and does not check.
