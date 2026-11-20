# Latency

Voice quality depends on how soon the caller hears the first words of a reply. This page says what the project measures about that, what it does not, and how to get real numbers. Every figure below has its source next to it. There are no real-call numbers here yet.

## What is measured

For every caller turn on a live call (`agent.message`, voice channel), the webhook times its own work with `time.perf_counter()` (a monotonic clock) and saves one row in `public.turn_timing` after the reply has been handed to the server. The clock starts when our handler receives the request.

| Column | Meaning |
| --- | --- |
| `first_line_ms` | Request received to the first line handed to AgentPhone: the first `interim` chunk of a streamed reply, or the single JSON reply. If the hold line went first, this is the time to the hold line. |
| `total_ms` | Request received to the last line handed over (the end of the turn on our side). |
| `verified_ms` | Request received to signature and timestamp checked. |
| `triage_ms` | Time in the stop triggers plus the stage step. |
| `model_ms`, `model_calls` | Time waiting on the model (summed over the turn) and how many model calls the turn made. Time the server spends between two pieces of the model's stream is not counted. |
| `model_first_ms` | How long the first model call took to give its first output. This is the model's time to first token as seen from our process. |
| `tool_ms`, `tool_calls`, `tools` | Time inside tools, in total and by tool name, with the call count for each. |
| `hold_line` | The early fixed hold line was used because nothing safe was ready within `HOLD_AFTER` (2.5 seconds). |
| `kind` | `fixed` (no model call), `model`, `stop` (a stop trigger closed the call), `blocked` (a sentence was blocked, so a fixed line was used), `failure` (a tool or save failed: fixed line and hang up), `replay` (a retry answered from the saved reply). |
| `stage`, `turn_seq`, `call_id` | Where the call was, which caller turn it was, and AgentPhone's call id. |

Only numbers, flags and ids are stored. No transcript, phone number, name or model output is ever written to this table. A test feeds a distinctive caller sentence and model sentence through a turn and checks that neither appears in any stored column.

The write runs as a background task of the response, after the last byte has gone to the server, so it cannot delay a reply. If the write fails, the error is logged and the call is unaffected. No query is added to the call path. Database time is not counted separately; it shows up inside `total_ms` and, for a streamed turn, inside the time to first line.

Timing is off unless the app is given a writer. `uv run serve` turns it on (it writes to the database). `create_app()` on its own records nothing, so the older tests run as before.

## What is not measured

- AgentPhone's speech-to-text and its endpointing, the wait between the caller finishing a sentence and the webhook being sent. This is usually a large part of what a caller feels, and it is not in these numbers.
- AgentPhone's text-to-speech, and playing the audio.
- Network time from AgentPhone to us and back.
- Anything before our handler receives the request (queueing in the web server's socket, TLS).
- A request answered from the in-memory repeat cache before its body is read (a retry of a webhook id the process has already answered) records nothing.
- A turn that ends in an HTTP 500 before any reply is sent records nothing.
- Database time on its own.

Time to first token of the model is only as good as the model call's own report: it is the time until the first piece arrives in our process, so it includes the network to the model's server.

## How to read the report

```text
uv run latency-report
uv run latency-report --since 2h
uv run latency-report --call <call id>
uv run latency-report --json
```

It prints, for all turns and for each kind (fixed line, model turn, model turn with tools), the count and the p50, p95 and max of the time to first line and of the whole turn, the share of turns that used the hold line, and the p50 and p95 of model time and tool time. It also lists the three slowest turns. Call ids are cut to a short prefix unless `--call` is given. On an empty table it prints `no turns recorded yet`.

Percentiles use linear interpolation between the nearest ranks. With fewer than 20 turns in a group the p95 is close to the max, so read it that way.

How to read the columns together: `first line` is what the caller waits for, `whole turn` is how long the turn held our worker, and the gap between them is the model writing the rest of a reply the caller is already hearing. A high `hold` share means turns often took longer than 2.5 seconds before anything safe was ready.

## Results from a simulated run

These numbers are **simulated, server overhead only, no model network time**. They are the signed fake caller (`sim/`) playing Sam Doe against the real webhook, with the project's fake model and fake phone, an in-memory tool store, and sessions kept as JSONL files in a temporary folder (not Postgres). The fake model answers at once, so `model` time is about zero by construction. Each call is seven turns: three fixed lines (greeting, identity, disclosure), two plain model answers, one model turn that calls a payment-plan tool, and a goodbye turn. 100 calls, 700 turns, on one Windows 11 laptop with Python 3.12, nothing else tuned. A real run will differ: a real model adds its own seconds, and Postgres replaces the JSONL files.

Reproduce it with `uv run demo latency` (it prints this table and the overhead comparison below). Each run differs a little.

```text
700 turns (milliseconds, our server only)
  by kind: fixed 300, model 400

                         turns |       first line        |       whole turn        |  hold |  model p50/p95 |  tools p50/p95
                               |     p50     p95     max |     p50     p95     max | share |                |
----------------------------------------------------------------------------------------------------------------------------
all turns                  700 |    31.1    55.0   103.4 |    34.7    59.1   107.6 |    0% |        0.0/0.1 |        0.7/1.3
fixed line (no model)      300 |     4.4     8.9    18.0 |     4.4     8.9    18.0 |    0% |            -/- |            -/-
model turn                 400 |    41.5    58.0   103.4 |    45.3    63.9   107.6 |    0% |        0.0/0.1 |        0.7/1.3
model turn with tools      100 |    46.3    65.4    77.6 |    48.8    71.8    83.7 |    0% |        0.0/0.1 |        0.7/1.3
```

What this shows: in this setup our code adds about 4 ms at the median to a fixed-line turn and about 41 ms at the median to a model turn before the first line (95th percentile: 9 ms and 58 ms), with the fake model costing nothing. Where that time goes inside our code (session files, thread hand-offs, the sentence filter) was not profiled. These numbers say nothing about how fast a real call feels.

### Cost of the timing itself

Timer work per turn (a few `perf_counter()` reads and dictionary updates), measured alone: about 16 microseconds. Measured through the webhook, the same simulated turns with timing off and on, interleaved so machine drift hits both alike, differed at the median by +1.2 ms and +0.8 ms in two runs of 700 turns each, and by +0.3 ms when both sides had timing off. Per-line differences in other runs ranged from about -2 ms to +1.7 ms either way. So the overhead is within the noise of this measurement; the direct measurement says it is tens of microseconds. The database write is not in this comparison: it runs after the reply has left.

## Real calls

Not measured yet. To fill this in:

1. Apply the migration to your local Supabase (`supabase db reset`, or `supabase migration up`) and run the webhook with `uv run serve` against a real AgentPhone number and a real model.
2. Place a few test calls to a number you own (the owner-only test-number list covers this), covering at least a plain question, one that makes the agent use a tool, and one stop request.
3. Run `uv run latency-report --since 1h` and paste the table here, with the date, the model, the machine and the number of calls.
4. Next to it, note that it excludes speech-to-text, text-to-speech and the network to AgentPhone (see "What is not measured").

| Date | Model | Where it ran | Calls | Turns | First line p50 / p95 | Whole turn p50 / p95 | Hold share |
| --- | --- | --- | --- | --- | --- | --- | --- |
| (not yet measured) | | | | | | | |
