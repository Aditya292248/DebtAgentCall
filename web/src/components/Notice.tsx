import { CircleAlert } from 'lucide-react'
import { useEffect } from 'react'

/** A short, plain message for the person using the page. The technical detail
 *  (for whoever runs the app) stays in the tooltip and the browser console. */
export function Notice({ message, detail }: { message: string; detail?: string | null }) {
  useEffect(() => {
    if (detail) console.warn(`${message}: ${detail}`)
  }, [message, detail])
  return (
    <p
      className="flex items-start gap-1.5 rounded-md bg-destructive/10 p-2 text-xs text-destructive"
      title={detail ?? undefined}
    >
      <CircleAlert className="mt-px size-3.5 shrink-0" />
      {message}
    </p>
  )
}
