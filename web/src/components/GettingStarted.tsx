import { PhoneCall, ShieldCheck, UserPlus, type LucideIcon } from 'lucide-react'

import { Button } from '@/components/ui/button'

type Step = { icon: LucideIcon; title: string; body: string }

const STEPS: Step[] = [
  {
    icon: UserPlus,
    title: 'Add your people',
    body: 'Each person, their phone number and what they owe.',
  },
  {
    icon: ShieldCheck,
    title: 'Checks run first',
    body: 'Calling hours, call limits, consent and stop requests are checked by code before every call. The AI never decides.',
  },
  {
    icon: PhoneCall,
    title: 'You approve each call',
    body: 'The agent asks you here. Nothing dials until you approve, and the checks run again at that moment.',
  },
]

/** Shown in the chat while nobody has been added: what this is and the three steps,
 *  with a button that opens the add-person form. It holds no data of its own. */
export function GettingStarted({ onAdd }: { onAdd: () => void }) {
  return (
    <section aria-label="Getting started" className="space-y-5">
      <ol className="grid gap-3 sm:grid-cols-3">
        {STEPS.map(({ icon: Icon, title, body }, i) => (
          <li
            key={title}
            className="flex flex-col gap-3 rounded-xl border bg-card p-4 shadow-xs transition-colors hover:bg-accent/40"
          >
            <div className="flex items-center justify-between">
              <span className="flex size-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Icon className="size-[18px]" aria-hidden />
              </span>
              <span className="text-xs font-medium text-muted-foreground tabular-nums">
                Step {i + 1}
              </span>
            </div>
            <div className="space-y-1">
              <h3 className="text-sm font-semibold">{title}</h3>
              <p className="text-sm leading-relaxed text-muted-foreground">{body}</p>
            </div>
          </li>
        ))}
      </ol>
      <div className="flex justify-center">
        <Button onClick={onAdd}>
          <UserPlus /> Add your first person
        </Button>
      </div>
    </section>
  )
}
