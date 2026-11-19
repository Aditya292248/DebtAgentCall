import { Plus, Search } from 'lucide-react'
import { useState } from 'react'

import { Notice } from '@/components/Notice'
import { StatusPill } from '@/components/StatusPill'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { money, type DebtView } from '@/lib/debts'
import { cn } from '@/lib/utils'

type Filter = 'all' | 'ready' | 'blocked' | 'archived'

type Props = {
  debts: DebtView[]
  archived: DebtView[]
  onAdd: () => void
  selected: string | null
  onSelect: (account: string) => void
  dbError: string | null
  apiError: string | null
}

export function DebtorList({ debts, archived, onAdd, selected, onSelect, dbError, apiError }: Props) {
  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')

  const ready = debts.filter((d) => d.check?.may_call)
  const blocked = debts.filter((d) => d.check && !d.check.may_call)
  const byFilter = { all: debts, ready, blocked, archived }[filter]
  const q = query.trim().toLowerCase()
  const shown = q
    ? byFilter.filter((d) =>
        [d.name, d.account, d.creditor, d.state].some((s) => s.toLowerCase().includes(q)),
      )
    : byFilter

  return (
    <div className="flex h-full flex-col">
      <div className="space-y-3 px-4 pt-5 pb-4">
        <div className="flex items-baseline justify-between">
          <h2 className="text-sm font-semibold">Debtors</h2>
          <Button size="sm" onClick={onAdd}>
            <Plus /> Add person
          </Button>
        </div>
        <div className="relative">
          <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search debtors"
            aria-label="Search debtors by name, account or creditor"
            className="h-9 bg-background pl-9"
          />
        </div>
        <Tabs value={filter} onValueChange={(v) => setFilter(v as Filter)}>
          <TabsList className="w-full">
            <TabsTrigger value="all" className="text-xs">
              All <span className="text-muted-foreground tabular-nums">{debts.length}</span>
            </TabsTrigger>
            <TabsTrigger value="ready" className="text-xs">
              Ready <span className="text-muted-foreground tabular-nums">{ready.length}</span>
            </TabsTrigger>
            <TabsTrigger value="blocked" className="text-xs">
              Can&apos;t call <span className="text-muted-foreground tabular-nums">{blocked.length}</span>
            </TabsTrigger>
            <TabsTrigger value="archived" className="text-xs">
              Archived{' '}
              <span className="text-muted-foreground tabular-nums">{archived.length}</span>
            </TabsTrigger>
          </TabsList>
        </Tabs>
        {apiError && (
          <Notice message="Can't check who may be called right now. Try again shortly." detail={apiError} />
        )}
        {dbError && <Notice message="Can't load the debtors right now." detail={dbError} />}
      </div>
      <ScrollArea className="min-h-0 flex-1 border-t">
        <ul className="space-y-0.5 p-2">
          {shown.map((d) => (
            <li key={d.account}>
              <button
                type="button"
                data-account={d.account}
                onClick={() => onSelect(d.account)}
                aria-current={selected === d.account ? 'true' : undefined}
                className={cn(
                  'flex w-full flex-col gap-1 rounded-lg px-3 py-2.5 text-left transition-colors hover:bg-sidebar-accent focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none',
                  selected === d.account && 'bg-sidebar-accent',
                )}
              >
                <span className="flex items-baseline justify-between gap-3">
                  <span className="text-sm font-medium">{d.name}</span>
                  <span className="shrink-0 text-sm tabular-nums">
                    {money.format(d.balanceCents / 100)}
                  </span>
                </span>
                <span className="flex items-baseline justify-between gap-3">
                  <span className="text-xs text-muted-foreground">
                    {d.account} · {d.creditor}
                  </span>
                  <StatusPill debt={d} className="shrink-0" />
                </span>
              </button>
            </li>
          ))}
          {shown.length === 0 && !dbError && (
            <li className="p-6 text-center text-sm text-muted-foreground">
              {filter === 'archived' ? 'No archived people.' : 'No debts match.'}
            </li>
          )}
        </ul>
      </ScrollArea>
    </div>
  )
}
