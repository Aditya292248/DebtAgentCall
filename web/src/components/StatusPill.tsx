import { blocksOf, type DebtView } from '@/lib/debts'
import { ruleName } from '@/lib/rules'
import { cn } from '@/lib/utils'

type Status = { tone: 'ok' | 'block' | 'none'; label: string; detail: string }

/** What a debt's check says, in words. Not checked is never shown as ready. */
function statusOf(debt: DebtView): Status {
  if (!debt.check) {
    return debt.checkFailed
      ? { tone: 'none', label: "Couldn't check", detail: "The pre-call check didn't answer." }
      : { tone: 'none', label: 'Checking', detail: 'The pre-call check is running.' }
  }
  if (debt.check.may_call) {
    return { tone: 'ok', label: 'Ready to call', detail: 'Every pre-call check passes.' }
  }
  const blocks = blocksOf(debt)
  return {
    tone: 'block',
    label: "Can't call",
    detail: blocks.length
      ? blocks.map((b) => `${ruleName(b.rule_id)}: ${b.reason}`).join('\n')
      : 'A pre-call check blocks this call.',
  }
}

const dot = { ok: 'bg-ok', block: 'bg-destructive', none: 'bg-muted-foreground/50' }

/** A small colored dot plus a word, never color alone. The blocking rules show
 *  on hover; `withRule` also names the first one inline. */
export function StatusPill({
  debt,
  withRule = false,
  className,
}: {
  debt: DebtView
  withRule?: boolean
  className?: string
}) {
  const s = statusOf(debt)
  const first = blocksOf(debt)[0]
  return (
    <span
      className={cn('inline-flex items-center gap-1.5 text-xs text-muted-foreground', className)}
      title={s.detail}
    >
      <span aria-hidden className={cn('size-1.5 shrink-0 rounded-full', dot[s.tone])} />
      <span className={cn(s.tone === 'block' && 'text-foreground')}>{s.label}</span>
      {withRule && s.tone === 'block' && first && (
        <span>
          · {ruleName(first.rule_id)}
          {blocksOf(debt).length > 1 && ` +${blocksOf(debt).length - 1}`}
        </span>
      )}
    </span>
  )
}
