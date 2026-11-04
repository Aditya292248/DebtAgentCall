-- Debtors, their phone numbers, and what they owe.
--
-- Row level security is on for all three tables, with only a read policy for
-- signed-in (`authenticated`) users. The server connects to Postgres directly
-- as the database owner, which RLS does not restrict.

create table public.debtor (
    id uuid primary key default gen_random_uuid(),
    first_name text not null,
    last_name text not null,
    dob date not null,
    address text not null,
    zip text not null,
    state text not null check (state ~ '^[A-Z]{2}$')
);

create table public.phone (
    id uuid primary key default gen_random_uuid(),
    debtor_id uuid not null references public.debtor (id) on delete cascade,
    e164 text not null check (e164 ~ '^\+[1-9][0-9]{1,14}$'),
    line_type text not null check (line_type in ('cell', 'landline')),
    consent_source text,
    consent_at timestamptz,
    revoked_at timestamptz
);

create table public.debt (
    id uuid primary key default gen_random_uuid(),
    debtor_id uuid not null references public.debtor (id) on delete cascade,
    -- The account number people see and say on the phone, e.g. ACC-1001.
    account_number text not null unique,
    creditor text not null,
    balance_cents bigint not null check (balance_cents >= 0),
    status text not null default 'active'
        check (status in ('active', 'disputed', 'paused', 'closed'))
);

create index phone_debtor_id_idx on public.phone (debtor_id);
create index debt_debtor_id_idx on public.debt (debtor_id);

alter table public.debtor enable row level security;
alter table public.phone enable row level security;
alter table public.debt enable row level security;

create policy "authenticated can read debtors" on public.debtor
    for select to authenticated using (true);
create policy "authenticated can read phones" on public.phone
    for select to authenticated using (true);
create policy "authenticated can read debts" on public.debt
    for select to authenticated using (true);
