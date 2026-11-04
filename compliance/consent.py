"""Consent to be called on this number (TCPA).

Our calls use an artificial (AI) voice, and the TCPA needs the person's prior
express consent for those. So every number needs a consent record, and once the
person takes it back, we stop.

``check_consent(phone)`` blocks the call when:
  - there is no phone record at all (``phone`` is ``None``),
  - the record has no consent (no ``consent_at``), or
  - consent was revoked (``revoked_at`` is set), whenever that happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from compliance.decision import Decision, allow, block

RULE_ID = "tcpa_consent"


@dataclass(frozen=True)
class PhoneConsent:
    e164: str
    consent_at: datetime | None = None
    consent_source: str | None = None
    revoked_at: datetime | None = None
    line_type: str | None = None


def check_consent(phone: PhoneConsent | None) -> Decision:
    """Allow the call only if this number has consent that hasn't been taken back."""
    if phone is None:
        return block(RULE_ID, "no phone record, so no consent on file", {})

    evidence = {
        "e164": phone.e164,
        "consent_at": phone.consent_at.isoformat() if phone.consent_at else None,
        "consent_source": phone.consent_source,
        "revoked_at": phone.revoked_at.isoformat() if phone.revoked_at else None,
    }
    if phone.revoked_at is not None:
        return block(RULE_ID, f"consent revoked on {phone.revoked_at:%Y-%m-%d}", evidence)
    if phone.consent_at is None:
        return block(RULE_ID, "no consent on file for this number", evidence)
    source = f" ({phone.consent_source})" if phone.consent_source else ""
    return allow(RULE_ID, f"consent since {phone.consent_at:%Y-%m-%d}{source}", evidence)
