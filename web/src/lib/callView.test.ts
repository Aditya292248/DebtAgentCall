// Run with `npm test`.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  buildTimeline,
  describeEvent,
  shouldPoll,
  stageLabel,
  worthShowing,
  type CallDetail,
  type CallEvent,
} from './callView.ts'

const call = (over: Partial<CallDetail> = {}): CallDetail => ({
  call_id: 'c1',
  debtor_name: 'Sam Doe',
  account_number: 'ACC-1',
  started: null,
  ended: false,
  outcome: 'placed',
  state: { stage: 'verify', verified: false, disclosed: false, turns: 1 },
  messages: [],
  tool_calls: [],
  pending_plans: [],
  audit_events: [],
  summary: null,
  ...over,
})

const event = (over: Partial<CallEvent>): CallEvent => ({
  seq: 1,
  at: '2026-11-10T15:00:03Z',
  kind: 'gate',
  rule_id: 'x',
  decision: 'allow',
  reason: 'fine',
  sentence: null,
  ...over,
})

test('polling stops when the call has ended or the tab is hidden', () => {
  assert.equal(shouldPoll(false, true), true)
  assert.equal(shouldPoll(true, true), false)
  assert.equal(shouldPoll(false, false), false)
})

test('the timeline keeps the order things happened and drops quiet allows', () => {
  const timeline = buildTimeline(
    call({
      messages: [
        { seq: 2, at: '2026-11-10T15:00:02Z', speaker: 'agent', text: 'Hello' },
        { seq: 1, at: '2026-11-10T15:00:01Z', speaker: 'caller', text: 'Hi' },
      ],
      tool_calls: [{ seq: 2, at: '2026-11-10T15:00:02Z', name: 'lookup', arguments: {}, result: null }],
      audit_events: [
        event({ seq: 7, kind: 'utterance_blocked', decision: 'block', sentence: 'No' }),
        event({ seq: 8 }),
      ],
    }),
  )
  assert.deepEqual(
    timeline.map((i) => i.kind),
    ['message', 'message', 'tool', 'event'],
  )
})

test('a blocked sentence is described as not spoken, and a stop trigger by name', () => {
  const blocked = event({ kind: 'utterance_blocked', decision: 'block', reason: 'a threat', sentence: 'S' })
  assert.equal(describeEvent(blocked).label, 'Not spoken: a threat')
  assert.equal(describeEvent(blocked).sentence, 'S')
  const stop = event({ kind: 'stop_trigger', decision: 'block', reason: 'asked to stop' })
  assert.match(describeEvent(stop).label, /^Stop trigger fired/)
  assert.equal(worthShowing(event({})), false)
})

test('stage labels are plain words', () => {
  assert.equal(stageLabel('third_party'), 'Third party')
  assert.equal(stageLabel(null), 'No stage yet')
})
