import { CircleCheck } from 'lucide-react'
import { useState } from 'react'

import { Notice } from '@/components/Notice'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import {
  addPerson,
  EMPTY_FORM,
  PersonError,
  validateForm,
  type FieldName,
  type Form,
  type Person,
  type Problems,
} from '@/lib/people'

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
  placeholder?: string
  type?: string
  className?: string
  hint?: string
}

function Field({ name, label, form, problems, set, placeholder, type, className, hint }: FieldProps) {
  const id = `person-${name}`
  const problem = problems[name]
  return (
    <div className={className}>
      <label htmlFor={id} className="mb-1.5 block text-xs font-medium">
        {label}
      </label>
      <Input
        id={id}
        type={type}
        value={form[name]}
        placeholder={placeholder}
        onChange={(e) => set(name, e.target.value)}
        aria-invalid={problem ? true : undefined}
        aria-describedby={problem ? `${id}-problem` : hint ? `${id}-hint` : undefined}
        autoComplete="off"
      />
      {problem ? (
        <p id={`${id}-problem`} className="mt-1 text-xs text-destructive">
          {problem}
        </p>
      ) : (
        hint && (
          <p id={`${id}-hint`} className="mt-1 text-xs text-muted-foreground">
            {hint}
          </p>
        )
      )}
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
    if (Object.keys(found).length) {
      setError('Some fields need another look.')
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
      <DialogContent className="max-w-xl">
        {saved ? (
          <div className="space-y-5 p-6">
            <div className="flex items-start gap-3">
              <CircleCheck className="mt-0.5 size-5 shrink-0 text-ok" aria-hidden />
              <div className="space-y-1">
                <DialogTitle>{saved.name} was added</DialogTitle>
                <DialogDescription>
                  Account {saved.account_number}, phone {saved.phone}.
                </DialogDescription>
              </div>
            </div>
            <p className="rounded-lg bg-muted p-3 text-sm text-muted-foreground">
              {saved.consent_recorded
                ? 'Consent was recorded, so the pre-call checks decide the rest.'
                : "No consent is on file, so calls to this number stay blocked until it is recorded."}
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
            <div className="space-y-1 border-b px-6 pt-6 pb-4">
              <DialogTitle>Add person</DialogTitle>
              <DialogDescription>
                A person, their phone and one debt. Nothing is saved until you add them.
              </DialogDescription>
            </div>
            <div className="min-h-0 flex-1 space-y-6 overflow-y-auto px-6 py-5">
              <section className="grid grid-cols-2 gap-x-3 gap-y-4">
                <h3 className="col-span-2 text-sm font-semibold">Person</h3>
                <Field {...common} name="first_name" label="First name" />
                <Field {...common} name="last_name" label="Last name" />
                <Field {...common} name="dob" label="Date of birth" type="date" />
                <Field {...common} name="state" label="State" placeholder="CA" />
                <Field {...common} name="address" label="Address" className="col-span-2" />
                <Field {...common} name="zip" label="ZIP" placeholder="90012" />
                <Field
                  {...common}
                  name="phone"
                  label="Phone"
                  placeholder="+12135550123"
                  hint="E.164, with the country code"
                />
                <div>
                  <label htmlFor="person-line_type" className="mb-1.5 block text-xs font-medium">
                    Line type
                  </label>
                  <select
                    id="person-line_type"
                    value={form.line_type}
                    onChange={(e) => set('line_type', e.target.value)}
                    className="h-8 w-full rounded-lg border border-input bg-background px-2 text-sm"
                  >
                    <option value="cell">Cell</option>
                    <option value="landline">Landline</option>
                  </select>
                </div>
              </section>

              <section className="grid grid-cols-2 gap-x-3 gap-y-4">
                <h3 className="col-span-2 text-sm font-semibold">Debt</h3>
                <Field {...common} name="creditor" label="Creditor" className="col-span-2" />
                <Field {...common} name="account_number" label="Account number" placeholder="ACC-2001" />
                <Field
                  {...common}
                  name="amount"
                  label="Amount owed (USD)"
                  placeholder="1250.00"
                />
              </section>

              <section className="grid grid-cols-2 gap-x-3 gap-y-4 rounded-lg border bg-card p-4">
                <div className="col-span-2 space-y-1">
                  <h3 className="text-sm font-semibold">Consent to be called</h3>
                  <p className="text-xs text-muted-foreground">
                    Fill this in only if the person has given consent, and say where. If you leave
                    it empty, they are saved but cannot be called until consent is recorded.
                  </p>
                </div>
                <Field
                  {...common}
                  name="consent_source"
                  label="Source"
                  placeholder="For example, signed form"
                />
                <Field {...common} name="consent_date" label="Date given" type="date" />
              </section>
              {error && <Notice message={error} />}
            </div>
            <div className="flex justify-end gap-2 border-t px-6 py-4">
              <Button type="button" variant="ghost" onClick={() => close(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={busy}>
                {busy ? 'Adding' : 'Add person'}
              </Button>
            </div>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
