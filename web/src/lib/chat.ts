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

export type Approval = {
  id: string
  debtor_id: string
  name: string
  /** Masked to the last four digits by the server. */
  phone: string | null
  tool_call_id: string
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

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const text = await res.text()
    let detail = text.slice(0, 200)
    try {
      const parsed = JSON.parse(text) as { detail?: unknown }
      if (typeof parsed.detail === 'string') detail = parsed.detail
    } catch {
      // Not JSON (e.g. the dev proxy's own error page); show the start of it.
    }
    throw new Error(`${res.status}: ${detail}`)
  }
  return (await res.json()) as T
}

export function sendChat(sessionId: string, message: string): Promise<ChatTurn> {
  return postJson<ChatTurn>('/api/chat', { session_id: sessionId, message })
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
