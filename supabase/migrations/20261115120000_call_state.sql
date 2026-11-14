-- Call stages: each call's state is saved as a `state` row in `call_message`.
--
-- `harness/call_state.py` writes one after every caller turn: the stage
-- (GREETING, VERIFY, DISCLOSE, DISCUSS, WRAP, THIRD_PARTY, STOPPED) and whether
-- the person was verified and the disclosure was said. The newest row wins.
-- Keeping it next to the call's messages means one place to read on every turn,
-- and it survives a restart like the messages do.

alter table public.call_message drop constraint call_message_kind_check;
alter table public.call_message add constraint call_message_kind_check
    check (kind in ('session', 'message', 'compaction', 'state'));
