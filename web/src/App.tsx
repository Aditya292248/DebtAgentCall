import { ChartColumn, Moon, Radio, ScrollText, Sun } from 'lucide-react'
import { useState } from 'react'

import { AddPersonDialog } from '@/components/AddPersonDialog'
import { AuditLog } from '@/components/AuditLog'
import { ChatPanel } from '@/components/ChatPanel'
import { GettingStarted } from '@/components/GettingStarted'
import { DebtorDetail } from '@/components/DebtorDetail'
import { DebtorList } from '@/components/DebtorList'
import { LiveCall } from '@/components/LiveCall'
import { Overview } from '@/components/Overview'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '@/components/ui/sheet'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type { CheckView } from '@/lib/checks'
import { useDebts } from '@/lib/debts'
import { applyTheme, loadTheme, type Theme } from '@/lib/theme'

/** What the side sheet shows, if it is open. */
type Panel = 'debtor' | 'overview' | 'log' | 'call'

export default function App() {
  const [theme, setTheme] = useState<Theme>(() => {
    const t = loadTheme()
    applyTheme(t)
    return t
  })
  const [selected, setSelected] = useState<string | null>(null)
  const [panel, setPanel] = useState<Panel | null>(null)
  // The call the live view shows; null means the most recent one.
  const [callId, setCallId] = useState<string | null>(null)

  // Calls are always checked for right now.
  const { debts, archived, dbError, apiError, loaded, reload } = useDebts(null)
  const [adding, setAdding] = useState(false)
  const selectedDebt = [...debts, ...archived].find((d) => d.account === selected)
  const accounts = new Map([...debts, ...archived].map((d) => [d.id, d.name]))
  const atLabel = 'Checked for right now'

  // The list's own check, which is for right now.
  const precallCheck = (debtorId: string): CheckView | null => {
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
    call: 'Live call',
  }

  return (
    <div className="flex h-dvh flex-col bg-background text-foreground">
      <header className="flex h-14 shrink-0 items-center gap-3 border-b px-5">
        <span className="flex size-7 items-center justify-center rounded-lg bg-primary text-xs font-semibold text-primary-foreground">
          D
        </span>
        <span className="text-sm font-semibold">DebtAgentCall</span>
        <div className="ml-auto flex items-center gap-1.5">
          <Button variant="ghost" onClick={() => setPanel('call')}>
            <Radio /> Live call
          </Button>
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
            loaded={loaded}
            archived={archived}
            onAdd={() => setAdding(true)}
            selected={selected}
            onSelect={select}
            dbError={dbError}
            apiError={apiError}
          />
        </aside>
        <main className="min-h-0 min-w-0">
          <ChatPanel
            precallCheck={precallCheck}
            emptyIntro={loaded && debts.length === 0 && archived.length === 0 && !dbError ? (
              <GettingStarted onAdd={() => setAdding(true)} />
            ) : undefined}
            onOpenCall={(id) => {
              setCallId(id)
              setPanel('call')
            }}
          />
        </main>
      </div>
      <AddPersonDialog open={adding} onOpenChange={setAdding} onAdded={reload} />
      <Sheet open={shown !== null} onOpenChange={(open) => !open && setPanel(null)}>
        <SheetContent className={shown === 'call' ? 'max-w-2xl' : undefined}>
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
                <TabsTrigger value="call" className="px-3">
                  Live call
                </TabsTrigger>
              </TabsList>
            </Tabs>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-6">
            {shown === 'debtor' && selectedDebt && (
              <DebtorDetail debt={selectedDebt} atLabel={atLabel} onChanged={reload} />
            )}
            {shown === 'overview' && <Overview debts={debts} atLabel={atLabel} />}
            {shown === 'log' && <AuditLog accounts={accounts} />}
            {shown === 'call' && <LiveCall callId={callId} onSelect={setCallId} />}
          </div>
        </SheetContent>
      </Sheet>
    </div>
  )
}
