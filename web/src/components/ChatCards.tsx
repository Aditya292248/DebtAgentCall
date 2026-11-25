import {
  ChevronRight,
  CircleCheck,
  CircleHelp,
  CircleX,
  Phone,
  NotebookPen,
  Receipt,
  UserPlus,
  Wrench,
} from 'lucide-react'

import { Button } from '@/components/ui/button'
import { prettyJson, type Approval, type Dial, type ToolCall } from '@/lib/chat'
import {
  blockedRules,
  canApprove,
  isFresh,
  MAX_CHECK_AGE_MS,
  parseCheck,
  passes,
  type CheckView,
} from '@/lib/checks'
import { ruleName } from '@/lib/rules'
import { cn } from '@/lib/utils'

const pill = 'inline-flex items-center gap-1 rounded-md border bg-background px-1.5 py-0.5 text-xs'

/** One rule's result: "pass" or "blocked", with the rule's name. Never color
 *  alone: the color is only on the small icon. */
export function RuleBadge({ ruleId, allow }: { ruleId: string; allow: boolean }) {
  return (
    <span className={pill} title={ruleId}>
      {allow ? (
        <CircleCheck className="size-3 text-ok" />
      ) : (
        <CircleX className="size-3 text-destructive" />
      )}
      <span className="text-muted-foreground">{allow ? 'pass' : 'blocked'}</span>
      {ruleName(ruleId)}
    </span>
  )
}

/** The whole check: "pass" if every rule allows, else each blocking rule. */
export function CheckSummary({ check }: { check: CheckView | null | undefined }) {
  if (!check) {
    return (
      <span className={cn(pill, 'text-muted-foreground')}>
        <CircleHelp className="size-3" /> not checked
      </span>
    )
  }
  const blocks = blockedRules(check)
  if (passes(check)) {
    return (
      <span className={pill}>
        <CircleCheck className="size-3 text-ok" /> pass
        <span className="text-muted-foreground">{check.rules.length} rules</span>
      </span>
    )
  }
  if (blocks.length === 0) {
    return (
      <span className={pill}>
        <CircleX className="size-3 text-destructive" /> blocked
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

/** A tool the agent ran: one quiet line, arguments and result folded away. */
export function ToolCallCard({ call }: { call: ToolCall }) {
  const check = call.name === 'check_call' ? parseCheck(call.result) : null
  const failed = call.result?.startsWith('error:') ?? false
  return (
    <details className="group rounded-lg border bg-card text-xs" data-tool={call.name}>
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2 px-3 py-2 text-muted-foreground select-none hover:text-foreground [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-3.5 shrink-0 transition-transform group-open:rotate-90" />
        <Wrench className="size-3.5 shrink-0" />
        <span>
          Ran <span className="font-medium text-foreground">{call.name ?? '(unnamed tool)'}</span>
        </span>
        <span className="ml-auto flex flex-wrap items-center gap-1">
          {check && <CheckSummary check={check} />}
          {failed && <span className={cn(pill, 'text-muted-foreground')}>error</span>}
        </span>
      </summary>
      <div className="space-y-3 border-t px-3 py-3">
        <section>
          <h4 className="mb-1 text-xs text-muted-foreground">Arguments</h4>
          <pre className="rounded-md bg-muted p-2 font-mono text-xs leading-relaxed break-all whitespace-pre-wrap">
            {prettyJson(call.arguments)}
          </pre>
        </section>
        {check && (
          <ul className="space-y-1.5">
            {check.rules.map((r) => (
              <li key={r.rule_id} className="flex items-start gap-2">
                <RuleBadge ruleId={r.rule_id} allow={r.allow} />
                <span className="pt-0.5 text-xs text-muted-foreground">{r.reason}</span>
              </li>
            ))}
          </ul>
        )}
        <section>
          <h4 className="mb-1 text-xs text-muted-foreground">Result</h4>
          <pre className="rounded-md bg-muted p-2 font-mono text-xs leading-relaxed break-all whitespace-pre-wrap">
            {prettyJson(call.result)}
          </pre>
        </section>
      </div>
    </details>
  )
}

/** What the server did after an approval: blocked by the checks, or dialed. */
export function DialResult({
  dial,
  onWatch,
}: {
  dial: Dial
  onWatch?: (callId: string) => void
}) {
  if (dial.blocked) {
    return (
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
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
        Outcome <span className="font-medium text-foreground">{dial.outcome ?? 'unknown'}</span>
      </span>
      {dial.call_id && <span>call {dial.call_id}</span>}
      {dial.call_id && onWatch && (
        <Button
          variant="outline"
          size="sm"
          onClick={() => onWatch(dial.call_id as string)}
        >
          Watch live
        </Button>
      )}
    </div>
  )
}

type PaymentApprovalProps = {
  approval: Approval
  busy: boolean
  onDecide: (approve: boolean) => void
}

/** A record_payment or reverse_payment waiting on the person: the exact debtor,
 *  amount and date the server will write. Nothing is written until Approve, and
 *  the server checks the balance again then. */
export function PaymentApprovalCard({ approval, busy, onDecide }: PaymentApprovalProps) {
  const d = approval.details
  const reversal = approval.action === 'reverse_payment'
  return (
    <div
      className="space-y-4 rounded-xl border border-primary/40 bg-card p-5 shadow-sm ring-4 ring-primary-soft"
      data-approval={approval.id}
    >
      <div className="flex items-center gap-2.5">
        <span className="flex size-7 items-center justify-center rounded-full bg-primary-soft text-primary">
          <Receipt className="size-3.5" />
        </span>
        <span className="text-sm font-semibold">
          {reversal ? 'Payment reversal' : 'Payment'} waiting for your approval
        </span>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
        <dt className="text-muted-foreground">Debtor</dt>
        <dd className="font-medium">
          {approval.name}{' '}
          <span className="font-normal text-muted-foreground">{approval.debtor_id}</span>
        </dd>
        <dt className="text-muted-foreground">Amount</dt>
        <dd className="font-medium tabular-nums">{d.amount ?? 'unknown'}</dd>
        <dt className="text-muted-foreground">{reversal ? 'Paid on' : 'Date'}</dt>
        <dd className="tabular-nums">
          {d.paid_on ?? 'unknown'}
          {d.time_zone && !reversal && (
            <span className="text-xs text-muted-foreground"> ({d.time_zone})</span>
          )}
        </dd>
        <dt className="text-muted-foreground">Method</dt>
        <dd>{d.method ?? 'unknown'}</dd>
        {reversal ? (
          <>
            <dt className="text-muted-foreground">Reason</dt>
            <dd>{d.reason}</dd>
          </>
        ) : (
          <>
            <dt className="text-muted-foreground">Balance</dt>
            <dd className="tabular-nums">
              {d.balance_before} to {d.balance_after}
            </dd>
          </>
        )}
      </dl>
      <div className="flex flex-wrap items-center gap-2 border-t pt-4">
        <Button size="lg" className="px-4" disabled={busy} onClick={() => onDecide(true)}>
          Approve
        </Button>
        <Button
          size="lg"
          variant="outline"
          className="px-4"
          disabled={busy}
          onClick={() => onDecide(false)}
        >
          Deny
        </Button>
        <p className="ml-auto text-xs text-muted-foreground">
          {reversal
            ? 'Adds a reversing entry. The payment itself is kept.'
            : 'The server checks the balance again before it writes.'}
        </p>
      </div>
    </div>
  )
}

/** An add_note or schedule_callback waiting on the person. Nothing is saved
 *  until Approve; the server checks a callback against the pre-call rules again. */
export function NoteApprovalCard({ approval, busy, onDecide }: PaymentApprovalProps) {
  const d = approval.details
  const note = approval.action === 'add_note'
  return (
    <div
      className="space-y-4 rounded-xl border border-primary/40 bg-card p-5 shadow-sm ring-4 ring-primary-soft"
      data-approval={approval.id}
    >
      <div className="flex items-center gap-2.5">
        <span className="flex size-7 items-center justify-center rounded-full bg-primary-soft text-primary">
          {note ? <NotebookPen className="size-3.5" /> : <Phone className="size-3.5" />}
        </span>
        <span className="text-sm font-semibold">
          {note ? 'Note' : 'Callback'} waiting for your approval
        </span>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
        <dt className="text-muted-foreground">Debtor</dt>
        <dd className="font-medium">
          {approval.name}{' '}
          <span className="font-normal text-muted-foreground">{approval.debtor_id}</span>
        </dd>
        <dt className="text-muted-foreground">{note ? 'Note' : 'When'}</dt>
        <dd className={note ? 'whitespace-pre-wrap' : 'tabular-nums'}>
          {(note ? d.text : d.shown) ?? 'unknown'}
        </dd>
      </dl>
      <div className="flex flex-wrap items-center gap-2 border-t pt-4">
        <Button size="lg" className="px-4" disabled={busy} onClick={() => onDecide(true)}>
          Approve
        </Button>
        <Button
          size="lg"
          variant="outline"
          className="px-4"
          disabled={busy}
          onClick={() => onDecide(false)}
        >
          Deny
        </Button>
        <p className="ml-auto text-xs text-muted-foreground">
          {note
            ? 'Notes are kept and can’t be edited.'
            : 'The server checks calling hours and stops again before it saves.'}
        </p>
      </div>
    </div>
  )
}

/** An add_person waiting on the person: the exact person, masked phone, debt and
 *  consent the server will save. Nothing is saved until Approve. Without consent
 *  the person is saved and calls stay blocked, and the card says so. */
export function AddPersonApprovalCard({ approval, busy, onDecide }: PaymentApprovalProps) {
  const d = approval.details
  return (
    <div
      className="space-y-4 rounded-xl border border-primary/40 bg-card p-5 shadow-sm ring-4 ring-primary-soft"
      data-approval={approval.id}
    >
      <div className="flex items-center gap-2.5">
        <span className="flex size-7 items-center justify-center rounded-full bg-primary-soft text-primary">
          <UserPlus className="size-3.5" />
        </span>
        <span className="text-sm font-semibold">New person waiting for your approval</span>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
        <dt className="text-muted-foreground">Name</dt>
        <dd className="font-medium">{approval.name}</dd>
        <dt className="text-muted-foreground">Phone</dt>
        <dd className="tabular-nums">{approval.phone ?? 'unknown'}</dd>
        <dt className="text-muted-foreground">Owes</dt>
        <dd className="font-medium tabular-nums">
          {d.amount ?? 'unknown'}{' '}
          <span className="font-normal text-muted-foreground">to {d.creditor ?? 'unknown'}</span>
        </dd>
        <dt className="text-muted-foreground">Account</dt>
        <dd>
          {d.account_number ?? approval.debtor_id}
          {d.account_generated && (
            <span className="text-xs text-muted-foreground"> (made for you)</span>
          )}
        </dd>
        <dt className="text-muted-foreground">Consent</dt>
        <dd className={d.consent_recorded ? undefined : 'font-medium'}>{d.consent_line ?? 'unknown'}</dd>
      </dl>
      <div className="flex flex-wrap items-center gap-2 border-t pt-4">
        <Button size="lg" className="px-4" disabled={busy} onClick={() => onDecide(true)}>
          Approve
        </Button>
        <Button
          size="lg"
          variant="outline"
          className="px-4"
          disabled={busy}
          onClick={() => onDecide(false)}
        >
          Deny
        </Button>
        <p className="ml-auto text-xs text-muted-foreground">
          Nothing is saved until you approve. The server checks for a duplicate number first.
        </p>
      </div>
    </div>
  )
}

type ApprovalProps = {
  approval: Approval
  check: CheckView | null | undefined
  /** The time now, in ms, for how old the check is. */
  now: number
  busy: boolean
  onDecide: (approve: boolean) => void
}

/** A place_call waiting on the person. Approve shows only when a recent check
 *  passes; a blocked, unchecked or stale call can only be denied. */
export function ApprovalCard({ approval, check, now, busy, onDecide }: ApprovalProps) {
  const allowed = canApprove(check, now)
  return (
    <div
      className="space-y-4 rounded-xl border border-primary/40 bg-card p-5 shadow-sm ring-4 ring-primary-soft"
      data-approval={approval.id}
    >
      <div className="flex items-center gap-2.5">
        <span className="flex size-7 items-center justify-center rounded-full bg-primary-soft text-primary">
          <Phone className="size-3.5" />
        </span>
        <span className="text-sm font-semibold">Call waiting for your approval</span>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
        <dt className="text-muted-foreground">Debtor</dt>
        <dd className="font-medium">
          {approval.name}{' '}
          <span className="font-normal text-muted-foreground">{approval.debtor_id}</span>
        </dd>
        <dt className="text-muted-foreground">Phone</dt>
        <dd className="tabular-nums">{approval.phone ?? 'no number on file'}</dd>
        <dt className="text-muted-foreground">Checks</dt>
        <dd className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <CheckSummary check={check} />
          {check && (
            <span className="text-xs text-muted-foreground">
              {check.source === 'chat' ? 'from the agent’s check' : 'from the list, right now'}
            </span>
          )}
        </dd>
      </dl>
      {!allowed && (
        <p className="text-sm text-muted-foreground">
          {!check
            ? 'No check result for this debtor yet, so it can’t be approved.'
            : !passes(check)
              ? 'The checks block this call, so it can’t be approved.'
              : !isFresh(check, now)
                ? `This check is more than ${MAX_CHECK_AGE_MS / 60_000} minutes old. Ask the agent to check again before approving.`
                : 'This call can’t be approved.'}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2 border-t pt-4">
        {allowed && (
          <Button size="lg" className="px-4" disabled={busy} onClick={() => onDecide(true)}>
            Approve
          </Button>
        )}
        <Button
          size="lg"
          variant="outline"
          className="px-4"
          disabled={busy}
          onClick={() => onDecide(false)}
        >
          Deny
        </Button>
        <p className="ml-auto text-xs text-muted-foreground">
          The server runs every check again before it dials.
        </p>
      </div>
    </div>
  )
}
