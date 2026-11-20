-- Loaded on every local `supabase start` / `supabase db reset` (see config.toml).
-- Holds no people and no debts: a fresh start is an empty database.
--
-- Read-only access for the local web UI (web/), which signs in as nobody and uses
-- the anon key. This lives here, not in a migration, on purpose: this file
-- only runs on a local `supabase db reset`, and `supabase db push` never sends it
-- to a hosted project, where anon read access would expose every debtor.
-- Only `select` on the tables the UI reads; anon still can't write anything.
-- `payment` is read by `current_balance_cents`, the balance the list shows.
create policy "anon can read debtors (local dev)" on public.debtor
    for select to anon using (true);
create policy "anon can read phones (local dev)" on public.phone
    for select to anon using (true);
create policy "anon can read debts (local dev)" on public.debt
    for select to anon using (true);
create policy "anon can read audit events (local dev)" on public.audit_event
    for select to anon using (true);
create policy "anon can read payments (local dev)" on public.payment
    for select to anon using (true);
