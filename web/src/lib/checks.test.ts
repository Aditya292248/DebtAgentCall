// Run with `npm test` (node's own test runner; Node 22.18+ strips the types).
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { ApiError, decideFailure } from './chat.ts'
import {
  canApprove,
  latestChecks,
  MAX_CHECK_AGE_MS,
  newerCheck,
  parseCheck,
  type CheckView,
} from './checks.ts'

const AT = '2026-11-05T19:00:00+00:00'
const T = Date.parse(AT)
const SOON = T + 60_000 // a minute after the check ran

type R = { rule_id: string; allow: boolean; reason: string }
const result = (decision: string, rules: R[], at: string = AT) =>
  JSON.stringify({ debtor_id: 'ACC-1001', at, decision, rules })

const pass = { rule_id: 'calling_hours', allow: true, reason: '11:00 is inside 08:00-21:00' }
const late = { rule_id: 'calling_hours', allow: false, reason: '21:01 is outside 08:00-21:00' }
const freq = { rule_id: 'call_frequency', allow: true, reason: '0 of 7 calls in 7 days' }

test('a recent call whose checks all pass can be approved', () => {
  const check = parseCheck(result('allow', [pass, freq]))
  assert.ok(check)
  assert.equal(check.checkedAt, T)
  assert.equal(canApprove(check, SOON), true)
})

test('Approve is not shown for a blocked call', () => {
  const check = parseCheck(result('block', [late, freq]))
  assert.ok(check)
  assert.equal(check.allow, false)
  assert.equal(canApprove(check, SOON), false)
})

test('a result that says allow but has a blocking rule is still blocked', () => {
  assert.equal(canApprove(parseCheck(result('allow', [late])), SOON), false)
})

test('no check, an unreadable one, or one with no rules is never approvable', () => {
  assert.equal(canApprove(null, SOON), false)
  assert.equal(canApprove(undefined, SOON), false)
  assert.equal(parseCheck('error: no debtor matches'), null)
  assert.equal(parseCheck(JSON.stringify({ debtor_id: 'ACC-1001', decision: 'maybe', rules: [] })), null)
  assert.equal(canApprove(parseCheck(result('allow', [])), SOON), false)
})

test('a passing check that has grown old can no longer be approved', () => {
  const check = parseCheck(result('allow', [pass]))
  assert.equal(canApprove(check, T + MAX_CHECK_AGE_MS), true)
  assert.equal(canApprove(check, T + MAX_CHECK_AGE_MS + 1), false)
  // No time in the result reads as the oldest possible, never as fresh.
  const undated = parseCheck(JSON.stringify({ debtor_id: 'ACC-1001', decision: 'allow', rules: [pass] }))
  assert.ok(undated)
  assert.equal(canApprove(undated, SOON), false)
})

test('the newer check wins: a later block hides an earlier pass', () => {
  const chatPass = parseCheck(result('allow', [pass]))
  const listBlock: CheckView = {
    debtorId: 'ACC-1001',
    allow: false,
    rules: [late],
    source: 'precall',
    checkedAt: T + 120_000,
  }
  assert.equal(newerCheck(chatPass, listBlock), listBlock)
  assert.equal(newerCheck(listBlock, chatPass), listBlock)
  assert.equal(canApprove(newerCheck(chatPass, listBlock), T + 150_000), false)
  assert.equal(newerCheck(null, chatPass), chatPass)
  assert.equal(newerCheck(undefined, undefined), null)
})

test('the latest check_call for a debtor wins, and other tools are ignored', () => {
  const checks = latestChecks([
    { name: 'check_call', result: result('allow', [pass]) },
    { name: 'find_debtor', result: '{"matches": []}' },
    { name: 'check_call', result: result('block', [late]) },
  ])
  assert.equal(checks.size, 1)
  assert.equal(canApprove(checks.get('ACC-1001'), SOON), false)
})

test('a failed approve never claims the call was not placed unless the server said so', () => {
  const lost = decideFailure(new ApiError(null, 'Failed to fetch'), true)
  assert.match(lost.text, /Couldn’t confirm whether the call was placed/)
  assert.equal(lost.closed, false)
  const proxy = decideFailure(new ApiError(502, 'Bad Gateway'), true)
  assert.match(proxy.text, /Couldn’t confirm/)
  const noPhone = decideFailure(new ApiError(503, 'AgentPhone is not set up'), true)
  assert.match(noPhone.text, /^Not dialed/)
  assert.equal(noPhone.closed, false)
})

test('a 404 on approve means the approval is closed, so its card goes', () => {
  for (const approve of [true, false]) {
    const gone = decideFailure(new ApiError(404, "no pending approval 'apr_x'"), approve)
    assert.equal(gone.closed, true)
    assert.doesNotMatch(gone.text, /not dialed|not placed/i)
  }
})
