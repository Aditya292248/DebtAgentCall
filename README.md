# DebtAgentCall

[![CI](https://github.com/Aditya292248/DebtAgentCall/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Aditya292248/DebtAgentCall/actions/workflows/ci.yml)

DebtAgentCall is a local prototype for a debt-collection voice agent. Ask the operator chat to call a fictional account and you can inspect the checks for calling hours, phone consent, recent call attempts, stop requests and do-not-call. If the checks pass, the operator can approve the simulated call. Approval runs the checks again and records every decision in an audit log.

The checks are plain Python, not model output: the model talks, the code decides. They cover the federal rules listed in [docs/compliance.md](docs/compliance.md) and nothing more. There are no state rules, nothing has been reviewed by a lawyer, and the project has not made calls to real people. It is not legal advice and not ready for production.

**Try it in about ten minutes, with no accounts or API keys:** a local database with nine fictional people, a scripted model and a simulated phone. Type `call Morgan Patel` and the chat shows the call blocked by the stop-request rule, with an approval card that offers only Deny. Nothing is dialed.

## Try the local demo

You need [Git](https://git-scm.com/), [uv](https://docs.astral.sh/uv/) (it installs Python 3.11 or newer if you don't have it), [Node.js](https://nodejs.org/) 20.19 or newer, and [Docker Desktop](https://www.docker.com/products/docker-desktop/), running. `npx` fetches the Supabase CLI, so there is nothing else to install.

This demo uses no model server and no phone account. **Don't copy the root `.env.example` to `.env`** for it: that file sets `LLM_BASE_URL`, and when that variable is set the demo talks to that model server instead of the scripted model. If you already have a `.env` with `LLM_BASE_URL`, comment that line out first.

**Terminal 1, in the repository root:**

```bash
git clone https://github.com/Aditya292248/DebtAgentCall.git
cd DebtAgentCall
uv sync --locked
npx supabase start
```

The first `npx supabase start` downloads the Supabase images and takes a few minutes. It starts Postgres on port 54322 and the Supabase API on port 54321. The database starts empty, with no people in it. Load the fictional people:

```bash
uv run seed-demo
```

It should print `demo data: loaded`. `uv run seed-demo --check` confirms all nine are there. (It refuses a database that already holds other people, so it can't mix sample data into real data.) Then print the local keys, and keep the `ANON_KEY` value (without the quotes) for the web app:

```bash
npx supabase status -o env
```

Start the demo API, and leave it running:

```bash
uv run demo part-24
```

It should print `model: fake (set LLM_BASE_URL)` and `phone: fake, so an approved call rings nobody`, then serve the API on http://127.0.0.1:8000.

**Terminal 2, in `web/`:** the web app has its own settings file, separate from the root one.

```bash
cd web
cp .env.example .env.local
```

(On Windows PowerShell, use `Copy-Item .env.example .env.local`.) Open `web/.env.local` and replace `paste-the-anon-key-from-supabase-status` with the `ANON_KEY` value from above. Leave `VITE_SUPABASE_URL=http://127.0.0.1:54321` as it is. Then:

```bash
npm ci
npm run dev
```

Open http://localhost:5173. The debtor list is on the left and the operator chat on the right.

### A blocked call

Type this in the chat:

```text
call Morgan Patel
```

The scripted model runs three tools, shown as collapsed lines: `find_debtor`, `check_call` and `place_call`. Then it replies that nothing has been dialed. You should see:

- A `blocked: Stop requests` badge on the `check_call` line. Open that line to see every rule with its result and reason. The stop-request rule (`cease`) reads `debtor asked us to stop on 2026-10-15`. At night in Texas, `Calling hours` is blocked as well.
- A "Call waiting for your approval" card for Morgan Patel (ACC-1007), with the phone number masked to the last four digits. It says "The checks block this call, so it can’t be approved." and has only a Deny button.

This result doesn't depend on when you try it: the stop request in the sample data never expires. Other people in the sample data are blocked for other reasons: Riley Chen (consent revoked), Taylor Brooks (no consent on file), Parker Reed (asked us to stop calling that number) and Avery Ortiz (on a do-not-call list).

### A call you can approve

```text
call Sam Doe
```

Sam Doe has consent on file and no recent calls, so the only rule that can block is calling hours: 08:00 to 21:00 in Los Angeles, where Sam's ZIP code and area code are. Inside those hours every rule passes and the card shows Approve. Outside them the card has only Deny.

Clicking Approve runs every check again on the server, with the debt row locked, and writes each decision to the audit log. Only if they all still pass does it record a call attempt and hand the call to the simulated phone, which rings nobody. Approve also only shows while the check is less than five minutes old. Open **Compliance log** in the top bar to see the rows that were written.

Each approval counts as a call attempt, so after seven approvals in seven days Sam Doe is blocked by the call limit (`Call limit (7 in 7 days)`). Casey Morgan's sample history has seven calls in early November 2026. That person was blocked by the call limit on the day the sample data was built, but the attempts have since aged out, so today the only rule that can block that call is calling hours.

### Stopping and starting over

Press Ctrl+C in each terminal, then run `npx supabase stop` from the repository root. To get back to a clean set of sample people, run `npx supabase db reset` and then `uv run seed-demo`. **`db reset` deletes everything in the local database**, so don't run it on a database that holds anything you want to keep.

For a longer walkthrough to present to an audience, with the dialer's dry run, a simulated phone call and the audit log, see [docs/demo.md](docs/demo.md).

## How a call works

1. **The operator asks.** In the chat, the operator agent (`harness/operator.py`) finds the person and runs `check_call`, which writes nothing. Its `place_call` tool never dials. It only opens an approval card.
2. **A person approves.** Only the Approve button reaches the dialer (`dialer/service.py`). The dialer locks the debt row, runs every pre-call check again, requires the debt to be active, and writes each decision to the audit log. Then, if everything passed, it records the attempt and asks the phone provider to place the call, once. Nothing is retried, so a person is never rung twice.
3. **The call runs on code-decided stages.** During a real call, [AgentPhone](https://docs.agentphone.ai) does the speech-to-text and text-to-speech and sends each caller turn to the webhook (`phone/webhook.py`). Code checks for stop requests before the model sees the turn, verifies identity, and says the required disclosure. Every sentence the model writes passes a filter before it is spoken.
4. **Everything is recorded.** Decisions go to `audit_event`, an append-only table with a hash chain that `uv run audit-verify` checks.

The demo above covers steps 1 and 2 with the simulated phone. `uv run demo part-13` plays scripted calls through steps 3 and 4. The human-approval step applies to the operator chat. The standalone dialer CLI (`uv run dialer --dry-run` to preview, `--confirm` with AgentPhone keys to place calls) runs the same checks with no chat and no approval card.

For the components, the call flow diagram and the data model, see [docs/architecture.md](docs/architecture.md). For each rule, its source, its test and its limits, see [docs/compliance.md](docs/compliance.md).

## Development and further reading

```bash
uv run verify
```

`uv run verify` runs the format check, lint, type check, tests and an import check, with no model and no phone. The tests that need a database create their own `debtagentcall_test` database on the local Supabase server and never touch the one the app uses. When the server isn't running, those tests are skipped.

- [CONTRIBUTING.md](CONTRIBUTING.md): setup for contributors, the compliance rules for changes, and what to do if Windows blocks the generated `.exe` launchers
- [docs/reference.md](docs/reference.md): the feature-by-feature reference (model settings, the agent loop, the database, each rule, the webhook, the web UI, the dialer) and the [full command list](docs/reference.md#commands)
- [docs/architecture.md](docs/architecture.md): components, the call flow and the data model
- [docs/compliance.md](docs/compliance.md): every rule the code enforces, and what is not handled
- [docs/demo.md](docs/demo.md): a 6 to 7 minute presentation walkthrough
- [docs/adding-people-by-chat.md](docs/adding-people-by-chat.md) and [docs/latency.md](docs/latency.md)
- [SECURITY.md](SECURITY.md): reporting a vulnerability

All data in this repository is made up. Never add real names, phone numbers or account details.

## Credits

The `model/` and `harness/` code is adapted from codingHarness, keeping only what each step needs.
