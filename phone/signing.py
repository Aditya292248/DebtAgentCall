"""Signing and checking AgentPhone webhooks.

AgentPhone signs every webhook it sends us. The signature is an HMAC-SHA256,
made with the webhook secret, over the text ``"{timestamp}.{raw_body}"``. It
arrives in three headers:

    X-Webhook-Signature  sha256=<hex digest>
    X-Webhook-Timestamp  when it was sent, in Unix seconds
    X-Webhook-ID         a unique id per webhook, so a repeat can be spotted

``verify()`` is what the server runs on every request: a missing or wrong
signature, or a timestamp more than five minutes away from now, is refused.
The timestamp is part of what is signed, so an old request can't be replayed
with a fresh time. ``sign()`` and ``signed_headers()`` are what the simulator
uses to send requests the server accepts.

This is written by hand (``hmac`` from the standard library) so the checks are
easy to read and test; it doesn't need the AgentPhone SDK.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

SIGNATURE_HEADER = "X-Webhook-Signature"
TIMESTAMP_HEADER = "X-Webhook-Timestamp"
ID_HEADER = "X-Webhook-ID"
EVENT_HEADER = "X-Webhook-Event"

MAX_AGE_SECONDS = 300  # older (or this far in the future) is refused


def sign(body: bytes, timestamp: str, secret: str) -> str:
    """The ``X-Webhook-Signature`` value for ``body`` sent at ``timestamp``."""
    message = timestamp.encode() + b"." + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Header lookup that ignores case, for plain dicts as well as HTTP headers."""
    wanted = name.lower()
    return next((v for k, v in headers.items() if k.lower() == wanted), None)


def verify(body: bytes, headers: Mapping[str, str], secret: str | None, now: float) -> str | None:
    """None if the webhook is genuine and fresh, otherwise the reason it isn't.

    ``body`` must be the raw bytes as received: parsing and re-encoding the JSON
    can change spacing or key order and break the signature.
    """
    if not secret:
        return "webhook secret is not set (AGENTPHONE_WEBHOOK_SECRET)"
    signature = _header(headers, SIGNATURE_HEADER)
    timestamp = _header(headers, TIMESTAMP_HEADER)
    if not signature:
        return f"missing {SIGNATURE_HEADER}"
    if not timestamp:
        return f"missing {TIMESTAMP_HEADER}"
    try:
        sent_at = int(timestamp)
    except ValueError:
        return f"{TIMESTAMP_HEADER} is not a Unix time: {timestamp[:40]!r}"
    if abs(now - sent_at) > MAX_AGE_SECONDS:
        return f"timestamp is {int(now - sent_at)} s from now (limit {MAX_AGE_SECONDS} s)"
    # compare_digest takes the same time whatever the input, so the signature
    # can't be guessed one byte at a time by timing the answers.
    if not hmac.compare_digest(signature, sign(body, timestamp, secret)):
        return "signature does not match"
    return None


def signed_headers(
    body: bytes, secret: str, now: float, webhook_id: str, event: str
) -> dict[str, str]:
    """All the headers AgentPhone would send with ``body``."""
    timestamp = str(int(now))
    return {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign(body, timestamp, secret),
        TIMESTAMP_HEADER: timestamp,
        ID_HEADER: webhook_id,
        EVENT_HEADER: event,
    }
