# Contributing

Thanks for looking at DebtAgentCall. This project is a collection agent that puts compliance first, built so that the rules around a call are easy to read, test and trust. Please read the compliance section below before changing anything near a call.

All data in the repository is made up. Never add real names, phone numbers, account numbers or recordings to code, tests, issues or pull requests.

## Set up

You need [uv](https://docs.astral.sh/uv/), Python 3.11 or newer, Node.js (the version in `.github/workflows/ci.yml` is known to work), and Docker Desktop running for the local database.

```bash
uv sync
```

Copy the example settings and fill in only what you need:

```bash
cp .env.example .env
```

On Windows PowerShell use `Copy-Item .env.example .env`. `.env` is ignored by git; never commit it. Real environment variables win over the file. The webhook secret, the AgentPhone keys and the model settings are all optional for offline work: `uv run verify` never touches a real model or phone.

Start the local database (Supabase, on port 54322) and apply the migrations:

```bash
npx supabase start
npx supabase migration up
```

A fresh start is an empty database. `uv run seed-demo` loads made-up people from `supabase/demo_seed.sql` when you want them (it refuses a database that holds anyone else). `npx supabase db reset` rebuilds the local database empty and **deletes everything in it**, so run it only on a database you can lose. Tests never use this database: they create and use their own `debtagentcall_test` and abort unless the name ends in `_test`. Never run migrations against a hosted project that holds real data.

## Check your work

Run the offline gate before every push. It runs the format check, lint, types, tests and a smoke import:

```bash
uv run verify
```

Tests marked `db` need the local database. Without it they are skipped, so `uv run verify` still works offline; CI starts the database and sets `REQUIRE_DATABASE=1`, which turns that skip into a failure. To run only those tests:

```bash
uv run pytest -m db -v
```

If you touch the web UI, run the same checks CI runs, from the `web/` folder:

```bash
cd web
npm ci
npx tsc -b
npm run lint
npm test
npm run build
```

Other commands (`uv run serve`, `uv run api`, `uv run dialer --dry-run`, `uv run audit-verify`, `uv run demo ...`) are described in [docs/reference.md](docs/reference.md#commands). Commands that call a real model say so there; use them only when you need them.

### Windows notes

If Application Control or antivirus blocks the launchers that uv generates (for example `audit-verify.exe` or `api.exe`), run the module instead:

```bash
uv run python -m tasks.audit_verify
uv run python -m harness.api
```

If something still does not work on Windows, use WSL.

## One topic per pull request

Keep each pull request to one topic, and include at least one negative test: a test that fails when the behavior you added is missing, or that shows a bad input being refused. A change with only happy-path tests is not finished.

Open pull requests against `main`. Do not stack pull requests on each other, and do not mix unrelated clean-ups into a change.

## How compliance rules work

A compliance rule is a plain check in `compliance/`. It is ordinary Python that looks at facts (the time, the call history, consent, flags on the debt) and returns an allow or a block with a reason. Rules follow these points:

- **Fail closed.** If a fact is missing, unreadable or unknown, the check blocks. It never guesses that a call is fine.
- **The model never decides.** The language model writes words; it does not decide whether a call may happen or what a rule means. Every gate is code that runs before the model is involved.
- **Every decision is logged.** Decisions are written to the append-only audit log in the database. `uv run audit-verify` checks the hash chain.
- **Every rule has a test that fails without it.** Add the test first, see it fail, then add the rule.

This is software, not legal advice. A rule change should say which requirement it is meant to meet, so a reviewer can check the intent.

## Commit and pull request style

- Short, plain titles that say what changed, for example "Block calls to numbers on the do-not-call list". No marketing words, no emoji.
- Commit messages are plain prose: what changed and why, in a sentence or two.
- A pull request description is two plain sentences: what the change does, and why. Add one line saying how you checked the negative case.
- Do not commit secrets, `.env` files, local tool settings, or unrelated lockfile changes.

## Reporting problems

Use the issue templates for bugs and ideas. For a security problem, do not open a public issue; follow [SECURITY.md](SECURITY.md).

By contributing you agree that your work is released under the [Apache License 2.0](LICENSE). Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
