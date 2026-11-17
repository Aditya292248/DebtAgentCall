import { ChartColumn, Clock, Moon, ScrollText, Sun } from 'lucide-react'
import { useState } from 'react'

import { AuditLog } from '@/components/AuditLog'
import { ChatPanel } from '@/components/ChatPanel'
import { DebtorDetail } from '@/components/DebtorDetail'
import { DebtorList } from '@/components/DebtorList'
import { Overview } from '@/components/Overview'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '@/components/ui/sheet'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type { CheckView } from '@/lib/checks'
import { useDebts } from '@/lib/debts'
import { applyTheme, loadTheme, type Theme } from '@/lib/theme'

/** What the side sheet shows, if it is open. */
type Panel = 'debtor' | 'overview' | 'log'

/** The value a datetime-local input wants, for a Date in the browser's own zone. */
function toLocalInput(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

export default function App() {
  const [theme, setTheme] = useState<Theme>(() => {
    const t = loadTheme()
    applyTheme(t)
    return t
  })
  // Empty means "now"; otherwise a datetime-local value in the browser's zone.
  const [at, setAt] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [panel, setPanel] = useState<Panel | null>(null)

  const atIso = at ? new Date(at).toISOString() : null
  const { debts, dbError, apiError } = useDebts(atIso)
  const selectedDebt = debts.find((d) => d.account === selected)
  const accounts = new Map(debts.map((d) => [d.id, d.name]))
  const atLabel = at
    ? `Checked for ${new Date(at).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}`
    : 'Checked for right now'

  // The list's own check, but only when it is for right now: a check for some
  // other chosen time says nothing about a call approved now.
  const precallCheck = (debtorId: string): CheckView | null => {
    if (at) return null
    const debt = debts.find((d) => d.account === debtorId)
    if (!debt?.check) return null
    return {
      debtorId,
      allow: debt.check.may_call,
      rules: debt.check.decisions,
      source: 'precall',
      checkedAt: debt.checkedAt,
    }
  }

  const toggle = () => {
    const next = theme === 'dark' ? 'light' : 'dark'
    applyTheme(next)
    setTheme(next)
  }

  const select = (account: string) => {
    setSelected(account)
    setPanel('debtor')
  }

  const shown: Panel | null = panel === 'debtor' && !selectedDebt ? 'overview' : panel
  const titles: Record<Panel, string> = {
    debtor: selectedDebt?.name ?? 'Debtor',
    overview: 'Overview',
    log: 'Compliance log',
  }

  return (
    <div className="flex h-dvh flex-col bg-background text-foreground">
      <header className="flex h-14 shrink-0 items-center gap-3 border-b px-5">
        <span className="flex size-7 items-center justify-center rounded-lg bg-primary text-xs font-semibold text-primary-foreground">
          D
        </span>
        <span className="text-sm font-semibold">DebtAgentCall</span>
        <div className="ml-auto flex items-center gap-1.5">
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            <Clock className="size-4" />
            <span className="hidden lg:inline">Check at</span>
            <input
              type="datetime-local"
              aria-label="Check the calls at this time"
              value={at || toLocalInput(new Date())}
              onChange={(e) => setAt(e.target.value)}
              className="h-8 rounded-lg border border-input bg-card px-2 text-sm text-foreground tabular-nums"
            />
          </label>
          <Button variant="outline" onClick={() => setAt('')} disabled={!at}>
            Now
          </Button>
          <span aria-hidden className="mx-2 h-5 w-px bg-border" />
          <Button variant="ghost" onClick={() => setPanel('overview')}>
            <ChartColumn /> Overview
          </Button>
          <Button variant="ghost" onClick={() => setPanel('log')}>
            <ScrollText /> Compliance log
          </Button>
          <Button
            size="icon"
            variant="ghost"
            onClick={toggle}
            aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          >
            {theme === 'dark' ? <Sun /> : <Moon />}
          </Button>
        </div>
      </header>
      <div className="grid min-h-0 flex-1 grid-cols-[19rem_1fr] xl:grid-cols-[22rem_1fr]">
        <aside className="min-h-0 border-r bg-sidebar">
          <DebtorList
            debts={debts}
            selected={selected}
            onSelect={select}
            dbError={dbError}
            apiError={apiError}
          />
        </aside>
        <main className="min-h-0 min-w-0">
          <ChatPanel precallCheck={precallCheck} />
        </main>
      </div>
      <Sheet open={shown !== null} onOpenChange={(open) => !open && setPanel(null)}>
        <SheetContent>
          <div className="space-y-4 border-b px-6 pt-5 pb-4">
            <SheetTitle>{shown ? titles[shown] : ''}</SheetTitle>
            <SheetDescription className="sr-only">
              Details, totals and the compliance log, outside the chat.
            </SheetDescription>
            <Tabs value={shown ?? 'overview'} onValueChange={(v) => setPanel(v as Panel)}>
              <TabsList>
                {selectedDebt && (
                  <TabsTrigger value="debtor" className="px-3">
                    Debtor
                  </TabsTrigger>
                )}
                <TabsTrigger value="overview" className="px-3">
                  Overview
                </TabsTrigger>
                <TabsTrigger value="log" className="px-3">
                  Compliance log
                </TabsTrigger>
              </TabsList>
            </Tabs>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-6">
            {shown === 'debtor' && selectedDebt && (
              <DebtorDetail debt={selectedDebt} atLabel={atLabel} />
            )}
            {shown === 'overview' && <Overview debts={debts} atLabel={atLabel} />}
            {shown === 'log' && <AuditLog accounts={accounts} />}
          </div>
        </SheetContent>
      </Sheet>
    </div>
  )
}
