import { CircleCheck, CircleX, RefreshCw, ShieldAlert, ShieldCheck } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

import { Notice } from '@/components/Notice'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
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

const pill = 'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium'

function ChainStatus({ chain, error }: { chain: ChainResponse | null; error: string | null }) {
  if (error || !chain) {
    return (
      <span className={cn(pill, 'bg-muted text-muted-foreground')} title={error ?? undefined}>
        {error ? "Can't verify the log" : 'Verifying the log'}
      </span>
    )
  }
  if (chain.ok) {
    return (
      <span
        className={cn(pill, 'bg-ok/15 text-ok')}
        title={`All ${chain.rows} entries are intact: nothing was changed or removed.`}
      >
        <ShieldCheck className="size-3" /> Verified
      </span>
    )
  }
  return (
    <span
      className={cn(pill, 'bg-destructive/15 text-destructive')}
      title={chain.problems.join('\n')}
    >
      <ShieldAlert className="size-3" /> Log was changed
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
    <div className="flex h-full flex-col">
      <div className="space-y-3 p-3">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold">Compliance log</h2>
          <ChainStatus chain={chain} error={chainError} />
          <Button
            size="icon-sm"
            variant="ghost"
            className="ml-auto"
            onClick={load}
            aria-label="Refresh the log"
          >
            <RefreshCw />
          </Button>
        </div>
        <Tabs
          value={onlyBlocks ? 'blocks' : 'all'}
          onValueChange={(v) => setOnlyBlocks(v === 'blocks')}
        >
          <TabsList className="w-full">
            <TabsTrigger value="all">All</TabsTrigger>
            <TabsTrigger value="blocks">Can't call only</TabsTrigger>
          </TabsList>
        </Tabs>
        {dbError && <Notice message="Can't load the log right now." detail={dbError} />}
      </div>
      <ScrollArea className="min-h-0 flex-1 border-t">
        {shown.length === 0 && !dbError && (
          <p className="p-4 text-center text-xs text-muted-foreground">Nothing recorded yet.</p>
        )}
        <ul className="p-1.5">
          {shown.map((r) => {
            const blocked = r.decision === 'block'
            return (
              <li key={r.seq} className="flex gap-2.5 rounded-md px-2 py-2 hover:bg-muted/60">
                {blocked ? (
                  <CircleX className="mt-0.5 size-3.5 shrink-0 text-destructive" aria-label="can't call" />
                ) : (
                  <CircleCheck
                    className="mt-0.5 size-3.5 shrink-0 text-muted-foreground/60"
                    aria-label="passed"
                  />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2 text-xs">
                    <span className="truncate font-medium">
                      {(r.debt_id && accounts.get(r.debt_id)) || 'Unknown account'}
                    </span>
                    <span className="ml-auto shrink-0 text-[10px] text-muted-foreground">
                      {time.format(new Date(r.ts))}
                    </span>
                  </div>
                  <div
                    className={cn('text-[11px]', blocked ? 'text-destructive' : 'text-muted-foreground')}
                  >
                    {blocked ? "Can't call: " : 'Passed: '}
                    {ruleName(r.rule_id)}
                  </div>
                  {blocked && <div className="text-[11px] text-muted-foreground">{r.reason}</div>}
                </div>
              </li>
            )
          })}
        </ul>
      </ScrollArea>
      {rows.length === LIMIT && (
        <p className="border-t p-2 text-[11px] text-muted-foreground">
          Showing the latest {LIMIT}.
        </p>
      )}
    </div>
  )
}
