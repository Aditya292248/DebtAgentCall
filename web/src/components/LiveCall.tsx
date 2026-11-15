import { PhoneCall } from 'lucide-react'

/** Where a call in progress will show (transcript, stage, stop button). Not
 *  wired up yet: the page has no feed of live calls to read from. */
export function LiveCallPlaceholder() {
  return (
    <div className="flex h-full flex-col">
      <div className="flex h-10 shrink-0 items-center gap-2 border-b px-3">
        <PhoneCall className="size-4 text-muted-foreground" />
        <h2 className="text-sm font-semibold">Live call</h2>
        <span className="ml-auto rounded-full border px-2 py-0.5 text-[10px] text-muted-foreground">
          Coming soon
        </span>
      </div>
      <div className="flex min-h-0 flex-1 items-center justify-center p-3">
        <p className="w-full rounded-md border border-dashed p-4 text-center text-xs text-muted-foreground">
          A call you approve will show here while it is connected.
        </p>
      </div>
    </div>
  )
}
