# Adding people by chat

The operator can add a person (a debtor with a phone number and a debt) by typing to the operator agent, the same way they would tell a colleague. This page says how it works and shows the tone it is meant to have.

## How it works

The model talks. Code decides.

- The model calls one tool, `add_person`, with everything it knows so far, every time. Nothing is remembered between calls: no half-built record lives in the model or in a table.
- Code reads the fields (`harness/person_intake.py`): a phone like "617 555 0123" becomes `+16175550123`, a date of birth like "March 3 85" becomes `1985-03-03`, "1.2k" becomes 120000 cents, and a ZIP gives the state when the state was left out and the ZIP maps to one state. A date that could be read two ways ("3/4/85") is refused, never guessed.
- The tool answers with a `status`:
  - `needs_more`: lists all missing facts. The model uses the conversation to choose a short follow-up. The operator can provide details or correct an earlier answer in any order.
  - `needs_fix`: lists each problem in plain words, such as a date of birth in the future.
  - `ready`: opens an approval card. Nothing is saved yet.
- Only `decide(approval_id, approve)` writes, through `harness.people.add_person`, the check and writer the web form uses. It reads the fields again from the tool call the card came from and refuses if they no longer match the card. A duplicate phone or account number is refused, and nothing is partly saved. The audit row says the person came from the operator chat and names the approval.
- Required to save: first and last name, date of birth, street address with ZIP and state, phone, creditor and amount. An account number is made if none is given, and the card says so.
- Consent is never assumed. The tool lists it as something to ask about. If the operator has no permission, or is not sure, the person is saved without consent and calls stay blocked by the TCPA check until consent is recorded some other way. If consent is given, its source and date are required and shown on the card. Code cannot tell a real consent source from an invented one, so the prompt forbids inventing one and the card shows exactly the text that was passed, for the operator to check.
- Phone numbers are masked in every result and on the card. The full number stays in the tool call's arguments, which only code reads.
- One person at a time. A newer approval (a correction, or the next person) replaces an older one that was never decided. Pasting a list of people is handled one by one; importing a batch is a separate feature.

## Example conversations

These are examples of the intended behavior, written by hand. They are not recordings of a real model. How close a real model gets still needs to be tried (see the messages at the end).

### Details are missing

> **Operator:** add Sam Lee, owes Acme Credit $480, phone 617 555 0123
>
> **Agent:** Got it. What's Sam's date of birth? And what's the street address, with the ZIP?
>
> **Operator:** born March 3 85, lives at 12 Elm Street, Boston 02115
>
> **Agent:** Thanks. One more thing: do you have permission to call this number? If so, how did Sam give it, and roughly when?
>
> **Operator:** yes, he signed our paper form on Oct 1
>
> **Agent:** I'm asking you to approve adding Sam Lee, $480 owed to Acme Credit. Nothing is saved until you approve.

(The approval card shows the name, the masked phone, the amount and creditor, the consent line and an Approve and Deny button.)

### A correction

> **Operator:** add Dana Ortiz, owes Northwind $840, phone 213 555 0188, born 1979-06-14, 40 Pine Ave Los Angeles 90012, no permission to call
>
> **Agent:** I'm asking you to approve adding Dana Ortiz, $840 owed to Northwind. She'll be saved without consent, so calls stay blocked. Nothing is saved yet.
>
> **Operator:** actually it's 480 not 840
>
> **Agent:** Updated: Dana Ortiz, $480 owed to Northwind. Nothing is saved until you approve the new card.

(The new card replaces the old one. The old one can no longer be approved.)

### Consent is unknown

> **Operator:** add Lee Park, owes Acme Credit 1.2k, 617 555 0144, born Nov 2 1990, 8 Oak Rd, Cambridge, 02139
>
> **Agent:** Do you have permission to call this number? If you're not sure, that's fine, I'll add Lee without it.
>
> **Operator:** not sure
>
> **Agent:** I'm asking you to approve adding Lee Park, about $1,200 owed to Acme Credit. Without consent on file, calls to this number stay blocked until it's recorded. Nothing is saved yet.

And when something can't be read:

> **Operator:** add Jo Ruiz, owes Acme $300, 617 555 0155, born 3/4/85
>
> **Agent:** I can't tell if 3/4/85 is March 4 or April 3. Which is it? I'll also need Jo's street address, ZIP and state.

## Try it with a real model

Run the web app against a test database and try these in the chat box:

1. `add Sam Lee, owes Acme Credit $480, phone 617 555 0123` (it should ask only for what is missing, one or two things at a time, without a checklist)
2. `Sam Lee, born March 3 85, lives in Boston, 02115` after the first (it should not ask again for the name, amount or phone, and should ask for a street address and for consent)
3. `actually it's 480 not 840` after an approval card appears (it should replace the card, not open a second one that can be approved)
