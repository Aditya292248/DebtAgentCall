"""Skills: playbooks for hard conversations, loaded only when the model wants one.

A skill is a Markdown file with a short header:

    ---
    name: hardship
    description: The person says they can't pay because of money trouble.
    ---
    (the full playbook: what to listen for, what to say, what to avoid ...)

Either ``skills/<name>/SKILL.md`` or a loose ``skills/<name>.md``.

This is progressive disclosure. Every model call carries only the menu, each
skill's name and description, in the system prompt. That costs a line per skill
no matter how long the skill is. When a call needs one, the model calls the
``load_skill`` tool and gets the full text as a tool result.

Skills are conversation guidance only. The rules that must hold on a call
(stop triggers, the sentence filter, the pre-call checks) are code and run
whether or not a skill was loaded. A skill can't loosen them.

Adapted from codingHarness.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness.tools import Tool

SKILL_FILE = "SKILL.md"
MAX_DESCRIPTION = 300  # every skill's description rides along on every call


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path  # the .md file

    def body(self) -> str:
        """The full playbook, header stripped."""
        _, body = _split_header(self.path.read_text(encoding="utf-8"))
        return body.strip()


def _split_header(text: str) -> tuple[dict[str, str], str]:
    """``---``-fenced ``key: value`` lines at the top, and the rest of the text."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1 :])
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip().lower()] = value.strip().strip("'\"")
    return {}, text  # no closing fence: treat the whole file as body


def read_skill(path: str | Path) -> Skill | None:
    """Name and description of the skill at ``path`` (its folder or its .md file).

    Reads only what the menu needs. Returns None if there's no skill there.
    """
    p = Path(path)
    if p.is_dir():
        p = p / SKILL_FILE
    if not p.is_file():
        return None
    try:
        meta, body = _split_header(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None

    name = meta.get("name") or (p.parent.name if p.name == SKILL_FILE else p.stem)
    # No description in the header: fall back to the first line of prose.
    description = meta.get("description") or next(
        (ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#")), ""
    )
    if len(description) > MAX_DESCRIPTION:
        description = description[: MAX_DESCRIPTION - 3] + "..."
    return Skill(name=name, description=description or "(no description)", path=p)


def discover(skills_dir: str | Path) -> list[Path]:
    """Paths of the skills in a folder: subfolders with a SKILL.md, and loose .md files."""
    root = Path(skills_dir)
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir() if (p.is_dir() and (p / SKILL_FILE).is_file())]
    found += [p for p in root.glob("*.md") if p.is_file()]
    return sorted(found, key=lambda p: p.name.lower())


def load_skills(paths: list[Path]) -> list[Skill]:
    """``read_skill`` over a list of paths. Unreadable ones are skipped; first name wins."""
    skills: dict[str, Skill] = {}
    for path in paths:
        skill = read_skill(path)
        if skill and skill.name not in skills:
            skills[skill.name] = skill
    return list(skills.values())


def skills_prompt(skills: list[Skill]) -> str:
    """The menu for the system prompt: names and descriptions only."""
    if not skills:
        return ""
    lines = [
        "## Skills",
        "You have these skills. Each one is a short playbook for a hard moment on a call. "
        "When the conversation matches one, call load_skill with its name and follow "
        "it. Don't load skills you don't need.",
        "",
        *(f"- {s.name}: {s.description}" for s in skills),
    ]
    return "\n".join(lines)


def skill_tool(skills: list[Skill]) -> Tool:
    """The ``load_skill`` tool, bound to this list of skills."""
    by_name = {s.name: s for s in skills}

    def load(name: str) -> str:
        skill = by_name.get(name)
        if skill is None:
            return f"error: no skill named {name!r}; available: {', '.join(by_name) or 'none'}"
        try:
            return skill.body()
        except (OSError, UnicodeDecodeError) as exc:
            return f"error: could not read skill {name!r}: {exc}"

    return Tool(
        name="load_skill",
        description="Load the full playbook of one of your skills, by name.",
        params={"name": "The skill's name, exactly as listed under Skills."},
        fn=load,
        safe_to_retry=True,
    )
