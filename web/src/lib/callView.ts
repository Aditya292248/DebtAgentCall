// What the live call view shows, built from the call API's answer. No imports,
// so `node --test` can load it directly.

export type CallSummaryRow = {
  call_id: string
  debtor_name: string
  account_number: string
  started: string | null
  stage: string | null
  outcome: string
  ended: boolean
}

export type CallMessage = { seq: number; at: string | null; speaker: 'caller' | 'agent'; text: string }

export type CallTool = {
  seq: number
  at: string | null
  name: string
  arguments: unknown
  /** null: the tool has not answered yet. */
  result: string | null
}

export type CallEvent = {
  seq: number
  at: string | null
  kind: string
  rule_id: string
  decision: 'allow' | 'block'
  reason: string
  sentence: string | null
}

export type PendingPlan = {
  down_cents: number
  installments: number
  frequency: string
  installment_cents: number
  created: string | null
}

export type CallDetail = {
  call_id: string
  debtor_name: string | null
  account_number: string | null
  started: string | null
  ended: boolean
  outcome: string | null
  state: { stage: string | null; verified: boolean; disclosed: boolean; turns: number }
  messages: CallMessage[]
  tool_calls: CallTool[]
  pending_plans: PendingPlan[]
  audit_events: CallEvent[]
  summary: { outcome: string; facts: string; narrative: string | null; created: string | null } | null
}

export type TimelineItem =
  | { kind: 'message'; key: string; message: CallMessage }
  | { kind: 'tool'; key: string; tool: CallTool }
  | { kind: 'event'; key: string; event: CallEvent }

/** How often an active call is read again. */
export const POLL_MS = 1500

/** Poll only while the call is on and the tab can be seen. */
export function shouldPoll(ended: boolean, visible: boolean): boolean {
  return !ended && visible
}

const STAGES: Record<string, string> = {
  greeting: 'Greeting',
  verify: 'Verifying',
  disclose: 'Disclosing',
  discuss: 'Discussing',
  wrap: 'Wrapping up',
  third_party: 'Third party',
  stopped: 'Stopped',
}

export function stageLabel(stage: string | null | undefined): string {
  if (!stage) return 'No stage yet'
  return STAGES[stage] ?? stage.replace(/_/g, ' ')
}

/** "ended_by_stop" -> "Ended by stop". */
export function outcomeLabel(outcome: string | null | undefined): string {
  if (!outcome) return 'No outcome yet'
  const text = outcome.replace(/_/g, ' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** Only these compliance events are shown: what was blocked, and stop triggers. */
export function worthShowing(event: CallEvent): boolean {
  return event.decision === 'block' || event.kind === 'stop_trigger'
}

const RANK = { message: 0, tool: 1, event: 2 } as const

/** Messages, tool calls and compliance events in the order they happened. */
export function buildTimeline(call: CallDetail): TimelineItem[] {
  const items: TimelineItem[] = [
    ...call.messages.map((message): TimelineItem => ({
      kind: 'message',
      key: `m${message.seq}`,
      message,
    })),
    ...call.tool_calls.map((tool, i): TimelineItem => ({
      kind: 'tool',
      key: `t${tool.seq}-${i}`,
      tool,
    })),
    ...call.audit_events.filter(worthShowing).map((event): TimelineItem => ({
      kind: 'event',
      key: `e${event.seq}`,
      event,
    })),
  ]
  const at = (item: TimelineItem): number => {
    const raw =
      item.kind === 'message' ? item.message.at : item.kind === 'tool' ? item.tool.at : item.event.at
    const ms = raw ? Date.parse(raw) : NaN
    return Number.isNaN(ms) ? 0 : ms
  }
  const seq = (item: TimelineItem): number =>
    item.kind === 'message' ? item.message.seq : item.kind === 'tool' ? item.tool.seq : item.event.seq
  return items
    .map((item, index) => ({ item, index }))
    .sort(
      (a, b) =>
        at(a.item) - at(b.item) ||
        RANK[a.item.kind] - RANK[b.item.kind] ||
        seq(a.item) - seq(b.item) ||
        a.index - b.index,
    )
    .map(({ item }) => item)
}

/** The line shown for a compliance event. A blocked sentence is "not spoken". */
export function describeEvent(event: CallEvent): { label: string; sentence: string | null } {
  if (event.kind === 'utterance_blocked') {
    return { label: `Not spoken: ${event.reason}`, sentence: event.sentence }
  }
  if (event.kind === 'stop_trigger') {
    return { label: `Stop trigger fired: ${event.reason}`, sentence: null }
  }
  return { label: `Check blocked (${event.rule_id}): ${event.reason}`, sentence: null }
}
