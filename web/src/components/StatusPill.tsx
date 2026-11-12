import { CircleCheck, CircleHelp, CircleX, LoaderCircle } from 'lucide-react'

import { blocksOf, type DebtView } from '@/lib/debts'
import { ruleName } from '@/lib/rules'
import { cn } from '@/lib/utils'

/** "Ready to call" or "Can't call: reason (+N)", always with an icon, never color alone. */
export function StatusPill({ debt, className }: { debt: DebtView; className?: string }) {
  const base = 'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium'
  if (!debt.check) {
    // Not checked means not cleared: never shown as ready.
    return debt.checkFailed ? (
      <span className={cn(base, 'bg-muted text-muted-foreground', className)}>
        <CircleHelp className="size-3" /> Couldn't check
      </span>
    ) : (
      <span className={cn(base, 'bg-muted text-muted-foreground', className)}>
        <LoaderCircle className="size-3 animate-spin" /> Checking
      </span>
    )
  }
  if (debt.check.may_call) {
    return (
      <span className={cn(base, 'bg-ok/15 text-ok', className)}>
        <CircleCheck className="size-3" /> Ready to call
      </span>
    )
  }
  const blocks = blocksOf(debt)
  return (
    <span className={cn(base, 'bg-destructive/15 text-destructive', className)}>
      <CircleX className="size-3" />
      Can't call: {ruleName(blocks[0]?.rule_id ?? '')}
      {blocks.length > 1 && <span className="opacity-75">+{blocks.length - 1}</span>}
    </span>
  )
}
