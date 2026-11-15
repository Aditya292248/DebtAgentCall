import { ChevronRight, CircleCheck, CircleHelp, CircleX, Phone, Wrench } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { prettyJson, type Approval, type Dial, type ToolCall } from '@/lib/chat'
import { blockedRules, canApprove, parseCheck, type CheckView } from '@/lib/checks'
import { ruleName } from '@/lib/rules'
import { cn } from '@/lib/utils'

const pill = 'inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[11px] font-medium'

/** One rule's result: "pass" or "blocked", with the rule id. Never color alone. */
export function RuleBadge({ ruleId, allow }: { ruleId: string; allow: boolean }) {
  return (
    <span
      className={cn(pill, allow ? 'bg-ok/15 text-ok' : 'bg-destructive/15 text-destructive')}
      title={ruleName(ruleId)}
    >
      {allow ? <CircleCheck className="size-3" /> : <CircleX className="size-3" />}
      {allow ? 'pass' : 'blocked'}
      <span className="font-mono">{ruleId}</span>
    </span>
  )
}

/** The whole check: "pass" if every rule allows, else each blocking rule. */
export function CheckSummary({ check }: { check: CheckView | null | undefined }) {
  if (!check) {
    return (
      <span className={cn(pill, 'bg-muted text-muted-foreground')}>
        <CircleHelp className="size-3" /> not checked
      </span>
    )
  }
  const blocks = blockedRules(check)
  if (canApprove(check)) {
    return (
      <span className={cn(pill, 'bg-ok/15 text-ok')}>
        <CircleCheck className="size-3" /> pass
        <span className="opacity-75">{check.rules.length} rules</span>
      </span>
    )
  }
  if (blocks.length === 0) {
    return (
      <span className={cn(pill, 'bg-destructive/15 text-destructive')}>
        <CircleX className="size-3" /> blocked
      </span>
    )
  }
  return (
    <span className="inline-flex flex-wrap gap-1">
      {blocks.map((r) => (
        <RuleBadge key={r.rule_id} ruleId={r.rule_id} allow={false} />
      ))}
    </span>
  )
}

/** A tool the agent ran: name up top, arguments and result folded away. */
export function ToolCallCard({ call }: { call: ToolCall }) {
  const check = call.name === 'check_call' ? parseCheck(call.result) : null
  const failed = call.result?.startsWith('error:') ?? false
  return (
    <details className="group rounded-md border bg-muted/30 text-xs" data-tool={call.name}>
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-2 py-1.5 select-none [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-3 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
        <Wrench className="size-3 shrink-0 text-muted-foreground" />
        <span className="font-mono font-medium">{call.name ?? '(unnamed tool)'}</span>
        <span className="ml-auto flex items-center gap-1">
          {check && <CheckSummary check={check} />}
          {failed && <span className={cn(pill, 'bg-muted text-muted-foreground')}>error</span>}
        </span>
      </summary>
      <div className="space-y-2 border-t px-2 py-2">
        <section>
          <h4 className="mb-1 text-[11px] text-muted-foreground">Arguments</h4>
          <pre className="overflow-x-auto rounded bg-background/60 p-1.5 font-mono text-[11px] leading-snug whitespace-pre-wrap break-all">
            {prettyJson(call.arguments)}
          </pre>
        </section>
        {check && (
          <ul className="space-y-1">
            {check.rules.map((r) => (
              <li key={r.rule_id} className="flex items-start gap-1.5">
                <RuleBadge ruleId={r.rule_id} allow={r.allow} />
                <span className="pt-0.5 text-[11px] text-muted-foreground">{r.reason}</span>
              </li>
            ))}
          </ul>
        )}
        <section>
          <h4 className="mb-1 text-[11px] text-muted-foreground">Result</h4>
          <pre className="max-h-64 overflow-auto rounded bg-background/60 p-1.5 font-mono text-[11px] leading-snug whitespace-pre-wrap break-all">
            {prettyJson(call.result)}
          </pre>
        </section>
      </div>
    </details>
  )
}

/** What the server did after an approval: blocked by the checks, or dialed. */
export function DialResult({ dial }: { dial: Dial }) {
  if (dial.blocked) {
    return (
      <div className="flex flex-wrap items-center gap-1 text-xs">
        <span className="text-muted-foreground">Not dialed:</span>
        {dial.reasons.map((r) => {
          const ruleId = r.split(':', 1)[0]
          return <RuleBadge key={r} ruleId={ruleId} allow={false} />
        })}
      </div>
    )
  }
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
      <Phone className="size-3" />
      <span>
        Outcome <span className="font-mono text-foreground">{dial.outcome ?? 'unknown'}</span>
      </span>
      {dial.call_id && <span className="font-mono">call {dial.call_id}</span>}
    </div>
  )
}

type ApprovalProps = {
  approval: Approval
  check: CheckView | null | undefined
  busy: boolean
  onDecide: (approve: boolean) => void
}

/** A place_call waiting on the person. Approve shows only when the check
 *  passes; a blocked or unchecked call can only be denied. */
export function ApprovalCard({ approval, check, busy, onDecide }: ApprovalProps) {
  const allowed = canApprove(check)
  return (
    <div
      className="space-y-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3"
      data-approval={approval.id}
    >
      <div className="flex items-center gap-2">
        <Phone className="size-3.5 text-amber-500" />
        <span className="text-xs font-semibold">Call waiting for your approval</span>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-muted-foreground">Debtor</dt>
        <dd className="font-medium">
          {approval.name} <span className="font-mono text-muted-foreground">{approval.debtor_id}</span>
        </dd>
        <dt className="text-muted-foreground">Phone</dt>
        <dd className="font-mono">{approval.phone ?? 'no number on file'}</dd>
        <dt className="text-muted-foreground">Checks</dt>
        <dd>
          <CheckSummary check={check} />
          {check && (
            <span className="ml-1 text-[11px] text-muted-foreground">
              {check.source === 'chat' ? 'from the agent’s check' : 'from the list, right now'}
            </span>
          )}
        </dd>
      </dl>
      {!allowed && (
        <p className="text-[11px] text-muted-foreground">
          {check
            ? 'The checks block this call, so it can’t be approved.'
            : 'No check result for this debtor yet, so it can’t be approved.'}
        </p>
      )}
      <div className="flex gap-2">
        {allowed && (
          <Button size="sm" disabled={busy} onClick={() => onDecide(true)}>
            Approve
          </Button>
        )}
        <Button size="sm" variant="outline" disabled={busy} onClick={() => onDecide(false)}>
          Deny
        </Button>
      </div>
      <p className="text-[11px] text-muted-foreground">
        The server runs every check again before it dials.
      </p>
    </div>
  )
}
