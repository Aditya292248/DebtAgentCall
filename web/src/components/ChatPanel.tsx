import { LoaderCircle, SendHorizontal } from 'lucide-react'
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'

import {
  AddPersonApprovalCard,
  ApprovalCard,
  DialResult,
  NoteApprovalCard,
  PaymentApprovalCard,
  ToolCallCard,
} from '@/components/ChatCards'
import { Notice } from '@/components/Notice'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  chatSessionId,
  decideApproval,
  decideFailure,
  sendChat,
  type Approval,
  type ChatTurn,
  type Dial,
  type ToolCall,
} from '@/lib/chat'
import { latestChecks, newerCheck, type CheckView } from '@/lib/checks'

type Item =
  | { kind: 'user'; text: string }
  | { kind: 'agent'; text: string; toolCalls: ToolCall[]; dial: Dial | null }
  | { kind: 'error'; text: string; detail: string }

const NOW_TICK_MS = 15_000

type Props = {
  /** The page's own pre-call result for a debt right now, if it has one. */
  precallCheck: (debtorId: string) => CheckView | null
  /** Open the live view for a call that was just placed. */
  onOpenCall: (callId: string) => void
}

export function ChatPanel({ precallCheck, onOpenCall }: Props) {
  const [sessionId] = useState(chatSessionId)
  const [items, setItems] = useState<Item[]>([])
  const [pending, setPending] = useState<Approval[]>([])
  const [checks, setChecks] = useState<Map<string, CheckView>>(() => new Map())
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const bottom = useRef<HTMLDivElement>(null)
  // The time the approval cards judge a check's age by, at most 15 s behind.
  // A check that just came in is newer than it, so it reads as fresh.
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), NOW_TICK_MS)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [items, pending])

  const take = (turn: ChatTurn) => {
    setItems((xs) => [
      ...xs,
      { kind: 'agent', text: turn.reply, toolCalls: turn.tool_calls, dial: turn.dial },
    ])
    setPending(turn.pending_approvals)
    const fresh = latestChecks(turn.tool_calls)
    if (fresh.size) setChecks((m) => new Map([...m, ...fresh]))
  }

  const fail = (text: string, err: unknown) => {
    const detail = err instanceof Error ? err.message : String(err)
    setItems((xs) => [...xs, { kind: 'error', text, detail }])
  }

  const send = async (e?: FormEvent) => {
    e?.preventDefault()
    const message = draft.trim()
    if (!message || busy) return
    setDraft('')
    setItems((xs) => [...xs, { kind: 'user', text: message }])
    setBusy(true)
    try {
      take(await sendChat(sessionId, message))
    } catch (err) {
      fail('The agent didn’t answer. Try again shortly.', err)
    } finally {
      setBusy(false)
    }
  }

  const decide = async (approval: Approval, approve: boolean) => {
    if (busy) return
    setBusy(true)
    try {
      take(await decideApproval(sessionId, approval.id, approve))
    } catch (err) {
      const { text, closed } = decideFailure(err, approve)
      fail(text, err)
      // The server has no such approval any more, so its card must go too.
      if (closed) setPending((ps) => ps.filter((p) => p.id !== approval.id))
    } finally {
      setBusy(false)
    }
  }

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void send()
    }
  }

  // Whichever check ran last: the agent's in this chat, or the list's, which
  // is re-run every minute.
  const checkFor = (debtorId: string) =>
    newerCheck(checks.get(debtorId), precallCheck(debtorId))

  return (
    <div className="flex h-full flex-col">
      <ScrollArea className="min-h-0 flex-1">
        <div className="mx-auto max-w-3xl space-y-6 px-6 py-8">
          {items.length === 0 && (
            <div className="rounded-xl border border-dashed px-6 py-8 text-center">
              <h2 className="text-base font-semibold">Ask the agent</h2>
              <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-muted-foreground">
                Find someone, check them, or call them, for example{' '}
                <span className="font-medium text-foreground">call Sam Doe</span>. A call always
                waits for your approval here, and the checks run again before anything dials.
              </p>
            </div>
          )}
          {items.map((item, i) => (
            <Message key={i} item={item} onOpenCall={onOpenCall} />
          ))}
          {pending.map((a) =>
            a.action === 'add_person' ? (
              <AddPersonApprovalCard
                key={a.id}
                approval={a}
                busy={busy}
                onDecide={(approve) => void decide(a, approve)}
              />
            ) : a.action === 'record_payment' || a.action === 'reverse_payment' ? (
              <PaymentApprovalCard
                key={a.id}
                approval={a}
                busy={busy}
                onDecide={(approve) => void decide(a, approve)}
              />
            ) : a.action === 'add_note' || a.action === 'schedule_callback' ? (
              <NoteApprovalCard
                key={a.id}
                approval={a}
                busy={busy}
                onDecide={(approve) => void decide(a, approve)}
              />
            ) : (
              <ApprovalCard
                key={a.id}
                approval={a}
                check={checkFor(a.debtor_id)}
                now={now}
                busy={busy}
                onDecide={(approve) => void decide(a, approve)}
              />
            ),
          )}
          {busy && (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <LoaderCircle className="size-4 animate-spin" /> Working
            </p>
          )}
          <div ref={bottom} />
        </div>
      </ScrollArea>
      <div className="px-6 pt-2 pb-5">
        <form
          onSubmit={(e) => void send(e)}
          className="mx-auto flex max-w-3xl items-end gap-2 rounded-xl border bg-card p-2 shadow-sm focus-within:border-ring/60 focus-within:ring-3 focus-within:ring-ring/20"
        >
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKey}
            rows={2}
            placeholder="Message the agent, e.g. call Sam Doe"
            aria-label="Message the agent"
            className="min-h-12 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm leading-relaxed outline-none placeholder:text-muted-foreground"
          />
          <Button type="submit" size="icon-lg" disabled={busy || !draft.trim()} aria-label="Send">
            <SendHorizontal />
          </Button>
        </form>
        <p
          className="mx-auto mt-2 max-w-3xl px-1 text-xs text-muted-foreground"
          title={`Chat session ${sessionId}`}
        >
          Enter to send, Shift+Enter for a new line.
        </p>
      </div>
    </div>
  )
}

function Message({ item, onOpenCall }: { item: Item; onOpenCall: (callId: string) => void }) {
  if (item.kind === 'user') {
    return (
      <div className="flex justify-end">
        <p className="max-w-[80%] rounded-2xl rounded-br-md bg-primary-soft px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap">
          {item.text}
        </p>
      </div>
    )
  }
  if (item.kind === 'error') {
    return <Notice message={item.text} detail={item.detail} />
  }
  return (
    <div className="space-y-2">
      {item.toolCalls.map((c, i) => (
        <ToolCallCard key={i} call={c} />
      ))}
      {item.dial && <DialResult dial={item.dial} onWatch={onOpenCall} />}
      {item.text && (
        <p className="pt-1 text-[0.9375rem] leading-7 whitespace-pre-wrap">{item.text}</p>
      )}
    </div>
  )
}
