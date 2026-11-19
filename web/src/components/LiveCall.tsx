import { ChevronRight, Hourglass, ShieldAlert, ShieldCheck, Wrench } from 'lucide-react'
import { useEffect, useRef } from 'react'

import { Notice } from '@/components/Notice'
import { useCall, useCalls } from '@/lib/calls'
import {
  buildTimeline,
  describeEvent,
  outcomeLabel,
  stageLabel,
  type CallDetail,
  type CallSummaryRow,
  type CallTool,
  type PendingPlan,
  type TimelineItem,
} from '@/lib/callView'
import { money } from '@/lib/debts'
import { cn } from '@/lib/utils'

const cents = (n: number) => money.format(n / 100)

function timeLabel(iso: string | null): string {
  if (!iso) return ''
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

type Props = {
  /** The call to show; null means the most recent one. */
  callId: string | null
  onSelect: (callId: string) => void
}

/** Watch one call: its conversation, stage, tools and compliance events, then
 *  its outcome and summary. Read-only: nothing here can dial or approve. */
export function LiveCall({ callId, onSelect }: Props) {
  const { calls, error: listError } = useCalls()
  const shownId = callId ?? calls?.[0]?.call_id ?? null
  const { call, error } = useCall(shownId)

  return (
    <div className="space-y-6">
      {listError && <Notice message="The call list didn’t load." detail={listError} />}
      {calls && calls.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No calls yet. When you approve a call in the chat, it shows up here.
        </p>
      )}
      {calls && calls.length > 0 && (
        <CallPicker calls={calls} value={shownId} onChange={onSelect} />
      )}
      {error?.missing && <p className="text-sm text-muted-foreground">That call was not found.</p>}
      {error && !error.missing && (
        <Notice message="The call didn’t load. It will try again." detail={error.message} />
      )}
      {call && <CallBody call={call} />}
    </div>
  )
}

function CallPicker({
  calls,
  value,
  onChange,
}: {
  calls: CallSummaryRow[]
  value: string | null
  onChange: (id: string) => void
}) {
  return (
    <label className="block text-sm">
      <span className="mb-1.5 block text-xs text-muted-foreground">Call</span>
      <select
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value)}
        className="h-9 w-full rounded-lg border border-input bg-card px-2.5 text-sm"
      >
        {calls.map((c) => (
          <option key={c.call_id} value={c.call_id}>
            {c.debtor_name} · {c.account_number} · {timeLabel(c.started)} ·{' '}
            {c.ended ? outcomeLabel(c.outcome) : 'in progress'}
          </option>
        ))}
      </select>
    </label>
  )
}

function CallBody({ call }: { call: CallDetail }) {
  const timeline = buildTimeline(call)
  const end = useRef<HTMLDivElement>(null)
  const count = timeline.length
  useEffect(() => {
    end.current?.scrollIntoView({ block: 'end' })
  }, [count, call.call_id])

  return (
    <div className="space-y-5">
      <header className="space-y-2">
        <div className="flex items-baseline justify-between gap-3">
          <h3 className="text-base font-semibold">{call.debtor_name ?? 'Unknown caller'}</h3>
          <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
            {call.account_number} · {timeLabel(call.started)}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span className="flex items-center gap-1.5">
            <span
              aria-hidden
              className={cn(
                'size-2 rounded-full',
                call.ended ? 'bg-muted-foreground/40' : 'animate-pulse bg-primary',
              )}
            />
            {call.ended ? 'Ended' : 'Live'}
          </span>
          <span>
            Stage <span className="font-medium text-foreground">{stageLabel(call.state.stage)}</span>
          </span>
          <span className="flex items-center gap-1">
            {call.state.verified ? (
              <ShieldCheck className="size-3.5" />
            ) : (
              <ShieldAlert className="size-3.5" />
            )}
            {call.state.verified ? 'Verified' : 'Not verified'}
          </span>
          {call.state.disclosed && <span>Disclosure given</span>}
        </div>
      </header>

      <div className="space-y-3">
        {count === 0 && (
          <p className="text-sm text-muted-foreground">Nothing has been said yet.</p>
        )}
        {timeline.map((item) => (
          <Row key={item.key} item={item} />
        ))}
        {call.pending_plans.map((p, i) => (
          <PendingPlanNote key={i} plan={p} />
        ))}
        <div ref={end} />
      </div>

      {call.ended && <Outcome call={call} />}
    </div>
  )
}

function Row({ item }: { item: TimelineItem }) {
  if (item.kind === 'message') {
    const agent = item.message.speaker === 'agent'
    return (
      <div className={cn('flex flex-col gap-1', agent ? 'items-end' : 'items-start')}>
        <span className="px-1 text-xs text-muted-foreground">{agent ? 'Agent' : 'Caller'}</span>
        <p
          className={cn(
            'max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed break-words whitespace-pre-wrap',
            agent ? 'rounded-br-md bg-primary-soft' : 'rounded-bl-md bg-muted',
          )}
        >
          {item.message.text}
        </p>
      </div>
    )
  }
  if (item.kind === 'tool') return <Tool tool={item.tool} />
  const { label, sentence } = describeEvent(item.event)
  return (
    <p className="px-1 text-xs leading-relaxed text-muted-foreground">
      {label}
      {sentence && (
        <>
          {' '}
          <span className="line-through decoration-muted-foreground/60">“{sentence}”</span>
        </>
      )}
    </p>
  )
}

function Tool({ tool }: { tool: CallTool }) {
  const args = JSON.stringify(tool.arguments, null, 2)
  return (
    <details className="group rounded-lg border bg-card text-xs" data-tool={tool.name}>
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-muted-foreground select-none hover:text-foreground [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-3.5 shrink-0 transition-transform group-open:rotate-90" />
        <Wrench className="size-3.5 shrink-0" />
        <span>
          Ran <span className="font-medium text-foreground">{tool.name}</span>
        </span>
        {tool.result === null && <span className="ml-auto">waiting for the answer</span>}
      </summary>
      <div className="space-y-3 border-t px-3 py-3">
        <section>
          <h4 className="mb-1 text-muted-foreground">Arguments</h4>
          <pre className="rounded-md bg-muted p-2 font-mono leading-relaxed break-all whitespace-pre-wrap">
            {args}
          </pre>
        </section>
        <section>
          <h4 className="mb-1 text-muted-foreground">Result</h4>
          <pre className="rounded-md bg-muted p-2 font-mono leading-relaxed break-all whitespace-pre-wrap">
            {tool.result ?? 'No answer saved yet.'}
          </pre>
        </section>
      </div>
    </details>
  )
}

function PendingPlanNote({ plan }: { plan: PendingPlan }) {
  return (
    <p className="flex items-start gap-2 rounded-lg border border-dashed px-3 py-2.5 text-sm text-muted-foreground">
      <Hourglass className="mt-0.5 size-4 shrink-0" />
      <span>
        Waiting for a person to approve a payment plan: {cents(plan.down_cents)} down, then{' '}
        {plan.installments} {plan.frequency} payments of {cents(plan.installment_cents)}.
      </span>
    </p>
  )
}

function Outcome({ call }: { call: CallDetail }) {
  return (
    <section className="space-y-2 rounded-xl border bg-card p-4">
      <h4 className="text-xs text-muted-foreground">After the call</h4>
      <p className="text-sm">
        Outcome <span className="font-medium">{outcomeLabel(call.outcome)}</span>
      </p>
      {call.summary ? (
        <div className="space-y-2 text-sm leading-relaxed">
          <p className="text-muted-foreground">{call.summary.facts}</p>
          {call.summary.narrative && <p>{call.summary.narrative}</p>}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">No summary was recorded for this call.</p>
      )}
    </section>
  )
}
