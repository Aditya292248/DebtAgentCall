import { useState } from 'react'

import { Notice } from '@/components/Notice'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import type { DebtView } from '@/lib/debts'
import { archivePerson, PersonError, restorePerson } from '@/lib/people'

/** Archive (or, for an archived person, restore) with a confirmation first.
 *  Archiving deletes nothing: the history stays and the person can't be called. */
export function ArchivePerson({ debt, onDone }: { debt: DebtView; onDone: () => void }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const restoring = debt.archived

  const confirm = async () => {
    setBusy(true)
    setError(null)
    try {
      await (restoring ? restorePerson(debt.account) : archivePerson(debt.account))
      setOpen(false)
      onDone()
    } catch (err) {
      setError(err instanceof PersonError ? err.message : "Couldn't reach the server. Nothing changed.")
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Button variant={restoring ? 'outline' : 'destructive'} onClick={() => setOpen(true)}>
        {restoring ? 'Restore' : 'Archive'}
      </Button>
      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (busy) return
          setOpen(next)
          if (!next) setError(null)
        }}
      >
        <DialogContent className="max-w-md">
          <div className="space-y-5 p-6">
            <div className="space-y-2">
              <DialogTitle>
                {restoring ? `Restore ${debt.name}?` : `Archive ${debt.name}?`}
              </DialogTitle>
              <DialogDescription>
                {restoring
                  ? 'They return to the list. Their consent, stop requests and history are as they were, and the pre-call checks decide whether they can be called.'
                  : 'They leave the list and cannot be called. Nothing is deleted: payments, calls and the compliance log are kept, and you can restore them later.'}
              </DialogDescription>
            </div>
            {error && <Notice message={error} />}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setOpen(false)} disabled={busy}>
                Cancel
              </Button>
              <Button onClick={confirm} disabled={busy}>
                {busy ? 'Working' : restoring ? 'Restore' : 'Archive'}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
