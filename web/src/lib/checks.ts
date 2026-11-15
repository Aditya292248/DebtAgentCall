// Reading check results out of the operator chat, and deciding whether the
// Approve button may show. No imports, so `node --test` can load it directly.

export type Rule = { rule_id: string; allow: boolean; reason: string }

/** A pre-call check for one debtor: whether a call is allowed, and each rule. */
export type CheckView = {
  debtorId: string
  allow: boolean
  rules: Rule[]
  /** Where it came from: the agent's own check_call, or the page's /api/precall. */
  source: 'chat' | 'precall'
}

function isRule(x: unknown): x is Rule {
  if (typeof x !== 'object' || x === null) return false
  const r = x as Record<string, unknown>
  return typeof r.rule_id === 'string' && typeof r.allow === 'boolean' && typeof r.reason === 'string'
}

/** A check_call result (`{debtor_id, decision, rules}`), or null if it isn't one.
 *  Anything that doesn't parse cleanly is null: unknown, never read as a pass. */
export function parseCheck(result: string | null | undefined): CheckView | null {
  if (!result) return null
  let body: unknown
  try {
    body = JSON.parse(result)
  } catch {
    return null
  }
  if (typeof body !== 'object' || body === null) return null
  const b = body as Record<string, unknown>
  if (typeof b.debtor_id !== 'string') return null
  if (b.decision !== 'allow' && b.decision !== 'block') return null
  if (!Array.isArray(b.rules) || !b.rules.every(isRule)) return null
  const rules = b.rules as Rule[]
  // Allowed only if the result says so and no rule in it blocks.
  const allow = b.decision === 'allow' && rules.every((r) => r.allow)
  return { debtorId: b.debtor_id, allow, rules, source: 'chat' }
}

type ToolCallLike = { name: string | null; result: string | null }

/** The latest check_call result per debtor, from tool calls oldest first. */
export function latestChecks(calls: ToolCallLike[]): Map<string, CheckView> {
  const out = new Map<string, CheckView>()
  for (const c of calls) {
    if (c.name !== 'check_call') continue
    const check = parseCheck(c.result)
    if (check) out.set(check.debtorId, check)
  }
  return out
}

/** Approve shows only for a check that is known and passes every rule. A
 *  blocked call, or one with no check result, gets no Approve button. The
 *  server runs every check again before dialing either way. */
export function canApprove(check: CheckView | null | undefined): boolean {
  return Boolean(
    check && check.allow && check.rules.length > 0 && check.rules.every((r) => r.allow),
  )
}

export function blockedRules(check: CheckView | null | undefined): Rule[] {
  return check?.rules.filter((r) => !r.allow) ?? []
}
