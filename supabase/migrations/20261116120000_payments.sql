-- Payments: an append-only ledger of money received on a debt, and the one
-- place the current balance is computed.
--
-- `debt.balance_cents` stays what it always was: the amount the debt was placed
-- with. It is never changed to reflect a payment. The balance owed now is
-- `public.current_balance_cents(debt)`:
--
--     balance_cents - sum(payments) + sum(reversals)
--
-- Every read of the balance that is shown or spoken (the call tools, the
-- sentence filter's known amounts, `lookup_account`, the web list) goes through
-- that function. Because it is a function of a `debt` row, the Supabase API also
-- offers it as a computed column: `debt(..., current_balance_cents)`.
--
-- `payment` rows are never changed or removed. A correction is a new row whose
-- `reverses_payment_id` names the payment it cancels, with the same amount. What
-- the database enforces:
--   * Triggers refuse every `update`, `delete` and `truncate`, and those
--     privileges are revoked from every role, the same as `audit_event`.
--   * A `before insert` trigger locks the debt row, so two payments at once
--     can't both pass the balance check, and then refuses a payment above the
--     current balance, one dated before the debt was opened or more than a day
--     past today in UTC (no time zone is that far ahead), and any reversal that
--     doesn't name a payment (not itself a reversal) on the same debt for the
--     same amount. A payment can be reversed at most once.
--   * A debt with payments can't be deleted (a `before delete` trigger on
--     `debt`; there are no foreign keys, see the table).
--
-- `debt.opened_on` is the day the debt was placed with us; no payment can be
-- dated before it. Debts that existed before this migration get the day it ran,
-- so a backdated payment on one of them is refused until a person sets the real
-- date. The seed sets it for the made-up debts.

alter table public.debt add column opened_on date not null default current_date;

create table public.payment (
    id uuid primary key default gen_random_uuid(),
    -- No foreign keys on this table: Postgres checks one by locking the
    -- referenced or referencing payment row, which needs the update privilege
    -- revoked below. Triggers do the same checks instead: the insert trigger
    -- requires the debt (and a reversal's payment) to exist, and a debt with
    -- payments can't be deleted.
    debt_id uuid not null,
    amount_cents bigint not null check (amount_cents > 0),
    paid_on date not null,
    method text not null check (method in ('cash', 'check', 'card', 'ach', 'other')),
    recorded_at timestamptz not null default now(),
    recorded_by text not null check (recorded_by <> ''),
    reverses_payment_id uuid,
    reason text,
    -- A reversal says why; a payment needs no reason.
    check (reverses_payment_id is null or coalesce(reason, '') <> '')
);

create index payment_debt_id_paid_on_idx on public.payment (debt_id, paid_on);
create unique index payment_reversed_once_idx on public.payment (reverses_payment_id)
    where reverses_payment_id is not null;

create function public.current_balance_cents(d public.debt) returns bigint
language sql
stable
set search_path = ''
as $$
    select d.balance_cents - coalesce((
        select pg_catalog.sum(
            case when p.reverses_payment_id is null then p.amount_cents else -p.amount_cents end
        )
        from public.payment p
        where p.debt_id = d.id
    ), 0)::bigint
$$;

create function public.payment_check_insert() returns trigger
language plpgsql
set search_path = ''
as $$
declare
    debt public.debt;
    reversed public.payment;
begin
    -- Held until commit, so the balance read here is the one the row commits against.
    select * into debt from public.debt where id = new.debt_id for update;
    if not found then
        raise exception 'payment: no debt %', new.debt_id using errcode = 'foreign_key_violation';
    end if;
    if new.paid_on < debt.opened_on then
        raise exception 'payment: paid_on % is before the debt was opened (%)',
            new.paid_on, debt.opened_on using errcode = 'check_violation';
    end if;
    if new.paid_on > (pg_catalog.now() at time zone 'UTC')::date + 1 then
        raise exception 'payment: paid_on % is in the future', new.paid_on
            using errcode = 'check_violation';
    end if;
    if new.reverses_payment_id is null then
        if new.amount_cents > public.current_balance_cents(debt) then
            raise exception 'payment: % cents is more than the balance of % cents',
                new.amount_cents, public.current_balance_cents(debt)
                using errcode = 'check_violation';
        end if;
    else
        select * into reversed from public.payment where id = new.reverses_payment_id;
        if not found or reversed.debt_id <> new.debt_id
           or reversed.reverses_payment_id is not null
           or reversed.amount_cents <> new.amount_cents then
            raise exception 'payment: a reversal must name a payment on the same debt, '
                'for the same amount' using errcode = 'check_violation';
        end if;
    end if;
    new.recorded_at := pg_catalog.now();
    return new;
end
$$;

create function public.payment_refuse_change() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    raise exception 'payment is append-only: % is not allowed', tg_op
        using errcode = 'insufficient_privilege',
              hint = 'Record a reversing payment instead of changing an old one.';
end
$$;

create function public.debt_keep_if_paid() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if exists (select 1 from public.payment p where p.debt_id = old.id) then
        raise exception 'debt % has payments and can''t be deleted', old.account_number
            using errcode = 'foreign_key_violation';
    end if;
    return old;
end
$$;

create trigger debt_keep_if_paid
    before delete on public.debt
    for each row execute function public.debt_keep_if_paid();

create trigger payment_check_insert
    before insert on public.payment
    for each row execute function public.payment_check_insert();

create trigger payment_no_update_or_delete
    before update or delete on public.payment
    for each row execute function public.payment_refuse_change();

create trigger payment_no_truncate
    before truncate on public.payment
    for each statement execute function public.payment_refuse_change();

revoke update, delete, truncate on public.payment
    from public, anon, authenticated, service_role, postgres;

alter table public.payment enable row level security;

create policy "authenticated can read payments" on public.payment
    for select to authenticated using (true);
