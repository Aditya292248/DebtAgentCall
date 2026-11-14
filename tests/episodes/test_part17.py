"""Skills: playbooks the agent loads for hard conversations.

What it checks: the agent finds skills in ``skills_dir``, puts each one's name
and description (never the body) in the system prompt, and offers a
``load_skill`` tool that returns the full playbook. Negative cases: an unknown
skill name is an ``error:`` string, no skills means no ``load_skill`` tool, and
no line of the repo's playbooks would be blocked by the sentence filter. All
offline, with the fake model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from compliance.utterance import SpeechState, check_sentence
from harness import Agent
from harness.skills import (
    MAX_DESCRIPTION,
    discover,
    load_skills,
    read_skill,
    skill_tool,
    skills_prompt,
)
from harness.tools import ToolRegistry, run_tool
from model import FakeProvider, fake
from model.fake import tool_call, tool_reply

REPO_SKILLS = Path(__file__).resolve().parents[2] / "skills"
EXPECTED_SKILLS = {
    "hardship",
    "angry-caller",
    "confused-caller",
    "payment-plan-talk",
    "callback-request",
}
RULES_LINE = "Rules are enforced in code, not here."


def write_skill(root: Path, folder: str, header: str, body: str = "the full playbook") -> Path:
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{header}\n---\n{body}\n", encoding="utf-8")
    return d


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    write_skill(
        root, "hardship", "name: hardship\ndescription: Money trouble.", "SECRET-HARDSHIP-STEPS"
    )
    write_skill(
        root, "callback", "name: callback\ndescription: Call back later.", "SECRET-CALLBACK-STEPS"
    )
    return root


def _responder(agent: Agent) -> FakeProvider:
    r = agent.provider.responder if agent.provider else None
    assert isinstance(r, FakeProvider)
    return r


def _tool_names(agent: Agent, call: int = 0) -> set[str]:
    tools = _responder(agent).kwargs[call]["tools"] or []
    return {t["function"]["name"] for t in tools}


# --- reading and discovery ---------------------------------------------------


def test_reads_name_description_and_body(skills_dir: Path) -> None:
    skill = read_skill(skills_dir / "hardship")
    assert skill is not None
    assert (skill.name, skill.description) == ("hardship", "Money trouble.")
    assert skill.body() == "SECRET-HARDSHIP-STEPS"
    assert read_skill(skills_dir / "hardship" / "SKILL.md") == skill


def test_no_header_falls_back_to_file_name_and_first_line(tmp_path: Path) -> None:
    (tmp_path / "wrap.md").write_text("# Wrap\n\nEnd the call kindly.\n", encoding="utf-8")
    skill = read_skill(tmp_path / "wrap.md")
    assert skill is not None and (skill.name, skill.description) == ("wrap", "End the call kindly.")


def test_long_descriptions_are_cut(tmp_path: Path) -> None:
    write_skill(tmp_path, "long", f"name: long\ndescription: {'word ' * 200}")
    skill = read_skill(tmp_path / "long")
    assert skill is not None
    assert len(skill.description) == MAX_DESCRIPTION and skill.description.endswith("...")


def test_missing_or_unreadable_is_none(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    (tmp_path / "blob.md").write_bytes(b"\xff\xfe\x00binary")
    assert read_skill(tmp_path / "nope") is None
    assert read_skill(tmp_path / "empty") is None
    assert read_skill(tmp_path / "blob.md") is None


def test_discover_finds_folders_and_loose_files_sorted(skills_dir: Path) -> None:
    (skills_dir / "not-a-skill").mkdir()
    (skills_dir / "a-loose.md").write_text("Loose skill.", encoding="utf-8")
    assert [p.name for p in discover(skills_dir)] == ["a-loose.md", "callback", "hardship"]
    assert discover(skills_dir / "nowhere") == []


def test_duplicate_names_keep_the_first(tmp_path: Path) -> None:
    write_skill(tmp_path, "a", "name: same\ndescription: first")
    write_skill(tmp_path, "b", "name: same\ndescription: second")
    assert [s.description for s in load_skills(discover(tmp_path))] == ["first"]


def test_menu_lists_descriptions_but_never_bodies(skills_dir: Path) -> None:
    text = skills_prompt(load_skills(discover(skills_dir)))
    assert text.startswith("## Skills")
    assert "- hardship: Money trouble." in text
    assert "- callback: Call back later." in text
    assert "load_skill" in text
    assert "SECRET" not in text
    assert skills_prompt([]) == ""


# --- load_skill ----------------------------------------------------------------


def test_load_skill_returns_the_body(skills_dir: Path) -> None:
    registry = ToolRegistry([skill_tool(load_skills(discover(skills_dir)))])
    assert run_tool(registry, "load_skill", '{"name": "hardship"}') == "SECRET-HARDSHIP-STEPS"


def test_unknown_skill_is_an_error_string(skills_dir: Path) -> None:
    registry = ToolRegistry([skill_tool(load_skills(discover(skills_dir)))])
    out = run_tool(registry, "load_skill", '{"name": "threaten"}')
    assert out.startswith("error: no skill named 'threaten'; available: callback, hardship")


# --- the agent -------------------------------------------------------------------


def test_menu_goes_in_the_system_prompt_after_instructions(
    skills_dir: Path, tmp_path: Path
) -> None:
    instructions = tmp_path / "system.md"
    instructions.write_text("be kind", encoding="utf-8")
    agent = Agent(
        provider=fake(),
        system_prompt_path=instructions,
        tools=ToolRegistry(),
        skills_dir=skills_dir,
    )
    agent.run("hi")
    system = _responder(agent).calls[0][0]
    assert system["role"] == "system"
    assert system["content"].startswith("be kind\n\n## Skills")
    assert "SECRET" not in system["content"]
    assert _tool_names(agent) == {"load_skill"}


def test_no_skills_dir_means_no_load_skill_tool(tmp_path: Path, no_instructions: Path) -> None:
    agent = Agent(
        provider=fake(),
        system_prompt_path=no_instructions,
        tools=ToolRegistry(),
        skills_dir=tmp_path / "no-such-folder",
    )
    agent.run("hi")
    assert _responder(agent).kwargs[0]["tools"] is None  # no tools at all
    assert [m["role"] for m in _responder(agent).calls[0]] == ["user"]  # and no menu


def test_default_skills_dir_is_empty_in_tests(no_instructions: Path) -> None:
    agent = Agent(provider=fake(), system_prompt_path=no_instructions)
    agent.run("hi")
    assert "load_skill" not in _tool_names(agent)
    assert _responder(agent).calls[0][0]["role"] == "user"


def test_model_loads_a_skill_and_sees_its_body(skills_dir: Path, no_instructions: Path) -> None:
    script = [tool_reply(tool_call("load_skill", name="hardship")), "I'm sorry to hear that."]
    agent = Agent(
        provider=fake(scripted=script),
        system_prompt_path=no_instructions,
        tools=ToolRegistry(),
        skills_dir=skills_dir,
    )
    assert agent.run("I lost my job.") == "I'm sorry to hear that."
    calls = _responder(agent).calls
    assert all("SECRET" not in (m.get("content") or "") for m in calls[0])
    assert calls[1][-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "SECRET-HARDSHIP-STEPS",
    }


def test_a_new_skill_shows_up_on_the_next_call(skills_dir: Path, no_instructions: Path) -> None:
    agent = Agent(provider=fake(), system_prompt_path=no_instructions, skills_dir=skills_dir)
    agent.run("one")
    write_skill(skills_dir, "confused", "name: confused\ndescription: Lost caller.")
    agent.run("two")
    first, second = (c[0]["content"] for c in _responder(agent).calls)
    assert "confused" not in first and "confused: Lost caller." in second


# --- the repo's playbooks ----------------------------------------------------------


def test_repo_ships_the_playbooks() -> None:
    skills = load_skills(discover(REPO_SKILLS))
    assert {s.name for s in skills} == EXPECTED_SKILLS
    for s in skills:
        assert s.description != "(no description)"
        assert s.body().splitlines()[0].startswith(RULES_LINE), s.name


# Verified and disclosed, so plain account words are fine; everything else at its
# strictest default. A line is still blocked for threats, legal-action talk, false
# identity, profanity, unchecked money talk or pressure to pay.
_OPEN_STATE = SpeechState(verified=True, disclosed=True)


def _playbook_lines() -> list[tuple[str, str]]:
    return [
        (f"{path.parent.name}/{path.name}", line.strip())
        for path in sorted(REPO_SKILLS.rglob("*.md"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_scan_would_catch_pressure_language() -> None:
    assert not check_sentence("You must pay today, this is your last chance.", _OPEN_STATE).allow
    assert not check_sentence("We will have you arrested.", _OPEN_STATE).allow


def test_playbooks_contain_no_blocked_phrases() -> None:
    lines = _playbook_lines()
    assert lines, "no playbook lines found"
    blocked = [
        f"{where}: {line!r} ({d.rule_id}: {d.reason})"
        for where, line in lines
        if not (d := check_sentence(line, _OPEN_STATE)).allow
    ]
    assert blocked == []
