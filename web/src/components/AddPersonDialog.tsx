import { CircleCheck, Receipt, ShieldCheck, UserPlus, UserRound } from 'lucide-react'
import { useState } from 'react'

import { Notice } from '@/components/Notice'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import {
  addPerson,
  EMPTY_FORM,
  FIELD_NAMES,
  PersonError,
  validateForm,
  type FieldName,
  type Form,
  type Person,
  type Problems,
} from '@/lib/people'
import { cn } from '@/lib/utils'

type Props = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Called after a person is saved, so the list can reload. */
  onAdded: (person: Person) => void
}

type FieldProps = {
  name: FieldName
  label: string
  form: Form
  problems: Problems
  set: (name: FieldName, value: string) => void
  /** Columns taken out of the dialog's six. */
  span?: 1 | 2 | 3 | 4 | 6
  placeholder?: string
  type?: string
  hint?: string
  prefix?: string
}

const SPAN = { 1: 'col-span-1', 2: 'col-span-2', 3: 'col-span-3', 4: 'col-span-4', 6: 'col-span-6' }

function Field({ name, label, form, problems, set, span = 3, placeholder, type, hint, prefix }: FieldProps) {
  const id = `person-${name}`
  const problem = problems[name]
  return (
    <div className={SPAN[span]}>
      <label htmlFor={id} className="mb-1.5 block text-xs font-medium text-foreground/80">
        {label}
      </label>
      <div className="relative">
        {prefix && (
          <span className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-sm text-muted-foreground">
            {prefix}
          </span>
        )}
        <Input
          id={id}
          type={type}
          value={form[name]}
          placeholder={placeholder}
          onChange={(e) => set(name, e.target.value)}
          aria-invalid={problem ? true : undefined}
          aria-describedby={problem ? `${id}-problem` : hint ? `${id}-hint` : undefined}
          autoComplete="off"
          className={cn('h-9 bg-background', prefix && 'pl-7')}
        />
      </div>
      {problem ? (
        <p id={`${id}-problem`} className="mt-1.5 text-xs text-destructive">
          {problem}
        </p>
      ) : (
        hint && (
          <p id={`${id}-hint`} className="mt-1.5 text-xs text-muted-foreground">
            {hint}
          </p>
        )
      )}
    </div>
  )
}

function SectionTitle({ icon: Icon, children }: { icon: typeof UserRound; children: string }) {
  return (
    <h3 className="col-span-6 flex items-center gap-2 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
      <Icon className="size-3.5" aria-hidden />
      {children}
    </h3>
  )
}

/** Cell or landline, as two joined buttons. */
function LineType({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="col-span-2">
      <span id="person-line_type-label" className="mb-1.5 block text-xs font-medium text-foreground/80">
        Line type
      </span>
      <div
        role="radiogroup"
        aria-labelledby="person-line_type-label"
        className="grid h-9 grid-cols-2 rounded-lg border border-input bg-muted/50 p-0.5"
      >
        {(['cell', 'landline'] as const).map((v) => (
          <button
            key={v}
            type="button"
            role="radio"
            aria-checked={value === v}
            onClick={() => onChange(v)}
            className={cn(
              'rounded-md text-sm capitalize transition-colors focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none',
              value === v
                ? 'bg-background font-medium text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {v}
          </button>
        ))}
      </div>
    </div>
  )
}

export function AddPersonDialog({ open, onOpenChange, onAdded }: Props) {
  const [form, setForm] = useState<Form>(EMPTY_FORM)
  const [problems, setProblems] = useState<Problems>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState<Person | null>(null)

  const set = (name: FieldName, value: string) => {
    setForm((f) => ({ ...f, [name]: value }))
    setProblems((p) => ({ ...p, [name]: undefined }))
  }

  const close = (next: boolean) => {
    if (busy) return
    onOpenChange(next)
    if (!next) {
      setForm(EMPTY_FORM)
      setProblems({})
      setError(null)
      setSaved(null)
    }
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    const found = validateForm(form)
    setProblems(found)
    setError(null)
    const bad = FIELD_NAMES.filter((n) => found[n])
    if (bad.length) {
      setError(
        bad.length === 1
          ? 'One field needs another look.'
          : `${bad.length} fields need another look.`,
      )
      document.getElementById(`person-${bad[0]}`)?.focus()
      return
    }
    setBusy(true)
    try {
      const person = await addPerson(form)
      setSaved(person)
      onAdded(person)
    } catch (err) {
      if (err instanceof PersonError) {
        setProblems(err.problems)
        setError(err.message)
      } else {
        setError("Couldn't reach the server. Nothing was saved.")
      }
    } finally {
      setBusy(false)
    }
  }

  const common = { form, problems, set }
  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-2xl">
        {saved ? (
          <div className="space-y-5 p-8">
            <div className="flex items-start gap-4">
              <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-ok/10 text-ok">
                <CircleCheck className="size-5" aria-hidden />
              </span>
              <div className="space-y-1 pt-0.5">
                <DialogTitle className="text-lg">{saved.name} was added</DialogTitle>
                <DialogDescription>
                  Account {saved.account_number} · phone {saved.phone}
                </DialogDescription>
              </div>
            </div>
            <p className="rounded-lg border bg-muted/50 p-3.5 text-sm leading-relaxed text-muted-foreground">
              {saved.consent_recorded
                ? 'Consent was recorded, so the pre-call checks decide the rest.'
                : 'No consent is on file, so calls to this number stay blocked until it is recorded.'}
            </p>
            <div className="flex justify-end gap-2">
              <Button
                variant="outline"
                onClick={() => {
                  setSaved(null)
                  setForm(EMPTY_FORM)
                }}
              >
                Add another
              </Button>
              <Button onClick={() => close(false)}>Done</Button>
            </div>
          </div>
        ) : (
          <form onSubmit={submit} noValidate className="flex min-h-0 flex-col">
            <div className="flex items-start gap-3.5 border-b px-7 pt-6 pb-5">
              <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <UserPlus className="size-4" aria-hidden />
              </span>
              <div className="space-y-0.5">
                <DialogTitle className="text-lg">Add person</DialogTitle>
                <DialogDescription>
                  A person, their phone and one debt. Nothing is saved until you add them.
                </DialogDescription>
              </div>
            </div>

            <div className="min-h-0 flex-1 space-y-8 overflow-y-auto px-7 py-6">
              <section className="grid grid-cols-6 gap-x-4 gap-y-4">
                <SectionTitle icon={UserRound}>Person</SectionTitle>
                <Field {...common} name="first_name" label="First name" />
                <Field {...common} name="last_name" label="Last name" />
                <Field {...common} name="dob" label="Date of birth" type="date" span={2} />
                <Field
                  {...common}
                  name="phone"
                  label="Phone"
                  placeholder="+12135550123"
                  hint="E.164, with country code"
                  span={2}
                />
                <LineType value={form.line_type} onChange={(v) => set('line_type', v)} />
                <Field {...common} name="address" label="Address" span={3} />
                <Field {...common} name="state" label="State" placeholder="CA" span={1} />
                <Field {...common} name="zip" label="ZIP" placeholder="90012" span={2} />
              </section>

              <section className="grid grid-cols-6 gap-x-4 gap-y-4">
                <SectionTitle icon={Receipt}>Debt</SectionTitle>
                <Field {...common} name="creditor" label="Creditor" span={6} />
                <Field
                  {...common}
                  name="account_number"
                  label="Account number"
                  placeholder="ACC-2001"
                  span={3}
                />
                <Field
                  {...common}
                  name="amount"
                  label="Amount owed"
                  placeholder="1,250.00"
                  prefix="$"
                  span={3}
                />
              </section>

              <section className="grid grid-cols-6 gap-x-4 gap-y-4 rounded-xl border bg-muted/40 p-5">
                <div className="col-span-6 flex items-start gap-3">
                  <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md bg-background text-muted-foreground ring-1 ring-border">
                    <ShieldCheck className="size-4" aria-hidden />
                  </span>
                  <div className="space-y-0.5">
                    <h3 className="text-sm font-semibold">Consent to be called</h3>
                    <p className="text-xs leading-relaxed text-muted-foreground">
                      Consent means the person agreed to receive calls from an automated voice at
                      this number. Record it only if they did, and say how and when they agreed.
                      Leave both fields empty if you do not have it: the person is saved, but cannot
                      be called until consent is recorded.
                    </p>
                  </div>
                </div>
                <Field
                  {...common}
                  name="consent_source"
                  label="How they agreed"
                  placeholder="For example, signed form or recorded call"
                />
                <Field {...common} name="consent_date" label="Date they agreed" type="date" />
              </section>
              {error && <Notice message={error} />}
            </div>

            <div className="flex items-center justify-between gap-3 border-t bg-muted/30 px-7 py-4">
              <span className="text-xs text-muted-foreground">
                Every field except consent is needed.
              </span>
              <div className="flex gap-2">
                <Button type="button" variant="ghost" onClick={() => close(false)}>
                  Cancel
                </Button>
                <Button type="submit" disabled={busy}>
                  {busy ? 'Adding' : 'Add person'}
                </Button>
              </div>
            </div>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
