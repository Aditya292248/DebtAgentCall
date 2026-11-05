-- The audit log: one row for every compliance decision, in a chain nobody can edit.
--
-- Each row stores a `hash` of the row before it (`prev_hash`) plus its own
-- fields, so changing or removing any old row breaks every hash after it.
-- `uv run audit-verify` recomputes the chain in Python and reports where.
--
-- What the database enforces:
--   * A `before insert` trigger takes a lock, picks the next `seq`, stamps `ts`,
--     and fills in `prev_hash` and `hash`. Whatever the caller sent for those
--     four columns is ignored, and two inserts at once can't fork the chain.
--     `seq` is numbered here rather than by a sequence, so a rolled-back insert
--     doesn't leave a gap.
--   * Triggers refuse every `update`, `delete` and `truncate`.
--   * `update`, `delete` and `truncate` are revoked from every role, the app's
--     own `postgres` login included. Corrections are new rows.
--   * Row level security is on, with read access for signed-in users only.
--
-- The hash, which `compliance/audit.py` repeats exactly:
--   hash = sha256(prev_hash || canonical) as lowercase hex, where `canonical` is
--   the JSON array [seq, ts, call_id, debt_id, kind, rule_id, decision, reason,
--   evidence] as Postgres prints it: ts in UTC as 2026-11-05T19:00:00.000000Z,
--   debt_id as text, and evidence as the text of its jsonb value. The first
--   row's prev_hash is 64 zeros.

create table public.audit_event (
    seq bigint primary key,
    ts timestamptz not null default now(),
    call_id text,
    debt_id uuid,  -- no foreign key: the log must outlive the rows it talks about
    kind text not null,
    rule_id text not null,
    decision text not null check (decision in ('allow', 'block')),
    reason text not null,
    evidence jsonb not null default '{}'::jsonb,
    prev_hash text not null,
    hash text not null unique
);

create index audit_event_debt_id_idx on public.audit_event (debt_id);
create index audit_event_call_id_idx on public.audit_event (call_id);

create function public.audit_event_canonical(
    seq bigint,
    ts timestamptz,
    call_id text,
    debt_id uuid,
    kind text,
    rule_id text,
    decision text,
    reason text,
    evidence jsonb
) returns text
language sql
immutable
set search_path = ''
as $$
    select pg_catalog.jsonb_build_array(
        seq,
        pg_catalog.to_char(ts at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        call_id,
        debt_id::text,
        kind,
        rule_id,
        decision,
        reason,
        evidence::text
    )::text
$$;

create function public.audit_event_chain() returns trigger
language plpgsql
set search_path = ''
as $$
declare
    last_seq bigint;
    last_hash text;
begin
    -- One writer at a time, held until commit, so each insert sees the last one.
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('public.audit_event'));

    select a.seq, a.hash into last_seq, last_hash
    from public.audit_event a
    order by a.seq desc
    limit 1;

    new.seq := coalesce(last_seq, 0) + 1;
    new.ts := pg_catalog.now();
    new.evidence := coalesce(new.evidence, '{}'::jsonb);
    new.prev_hash := coalesce(last_hash, pg_catalog.repeat('0', 64));
    new.hash := pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(
            new.prev_hash || public.audit_event_canonical(
                new.seq, new.ts, new.call_id, new.debt_id, new.kind,
                new.rule_id, new.decision, new.reason, new.evidence
            ),
            'UTF8'
        )),
        'hex'
    );
    return new;
end
$$;

create function public.audit_event_refuse_change() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    raise exception 'audit_event is append-only: % is not allowed', tg_op
        using errcode = 'insufficient_privilege',
              hint = 'Record a new event instead of changing an old one.';
end
$$;

create trigger audit_event_chain
    before insert on public.audit_event
    for each row execute function public.audit_event_chain();

create trigger audit_event_no_update_or_delete
    before update or delete on public.audit_event
    for each row execute function public.audit_event_refuse_change();

create trigger audit_event_no_truncate
    before truncate on public.audit_event
    for each statement execute function public.audit_event_refuse_change();

revoke update, delete, truncate on public.audit_event
    from public, anon, authenticated, service_role, postgres;

alter table public.audit_event enable row level security;

create policy "authenticated can read audit events" on public.audit_event
    for select to authenticated using (true);
