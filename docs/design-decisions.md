# Design decisions

The main decisions behind DebtAgentCall, the alternative that was considered, the reason, the cost, and where each lives in the code. This is an account of trade-offs, not a claim that the choices are right for every project. Nothing here is legal advice.

The rule behind most of them: **the model talks, the code decides.** A prompt can ask a model to behave, but it cannot be the only thing that enforces a rule that must hold on a phone call.

## 1. Code decides; the model only talks

- **Decision.** Anything that must hold is enforced in Python: who may be called, when, whether the person is verified, which tools are offered, what may be spoken. The model writes wording inside limits that code sets.
- **Alternative.** One system prompt that lists the rules and trusts the model to follow them, with a reviewer afterwards.
- **Why.** A prompt can be argued with or ignored. A check in code runs the same way every time and can be tested with a failing case.
- **Cost.** Every rule has to be written, wired into the live path and tested. Rules written as regexes are narrower than a person's judgment (see decision 3).
- **Where.** `compliance/` (pure checks), `harness/stages.py` (`next_stage`), `phone/webhook.py` (the live path), `harness/call_tools.py` (tools). After-call review (`harness/review.py`, `harness/subagents.py`) is advisory and routes uncertain cases to a person.

## 2. A fixed stage script, with the model only in the discussion stage

- **Decision.** Every call moves through greeting, identity check, disclosure, discussion, and wrap-up in an order code controls. Greeting, verification questions, the disclosure and the closes are fixed text. Date of birth and ZIP code are compared with the database in code. The model is called only in the discussion stage, and for a question asked during an earlier stage.
- **Alternative.** Let the model run the whole conversation, with tools for lookup and a prompt describing the order.
- **Why.** What may be said before a person is verified is the most sensitive part of a call. Fixed lines and a code comparison remove the model from it. The same step keeps fragments of speech from costing a model call (a turn with nothing usable gets a fixed re-ask).
- **Cost.** The call sounds scripted at the start, and a caller who answers in an unexpected order gets a re-ask rather than an adaptive reply. Stage logic and the fixed wording need legal review before real use.
- **Where.** `harness/stages.py` (`next_stage`, `purpose_line`), `harness/spoken.py` (reads dates and ZIP codes said in words), `harness/call_state.py`, `instructions/stages/`. Tests: `tests/harness/test_call_stages.py`, `tests/harness/test_spoken_verify.py`, `tests/harness/test_spoken_numbers.py`.

## 3. Stop triggers run before the model, and the sentence filter runs on every sentence

- **Decision.** What the caller says goes through `detect` before the model sees it. A hit such as "stop calling", a dispute, an objection to recording or a wrong number ends the call with a fixed line. The state change is saved before the line promises anything. What the model writes is cut into sentences and each one is checked before it is streamed; the first blocked sentence is never sent, is written to the audit log, and ends the turn with a fixed safe line.
- **Alternative.** Ask the model to notice stop requests and to avoid threats, and review transcripts afterwards.
- **Why.** A missed stop request or a threat spoken aloud cannot be taken back, and an after-call review comes too late. The checks are cheap and run on every turn.
- **Cost, stated plainly.** Both are regular expressions, and they are eager on purpose: a false alarm ends a call politely, while a miss is worse. That has known limits.
  - Matching is on whole words and does not understand negation, so "we will not sue you" is blocked like "we will sue you".
  - A threat phrased in an unusual way can pass; the after-call review is meant to catch it.
  - A stop request that does not use the listed phrases is not caught.
  - Spoken money must be digits that match an amount on file; a vague "a few hundred dollars" is blocked.
  - Speech-to-text errors can hide a trigger or create one.
- **Where.** `compliance/triggers.py` (`detect`), `compliance/utterance.py` (`check_sentence`, `split_sentences`), `phone/replies.py` (`SentenceBuffer`), `phone/webhook.py`. Tests: `tests/compliance/test_stop_triggers.py`, `tests/compliance/test_utterance_filter.py`, `tests/phone/test_webhook_streaming.py`.

## 4. Call tools pass a gate, then an approver

- **Decision.** The model can ask for tools (look up this call's debt, offer a payment plan, schedule a callback, log a dispute, end the call, escalate). Each request first passes a compliance gate that reads call state, then an approver. A plan outside policy is saved as pending rather than refused, because a live call cannot wait for a person. The model never supplies an account id; a tool can reach only this call's debt.
- **Alternative.** Give the model direct database access or tools that take an account number.
- **Why.** The model's claim that someone is verified or that a plan is approved has no effect on state. Only a row written by code does, and a sentence calling something approved is blocked unless code saved it.
- **Cost.** The model cannot do anything the tool list does not already allow, and every new tool needs a gate rule and tests.
- **Where.** `harness/call_tools.py` (`CallTools.gate`, `PolicyApprover`), `phone/live_tools.py`, `phone/webhook.py` (`COMMITMENT_RULE`, `check_commitment`). Tests: `tests/harness/test_call_tools.py`, `tests/phone/test_webhook_call_tools.py`.

## 5. Tool outcomes, including `unknown`

- **Decision.** Every tool call ends in exactly one of `success`, `error`, `timeout`, `denied`, `aborted`, `not_started` or `unknown`, and the model reads the outcome on the first line of the result. `unknown` means the harness cannot tell whether a tool that may have written actually did. It is never retried, and on a live call it fails closed with a fixed line and a hangup.
- **Alternative.** Treat any failure as a plain error and let the model retry or carry on.
- **Why.** A retried write could be applied twice, and a call that continues after a failed save could promise something that was not recorded.
- **Cost.** A transient failure ends a call that might have recovered, and a person has to look.
- **Where.** `harness/tool_outcome.py`, `harness/tools.py`. Test: `tests/harness/test_tool_outcomes.py`.

## 6. An append-only, hash-chained audit log enforced by the database

- **Decision.** Every check, block, stop and approval is written to `audit_event`. Database triggers assign the sequence number, chain each row's hash to the previous one, and refuse every update, delete and truncate. `uv run audit-verify` recomputes the chain.
- **Alternative.** A log table the application promises not to edit, or a log file.
- **Why.** The guarantee holds even if the application has a bug, and an edited or removed early row is detectable.
- **Cost.** The hash method is public and uses no secret, so someone with full database access who recomputes every later hash leaves a chain that checks out, and cutting rows off the end is not detected. Both need a copy of the newest sequence and hash kept outside the database, which does not exist yet. The design also does not allow deleting a row on request, which may conflict with some retention obligations.
- **Where.** `supabase/migrations/20261104120000_audit_log.sql`, `compliance/audit.py` (`record`, `verify_chain`), `tasks/audit_verify.py`. Test: `tests/compliance/test_audit_log.py`.

## 7. The dialer dials once and never retries

- **Decision.** The dialer locks the debt row, runs every pre-call check, records the decision and a `call_attempt` row, commits, and only then calls `make_call`, once. A refusal (402, 409, 429) is recorded as such. A timeout or 5xx becomes `placed_unknown`, and nothing retries it automatically. The queue stops early when an outcome affects every call.
- **Alternative.** Retry on failure, as most API clients do by default.
- **Why.** AgentPhone's create-call request has no idempotency key (see [agentphone-integration.md](agentphone-integration.md)), so a retry after a lost reply could ring a person twice. Reading the call count under the row lock also stops two workers from both passing a limit on stale facts.
- **Cost.** A call that did not ring and was marked `placed_unknown` stays unplaced until a person decides. Each such attempt counts against the debtor's call allowance.
- **Where.** `dialer/service.py` (`dial_one`, `dial_queue`, `STOP_OUTCOMES`), `phone/client.py`, `dialer/reconcile.py` for calls that never report an end. Tests: `tests/dialer/test_dialer.py`, `tests/dialer/test_reconcile.py`, `tests/phone/test_client_errors.py`.

## 8. Operator approvals: the model asks, only code writes or dials

- **Decision.** In the operator chat, the model can propose placing a call or recording a payment, note or callback. That opens a pending approval that shows who and what. Only `decide(approval_id, approve)`, called by code from the web UI, dials or writes, and only for an approval still pending. It re-runs every pre-call check before dialing.
- **Alternative.** Let the chat model call a dialing tool directly.
- **Why.** A person approves the exact debtor and amount, not an id the model chose.
- **Cost.** Every action takes a click, and an action that fails part-way is "not confirmed", never marked done and never run a second time.
- **Where.** `harness/operator.py` (`Operator`, `pending_approvals`), `harness/api.py`. Tests: `tests/harness/test_operator_chat.py`, `tests/harness/test_payments.py`.

## 9. Archive instead of delete

- **Decision.** The web UI's Remove button archives a person. The archive is one of the pre-call checks, so the dialer, the operator chat and every live-call tool stop at it, and a missing archive fact blocks rather than passes.
- **Alternative.** Delete the row.
- **Why.** The history of calls and decisions is kept, and the audit log cannot be edited anyway.
- **Cost.** Removal from a person's point of view is not erasure. Deletion on request is not implemented.
- **Where.** `compliance/archive.py` (`check_archived`), `harness/people.py`, `supabase/migrations/20261119120000_people_archive.sql`. Test: `tests/compliance/test_archive_check.py`.

## 10. Tests on a fake provider and a signed fake caller, with a negative test per rule

- **Decision.** Tests use a fake model, a fake phone client, and a simulator that sends correctly signed webhooks, so a whole call runs in a test with no network. Each rule gets at least one test where the rule must block, not only one where it allows.
- **Alternative.** Test mainly against a live model and a real phone account.
- **Why.** Repeatable, free and safe: no real person is called. A rule with no failing case can silently stop working.
- **Cost.** The fakes encode the project's assumptions about AgentPhone and about speech, so a wrong assumption passes every test. Nothing in the suite has been checked against recorded real calls.
- **Where.** `sim/agentphone_sim.py`, `model/fake.py`, `phone/client.py` (`FakePhoneClient`), `tests/` (by area: `compliance`, `dialer`, `harness`, `model`, `phone`). `uv run verify` runs format, lint, types and tests. Database tests are marked `db` and run in CI against a local Supabase database.

## What this project does not do

- **Some legal checks were removed.** The checks for attorney representation, bankruptcy and a deceased debtor were dropped to keep the project simple. A caller who says any of those things is not stopped by a rule. They must come back, reviewed by counsel, before any real use. See [compliance.md](compliance.md).
- **No per-state rules.** The debtor's state is stored but no rule reads it. State law on hours, frequency, disclosures, licensing and recording consent is not implemented, and the time zone table is a small sample.
- **Regex limits.** The stop triggers and the sentence filter are regular expressions (decision 3). A classifier layer is not built.
- **No answering-machine detection** during a call, and the recording and transfer behavior of AgentPhone is not confirmed (see the not confirmed list in [agentphone-integration.md](agentphone-integration.md)).
- **Single-process assumptions.** Turns of one call are serialized with in-process locks, and the recent webhook ids are kept in one process's memory (with a saved copy in the call state). Several server processes would need a database lock for the same guarantee.
- **The audit log has no external anchor** (decision 6).
- **No sign-in on the web app.**
- **No real-call testing.** The system has not been run with real calls to real people.
- **Not legal advice.** Nothing here has been reviewed by counsel, and the code is not a compliance guarantee.

## How it was built

The project was developed with AI coding agents under the maintainer's direction and review. Changes are made one topic per pull request, each with a test that fails without the change, and CI runs on every push (a Python job with the database, and a web job). Nothing more is claimed about how the code was reviewed or about its fitness for real use.
