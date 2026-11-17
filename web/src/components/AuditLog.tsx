import { CircleCheck, CircleX, RefreshCw, ShieldAlert, ShieldCheck } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

import { Notice } from '@/components/Notice'
import { Button } from '@/components/ui/button'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { fetchChain, type ChainResponse } from '@/lib/api'
import { ruleName } from '@/lib/rules'
import { supabase, supabaseConfigError } from '@/lib/supabase'
import { cn } from '@/lib/utils'

type AuditRow = {
  seq: number
  ts: string
  debt_id: string | null
  rule_id: string
  decision: 'allow' | 'block'
  reason: string
}

const LIMIT = 200

const pill = 'inline-flex items-center gap-1.5 text-xs'

function ChainStatus({ chain, error }: { chain: ChainResponse | null; error: string | null }) {
  if (error || !chain) {
    return (
      <span className={cn(pill, 'text-muted-foreground')} title={error ?? undefined}>
        {error ? "Can't verify the log" : 'Verifying the log'}
      </span>
    )
  }
  if (chain.ok) {
    return (
      <span
        className={cn(pill, 'text-muted-foreground')}
        title={`All ${chain.rows} entries are intact: nothing was changed or removed.`}
      >
        <ShieldCheck className="size-3.5 text-ok" /> Verified
      </span>
    )
  }
  return (
    <span
      className={cn(pill, 'font-medium text-destructive')}
      title={chain.problems.join('\n')}
    >
      <ShieldAlert className="size-3.5" /> Log was changed
    </span>
  )
}

const time = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
})

export function AuditLog({ accounts }: { accounts: Map<string, string> }) {
  const [rows, setRows] = useState<AuditRow[]>([])
  const [dbError, setDbError] = useState<string | null>(supabaseConfigError)
  const [chain, setChain] = useState<ChainResponse | null>(null)
  const [chainError, setChainError] = useState<string | null>(null)
  const [onlyBlocks, setOnlyBlocks] = useState(false)

  const load = useCallback(() => {
    if (!supabaseConfigError) {
      supabase
        .from('audit_event')
        .select('seq, ts, debt_id, rule_id, decision, reason')
        .order('seq', { ascending: false })
        .limit(LIMIT)
        .then(({ data, error }) => {
          if (error) setDbError(error.message)
          else {
            setRows((data ?? []) as AuditRow[])
            setDbError(null)
          }
        })
    }
    fetchChain()
      .then((c) => {
        setChain(c)
        setChainError(null)
      })
      .catch((err: unknown) => setChainError(err instanceof Error ? err.message : String(err)))
  }, [])

  useEffect(load, [load])

  const shown = onlyBlocks ? rows.filter((r) => r.decision === 'block') : rows

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <ChainStatus chain={chain} error={chainError} />
        <Tabs
          value={onlyBlocks ? 'blocks' : 'all'}
          onValueChange={(v) => setOnlyBlocks(v === 'blocks')}
          className="ml-auto"
        >
          <TabsList>
            <TabsTrigger value="all" className="px-3 text-xs">All</TabsTrigger>
            <TabsTrigger value="blocks" className="px-3 text-xs">Can&apos;t call only</TabsTrigger>
          </TabsList>
        </Tabs>
        <Button size="icon-sm" variant="ghost" onClick={load} aria-label="Refresh the log">
          <RefreshCw />
        </Button>
      </div>
      {dbError && <Notice message="Can't load the log right now." detail={dbError} />}
      {shown.length === 0 && !dbError && (
        <p className="py-6 text-center text-sm text-muted-foreground">Nothing recorded yet.</p>
      )}
      <ul className="divide-y rounded-lg border bg-card empty:hidden">
        {shown.map((r) => {
          const blocked = r.decision === 'block'
          return (
            <li key={r.seq} className="flex gap-3 px-4 py-3">
              {blocked ? (
                <CircleX className="mt-0.5 size-4 shrink-0 text-destructive" aria-label="can't call" />
              ) : (
                <CircleCheck
                  className="mt-0.5 size-4 shrink-0 text-muted-foreground/60"
                  aria-label="passed"
                />
              )}
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline gap-3 text-sm">
                  <span className="font-medium">
                    {(r.debt_id && accounts.get(r.debt_id)) || 'Unknown account'}
                  </span>
                  <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">
                    {time.format(new Date(r.ts))}
                  </span>
                </div>
                <div className="text-xs leading-relaxed text-muted-foreground">
                  {blocked ? "Can't call: " : 'Passed: '}
                  <span className={cn(blocked && 'text-foreground')}>{ruleName(r.rule_id)}</span>
                </div>
                {blocked && (
                  <div className="text-xs leading-relaxed text-muted-foreground">{r.reason}</div>
                )}
              </div>
            </li>
          )
        })}
      </ul>
      {rows.length === LIMIT && (
        <p className="text-xs text-muted-foreground">Showing the latest {LIMIT}.</p>
      )}
    </div>
  )
}
