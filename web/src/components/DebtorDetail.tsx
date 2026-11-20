import { CircleCheck, CircleX } from 'lucide-react'

import { ArchivePerson } from '@/components/ArchivePerson'
import { StatusPill } from '@/components/StatusPill'
import { money, type DebtView } from '@/lib/debts'
import { ruleName } from '@/lib/rules'

export function DebtorDetail({
  debt,
  atLabel,
  onChanged,
}: {
  debt: DebtView
  atLabel: string
  onChanged: () => void
}) {
  return (
    <div className="space-y-6">
      <p className="text-sm text-muted-foreground">
        {debt.account} · {debt.zip} {debt.state}
      </p>

      <dl className="grid grid-cols-2 gap-3">
        <div className="rounded-lg border bg-card p-4">
          <dt className="text-xs text-muted-foreground">Owes {debt.creditor}</dt>
          <dd className="mt-1.5 text-xl font-semibold tabular-nums">
            {money.format(debt.balanceCents / 100)}
          </dd>
          <dd className="mt-0.5 text-xs text-muted-foreground capitalize">{debt.status}</dd>
        </div>
        <div className="rounded-lg border bg-card p-4">
          <dt className="text-xs text-muted-foreground">Phones</dt>
          {debt.phones.map((p) => (
            <dd key={p.e164} className="mt-1.5 text-sm tabular-nums">
              {p.e164} <span className="text-muted-foreground">{p.line_type}</span>
            </dd>
          ))}
        </div>
      </dl>

      <section className="rounded-lg border bg-card">
        <div className="flex items-start justify-between gap-3 border-b px-4 py-3">
          <div>
            <h4 className="text-sm font-semibold">Pre-call checks</h4>
            <p className="text-xs text-muted-foreground">{atLabel}</p>
          </div>
          <StatusPill debt={debt} className="pt-0.5" />
        </div>
        <ul className="divide-y">
          {(debt.check?.decisions ?? []).map((d) => (
            <li key={d.rule_id} className="flex gap-3 px-4 py-3">
              {d.allow ? (
                <CircleCheck className="mt-0.5 size-4 shrink-0 text-ok" aria-label="allowed" />
              ) : (
                <CircleX className="mt-0.5 size-4 shrink-0 text-destructive" aria-label="blocked" />
              )}
              <div className="min-w-0">
                <div className="text-sm font-medium">{ruleName(d.rule_id)}</div>
                <div className="text-xs leading-relaxed text-muted-foreground">
                  {d.allow ? 'OK' : d.reason}
                </div>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <div className="flex items-center justify-between gap-4">
        <p className="text-sm text-muted-foreground">
          {debt.archived
            ? `${debt.name} is archived and cannot be called.`
            : `To call ${debt.name}, ask the agent. Each call waits for your approval.`}
        </p>
        <ArchivePerson debt={debt} onDone={onChanged} />
      </div>
    </div>
  )
}
