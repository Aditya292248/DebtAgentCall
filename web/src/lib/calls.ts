// The read-only call API (`GET /api/calls`), polled while a call is on.
//
// Polling, not Supabase Realtime: Realtime is not set up here, and a read every
// 1.5 s from one open panel is simple and cheap. The panel stops reading when
// the call has ended or the tab is hidden.
import { useCallback, useEffect, useRef, useState } from 'react'

import { POLL_MS, shouldPoll, type CallDetail, type CallSummaryRow } from '@/lib/callView'

const LIST_POLL_MS = 5000

export class NotFound extends Error {}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (res.status === 404) throw new NotFound('No such call')
  if (!res.ok) {
    let detail = (await res.text()).slice(0, 200)
    try {
      detail = (JSON.parse(detail) as { detail?: string }).detail ?? detail
    } catch {
      // Not JSON; show the start of it.
    }
    throw new Error(`${res.status}: ${detail}`)
  }
  return (await res.json()) as T
}

const visible = () => document.visibilityState === 'visible'

/** Run `read` now and then again every `every` ms while `keepGoing()` says so,
 *  one read at a time, pausing while the tab is hidden. */
function usePolling(read: () => Promise<boolean>, every: number, key: string) {
  const readRef = useRef(read)
  useEffect(() => {
    readRef.current = read
  })
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const tick = async () => {
      let again = true
      if (visible()) again = await readRef.current()
      if (!stopped && again) timer = setTimeout(() => void tick(), every)
      // `again` false means the call ended: nothing more to read.
    }
    const wake = () => {
      if (visible() && !stopped) {
        clearTimeout(timer)
        void tick()
      }
    }
    document.addEventListener('visibilitychange', wake)
    void tick()
    return () => {
      stopped = true
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', wake)
    }
  }, [every, key])
}

/** The recent calls, refreshed every few seconds while the tab is visible. */
export function useCalls() {
  const [calls, setCalls] = useState<CallSummaryRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const read = useCallback(async () => {
    try {
      setCalls((await get<{ calls: CallSummaryRow[] }>('/api/calls')).calls)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
    return true
  }, [])
  usePolling(read, LIST_POLL_MS, 'list')
  return { calls, error }
}

/** One call, read every 1.5 s until it has ended. */
export function useCall(callId: string | null) {
  const [loaded, setLoaded] = useState<CallDetail | null>(null)
  const [error, setError] = useState<{ id: string; message: string; missing: boolean } | null>(null)
  const read = useCallback(async () => {
    if (!callId) return false
    try {
      const detail = await get<CallDetail>(`/api/calls/${encodeURIComponent(callId)}`)
      setLoaded(detail)
      setError(null)
      return shouldPoll(detail.ended, true)
    } catch (err) {
      setError({
        id: callId,
        message: err instanceof Error ? err.message : String(err),
        missing: err instanceof NotFound,
      })
      return !(err instanceof NotFound)
    }
  }, [callId])
  usePolling(read, POLL_MS, callId ?? '')
  // What was loaded for another call is not shown for this one.
  return {
    call: loaded && loaded.call_id === callId ? loaded : null,
    error: error && error.id === callId ? error : null,
  }
}
