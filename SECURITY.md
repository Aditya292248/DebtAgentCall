# Security policy

## Not legal advice

This project is not legal advice and has not been reviewed by counsel. Debt collection is regulated, and the rules differ by place and by debt. Do not use this software to contact real people without legal review.

## Reporting a vulnerability

Please report security problems privately, not in a public issue or pull request.

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**. (Maintainers: this must be enabled under Settings, then Code security, then Private vulnerability reporting.)

Include what you found, how to reproduce it using made-up data, and what you think the impact is. We will acknowledge a report as soon as we can and tell you when it is fixed.

## What is in scope

- The webhook signature check: any way to get the webhook to accept a request that AgentPhone did not sign, or to accept one when no secret is set.
- The compliance gates in `compliance/`: any way to place, or let a call continue, when a rule should have blocked it, including a rule that fails open when a fact is missing.
- The audit log: any way to edit, delete or reorder rows without `uv run audit-verify` noticing, beyond the limits the README already states.
- Approvals: any way to record a payment or another approved action without a person's approval.
- Secrets handling: keys, tokens or webhook secrets leaking into the repository, logs, the web UI or API responses.

Out of scope: problems that need full control of the database or the machine, findings in third-party services themselves, and missing legal compliance for a particular place (that is a legal review question, not a vulnerability).

## Never commit real personal data

Real personal data must never be committed to this repository or put in an issue, pull request or comment. That means real names, phone numbers, addresses, account numbers, recordings and transcripts. All data in the repository is made up; use made-up data in reports as well. If you find real personal data in the repository, report it privately as described above.
