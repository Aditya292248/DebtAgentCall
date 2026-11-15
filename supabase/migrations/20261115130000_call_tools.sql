-- Call tools: what the voice agent's tools write during a call.
--
-- `plan_policy` is what a payment plan needs to be approved on the call without
-- a person: a minimum down payment (percent of the balance) and a longest plan.
-- A row with no creditor is the default; `harness/call_tools.py` falls back to
-- its own default when the table is empty.
--
-- `payment_plan` holds every plan offered: `approved` inside the policy,
-- `pending` outside it, waiting for a specialist. `callback`, `dispute` and
-- `escalation` hold the other tools' requests. Each row names the call and debt.
-- Logging a dispute also sets `debt.status` to `disputed`, so later turns and
-- later calls see it.
--
-- Row level security is on with only a read policy for signed-in users, the
-- same as the other tables.

create table public.plan_policy (
    id uuid primary key default gen_random_uuid(),
    creditor text unique,
    min_down_pct integer not null check (min_down_pct between 0 and 100),
    max_months integer not null check (max_months > 0),
    max_installments integer not null check (max_installments > 0)
);

create table public.payment_plan (
    id uuid primary key default gen_random_uuid(),
    call_id text not null,
    debt_id uuid not null references public.debt (id) on delete cascade,
    down_cents bigint not null check (down_cents >= 0),
    installments integer not null check (installments > 0),
    frequency text not null check (frequency in ('weekly', 'biweekly', 'monthly')),
    installment_cents bigint not null check (installment_cents >= 0),
    status text not null check (status in ('approved', 'pending', 'confirmed', 'rejected')),
    created_at timestamptz not null default now()
);

create table public.callback (
    id uuid primary key default gen_random_uuid(),
    call_id text not null,
    debt_id uuid not null references public.debt (id) on delete cascade,
    scheduled_at timestamptz not null,
    tz text not null,
    created_at timestamptz not null default now()
);

create table public.dispute (
    id uuid primary key default gen_random_uuid(),
    call_id text not null,
    debt_id uuid not null references public.debt (id) on delete cascade,
    reason text not null,
    created_at timestamptz not null default now()
);

create table public.escalation (
    id uuid primary key default gen_random_uuid(),
    call_id text not null,
    debt_id uuid not null references public.debt (id) on delete cascade,
    reason text not null,
    created_at timestamptz not null default now()
);

-- At most one default policy, so the fallback is never a coin toss.
create unique index plan_policy_one_default_idx on public.plan_policy ((true))
    where creditor is null;

create index payment_plan_debt_id_idx on public.payment_plan (debt_id);
create index callback_debt_id_idx on public.callback (debt_id);
create index dispute_debt_id_idx on public.dispute (debt_id);
create index escalation_debt_id_idx on public.escalation (debt_id);

alter table public.plan_policy enable row level security;
alter table public.payment_plan enable row level security;
alter table public.callback enable row level security;
alter table public.dispute enable row level security;
alter table public.escalation enable row level security;

create policy "authenticated can read plan policies" on public.plan_policy
    for select to authenticated using (true);
create policy "authenticated can read payment plans" on public.payment_plan
    for select to authenticated using (true);
create policy "authenticated can read callbacks" on public.callback
    for select to authenticated using (true);
create policy "authenticated can read disputes" on public.dispute
    for select to authenticated using (true);
create policy "authenticated can read escalations" on public.escalation
    for select to authenticated using (true);

insert into public.plan_policy (creditor, min_down_pct, max_months, max_installments)
values (null, 10, 12, 52);
