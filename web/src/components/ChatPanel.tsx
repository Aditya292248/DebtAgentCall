import { Bot, ImageUp, LoaderCircle, SendHorizontal } from 'lucide-react'
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'

import { ApprovalCard, DialResult, ToolCallCard } from '@/components/ChatCards'
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
}

export function ChatPanel({ precallCheck }: Props) {
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
      <div className="flex h-10 shrink-0 items-center gap-2 border-b px-3">
        <Bot className="size-4 text-muted-foreground" />
        <h2 className="text-sm font-semibold">Agent</h2>
        <span className="ml-auto font-mono text-[10px] text-muted-foreground" title="Chat session id">
          {sessionId}
        </span>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-3 p-3">
          {items.length === 0 && (
            <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
              Ask the agent to find someone, check them, or call them, for example{' '}
              <span className="font-mono text-foreground">call Sam Doe</span>. A call always waits
              for your approval here, and the checks run again before anything dials.
            </p>
          )}
          {items.map((item, i) => (
            <Message key={i} item={item} />
          ))}
          {pending.map((a) => (
            <ApprovalCard
              key={a.id}
              approval={a}
              check={checkFor(a.debtor_id)}
              now={now}
              busy={busy}
              onDecide={(approve) => void decide(a, approve)}
            />
          ))}
          {busy && (
            <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <LoaderCircle className="size-3 animate-spin" /> Working
            </p>
          )}
          <div ref={bottom} />
        </div>
      </ScrollArea>
      <div className="space-y-2 border-t p-3">
        <div
          aria-disabled="true"
          title="Bill upload isn't available yet"
          className="flex items-center gap-2 rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground opacity-60"
        >
          <ImageUp className="size-3.5" />
          Drop a bill image here (coming soon)
        </div>
        <form onSubmit={(e) => void send(e)} className="flex items-end gap-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKey}
            rows={2}
            placeholder="Message the agent, e.g. call Sam Doe"
            aria-label="Message the agent"
            className="min-h-10 flex-1 resize-none rounded-md border bg-transparent px-2.5 py-2 text-sm outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring/50"
          />
          <Button
            type="submit"
            size="icon"
            disabled={busy || !draft.trim()}
            aria-label="Send"
          >
            <SendHorizontal />
          </Button>
        </form>
      </div>
    </div>
  )
}

function Message({ item }: { item: Item }) {
  if (item.kind === 'user') {
    return (
      <div className="flex justify-end">
        <p className="max-w-[85%] rounded-lg bg-primary px-3 py-2 text-sm whitespace-pre-wrap text-primary-foreground">
          {item.text}
        </p>
      </div>
    )
  }
  if (item.kind === 'error') {
    return <Notice message={item.text} detail={item.detail} />
  }
  return (
    <div className="space-y-1.5">
      {item.toolCalls.map((c, i) => (
        <ToolCallCard key={i} call={c} />
      ))}
      {item.dial && <DialResult dial={item.dial} />}
      {item.text && (
        <p className="text-sm leading-relaxed whitespace-pre-wrap">{item.text}</p>
      )}
    </div>
  )
}
