// Run with `npm test` (node's own test runner; Node 22.18+ strips the types).
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { centsFromDollars, EMPTY_FORM, personError, validateForm, type Form } from './people.ts'

const NOW = new Date(2026, 10, 19)
const good: Form = {
  first_name: 'Casey',
  last_name: 'Example',
  dob: '1985-04-12',
  address: '100 Example Street, Los Angeles',
  zip: '90012',
  state: 'CA',
  phone: '+12135550123',
  line_type: 'cell',
  creditor: 'Northwind Furniture',
  account_number: 'PM-1001',
  amount: '1,250.00',
  consent_source: '',
  consent_date: '',
}

test('a good form has no problems, with or without consent', () => {
  assert.deepEqual(validateForm(good, NOW), {})
  const consent = { ...good, consent_source: 'paper form', consent_date: '2026-10-01' }
  assert.deepEqual(validateForm(consent, NOW), {})
})

test('a blank form never pre-fills consent', () => {
  assert.equal(EMPTY_FORM.consent_source, '')
  assert.equal(EMPTY_FORM.consent_date, '')
})

test('each bad field is named', () => {
  const bad: Form = {
    ...good,
    phone: '2135550123',
    zip: '9001',
    state: 'ZZ',
    dob: '1985-02-30',
    amount: '0',
    account_number: 'has space',
  }
  assert.deepEqual(Object.keys(validateForm(bad, NOW)).sort(), [
    'account_number',
    'amount',
    'dob',
    'phone',
    'state',
    'zip',
  ])
  assert.ok(validateForm({ ...good, dob: '2999-01-01' }, NOW).dob)
})

test('half a consent record is a problem on the missing half', () => {
  assert.ok(validateForm({ ...good, consent_source: 'paper form' }, NOW).consent_date)
  assert.ok(validateForm({ ...good, consent_date: '2026-10-01' }, NOW).consent_source)
  assert.ok(
    validateForm({ ...good, consent_source: 'x', consent_date: '2999-01-01' }, NOW).consent_date,
  )
})

test('dollars become whole cents, and anything else is refused', () => {
  assert.equal(centsFromDollars('1,250.00'), 125000)
  assert.equal(centsFromDollars('$12.5'), 1250)
  assert.equal(centsFromDollars('80'), 8000)
  for (const t of ['', '0', '-5', '1.234', 'abc', '1e3', '1,00.00.1']) {
    assert.equal(centsFromDollars(t), null, t)
  }
})

test('the API 422 and 409 bodies become messages on the right field', () => {
  const invalid = personError(422, { problems: { balance_cents: 'must be more than 0' } })
  assert.equal(invalid.problems.amount, 'Must be more than 0')
  const dup = personError(409, { field: 'phone', message: 'on file under account ACC-1' })
  assert.equal(dup.message, 'on file under account ACC-1')
  assert.equal(dup.problems.phone, 'on file under account ACC-1')
  assert.equal(personError(409, 'ACC-1 is already archived').message, 'ACC-1 is already archived')
})
