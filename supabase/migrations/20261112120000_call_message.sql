-- Each call's conversation, one row per record, so every webhook turn can see
-- the turns before it.
--
-- `harness/memory.py` writes and reads these rows (`PostgresSession`). A call's
-- history is its rows in `seq` order, replayed: a `session` row first, then a
-- `message` row per message, and a `compaction` row whenever the history was
-- replaced by a shorter one. Rows are only ever added.
--
-- `call_id` is AgentPhone's id for the call. There is no foreign key: a call's
-- messages arrive before anything else about the call is stored.
--
-- Row level security is on with only a read policy for signed-in users, the
-- same as the other tables.

create table public.call_message (
    seq bigserial primary key,
    call_id text not null,
    kind text not null check (kind in ('session', 'message', 'compaction')),
    body jsonb not null,
    created_at timestamptz not null default now()
);

create index call_message_call_id_seq_idx on public.call_message (call_id, seq);

alter table public.call_message enable row level security;

create policy "authenticated can read call messages" on public.call_message
    for select to authenticated using (true);
