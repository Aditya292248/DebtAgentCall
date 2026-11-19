// Adding, archiving and restoring people: the form's checks and the API calls.
// The checks mirror harness/people.py so a mistake shows next to its field before
// anything is sent; the server checks again and has the last word.
import { ApiError } from './chat.ts'

export const FIELD_NAMES = [
  'first_name',
  'last_name',
  'dob',
  'address',
  'zip',
  'state',
  'phone',
  'line_type',
  'creditor',
  'account_number',
  'amount',
  'consent_source',
  'consent_date',
] as const
export type FieldName = (typeof FIELD_NAMES)[number]
export type Form = Record<FieldName, string>
export type Problems = Partial<Record<FieldName, string>>

/** A blank form. Consent is never pre-filled: it stays empty until a person types it. */
export const EMPTY_FORM: Form = {
  first_name: '',
  last_name: '',
  dob: '',
  address: '',
  zip: '',
  state: '',
  phone: '',
  line_type: 'cell',
  creditor: '',
  account_number: '',
  amount: '',
  consent_source: '',
  consent_date: '',
}

const STATES = new Set(
  (
    'AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ ' +
    'NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC AS GU MP PR VI'
  ).split(' '),
)
const MAX_CENTS = 100_000_000_00

/** "1,234.56" or "$1234" as whole cents, or null if it is not a plain positive amount. */
export function centsFromDollars(text: string): number | null {
  const t = text.trim().replace(/^\$/, '').replace(/,/g, '')
  const m = /^(\d+)(?:\.(\d{1,2}))?$/.exec(t)
  if (!m) return null
  const cents = Number(m[1]) * 100 + Number((m[2] ?? '').padEnd(2, '0') || '0')
  return Number.isSafeInteger(cents) && cents > 0 && cents <= MAX_CENTS ? cents : null
}

/** True for a real calendar date written YYYY-MM-DD. */
function realDate(text: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text)
  if (!m) return null
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])))
  return d.getUTCFullYear() === Number(m[1]) &&
    d.getUTCMonth() === Number(m[2]) - 1 &&
    d.getUTCDate() === Number(m[3])
    ? d
    : null
}

/** Every problem with the form, by field. Empty means it can be sent. */
export function validateForm(f: Form, now: Date = new Date()): Problems {
  const p: Problems = {}
  const need = (name: FieldName, label: string) => {
    if (!f[name].trim()) p[name] = `${label} is needed`
  }
  need('first_name', 'First name')
  need('last_name', 'Last name')
  need('address', 'Address')
  need('creditor', 'Creditor')

  if (!/^\d{5}(-\d{4})?$/.test(f.zip)) p.zip = 'Use a 5-digit ZIP, or ZIP+4 like 90012-1234'
  if (!STATES.has(f.state)) p.state = 'Use a two-letter state code, like CA'
  if (!/^\+[1-9]\d{7,14}$/.test(f.phone)) p.phone = 'Use E.164, like +12135550123'
  if (f.line_type !== 'cell' && f.line_type !== 'landline') p.line_type = 'Choose cell or landline'

  const today = new Date(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()))
  const dob = realDate(f.dob)
  if (!dob) p.dob = 'Use a real date, YYYY-MM-DD'
  else if (dob >= today || dob.getUTCFullYear() < 1900) {
    p.dob = 'Use a real date of birth in the past'
  }

  if (!/^[A-Za-z0-9-]{1,40}$/.test(f.account_number)) {
    p.account_number = 'Use 1-40 letters, digits or dashes'
  }
  if (centsFromDollars(f.amount) === null) {
    p.amount = 'Use an amount above $0, like 1250.00'
  }

  const source = f.consent_source.trim()
  const when = f.consent_date.trim()
  if (source && !when) p.consent_date = 'A consent date is needed with a source'
  if (when && !source) p.consent_source = 'A consent source is needed with a date'
  if (when) {
    const d = realDate(when)
    if (!d || d > today) p.consent_date = 'Use a real date that is not in the future'
  }
  return p
}

/** The JSON body the API takes. Call only when `validateForm` found nothing. */
export function toRequest(f: Form): Record<string, unknown> {
  return {
    first_name: f.first_name.trim(),
    last_name: f.last_name.trim(),
    dob: f.dob,
    address: f.address.trim(),
    zip: f.zip,
    state: f.state,
    phone: f.phone,
    line_type: f.line_type,
    creditor: f.creditor.trim(),
    account_number: f.account_number,
    balance_cents: centsFromDollars(f.amount),
    consent_source: f.consent_source.trim(),
    consent_date: f.consent_date.trim(),
  }
}

export type Person = {
  account_number: string
  name: string
  phone: string | null
  archived: boolean
  consent_recorded?: boolean
}

/** What the server said was wrong: per-field messages, or one plain message. */
export class PersonError extends Error {
  readonly status: number | null
  readonly problems: Problems

  constructor(status: number | null, message: string, problems: Problems = {}) {
    super(message)
    this.status = status
    this.problems = problems
  }
}

const SERVER_FIELD: Record<string, FieldName> = { balance_cents: 'amount' }

async function post<T>(path: string, body?: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (err) {
    throw new ApiError(null, err instanceof Error ? err.message : String(err))
  }
  if (res.ok) return (await res.json()) as T
  const text = await res.text()
  let detail: unknown
  try {
    detail = (JSON.parse(text) as { detail?: unknown }).detail
  } catch {
    // Not JSON (for example the dev proxy's own error page).
  }
  throw personError(res.status, detail, text)
}

/** Turns the API's 409 and 422 bodies into messages a person can act on. */
export function personError(status: number, detail: unknown, raw = ''): PersonError {
  if (typeof detail === 'object' && detail !== null) {
    const d = detail as { problems?: Record<string, string>; field?: string; message?: string }
    if (d.problems) {
      const problems: Problems = {}
      for (const [k, v] of Object.entries(d.problems)) {
        const name = SERVER_FIELD[k] ?? k
        if ((FIELD_NAMES as readonly string[]).includes(name)) problems[name as FieldName] = v
      }
      return new PersonError(status, 'Some fields need another look.', problems)
    }
    if (d.message) {
      const problems: Problems = {}
      if (d.field && (FIELD_NAMES as readonly string[]).includes(d.field)) {
        problems[d.field as FieldName] = d.message
      }
      return new PersonError(status, d.message, problems)
    }
  }
  if (typeof detail === 'string') return new PersonError(status, detail)
  return new PersonError(status, `The server answered ${status}. ${raw.slice(0, 120)}`.trim())
}

export const addPerson = (f: Form) => post<Person>('/api/people', toRequest(f))
export const archivePerson = (account: string) =>
  post<Person>(`/api/people/${encodeURIComponent(account)}/archive`)
export const restorePerson = (account: string) =>
  post<Person>(`/api/people/${encodeURIComponent(account)}/restore`)
