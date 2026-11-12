import { Clock, Moon, Sun } from 'lucide-react'
import { useState } from 'react'

import { AuditLog } from '@/components/AuditLog'
import { DebtorDetail } from '@/components/DebtorDetail'
import { DebtorList } from '@/components/DebtorList'
import { Overview } from '@/components/Overview'
import { Button } from '@/components/ui/button'
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from '@/components/ui/resizable'
import { useDebts } from '@/lib/debts'
import { applyTheme, loadTheme, type Theme } from '@/lib/theme'

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

  const atIso = at ? new Date(at).toISOString() : null
  const { debts, dbError, apiError } = useDebts(atIso)
  const selectedDebt = debts.find((d) => d.account === selected)
  const accounts = new Map(debts.map((d) => [d.id, d.name]))
  const atLabel = at
    ? `Checked for ${new Date(at).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}`
    : 'Checked for right now'

  const toggle = () => {
    const next = theme === 'dark' ? 'light' : 'dark'
    applyTheme(next)
    setTheme(next)
  }

  return (
    <div className="flex h-dvh flex-col bg-background text-foreground">
      <header className="flex h-12 shrink-0 items-center gap-3 border-b px-3">
        <span className="flex size-6 items-center justify-center rounded-md bg-foreground text-[11px] font-bold text-background">
          D
        </span>
        <span className="text-sm font-semibold">DebtAgentCall</span>
        <span className="hidden rounded-full border px-2 py-0.5 text-[11px] text-muted-foreground sm:inline">
          View only
        </span>
        <div className="ml-auto flex items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Clock className="size-3.5" />
            <span className="hidden md:inline">Check at</span>
            <input
              type="datetime-local"
              aria-label="Check the calls at this time"
              value={at || toLocalInput(new Date())}
              onChange={(e) => setAt(e.target.value)}
              className="h-8 rounded-md border bg-transparent px-2 text-xs text-foreground"
            />
          </label>
          <Button size="sm" variant="outline" onClick={() => setAt('')} disabled={!at}>
            Now
          </Button>
          <Button
            size="icon-sm"
            variant="ghost"
            onClick={toggle}
            aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          >
            {theme === 'dark' ? <Sun /> : <Moon />}
          </Button>
        </div>
      </header>
      <ResizablePanelGroup orientation="horizontal" className="min-h-0 flex-1">
        <ResizablePanel defaultSize="28" minSize="20">
          <DebtorList
            debts={debts}
            selected={selected}
            onSelect={setSelected}
            dbError={dbError}
            apiError={apiError}
          />
        </ResizablePanel>
        <ResizableHandle withHandle />
        <ResizablePanel defaultSize="44" minSize="25">
          {selectedDebt ? (
            <DebtorDetail debt={selectedDebt} atLabel={atLabel} onClose={() => setSelected(null)} />
          ) : (
            <Overview debts={debts} atLabel={atLabel} />
          )}
        </ResizablePanel>
        <ResizableHandle withHandle />
        <ResizablePanel defaultSize="28" minSize="18">
          <AuditLog accounts={accounts} />
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  )
}
