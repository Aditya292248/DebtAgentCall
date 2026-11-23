// The operator chat API (`POST /api/chat`, `POST /api/chat/approve` in
// harness/api.py), reached through the Vite proxy at /api. The browser only
// asks; the server runs every check again and does any dialing.

export type ToolCall = {
  name: string | null
  /** The arguments as the model wrote them: a JSON string, or null. */
  arguments: string | null
  /** The tool's result as text (often JSON), or null if it never ran. */
  result: string | null
}

/** What the server is waiting on: a call, a payment, or a payment reversal. */
export type ApprovalAction =
  | 'place_call'
  | 'record_payment'
  | 'reverse_payment'
  | 'add_note'
  | 'schedule_callback'

/** Written by the server for a payment, reversal, note or callback; shown as it is. */
export type PaymentDetails = {
  /** add_note: the note as it will be saved. */
  text?: string
  /** schedule_callback: the time, e.g. "2026-11-18 10:30 America/Los_Angeles". */
  shown?: string
  amount?: string
  paid_on?: string
  method?: string
  balance_before?: string
  balance_after?: string
  time_zone?: string
  payment_id?: string
  reason?: string
}

export type Approval = {
  id: string
  debtor_id: string
  name: string
  /** Masked to the last four digits by the server. */
  phone: string | null
  tool_call_id: string
  action: ApprovalAction
  details: PaymentDetails
}

export type Dial = {
  debtor_id: string
  blocked: boolean
  /** "rule_id: reason" for each rule that blocked. */
  reasons: string[]
  outcome: string | null
  attempt_id: string | null
  call_id: string | null
}

export type ChatTurn = {
  reply: string
  tool_calls: ToolCall[]
  pending_approvals: Approval[]
  dial: Dial | null
}

/** A failed API call. `status` is the HTTP status, or null if no answer came
 *  back at all (network error, proxy gave up). */
export class ApiError extends Error {
  readonly status: number | null

  constructor(status: number | null, detail: string) {
    super(status === null ? detail : `${status}: ${detail}`)
    this.status = status
  }
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch (err) {
    throw new ApiError(null, err instanceof Error ? err.message : String(err))
  }
  if (!res.ok) {
    const text = await res.text()
    let detail = text.slice(0, 200)
    try {
      const parsed = JSON.parse(text) as { detail?: unknown }
      if (typeof parsed.detail === 'string') detail = parsed.detail
    } catch {
      // Not JSON (e.g. the dev proxy's own error page); show the start of it.
    }
    throw new ApiError(res.status, detail)
  }
  return (await res.json()) as T
}

/** What a failed approve or deny means, from what the server said, if anything.
 *
 *  Only a 503 (AgentPhone isn't set up) proves nothing was dialed: the server
 *  refuses before it records an attempt. A 404 means the approval was already
 *  closed, maybe by an earlier click that did dial. Anything else, including
 *  no answer at all, leaves it unknown: the server may have dialed anyway. */
export function decideFailure(
  err: unknown,
  approve: boolean,
): { text: string; closed: boolean } {
  const status = err instanceof ApiError ? err.status : null
  if (status === 404) {
    return {
      text: 'That call was already approved or denied. Ask the agent for its status.',
      closed: true,
    }
  }
  if (!approve) return { text: 'Couldn’t deny the call. Try again.', closed: false }
  if (status === 503) {
    return { text: 'Not dialed: AgentPhone isn’t set up. The call is still waiting.', closed: false }
  }
  return {
    text: 'Couldn’t confirm whether the call was placed. Ask the agent for its status before approving again.',
    closed: false,
  }
}

/** The browser's IANA time zone, so "paid yesterday" means the operator's yesterday. */
export function localTimeZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined
  } catch {
    return undefined
  }
}

const progressLabels: Record<string, string> = {
  find_debtor: 'Finding matching people',
  list_debtors: 'Checking the account list',
  call_history: 'Checking call history',
  call_status: 'Checking call status',
  account_summary: 'Reading the account',
  check_call: 'Checking calling rules',
  next_allowed_call: 'Checking the next calling time',
  place_call: 'Preparing call approval',
  payment_history: 'Checking payment history',
  portfolio_summary: 'Reading portfolio totals',
  audit_for: 'Reading the compliance log',
  on_time_report: 'Checking payment schedules',
  record_payment: 'Preparing payment approval',
  reverse_payment: 'Preparing reversal approval',
  schedule_callback: 'Checking callback details',
  add_note: 'Preparing the note',
  record_stop: 'Recording the stop request',
  read_bill: 'Reading the bill draft',
}

export async function streamChat(
  sessionId: string, message: string, onProgress: (label: string) => void,
): Promise<ChatTurn> {
  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message, tz: localTimeZone() }),
  })
  if (!res.ok || !res.body) throw new ApiError(res.status, 'Chat is unavailable')
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += value
      let end: number
      while ((end = buffer.indexOf('\n')) >= 0) {
        const event = JSON.parse(buffer.slice(0, end))
        buffer = buffer.slice(end + 1)
        if (event.type === 'done') return event.turn
        if (event.type === 'error') throw new Error(event.message)
        if (event.type === 'progress') onProgress(progressLabels[event.tool] ?? 'Working on your request')
      }
    }
    throw new Error('Connection ended before the answer arrived.')
  } finally {
    reader.releaseLock()
  }
}

export function sendChat(sessionId: string, message: string): Promise<ChatTurn> {
  return postJson<ChatTurn>('/api/chat', { session_id: sessionId, message, tz: localTimeZone() })
}

export function decideApproval(
  sessionId: string,
  approvalId: string,
  approve: boolean,
): Promise<ChatTurn> {
  return postJson<ChatTurn>('/api/chat/approve', {
    session_id: sessionId,
    approval_id: approvalId,
    approve,
    tz: localTimeZone(),
  })
}

const SESSION_KEY = 'debtagentcall-chat-session'

/** One chat id per browser tab, matching the server's `[A-Za-z0-9_-]{1,64}`. */
export function chatSessionId(): string {
  try {
    const saved = sessionStorage.getItem(SESSION_KEY)
    if (saved && /^[A-Za-z0-9_-]{1,64}$/.test(saved)) return saved
  } catch {
    // Storage can be blocked; a fresh id per load still works.
  }
  const bytes = crypto.getRandomValues(new Uint8Array(8))
  const id = 'web_' + Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  try {
    sessionStorage.setItem(SESSION_KEY, id)
  } catch {
    // As above.
  }
  return id
}

/** Pretty-print JSON text for a monospace block; anything else as it is. */
export function prettyJson(text: string | null): string {
  if (text == null) return '(none)'
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}
