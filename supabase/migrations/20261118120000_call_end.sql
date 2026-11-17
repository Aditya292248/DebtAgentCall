-- What happens when a call ends.
--
-- `call_attempt.ended_at` is set once, when the end of the call is recorded
-- (by the `agent.call_ended` webhook or by `uv run reconcile`). Null means no
-- end was recorded yet; that is how a second end event is recognised and
-- changes nothing, and how the reconciler finds attempts that never ended.
-- `end_source` says which of the two recorded it.
--
-- New outcomes, set only from what the saved call state shows:
--   refused         identity could not be confirmed, or the caller declined
--   ended_by_stop   a stop trigger ended the call
--   third_party     someone other than the debtor answered
--   abandoned       the caller spoke but the call ended before verification
--   ended_unknown   the end could not be explained; counted as no conversation
--                   and written to the audit log for a person to look at

alter table public.call_attempt add column ended_at timestamptz;
alter table public.call_attempt add column end_source text
    check (end_source in ('webhook', 'reconciler'));

alter table public.call_attempt drop constraint call_attempt_outcome_check;
alter table public.call_attempt add constraint call_attempt_outcome_check
    check (outcome in (
        'no_answer', 'busy', 'voicemail', 'answered', 'failed',
        'placing', 'placed', 'placed_unknown',
        'throttled', 'concurrency_limit', 'number_busy', 'no_funds',
        'refused', 'ended_by_stop', 'third_party', 'abandoned', 'ended_unknown'
    ));

-- Episodic memory: a short summary of each finished call, so the next call
-- about the same debt knows how the last one went. `facts` is built by code
-- from the saved state and the rows the call's tools saved; `narrative` is a
-- model's one paragraph, kept only if code checked it against the database
-- (null when it failed the check or was not asked for). One row per call.

create table public.call_summary (
    id uuid primary key default gen_random_uuid(),
    call_id text not null unique,
    debt_id uuid not null references public.debt (id) on delete cascade,
    created_at timestamptz not null default now(),
    outcome text not null,
    facts jsonb not null,
    facts_text text not null,
    narrative text
);

create index call_summary_debt_id_created_at_idx on public.call_summary (debt_id, created_at desc);

alter table public.call_summary enable row level security;

create policy "authenticated can read call summaries" on public.call_summary
    for select to authenticated using (true);
