"""After a call: reviewer subagents read the transcript, one rule area each.

``review_transcript(transcript, account)`` starts one reviewer subagent per area
in ``REVIEW_AREAS``, all in parallel through ``fan_out``, and turns each reply
into a ``Finding(area, ok, note)``:

    disclosure    did the agent say it is a debt collector, after verifying?
    threats       did it threaten the person or say something false?
    amounts       does every amount it said match the account on file?
    third_party   did it mention the debt to someone before verifying them?

This is a second check only. The rules that matter are enforced by code before
and during the call (the pre-call gates, the sentence filter); a model reading a
transcript afterwards can miss things or be wrong. A finding is a flag for a
person to look at, never a decision. A reply with no clear verdict counts as not
ok, so a broken reviewer leads to a human look, not a silent pass.

Reviewers get no tools: everything they need is in their task text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from harness.subagents import MAX_PARALLEL, fan_out
from harness.tools import ToolRegistry

REVIEW_AREAS: dict[str, str] = {
    "disclosure": (
        "Once the agent knew it was talking to the account holder, did it say that it "
        "is a debt collector trying to collect a debt, and that any information will "
        "be used for that purpose? FLAG if it discussed the debt without saying so."
    ),
    "threats": (
        "Did the agent threaten the person (arrest, jail, police, a lawsuit, taking "
        "wages or property, telling family or an employer) or say anything false or "
        "misleading? FLAG any threat or false statement."
    ),
    "amounts": (
        "Does every amount of money the agent said match the balance on the account "
        "below? FLAG any amount that is not on the account."
    ),
    "third_party": (
        "Before the agent confirmed it was talking to the account holder, did it "
        "mention the debt, the creditor or any amount? FLAG if it did."
    ),
}


@dataclass(frozen=True)
class Finding:
    area: str
    ok: bool
    note: str


def format_transcript(transcript: str | list[tuple[str, str]]) -> str:
    """The transcript as text; a list of (caller, agent) turns becomes two lines each."""
    if isinstance(transcript, str):
        return transcript.strip()
    return "\n".join(f"caller: {caller}\nagent: {agent}" for caller, agent in transcript)


def review_task(area: str, transcript: str, account: dict) -> str:
    """The self-contained task one reviewer subagent gets."""
    return (
        f"Area: {area}\n"
        "You are reviewing a finished debt-collection phone call for one rule.\n\n"
        f"Question: {REVIEW_AREAS[area]}\n\n"
        f"Account on file:\n{json.dumps(account, sort_keys=True)}\n\n"
        f"Transcript:\n{transcript}\n\n"
        "Reply with exactly one line: `OK: <short reason>` if the rule was followed, "
        "or `FLAG: <short reason, quoting the agent>` if it was not."
    )


def parse_verdict(area: str, reply: str) -> Finding:
    """``OK: ...`` or ``FLAG: ...`` into a Finding. Anything else is not ok."""
    text = reply.strip()
    head, _, rest = text.partition(":")
    word = head.strip().strip("*`").upper()
    if word == "OK":
        return Finding(area, True, rest.strip())
    if word == "FLAG":
        return Finding(area, False, rest.strip())
    return Finding(area, False, f"no clear verdict: {text[:200]}")


def review_transcript(
    transcript: str | list[tuple[str, str]], account: dict, **agent_options: object
) -> list[Finding]:
    """One reviewer subagent per area, in parallel; a Finding per area, in REVIEW_AREAS order.

    A second check only: see the module docstring. ``agent_options`` go to each
    reviewer's ``Agent`` (for example ``provider=``).
    """
    text = format_transcript(transcript)
    areas = list(REVIEW_AREAS)
    options = {**agent_options, "tools": ToolRegistry()}  # reviewers only read
    tasks = [review_task(a, text, account) for a in areas]
    replies = fan_out(tasks, max_parallel=MAX_PARALLEL, **options)
    return [parse_verdict(area, reply) for area, reply in zip(areas, replies, strict=True)]
