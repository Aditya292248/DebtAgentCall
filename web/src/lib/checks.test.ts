// Run with `npm test` (node's own test runner; Node 22.18+ strips the types).
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { canApprove, latestChecks, parseCheck } from './checks.ts'

const result = (decision: string, rules: { rule_id: string; allow: boolean; reason: string }[]) =>
  JSON.stringify({ debtor_id: 'ACC-1001', at: '2026-11-05T19:00:00+00:00', decision, rules })

const pass = { rule_id: 'calling_hours', allow: true, reason: '11:00 is inside 08:00-21:00' }
const late = { rule_id: 'calling_hours', allow: false, reason: '04:00 is outside 08:00-21:00' }
const freq = { rule_id: 'call_frequency', allow: true, reason: '0 of 7 calls in 7 days' }

test('a call whose checks all pass can be approved', () => {
  const check = parseCheck(result('allow', [pass, freq]))
  assert.ok(check)
  assert.equal(canApprove(check), true)
})

test('Approve is not shown for a blocked call', () => {
  const check = parseCheck(result('block', [late, freq]))
  assert.ok(check)
  assert.equal(check.allow, false)
  assert.equal(canApprove(check), false)
})

test('a result that says allow but has a blocking rule is still blocked', () => {
  assert.equal(canApprove(parseCheck(result('allow', [late]))), false)
})

test('no check, an unreadable one, or one with no rules is never approvable', () => {
  assert.equal(canApprove(null), false)
  assert.equal(canApprove(undefined), false)
  assert.equal(parseCheck('error: no debtor matches'), null)
  assert.equal(parseCheck(JSON.stringify({ debtor_id: 'ACC-1001', decision: 'maybe', rules: [] })), null)
  assert.equal(canApprove(parseCheck(result('allow', []))), false)
})

test('the latest check_call for a debtor wins, and other tools are ignored', () => {
  const checks = latestChecks([
    { name: 'check_call', result: result('allow', [pass]) },
    { name: 'find_debtor', result: '{"matches": []}' },
    { name: 'check_call', result: result('block', [late]) },
  ])
  assert.equal(checks.size, 1)
  assert.equal(canApprove(checks.get('ACC-1001')), false)
})
