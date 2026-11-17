-- Notes: what an operator wrote about a debt, kept forever.
--
-- `debt_note` is append-only like `payment` and `audit_event`: triggers refuse
-- every `update`, `delete` and `truncate`, and those privileges are revoked
-- from every role. A correction is a new note. The operator chat adds one only
-- after the operator approves it (`add_note` in harness/operator.py).
--
-- No foreign key, for the same reason as `payment`: Postgres checks one by
-- locking the note rows, which needs the revoked update privilege. The insert
-- trigger requires the debt to exist, and a debt with notes can't be deleted.

create table public.debt_note (
    id uuid primary key default gen_random_uuid(),
    debt_id uuid not null,
    body text not null check (length(btrim(body)) between 1 and 2000),
    created_at timestamptz not null default now(),
    created_by text not null check (created_by <> '')
);

create index debt_note_debt_id_idx on public.debt_note (debt_id, created_at);

create function public.debt_note_check_insert() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if not exists (select 1 from public.debt d where d.id = new.debt_id) then
        raise exception 'debt_note: no debt %', new.debt_id using errcode = 'foreign_key_violation';
    end if;
    new.created_at := pg_catalog.now();
    return new;
end
$$;

create function public.debt_note_refuse_change() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    raise exception 'debt_note is append-only: % is not allowed', tg_op
        using errcode = 'insufficient_privilege',
              hint = 'Add a new note instead of changing an old one.';
end
$$;

create function public.debt_keep_if_noted() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if exists (select 1 from public.debt_note n where n.debt_id = old.id) then
        raise exception 'debt % has notes and can''t be deleted', old.account_number
            using errcode = 'foreign_key_violation';
    end if;
    return old;
end
$$;

create trigger debt_note_check_insert
    before insert on public.debt_note
    for each row execute function public.debt_note_check_insert();

create trigger debt_note_no_update_or_delete
    before update or delete on public.debt_note
    for each row execute function public.debt_note_refuse_change();

create trigger debt_note_no_truncate
    before truncate on public.debt_note
    for each statement execute function public.debt_note_refuse_change();

create trigger debt_keep_if_noted
    before delete on public.debt
    for each row execute function public.debt_keep_if_noted();

revoke update, delete, truncate on public.debt_note
    from public, anon, authenticated, service_role, postgres;

alter table public.debt_note enable row level security;

create policy "authenticated can read debt notes" on public.debt_note
    for select to authenticated using (true);
