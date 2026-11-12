import { CircleCheck, CircleX, X } from 'lucide-react'

import { StatusPill } from '@/components/StatusPill'
import { Button } from '@/components/ui/button'
import { money, type DebtView } from '@/lib/debts'
import { ruleName } from '@/lib/rules'

export function DebtorDetail({
  debt,
  atLabel,
  onClose,
}: {
  debt: DebtView
  atLabel: string
  onClose: () => void
}) {
  return (
    <div className="mx-auto flex h-full max-w-2xl flex-col gap-5 overflow-auto p-5">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-lg font-semibold">{debt.name}</h2>
          <p className="font-mono text-xs text-muted-foreground">
            {debt.account} · {debt.zip} {debt.state}
          </p>
        </div>
        <Button size="icon-sm" variant="ghost" onClick={onClose} aria-label="Back to the overview">
          <X />
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <div className="rounded-lg border bg-card p-3">
          <div className="text-[11px] text-muted-foreground">Owes {debt.creditor}</div>
          <div className="mt-1 text-xl font-semibold tabular-nums">
            {money.format(debt.balanceCents / 100)}
          </div>
          <div className="text-[11px] text-muted-foreground capitalize">{debt.status}</div>
        </div>
        <div className="rounded-lg border bg-card p-3">
          <div className="text-[11px] text-muted-foreground">Phones</div>
          {debt.phones.map((p) => (
            <div key={p.e164} className="mt-1 font-mono text-xs">
              {p.e164} <span className="text-muted-foreground">{p.line_type}</span>
            </div>
          ))}
        </div>
      </div>

      <section className="rounded-lg border bg-card p-4">
        <div className="mb-3 flex items-center justify-between gap-2">
          <div>
            <h3 className="text-xs font-semibold">Pre-call checks</h3>
            <p className="text-[11px] text-muted-foreground">{atLabel}</p>
          </div>
          <StatusPill debt={debt} />
        </div>
        <ul className="divide-y">
          {(debt.check?.decisions ?? []).map((d) => (
            <li key={d.rule_id} className="flex gap-2.5 py-2">
              {d.allow ? (
                <CircleCheck className="mt-0.5 size-4 shrink-0 text-ok" aria-label="allowed" />
              ) : (
                <CircleX className="mt-0.5 size-4 shrink-0 text-destructive" aria-label="blocked" />
              )}
              <div className="min-w-0">
                <div className="text-xs font-medium">
                  {ruleName(d.rule_id)}
                </div>
                <div className="text-[11px] text-muted-foreground">
                  {d.allow ? 'OK' : d.reason}
                </div>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <div className="flex items-center gap-3">
        <Button disabled>Simulate call</Button>
        <span className="text-[11px] text-muted-foreground">Calling isn't switched on yet.</span>
      </div>
    </div>
  )
}
