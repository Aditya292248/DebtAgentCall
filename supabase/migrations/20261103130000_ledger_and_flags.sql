-- The call ledger and the flags that stop calls.
--
-- `call_attempt` has one row per call we placed, answered or not. The pre-call
-- checks count these for the Reg F limit (7 calls in 7 days, and none for 7
-- days after a conversation).
--
-- `flags` holds the facts that mean "don't call": the debtor asked us to stop,
-- has a lawyer, filed for bankruptcy, has died, opted one number out, or a
-- number is on a do-not-call list. A flag with no `debt_id` covers every debt
-- of that debtor. `data` holds details, such as `{"e164": "+1..."}` for a flag
-- that is about one phone number.
--
-- Row level security is on with only a read policy for signed-in users, the
-- same as the other tables.

create table public.call_attempt (
    id uuid primary key default gen_random_uuid(),
    debt_id uuid not null references public.debt (id) on delete cascade,
    phone_id uuid references public.phone (id) on delete set null,
    placed_at timestamptz not null,
    outcome text not null
        check (outcome in ('no_answer', 'busy', 'voicemail', 'answered', 'failed')),
    connected boolean not null default false,
    conversation_with_debtor boolean not null default false,
    -- We can only have talked with the debtor if the call connected.
    check (connected or not conversation_with_debtor)
);

create table public.flags (
    id uuid primary key default gen_random_uuid(),
    debtor_id uuid not null references public.debtor (id) on delete cascade,
    debt_id uuid references public.debt (id) on delete cascade,
    kind text not null
        check (kind in ('cease', 'attorney', 'bankruptcy', 'deceased', 'opt_out_phone', 'dnc')),
    data jsonb not null default '{}'::jsonb,
    set_at timestamptz not null default now(),
    source text not null
);

create index call_attempt_debt_id_placed_at_idx on public.call_attempt (debt_id, placed_at);
create index call_attempt_phone_id_idx on public.call_attempt (phone_id);
create index flags_debtor_id_idx on public.flags (debtor_id);
create index flags_debt_id_idx on public.flags (debt_id);

alter table public.call_attempt enable row level security;
alter table public.flags enable row level security;

create policy "authenticated can read call attempts" on public.call_attempt
    for select to authenticated using (true);
create policy "authenticated can read flags" on public.flags
    for select to authenticated using (true);
