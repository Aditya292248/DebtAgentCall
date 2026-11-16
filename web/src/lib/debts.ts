import { useEffect, useState } from 'react'

import { fetchPrecall, type DebtCheck } from '@/lib/api'
import { timeOf } from '@/lib/checks'
import { supabase, supabaseConfigError } from '@/lib/supabase'

/** One debt as the page shows it: the database row plus the pre-call result. */
export type DebtView = {
  id: string
  account: string
  name: string
  zip: string
  state: string
  creditor: string
  balanceCents: number
  status: string
  phones: { e164: string; line_type: string }[]
  check: DebtCheck | undefined
  /** When `check` ran, in ms since the epoch (0 if unknown). */
  checkedAt: number
  checkFailed: boolean
}

type DebtorRow = {
  first_name: string
  last_name: string
  state: string
  zip: string
  phone: { e164: string; line_type: string }[]
  debt: {
    id: string
    account_number: string
    creditor: string
    /** Owed now: public.current_balance_cents, after payments. */
    current_balance_cents: number
    status: string
  }[]
}

export const money = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' })

/** The checks we got back, and which time they were run for. */
type CheckResult = {
  key: string
  at: number
  checks: Map<string, DebtCheck>
  error: string | null
}

const NOW_REFRESH_MS = 60_000

/** The debts from Supabase (read once) joined with the checks for `at` (null = now).
 *
 *  A result is only shown for the time it was checked for, so a failed re-check
 *  never leaves an earlier time's "Ready to call" on screen. "Now" is checked
 *  again every minute, since calling hours can close while the page is open. */
export function useDebts(at: string | null) {
  const [rows, setRows] = useState<DebtorRow[]>([])
  const [dbError, setDbError] = useState<string | null>(supabaseConfigError)
  const [result, setResult] = useState<CheckResult | null>(null)
  const [tick, setTick] = useState(0)
  const key = at ?? `now:${tick}`

  useEffect(() => {
    if (at) return
    const id = setInterval(() => setTick((t) => t + 1), NOW_REFRESH_MS)
    return () => clearInterval(id)
  }, [at])

  useEffect(() => {
    if (supabaseConfigError) return
    supabase
      .from('debtor')
      .select(
        'first_name, last_name, state, zip, phone(e164, line_type), ' +
          'debt(id, account_number, creditor, current_balance_cents, status)',
      )
      .order('last_name')
      .then(({ data, error }) => {
        if (error) setDbError(error.message)
        else setRows((data ?? []) as unknown as DebtorRow[])
      })
  }, [])

  useEffect(() => {
    let cancelled = false
    fetchPrecall(at)
      .then((res) => {
        if (cancelled) return
        const checks = new Map(res.debts.map((d) => [d.account_number, d]))
        setResult({ key, at: timeOf(res.at), checks, error: null })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const error = err instanceof Error ? err.message : String(err)
        setResult({ key, at: 0, checks: new Map(), error })
      })
    return () => {
      cancelled = true
    }
  }, [at, key])

  // While "now" is re-checked each minute, keep showing last minute's good result
  // rather than flashing "checking"; a chosen time only ever shows its own result.
  const current =
    result &&
    (result.key === key || (!at && result.key.startsWith('now:') && !result.error))
      ? result
      : null
  const apiError = current?.error ?? null

  const debts: DebtView[] = rows.flatMap((p) =>
    p.debt.map((d) => ({
      id: d.id,
      account: d.account_number,
      name: `${p.first_name} ${p.last_name}`,
      zip: p.zip,
      state: p.state,
      creditor: d.creditor,
      balanceCents: d.current_balance_cents,
      status: d.status,
      phones: p.phone,
      check: current?.checks.get(d.account_number),
      checkedAt: current?.at ?? 0,
      // Checked, but with no answer for this debt: unknown, never assumed fine.
      checkFailed: current !== null && !current.checks.has(d.account_number),
    })),
  )
  return { debts, dbError, apiError }
}

export function blocksOf(debt: DebtView) {
  return debt.check?.decisions.filter((d) => !d.allow) ?? []
}
