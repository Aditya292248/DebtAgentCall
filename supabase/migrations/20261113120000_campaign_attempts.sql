-- What the campaign runner needs from the call ledger.
--
-- The runner writes a `call_attempt` row *before* it asks AgentPhone to dial,
-- because AgentPhone has no idempotency key: if we crash or time out after the
-- request, the row is what stops us from calling twice. So an attempt now has
-- states from before we know how the call went:
--
--   placing         row written, call not requested yet (or we died right here)
--   placed          AgentPhone took the call; `agentphone_call_id` is set
--   placed_unknown  the request timed out or failed with no clear answer, so
--                   the call may have rung; a person or the reconciler checks
--   throttled, concurrency_limit, number_busy, no_funds
--                   AgentPhone refused the call (429, 429, 409, 402)
--
-- Every attempt counts toward the 7-in-7 limit, refused ones too. That can
-- waste an attempt, but it never under-counts a call that did ring.

alter table public.call_attempt add column agentphone_call_id text unique;

alter table public.call_attempt drop constraint call_attempt_outcome_check;
alter table public.call_attempt add constraint call_attempt_outcome_check
    check (outcome in (
        'no_answer', 'busy', 'voicemail', 'answered', 'failed',
        'placing', 'placed', 'placed_unknown',
        'throttled', 'concurrency_limit', 'number_busy', 'no_funds'
    ));
