# Compliance map

**Not legal advice. Not reviewed by counsel.** This page lists the rules the code enforces and how, so that engineers and reviewers can check the code against the rules. It does not say the project complies with the FDCPA, Regulation F, the TCPA or any state law, and it should not be used to claim that it does. The citations come from comments in the code and may be incomplete or wrong. A lawyer who works in debt collection must review the rules, the wording of every fixed line, and everything under "Needs a lawyer" below before this is used with real people.

All people, numbers and amounts in the repository are made up.

How to read the tables:

- **Rule id** is the `rule_id` written to the audit log (`audit_event.rule_id`) when the rule decides something.
- **Source** is the law or policy the code comment names. "Policy" means a choice of this project, not a citation.
- Every table has a **Limits** column. It says what the rule can miss, what is only a prompt instruction and not enforced in code, and what depends on AgentPhone behavior nobody has confirmed.
- The principle behind all of it: a rule is plain Python that looks at facts (the time, the call history, the flags, the call state) or at text with a pattern. The model is never asked whether a rule applies. When the code cannot tell, it blocks.

## 1. Pre-call checks

Enforced before a call is placed. `compliance/precall.py: check_before_call` runs all of them and returns every decision, allowed and blocked. `dialer/service.py: dial_one` records each one in the audit log, then dials only if all allow (`may_call`). `GET /api/precall` runs the same function, and the operator chat's `check_call` runs the dialer's own dry run (`plan_queue`, which also checks the debt status), so the UI, the operator and the dialer use the same code. Tests that cover the whole set: `tests/compliance/test_precall_checks.py`, `tests/dialer/test_dialer.py`.

| Rule in plain words | Source | Rule id | File and function | Test file | Limits |
| --- | --- | --- | --- | --- | --- |
| No call before 8am or from 9pm in the debtor's local time, in every zone the ZIP or the area code points to. Also blocks inside a window the debtor called inconvenient. | FDCPA 805(a)(1); Reg F 1006.6(b)(1)(i) | `calling_hours` | `compliance/hours.py: check_calling_hours`, zones from `compliance/timezones.py: zones_for` | `tests/compliance/test_calling_hours.py` | The zone table is small and hand-made: 14 ZIP prefixes and 14 area codes. An unknown ZIP and area code blocks the call (safe, but it blocks most real numbers until the table is replaced). The "inconvenient window" feature exists in the function, but nothing in the live path fills it in, so in practice only 8am to 9pm is enforced. Uses the zone of the ZIP and of the area code, not the debtor's actual location. |
| At most 7 calls in 7 days for one debt, and no calls for 7 days after a conversation with the debtor about that debt. Unanswered calls count. | Reg F 1006.14(b)(2) | `call_frequency` | `compliance/attempts.py: check_call_frequency`; the count is read under a row lock in `dialer/service.py: reserve` | `tests/compliance/test_precall_checks.py`, `tests/dialer/test_dialer.py` | Counts only calls this system placed (`call_attempt`), not other collectors' calls or calls from other systems. A "conversation" is recorded only from positive evidence (the caller spoke and was verified, `phone/call_end.py: derive`), so a call that ends unexplained counts as no conversation and does not start the 7-day pause (it is logged as `call_end_unknown_outcome` for a person to look at). If the call-ended event never arrives, the attempt has no outcome until `uv run reconcile` runs. Blocks when the history could not be loaded. |
| Consent to call this number with an artificial voice must be on file and not revoked. | TCPA | `tcpa_consent` | `compliance/consent.py: check_consent` | `tests/compliance/test_precall_checks.py` | Checks that a consent record exists and has not been revoked. It cannot tell whether the consent is valid (what was disclosed, who gave it, whether it covers this caller). `line_type` (cell or landline) is stored but no compliance rule uses it. Bill uploads save a phone with no consent, so those numbers are blocked until a person records consent some other way (nothing in the repo records it). |
| Stop if the debtor asked us to stop, or to stop using this number. | FDCPA 805(c) | `cease` | `compliance/flags.py: check_cease` (flag kinds `cease`, `opt_out_phone`) | `tests/compliance/test_precall_checks.py` | Only as good as the flags. A written request that nobody entered into `flags` is not seen. A flag for one number applies to that number; if the number is unknown, the flag applies. |
| Stop if the number is on a do-not-call list. | Policy (no statute cited in code) | `dnc` | `compliance/flags.py: check_dnc` | `tests/compliance/test_precall_checks.py` | There is no integration with a national or state registry; only a `dnc` flag someone has entered. Which lists apply to which calls is a question for counsel. |
| Do not contact the debtor directly if a lawyer represents them on this debt. | FDCPA 805(a)(2) | `attorney` | `compliance/flags.py: check_attorney` | `tests/compliance/test_precall_checks.py` | Flag-based. The lawyer's contact details are stored in the flag but nothing contacts the lawyer. |
| Stop collection if the debtor filed for bankruptcy. | Policy (the code cites the automatic stay) | `bankruptcy` | `compliance/flags.py: check_bankruptcy` | `tests/compliance/test_precall_checks.py` | Flag-based. No bankruptcy-scrub integration. Exceptions and case specifics need a lawyer. |
| Do not call a deceased person's number to collect. | Policy | `deceased` | `compliance/flags.py: check_deceased` | `tests/compliance/test_precall_checks.py` | Flag-based. Handling of estates is not implemented. |
| Only a debt whose status is `active` may be called; disputed, paused and closed debts are blocked. | Policy | `debt_status` | `dialer/service.py: check_debt_status`, read under the same row lock | `tests/dialer/test_dialer.py` | Applies to the dialer only (including the operator's approved calls). `check_before_call` alone does not look at the status. A dispute on a live call sets the status, so it depends on that write succeeding; see in-call triggers. |
| Missing data blocks: no facts for the debt, no phone record, no call history, no flags loaded. | Policy (fail closed) | `precall`, or the rule's own id | `compliance/precall.py: check_before_call`; each rule above | `tests/compliance/test_precall_checks.py` (`test_no_facts_at_all_blocks`, `test_missing_data_blocks`, `test_flags_not_loaded_block`) | None known beyond the data being wrong. |

### The owner-only test-number list

`TEST_NUMBERS` is a comma-separated list of full E.164 numbers, set in the environment or `.env` (see `.env.example`). It is empty by default, and while empty nothing changes. It exists so the repo owner can place real test calls to their own phone at any hour and repeatedly.

- It skips exactly two rules for a listed number: `calling_hours` and `call_frequency` (`EXEMPT_RULES` in `compliance/testnumbers.py`). Consent, cease, do-not-call, lawyer, bankruptcy, deceased and debt status still run and still block.
- It is applied in one place, `check_before_call`, so the dialer, the operator chat and `/api/precall` agree. It does not apply to the call tool gate: a callback booked during a call is still checked against calling hours.
- A skip is audited: the decision is written with `evidence.exemption = "test_number"` and `would_have_blocked` (what the rule would have said), and a second audit row with `kind = "test_number_exemption"` is written for each skip (`harness/precall.py: check_and_record`). The dialer still records every attempt.
- An entry must be a full E.164 number (a `+` and 8 to 15 digits; spaces, dashes, dots and brackets are ignored). Anything else is logged and ignored. There are no wildcards and no prefix matching.
- It cannot make a call that another rule blocks go through, and it cannot skip a rule at all if the history it needs was not loaded (missing data is never exempted).
- Limit: the list is trusted configuration. Anyone who can edit the server's environment can add a number, and a listed number that belongs to a real debtor would be called outside hours. Do not put anyone's number on it but your own.
- Tests: `tests/compliance/test_test_numbers.py`. Demo: `uv run demo part-27`.

## 2. Stop triggers: in-call, before the model

Enforced on every caller turn before anything else sees the text. `compliance/triggers.py: detect` finds the trigger; `phone/webhook.py: _stop_turn` and `record_stop_in_db` save the result, speak the fixed close and hang up. The model is not called. Tests: `tests/compliance/test_stop_triggers.py` (the patterns), `tests/phone/test_webhook_live_checks.py` (the webhook behavior, including a failed save, a broken session and a stopped call staying stopped).

| Rule in plain words | Source | Rule id (the trigger kind) | What the code does | Limits |
| --- | --- | --- | --- | --- |
| The caller has a lawyer: end the call, record it, flag for a person. | FDCPA 805(a)(2) | `attorney` | Saves an `attorney` flag for this debt, audit row, fixed close, hang up. | Regex. Fires on any mention of lawyer, attorney, counsel, solicitor, "represented by", legal aid, even "my lawyer friend said this is fine". Misses a lawyer mentioned in other words. |
| Bankruptcy: end the call. | Policy | `bankruptcy` | Saves a `bankruptcy` flag for every debt of the debtor. | Regex on "bankrupt", "chapter 7/11/13". Does not understand negation: "I'm not bankrupt" fires. |
| The person has died: end the call. | Policy | `deceased` | Saves a `deceased` flag for every debt. | Regex on "passed away", "died", "deceased", "is dead", "he/she/... passed" (with guards for "passed the exam"). It can fire on a stranger's story, and can miss unusual phrasing. |
| "Stop calling": end the call and revoke consent for the number called. | FDCPA 805(c); TCPA | `cease` | Saves a `cease` flag for every debt, sets `phone.revoked_at` on the number called, audit row, close, hang up. It sets `cease` rather than `opt_out_phone` on purpose: "stop calling" may mean this number only, but `cease` stops every call, which is the safer reading. | Regex over many phrasings (stop calling, don't call me, take me off your list, opt out, unsubscribe, leave me alone, revoke consent). "Please stop calling me at work" fires `cease` for all calls. A request in words the patterns do not contain is not caught. |
| The caller disputes the debt: end the call; stop collection until a person reviews it. | FDCPA 809 (disputes); the code does not cite a section | `dispute` | Only if the person is verified: saves a `dispute` row, sets `debt.status = 'disputed'` (so the dialer stops), audit row, close. Before verification: a neutral goodbye, a `dispute` row with `verified = false` for a person to review, and nothing changed on the debt (a stranger saying "it isn't mine" is not a dispute). | Regex ("not my debt", "I don't owe", "dispute", "already paid this", and so on). The code treats a spoken dispute as a stop, which is broader than the written-dispute rule; whether that is right, and what follow-up writing is owed, needs a lawyer. No validation notice or written follow-up is generated: the close says someone will follow up in writing, but nothing sends it. |
| The caller objects to recording: end the call. | Policy | `recording_objection` | Fixed close, hang up. Sets no flag. | Regex. Ending the call does not stop AgentPhone's recording of the part already made, and whether recording can be turned off per call is unconfirmed (see below). |
| Wrong number: end the call, say nothing about a debt. | Policy | `wrong_number` | Fixed close, hang up. Sets no flag, because no flag kind fits and a wrong number does not stop calls to the right person. | Regex. Nothing marks the number as bad, so it can be called again. |

Common limits:

- Matching ignores case and works on whole words. It is deliberately eager: a false alarm only ends the call politely; a miss could break the law.
- It reads the speech-to-text transcript. A mis-transcribed "stop calling" is missed.
- Several triggers can fire at once; they are applied in priority order (attorney, bankruptcy, deceased, cease, dispute, recording objection, wrong number). Before verification, a dispute gives way to any other hit.
- `compliance/triggers.py: combine` can merge regex hits with a second classifier's hits (a trigger fires if either finds it). No classifier exists in the repo yet; only the merge function and its tests do. Today the triggers are regex only.
- The fixed closes never name the debt, the amount or the creditor, so they are safe to say to the wrong person (`tests/compliance/test_stop_triggers.py: test_closes_never_name_the_debt`).

## 3. Call stages: identity and disclosure

Enforced by the call stage machine, `harness/stages.py: next_stage`, with fixed lines spoken by code. These have no `rule_id` and write no audit row of their own. Tests: `tests/harness/test_call_stages.py`, `tests/harness/test_spoken_verify.py`, `tests/harness/test_spoken_numbers.py`, `tests/harness/test_call_purpose.py`.

| Rule in plain words | Source | Where in code | Limits |
| --- | --- | --- | --- |
| No talk about a debt with anyone until the debtor is verified. Date of birth and ZIP are compared in code with the database, two tries, then a polite end with nothing about a debt. | FDCPA 805(b) | `next_stage`, `harness/spoken.py: find_dob, find_zip`; the identity questions and the ending are fixed text | Date of birth and ZIP are weak identifiers; whether they are enough is a policy and legal question. A spoken date that could be read two ways is no answer, never a match. The greeting asks for the person by full name, which a stranger or an answering machine may hear. |
| After verification, a fixed disclosure: this call is recorded, this is an automated assistant, this is a debt collector attempting to collect a debt. | FDCPA 807(11) for the collector statement; the recording and automated-assistant lines are policy | `DISCLOSURE` in `harness/stages.py`, spoken by code in the same turn the match happens | The wording has not been reviewed by counsel. The recording notice is always said, but the project does not control whether AgentPhone records (see below). Whether a first-call notice, Reg F's validation notice or state-specific wording is also needed is not implemented. |
| Say why we called, with the creditor and the balance from the debt's own row; for a disputed, paused or closed debt, no amount and no payment request; if the row cannot be read or does not match, no amount and the call ends. | FDCPA 807(2)(A) (amount) | `harness/stages.py: purpose_line`, `phone/webhook.py: _add_purpose` | The amount is the balance owed now, from `public.current_balance_cents`. If the row is wrong, the line is wrong. |
| The model never talks before the debtor is verified and has heard the disclosure, and has no tools or skills until then. | Policy | `phone/webhook.py: _locked_turn` (tools offered only in `DISCUSS`) | Questions from the caller in `GREETING`, `VERIFY` and `THIRD_PARTY` do go to the model, with no tools; its sentences still pass the filter below. |
| If someone else answers, ask only for a good time to reach the person, then say goodbye. | FDCPA 805(b) | `next_stage` (`THIRD_PARTY`), `LOCATION_ASK`, `THIRD_PARTY_CLOSE` | Detection of "not here" is a small regex set, not understanding. |

## 4. Call tools: the gate and the approver

Enforced when the model asks for a tool in `DISCUSS`. `harness/call_tools.py: CallTools.gate` runs first, then `PolicyApprover`. Both write an audit row. Tests: `tests/harness/test_call_tools.py`, `tests/phone/test_webhook_call_tools.py`.

| Rule in plain words | Source | Rule id | File and function | Limits |
| --- | --- | --- | --- | --- |
| A tool runs only when the stage is `DISCUSS` or `WRAP` and the person is verified and has heard the disclosure. `log_dispute`, `escalate_to_human` and `end_call` are always allowed. | Policy | `call_tool_gate` | `CallTools.gate` | The state comes from the server's saved `CallState`, never from the model. |
| After a dispute on the debt or on this call, offers and callbacks are blocked. If the store cannot say whether the debt is disputed, it is treated as disputed. | Policy (related to FDCPA 809) | `call_tool_gate` | `CallTools.gate`, `CallTools.disputed` | Depends on the dispute row being saved; if saving fails, offers stay blocked for that call. |
| After a `cease` flag, offers and callbacks are blocked. If flags were not loaded, blocked. | FDCPA 805(c) | `call_tool_gate` | `CallTools.gate` using `compliance.flags.check_cease` | The flag list is loaded once per turn, when the first tool is called. |
| A callback must fall within the next 30 days and inside calling hours in every zone the debtor may be in. | FDCPA 805(a)(1); Reg F 1006.6(b)(1)(i) | `call_tool_gate` | `CallTools.gate` using `check_calling_hours` | Same zone-table limits as `calling_hours`. Test numbers do not skip this. |
| A payment plan is approved on the call only inside the plan policy (default: 10 percent down, at most 12 months, at most 52 installments). Outside it, the plan is saved as `pending`, the caller hears that a specialist will confirm it, and nothing is agreed. Unparseable arguments are denied. | Policy | `call_tool_approver` | `PolicyApprover`, `outside_policy` | The limits are this project's defaults, not law. They can be overridden per creditor in the `plan_policy` table, which nothing in the code writes. |
| A spoken sentence may not say something was approved, agreed, booked or confirmed unless code saved an approved plan or a callback this turn. | Policy; FDCPA 807 (false representations) | `unbacked_commitment` | `phone/live_tools.py: check_commitment`, called from `phone/webhook.py: check_utterance` | A fixed list of words (approved, agreed, accepted, confirmed, all set, locked in, booked, scheduled). A promise in other words is not caught; a harmless "scheduled" in another sense is blocked. Tested in `tests/phone/test_webhook_call_tools.py` and `tests/phone/test_webhook_streaming.py`. |
| An escalation saves a request for a person. It transfers the call only when `SUPERVISOR_NUMBER` is set. | Policy | none | `escalate_to_human`; `phone/webhook.py` | The transfer is an `{"action": "transfer"}` reply to AgentPhone. How AgentPhone carries it out (which number it dials, what the caller hears) is not confirmed. Without `SUPERVISOR_NUMBER`, nothing transfers and the call continues. |

## 5. The sentence filter

Enforced on every sentence the model writes, as it streams, before the caller hears it. `compliance/utterance.py: check_sentence` returns a decision; the first blocked sentence is never spoken, is written to the audit log (`kind = "utterance_blocked"`), and the turn ends with a fixed safe line. Rules are checked in this order and the first that blocks wins. Tests: `tests/compliance/test_utterance_filter.py` (the rules), `tests/phone/test_webhook_streaming.py` and `tests/phone/test_webhook_live_checks.py` (the live behavior). Demo: `uv run demo part-11`.

| Rule in plain words | Source | Rule id | Limits |
| --- | --- | --- | --- |
| Before the person is verified: no debt words, no creditor name, no money. | FDCPA 805(b) | `third_party_disclosure` | Word list (debt, collection, balance, owe, payment, past due, and so on), whole words, case-insensitive. A hint with none of those words gets through. |
| After verifying but before the disclosure was said: the same words stay blocked. | FDCPA 807(11) | `mini_miranda_first` | Same word list. The disclosure itself is spoken by code and does not go through the filter. |
| No claiming to be, or to call from, a lawyer, court, the IRS or government. | FDCPA 807(1), 807(3) | `false_identity` | Pattern of a claim phrase plus an authority word. Other ways of implying authority are missed. Asking about the debtor's attorney is allowed. |
| No threats of arrest, jail, prison, police, criminal action or a warrant, ever. No suing, garnishing, seizing or repossessing unless legal action is authorized for the debt. | FDCPA 806, 807(4), 807(5) | `threat` | Word list. "Sue" counts only as a verb phrase ("sue you", "will sue", "suing") so a person named Sue can be greeted. No negation: "we will not sue you" is blocked like "we will sue you". An unusual phrasing of a threat could slip past; the after-call review is meant to catch it. In the live webhook, legal action is never authorized, so all legal-action words are always blocked. |
| No legal-action words on a time-barred debt, even if otherwise authorized. | Reg F 1006.26(b) | `time_barred` | The filter supports it, but the live webhook never marks a debt as time-barred or authorizes legal action (`speech_state` in `phone/webhook.py` leaves both at their defaults), so the `threat` rule fires first. The statute-of-limitations question is not computed anywhere. |
| No profanity. | FDCPA 806(2) | `profanity` | A short word list. It includes "hell" and "damn", so a harmless use is blocked. |
| Every dollar amount spoken must be an amount on file, to the cent. Money words with no amount in digits ("a few hundred dollars") are blocked. | FDCPA 807(2)(A) | `amount_mismatch` | Known amounts are the balance owed now plus the amounts of a plan code approved this turn. Amounts spoken as words ("twelve hundred") with a money word are blocked as unverifiable; amounts without a money word or `$` are not seen. |
| No pressure to pay now ("must pay today", "immediately", "final notice", "act now") during the validation period. | FDCPA 809(b); Reg F 1006.34 | `overshadowing` | Word list. The validation period defaults to on and nothing in the live path turns it off, so these words are always blocked. No validation notice is sent and no 30-day window is tracked. |

Common limits:

- It reads text with patterns, so it is deliberately blunt: when unsure, it blocks. The cost is that a good sentence is sometimes replaced by "Sorry, could you say that again?".
- It does not understand meaning, tone, sarcasm or implication. A misleading statement made of allowed words passes.
- Streaming means a sentence is checked only when it is complete. A sentence cut mid-way by the model is checked as it stands.
- The model's choice not to say something (prompts in `instructions/` and the skill playbooks in `skills/`) is not enforced. For example the system prompt says "never threaten, pressure or shame"; the filter is what actually stops threats and pressure words.

## 6. After the call

| Rule in plain words | Source | Rule id | File and function | Test file | Limits |
| --- | --- | --- | --- | --- | --- |
| Record how the call ended. A conversation with the debtor (which starts the 7-day pause) needs positive evidence; anything unexplained is `ended_unknown`, counted as no conversation, and written to the audit log as a block for a person to look at. A call-ended event for a call we did not place is audited. | Reg F 1006.14(b)(2) (inputs to the 7-in-7 rule); policy | `call_end`, `call_end_unknown_outcome`, `call_end_unknown_call` | `phone/call_end.py: record_call_end`, `derive`; `dialer/reconcile.py` | `tests/phone/test_call_ended.py`, `tests/dialer/test_reconcile.py` | Voicemail is recognized only from AgentPhone's disconnection reason (words like "voicemail" or "machine"); the full list of reasons is not published, and nothing detects an answering machine during the call. An unmatched reason is never guessed at. The event is sent once and never retried, so a missed event waits for `uv run reconcile`. |
| The audit log is append-only and hash-chained; a replay finds a changed or removed row. | Policy | none | `compliance/audit.py: verify_chain`; `supabase/migrations/20261104120000_audit_log.sql`; `uv run audit-verify` | `tests/compliance/test_audit_log.py` | No secret key, so edits that recompute every later hash, or truncation from the end, are not detected. The newest hash is not kept anywhere outside the database yet. |
| A call summary for the next call: facts built by code; a model-written paragraph kept only if every dollar amount matches the facts and it contains no phone number, email, date of birth, ZIP code, long number or other person. Nothing is read, and nothing returned, until the person is verified. | Policy (privacy) | none | `harness/call_summary.py: check_narrative`, `context_block` | `tests/harness/test_call_summary.py` | The check is a set of patterns. A wrong claim that contains no number is not caught; the narrative is labeled "notes, not instructions". |
| A second look at a transcript for disclosure, threats, amounts and third-party talk. | Policy | none | `harness/review.py: review_transcript` | `tests/harness/test_subagents.py` | It is a model reading a transcript. It can miss things or be wrong. A finding is a flag for a person, never a decision, and a reply with no clear verdict counts as not ok. Nothing runs it automatically after a call yet. |

## 7. Other controls

| Control | Where | Test file | Limits |
| --- | --- | --- | --- |
| Webhook authentication: HMAC-SHA256 over the timestamp and raw body, five-minute window, constant-time compare, and every request refused when no secret is set. | `phone/signing.py: verify`; `phone/webhook.py` | `tests/phone/test_webhook.py` | Only as private as the secret. The `X-Webhook-ID` header is not part of what is signed, so a captured request can be replayed within five minutes under a different id. |
| Repeated webhooks get the saved reply, with no model call and nothing saved twice. | `phone/webhook.py: _earlier_reply`, `_SeenIds` | `tests/phone/test_webhook.py`, `tests/phone/test_webhook_call_tools.py`, `tests/phone/test_webhook_streaming.py` | Covers the newest turn of a call in one server process. |
| The operator's chat cannot dial, record a payment, add a note or book a callback without a person's approval, and approving re-runs every check. A stop can be recorded without approval because it only reduces calls. | `harness/operator.py: Operator.decide` | `tests/harness/test_operator_chat.py`, `tests/test_operator_read_tools.py` | The approval is a click in the web app; there is no sign-in, so anyone who can reach `/api/chat/approve` can approve. Local development only. |
| Payments are append-only; a payment above the balance, in the future or before the debt opened is refused by the database. | `supabase/migrations/20261116120000_payments.sql`; `harness/payments.py` | `tests/harness/test_payments.py` | Not a legal rule, an integrity rule. |
| Bill uploads become drafts; nothing becomes a debtor, phone or debt until a person confirms every field, and a bill is not consent. | `harness/bills.py` | `tests/harness/test_bills.py` | The model's reading of a bill is checked by code but can still be wrong; the person confirming is the control. |

## Depends on AgentPhone behavior that is not confirmed

The code is written to fail safe around these, but none of them has been verified against a real AgentPhone account by this repo.

- **Voicemail detection.** Nothing in the call detects an answering machine. The `voicemail` outcome is set only from words in the disconnection reason after the call, and the greeting ("Hi, may I please speak with <name>?") would be spoken to a machine.
- **Concurrency cap.** A 429 without a `Retry-After` header is read as "too many live calls" (`concurrency_limit`). The actual cap, and whether other responses mean the same, are not confirmed.
- **Transfer.** `escalate_to_human` replies `{"action": "transfer"}` when `SUPERVISOR_NUMBER` is set. The behavior on AgentPhone's side is not confirmed.
- **Recording toggle.** The dialer passes `disable_recording=None` and so leaves AgentPhone's default (a `TODO(recording consent)` in `dialer/service.py: dial_one` marks the spot). The disclosure says the call is recorded regardless. No recording-consent decision exists, and the code cannot confirm whether a call is recorded.
- **Disconnection reasons.** Only `agent_hangup` (from AgentPhone's example) and the simulator's `user_hangup` are known; others are matched by words.

## Needs a lawyer

None of this is encoded, or what is encoded is a placeholder:

- **State rules.** There is no per-state table. The debtor's `state` is stored but no rule reads it. State laws on calling hours, call frequency, required disclosures, licensing, recording consent, cease and desist, and medical debt differ from the federal rules and are not implemented.
- **The time zone table.** It is a small sample, not a data source. Replacing it, and deciding what to do when the ZIP and the area code disagree (the code requires both zones to be convenient), is a policy decision.
- **Consent.** What counts as prior express consent for an artificial voice, how it is recorded, and revocation by channels other than speech on a call.
- **Disclosure wording.** The fixed lines in `harness/stages.py` and `compliance/triggers.py`, including the debt collector statement, the recording and automated-assistant notices, and the closes.
- **Validation notice and dispute handling.** Reg F's validation information and the timing of disputes are not implemented; a spoken dispute only stops calls.
- **Time-barred debt.** No statute-of-limitations logic exists.
- **Do-not-call lists, bankruptcy and deceased scrubs, and attorney handling.** The flags exist; the data sources and processes do not.
- **The plan policy limits** and what may be promised on a call.
- **Records.** How long to keep audit rows, call messages and summaries, and what must be deletable on request. The append-only design may conflict with some deletion obligations.
- **Any use with real people.** This project has not been tested with real debtors and is not a product.
