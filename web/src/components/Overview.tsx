
import { blocksOf, money, type DebtView } from '@/lib/debts'
import { ruleName } from '@/lib/rules'

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg border bg-card p-3">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-1 text-xl font-semibold tabular-nums">{value}</div>
      {hint && <div className="text-[11px] text-muted-foreground">{hint}</div>}
    </div>
  )
}

/** How many debts each rule blocks: one series, one color, values labelled directly. */
function BlockedByRule({ debts }: { debts: DebtView[] }) {
  const counts = new Map<string, number>()
  for (const d of debts) for (const b of blocksOf(d)) counts.set(b.rule_id, (counts.get(b.rule_id) ?? 0) + 1)
  const rows = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  const max = Math.max(1, ...rows.map(([, n]) => n))

  if (rows.length === 0) {
    return <p className="text-xs text-muted-foreground">Nothing is blocked at this time.</p>
  }
  return (
    <ul className="space-y-1.5" aria-label="Debts blocked by each rule">
      {rows.map(([rule, n]) => (
        <li
          key={rule}
          className="group grid grid-cols-[10rem_1fr_2rem] items-center gap-2 rounded px-1 py-0.5 hover:bg-muted/60"
          title={`${ruleName(rule)}: stops ${n} of ${debts.length} debts`}
        >
          <span className="truncate text-xs">{ruleName(rule)}</span>
          <span className="h-3">
            <span
              className="block h-full rounded-r bg-bar transition-[width] group-hover:opacity-80"
              style={{ width: `${(n / max) * 100}%` }}
            />
          </span>
          <span className="text-right text-xs tabular-nums text-muted-foreground">{n}</span>
        </li>
      ))}
    </ul>
  )
}

export function Overview({ debts, atLabel }: { debts: DebtView[]; atLabel: string }) {
  const checked = debts.filter((d) => d.check)
  const ready = checked.filter((d) => d.check?.may_call)
  const total = debts.reduce((sum, d) => sum + d.balanceCents, 0)
  const readyTotal = ready.reduce((sum, d) => sum + d.balanceCents, 0)

  return (
    <div className="mx-auto flex h-full max-w-2xl flex-col gap-5 overflow-auto p-5">
      <div>
        <h2 className="text-sm font-semibold">Overview</h2>
        <p className="text-xs text-muted-foreground">
          {atLabel}. Pick a debtor on the left to see every check.
        </p>
      </div>
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        <Tile label="Debts" value={String(debts.length)} />
        <Tile label="Ready to call" value={String(ready.length)} hint={money.format(readyTotal / 100)} />
        <Tile label="Can't call" value={String(checked.length - ready.length)} />
        <Tile label="Total owed" value={money.format(total / 100)} />
      </div>
      <section className="rounded-lg border bg-card p-4">
        <h3 className="text-xs font-semibold">Why we can&apos;t call</h3>
        <p className="mb-3 text-[11px] text-muted-foreground">
          How many debts each reason stops. One debt can have more than one reason.
        </p>
        <BlockedByRule debts={checked} />
      </section>
    </div>
  )
}
