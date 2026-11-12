// The read-only Python API (`uv run api`), reached through the Vite proxy at /api.

export type Decision = { rule_id: string; allow: boolean; reason: string }

export type DebtCheck = {
  account_number: string
  name: string
  may_call: boolean
  decisions: Decision[]
}

export type PrecallResponse = { at: string; debts: DebtCheck[] }

export type ChainResponse = { ok: boolean; rows: number; problems: string[] }

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) {
    const body = await res.text()
    let detail = body.slice(0, 200)
    try {
      detail = (JSON.parse(body) as { detail?: string }).detail ?? detail
    } catch {
      // Not JSON (e.g. the dev proxy's own error page); show the start of it.
    }
    throw new Error(`${res.status}: ${detail}`)
  }
  return (await res.json()) as T
}

/** Every debt's pre-call decisions at `at` (an ISO time), or now when it is null. */
export function fetchPrecall(at: string | null): Promise<PrecallResponse> {
  const query = at ? `?at=${encodeURIComponent(at)}` : ''
  return getJson<PrecallResponse>(`/api/precall${query}`)
}

export function fetchChain(): Promise<ChainResponse> {
  return getJson<ChainResponse>('/api/audit/verify')
}
