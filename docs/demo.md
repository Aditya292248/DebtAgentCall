# Founders' demo script

A 6 to 7 minute walk through DebtAgentCall for people who build startups. The point to land, at every step: **the model talks, the code decides.** Everything shown uses made-up people and numbers, and nothing rings a real phone.

This is not legal advice, and the demo does not show legal compliance. Say so up front. See [compliance.md](compliance.md) for what each rule does and does not cover.

## Before the audience arrives

The one-command start the owner uses is a local script that is not in this repository. These are the manual steps it replaces. Do them once, then rehearse.

You need [uv](https://docs.astral.sh/uv/), Node.js, and Docker Desktop running.

1. Install the Python dependencies.

   ```bash
   uv sync
   ```

2. Create the settings file and open it.

   ```bash
   cp .env.example .env
   ```

   On Windows PowerShell use `Copy-Item .env.example .env`. Set `AGENTPHONE_WEBHOOK_SECRET` to any string (the simulator in step 5 and the server must use the same one). For the chat with a real model, also set `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` (the defaults point at LM Studio on `localhost:1234`). To approve a call at any hour in step 3, set `TEST_NUMBERS=+12135550101`, which is Sam Doe's made-up number from the seed; see "What to say if asked" for what that list can and cannot skip. Do not commit `.env`.

3. Start the local database. The first start applies `supabase/migrations/` and loads `supabase/seed.sql` (nine made-up debtors, each built to trip one pre-call check).

   ```bash
   npx supabase start
   ```

   To get back to a clean seed at any time (this wipes the local database, including the audit log):

   ```bash
   npx supabase db reset
   ```

4. Start the chat API for the web app, in its own terminal. It serves `/api` on port 8000, which is where the web dev server forwards.

   ```bash
   uv run api
   ```

   No model server, or no AgentPhone account? Run this instead of `uv run api`. It serves the same API with a scripted fake model and a fake phone, so an approved call rings nobody:

   ```bash
   uv run demo part-24
   ```

   (`uv run api` with a real model needs `AGENTPHONE_API_KEY`, `AGENTPHONE_AGENT_ID` and `AGENTPHONE_FROM_NUMBER_ID` before it will dial anything; without them, approving a call returns "AgentPhone is not set up" and nothing is recorded.)

5. Start the web app, in its own terminal. First copy `web/.env.example` to `web/.env.local` and paste in the anon key that `npx supabase status` prints.

   ```bash
   cd web && npm install && npm run dev
   ```

   Open http://localhost:5173.

6. Optional, for a real AgentPhone call (not part of the timed script, and not verified end to end in this repository): start the webhook, expose it with a tunnel of your choice (for example `cloudflared` or `ngrok`, which are not part of this repo), and set the AgentPhone agent's webhook URL to `<tunnel URL>/webhooks/agentphone`.

   ```bash
   uv run serve --port 8010
   ```

   `uv run serve` defaults to port 8000, which `uv run api` is using, so give it another port. The same server is what step 5 of the script uses with the simulator.

Check that the offline tests pass on your machine before the demo:

```bash
uv run verify
```

The `db`-marked tests need the local database from step 3 and a clean seed, so run this right after `npx supabase db reset`.

## The script

Times add up to about 6.5 minutes. Say the quoted lines in your own words.

### 1. The debtor list (0:00 to 0:45)

Show the web app. Say: "Twelve made-up people. Each badge, Ready to call or Can't call, comes from the Python rules, not from the browser."

**See:** the debtor list with a status dot per row; hover a "Can't call" to read the blocking rule and reason.

**Why it matters:** the page repeats no rule in JavaScript. It asks `GET /api/precall`, which runs `compliance.check_before_call`, the same function the dialer uses.

Today at night the list shows most people as "Can't call" because of calling hours. That is itself the demo. Use "Check at" in the top bar to look at 2pm instead.

### 2. Who may be called, and why not (0:45 to 1:45)

In a terminal, run the dialer's dry run at the fixed time the seed was built around. It writes nothing and dials nobody.

```bash
uv run dialer --dry-run --when 2026-11-05T19:00:00+00:00
```

**See** (on a clean seed): two debts that would be called and seven blocked, each with a rule and a reason:

```text
  blocked  ACC-1005  tcpa_consent: consent revoked on 2026-10-28
  blocked  ACC-1006  tcpa_consent: no consent on file for this number
  blocked  ACC-1007  cease: debtor asked us to stop on 2026-10-15
  blocked  ACC-1008  cease: debtor opted this number out on 2026-10-21
  blocked  ACC-1009  dnc: number is on a do-not-call list on 2026-09-30
  call     ACC-1001  +12135550101  (every check allows)
  blocked  ACC-1003  call_frequency: talked with the debtor on 2026-11-02; no calls until 2026-11-09
  call     ACC-1002  +14155550102  (every check allows)
  blocked  ACC-1004  call_frequency: 7 calls in the last 7 days; the limit is 7
would call 2, blocked 7
```

**Why it matters:** there is no model in this path at all. The decision to call is a list of plain functions in `compliance/`, and a missing fact (no history, no flags) blocks instead of passing.

### 3. The chat: the model proposes, a person approves (1:45 to 3:00)

In the web app's chat, type `call Sam Doe`.

**See:** collapsed tool lines (`find_debtor`, `check_call`, `place_call`), then an approval card that shows the person's name and a masked phone number, and the message "Nothing has been dialed". Open the `check_call` line to show every rule and its result. Click Approve.

**Why it matters:** `place_call` does not dial. Only the approve button does, and approving runs every check again inside one transaction before a single dial attempt. If a check fails at that moment, the card cannot be approved, and the model has no way around it. Then try `call Morgan Patel`, who asked us to stop: `check_call` shows the `cease` rule blocking, and the card has only Deny. (Do not use Casey Morgan: the seed's call history is dated early November, so that person's call limit has long expired.)

If the card shows only Deny for everyone, it is outside calling hours; set `TEST_NUMBERS` as described above, or use "Check at".

Approving records a call attempt and audit rows even with the fake phone, so a later dry run at the real current time is affected; the fixed `--when` times used below are not.

**Depends on:** a model for the chat (`uv run api`), or `uv run demo part-24`, which scripts the model.

### 4. A simulated call, offline (3:00 to 4:30)

Run the three scripted calls through the real webhook code, with a fake model that is deliberately badly behaved: it blurts out the balance every time it speaks.

```bash
uv run demo part-13
```

(`uv run demo` with no argument lists every demo name; the names are the ones the command accepts.)

**See:** the stage printed beside each line (`GREETING`, `VERIFY`, `DISCLOSE`, `DISCUSS`, `THIRD_PARTY`, `WRAP`). The greeting, the date of birth and ZIP check, and the disclosure are fixed lines from code. In the call where a stranger answers, the model's "Your balance with Northwind Furniture is $1,250.00" never reaches the caller, and the demo prints it as dropped with the rule that stopped it (`third_party_disclosure`).

**Why it matters:** identity is checked by code against the database, the legally required disclosure is code-spoken text, and every sentence the model writes goes through `compliance.check_sentence` before it is said. A model that misbehaves cannot leak a balance.

For 30 seconds more, show the filter alone:

```bash
uv run demo part-11
```

**See:** the same sentence allowed or blocked depending on the call state: allowed after the disclosure, blocked before verification, blocked with a wrong amount, blocked as a threat.

### 5. A blocked call: the caller says stop (4:30 to 5:30)

This one uses the real webhook server and the simulator, and it changes the local database (it records a stop for Sam Doe). Start the server in one terminal:

```bash
uv run serve --port 8010
```

In another terminal, with the same `AGENTPHONE_WEBHOOK_SECRET` set in the environment or `.env`, play the scripted caller who says "Stop calling me":

```bash
uv run python -m sim.agentphone_sim sam_stop_calling --url http://127.0.0.1:8010
```

**See:** the agent says the greeting, the caller says stop, and the agent says "Understood. We'll stop calling you. Goodbye." and hangs up. The server needed no model for this call.

Now run the dry run again:

```bash
uv run dialer --dry-run --when 2026-11-05T19:00:00+00:00
```

**See:** Sam Doe (`ACC-1001`) has moved from "call" to "blocked", with `tcpa_consent: consent revoked` and `cease: debtor asked us to stop`.

**Why it matters:** a stop request is caught by a pattern in `compliance/triggers.py` before the model sees the turn. Code saves the flag, revokes consent for the number, writes an audit row and hangs up, all in one transaction. Even if saving failed, the call would still end. And because the pre-call checks read the same flags, the person is not called again.

Reset afterwards with `npx supabase db reset`.

### 6. The audit log (5:30 to 6:30)

In the web app, open "Compliance log" in the top bar.

**See:** the newest decisions first: rule, allow or block, reason, account, time, and a "Verified" mark for the hash chain. The stop from step 5 is there as a `cease` row.

Then in a terminal:

```bash
uv run audit-verify
```

**See:** `audit chain OK: <n> rows, every hash matches`.

Then try to change history. The container name is the one `docker ps` shows for the local database (the default for this repository is `supabase_db_DebtAgentCall`):

```bash
docker exec supabase_db_DebtAgentCall psql -U postgres -c "update public.audit_event set reason = 'edited' where seq = 1"
```

**See:** `ERROR:  permission denied for table audit_event`. Update, delete and truncate are revoked from every role, and triggers refuse them as well. A correction is a new row.

**Why it matters:** every decision, allowed or blocked, is a row whose hash covers the row before it, and `audit-verify` recomputes the chain in Python. Say the limit plainly: there is no secret key and no external copy of the newest hash yet, so someone with write access who recomputes every later hash would not be caught.

### Close (6:30 to 7:00)

Say: "The model writes the words. Code decides whether a call is placed, whether the call must end, what stage it is in, whether a sentence is spoken and whether a tool runs. None of this is reviewed by a lawyer yet, and the compliance page says exactly what is not covered."

## Not on main yet

Check `gh pr list` before the demo. When this was written:

- **Live call view** (open pull request 39): a screen in the web app that follows a call as it happens. Skip this part of any demo until it has merged.
- **Add and archive people from the web UI** (open pull request 40): not on main. The rule underneath already is: no tool deletes anything. Do not demo the screen until it has merged.

The bill upload that is on main is an API only (`/api/bills`); the web app has no screen for it, so it is not part of this script.

## What to say if asked

**Why regex, and what about a classifier?** Stop triggers and the sentence filter are regular expressions on purpose: they are deterministic, readable, and testable line by line (`tests/compliance/test_stop_triggers.py`, `tests/compliance/test_utterance_filter.py`), and a reviewer or lawyer can read the exact words that fire. They are eager: a false alarm ends a call politely, a miss could break the law, so "please don't call me at work" ends the call too. Their weakness is language they do not list and negation they do not understand. A second layer, a small classifier, is planned but does not exist yet: `compliance/triggers.py: combine` already merges its hits with the regex hits (either one firing stops the call), but only the merge function exists. A classifier would only ever add stops, never remove one.

**Why archive instead of delete?** Compliance records are evidence. `audit_event`, `payment` and `debt_note` refuse update, delete and truncate in the database, and a debt that has payments or notes cannot be deleted. A mistake is corrected with a new row. The people screen that archives instead of deleting is in an open pull request and not on main yet.

**How does fail-closed work?** When the code cannot tell, it says no. A few examples you can point at: no webhook secret refuses every webhook; a missing phone record, call history or flag list blocks the pre-call check; an unknown ZIP and area code blocks the call; an unreadable debt row ends the call after the disclosure with no amount; a failed save on a stop still hangs up; a failed tool save replaces the model's reply with a fixed line and hangs up; a dispute whose status cannot be read is treated as a dispute. Each has a test.

**What does `TEST_NUMBERS` do?** It is an owner-only list of full phone numbers, empty by default, for testing on your own phone outside calling hours and past the call limit. It skips exactly those two rules for exactly those numbers. Consent, stop requests, do-not-call and debt status still block. Every skip is audited. It is not for real debtors.

**Does the model ever decide anything legal?** No. It writes words in one stage, `DISCUSS`, after code has verified the person and said the disclosure. Every sentence it writes is checked, and its tools are gated. After a call, reviewer models can flag a transcript for a person, but they decide nothing.

**Is it compliant?** Nobody has said so. It is an engineering project that makes the rules easy to read, test and audit. It has not been reviewed by counsel, it has no state rules, and the compliance page lists what is missing.

**What is not done yet?** Stated in [compliance.md](compliance.md) in full. In short: no per-state rules; a small hand-made time zone table; no validation notice or time-barred-debt logic; no classifier layer; nothing detects an answering machine during a call; whether AgentPhone records or can skip recording per call is unconfirmed, and so is how its transfer and concurrency limit behave; no sign-in on the web app; the audit chain has no external anchor; and the system has not been run with real calls to real people.

**Can it make a real call?** The dialer can, with AgentPhone credentials and `uv run dialer --confirm`, and the webhook answers real AgentPhone turns. The demo here is simulated, and a real-call run is not part of this script.
