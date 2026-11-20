-- Archiving a person instead of deleting them.
--
-- Debt-collection records are kept for three years (Reg F, 12 CFR 1006.100), and
-- the audit log and call ledger are append-only, so removing a person from the
-- web UI sets archived_at and nothing else. An archived person is blocked by
-- the pre-call checks and leaves the default list; history stays as it was.

alter table public.debtor add column archived_at timestamptz;

create index debtor_archived_at_idx on public.debtor (archived_at)
    where archived_at is not null;
