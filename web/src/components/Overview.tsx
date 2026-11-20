import { blocksOf, money, type DebtView } from '@/lib/debts'
import { ruleName } from '@/lib/rules'

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1.5 text-xl font-semibold tabular-nums">{value}</div>
      {hint && <div className="mt-0.5 text-xs text-muted-foreground tabular-nums">{hint}</div>}
    </div>
  )
}

/** How many debts each rule blocks: one series, one muted color, values labelled directly. */
function BlockedByRule({ debts }: { debts: DebtView[] }) {
  const counts = new Map<string, number>()
  for (const d of debts) for (const b of blocksOf(d)) counts.set(b.rule_id, (counts.get(b.rule_id) ?? 0) + 1)
  const rows = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  const max = Math.max(1, ...rows.map(([, n]) => n))

  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">Nothing is blocked at this time.</p>
  }
  return (
    <ul className="space-y-2.5" aria-label="Debts blocked by each rule">
      {rows.map(([rule, n]) => (
        <li
          key={rule}
          className="grid grid-cols-[minmax(7rem,11rem)_1fr_1.5rem] items-center gap-3"
          title={`${ruleName(rule)}: stops ${n} of ${debts.length} debts`}
        >
          <span className="text-sm">{ruleName(rule)}</span>
          <span className="h-2 rounded-full bg-muted">
            <span
              className="block h-full rounded-full bg-bar"
              style={{ width: `${(n / max) * 100}%` }}
            />
          </span>
          <span className="text-right text-sm text-muted-foreground tabular-nums">{n}</span>
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
    <div className="space-y-6">
      <p className="text-sm text-muted-foreground">
        {debts.length === 0
          ? 'No people yet. The numbers below are all zero until someone is added.'
          : `${atLabel}. Pick a debtor in the list to see every check.`}
      </p>
      <div className="grid grid-cols-2 gap-3">
        <Tile label="Debts" value={String(debts.length)} />
        <Tile label="Total owed" value={money.format(total / 100)} />
        <Tile label="Ready to call" value={String(ready.length)} hint={money.format(readyTotal / 100)} />
        <Tile label="Can't call" value={String(checked.length - ready.length)} />
      </div>
      <section className="rounded-lg border bg-card p-4">
        <h4 className="text-sm font-semibold">Why we can&apos;t call</h4>
        <p className="mt-0.5 mb-4 text-xs text-muted-foreground">
          How many debts each reason stops. One debt can have more than one reason.
        </p>
        <BlockedByRule debts={checked} />
      </section>
    </div>
  )
}
