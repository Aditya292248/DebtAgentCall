import { Search } from 'lucide-react'
import { useState } from 'react'

import { Notice } from '@/components/Notice'
import { StatusPill } from '@/components/StatusPill'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { blocksOf, money, type DebtView } from '@/lib/debts'
import { cn } from '@/lib/utils'

type Filter = 'all' | 'ready' | 'blocked'

function initials(name: string) {
  return name
    .split(' ')
    .map((part) => part[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()
}

type Props = {
  debts: DebtView[]
  selected: string | null
  onSelect: (account: string) => void
  dbError: string | null
  apiError: string | null
}

export function DebtorList({ debts, selected, onSelect, dbError, apiError }: Props) {
  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')

  const ready = debts.filter((d) => d.check?.may_call)
  const blocked = debts.filter((d) => d.check && !d.check.may_call)
  const byFilter = { all: debts, ready, blocked }[filter]
  const q = query.trim().toLowerCase()
  const shown = q
    ? byFilter.filter((d) =>
        [d.name, d.account, d.creditor, d.state].some((s) => s.toLowerCase().includes(q)),
      )
    : byFilter

  return (
    <div className="flex h-full flex-col">
      <div className="space-y-3 p-3">
        <div className="flex items-baseline justify-between">
          <h2 className="text-sm font-semibold">Debtors</h2>
          <span className="text-xs text-muted-foreground">{debts.length} debts</span>
        </div>
        <Tabs value={filter} onValueChange={(v) => setFilter(v as Filter)}>
          <TabsList className="w-full">
            <TabsTrigger value="all">All {debts.length}</TabsTrigger>
            <TabsTrigger value="ready">Ready {ready.length}</TabsTrigger>
            <TabsTrigger value="blocked">Can&apos;t call {blocked.length}</TabsTrigger>
          </TabsList>
        </Tabs>
        <div className="relative">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search name, account, creditor"
            aria-label="Search debtors"
            className="h-8 pl-8 text-xs"
          />
        </div>
        {apiError && (
          <Notice message="Can't check who may be called right now. Try again shortly." detail={apiError} />
        )}
        {dbError && <Notice message="Can't load the debtors right now." detail={dbError} />}
      </div>
      <ScrollArea className="min-h-0 flex-1 border-t">
        <ul className="p-1.5">
          {shown.map((d) => (
            <li key={d.account}>
              <button
                type="button"
                data-account={d.account}
                onClick={() => onSelect(d.account)}
                className={cn(
                  'flex w-full gap-3 rounded-md p-2.5 text-left transition-colors hover:bg-muted/60',
                  selected === d.account && 'bg-muted',
                )}
              >
                <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-muted text-[11px] font-semibold text-muted-foreground">
                  {initials(d.name)}
                </span>
                <span className="min-w-0 flex-1 space-y-1">
                  <span className="flex items-baseline justify-between gap-2">
                    <span className="truncate text-sm font-medium">{d.name}</span>
                    <span className="shrink-0 text-sm tabular-nums">
                      {money.format(d.balanceCents / 100)}
                    </span>
                  </span>
                  <span className="flex items-baseline justify-between gap-2 text-[11px] text-muted-foreground">
                    <span className="truncate font-mono">
                      {d.account} · {d.state}
                    </span>
                    <span className="truncate">{d.creditor}</span>
                  </span>
                  <span className="block">
                    <StatusPill debt={d} />
                  </span>
                  {blocksOf(d)[0] && (
                    <span className="line-clamp-2 block text-[11px] text-muted-foreground">
                      {blocksOf(d)[0].reason}
                    </span>
                  )}
                </span>
              </button>
            </li>
          ))}
          {shown.length === 0 && !dbError && (
            <li className="p-4 text-center text-xs text-muted-foreground">No debts match.</li>
          )}
        </ul>
      </ScrollArea>
    </div>
  )
}
