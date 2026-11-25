-- How long each live caller turn took on our side.
--
-- One row per `agent.message` voice turn, written after the reply has gone out.
-- Numbers, flags and ids only: no transcript, no phone number, no name, no model
-- output. `call_id` is AgentPhone's id for the call, `turn_seq` the caller turn
-- (the saved state's turn count). `uv run latency-report` reads this table.
--
-- kind: fixed (no model call), model (the model answered), stop (a stop trigger
-- closed the call), blocked (a sentence was blocked, so a fixed line was used),
-- failure (a tool or save failed, so a fixed line and hang up), replay (a retry
-- answered from the saved reply).

create table public.turn_timing (
    id uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    call_id text not null,
    turn_seq integer,
    stage text,
    kind text not null
        check (kind in ('fixed', 'model', 'stop', 'blocked', 'failure', 'replay')),
    total_ms real not null,
    verified_ms real,
    triage_ms real,
    first_line_ms real,
    model_ms real not null default 0,
    model_calls integer not null default 0,
    model_first_ms real,
    tool_ms real not null default 0,
    tool_calls integer not null default 0,
    tools jsonb not null default '{}'::jsonb,
    hold_line boolean not null default false,
    streamed boolean not null default false
);

create index turn_timing_created_at_idx on public.turn_timing (created_at);
create index turn_timing_call_id_idx on public.turn_timing (call_id);

alter table public.turn_timing enable row level security;
