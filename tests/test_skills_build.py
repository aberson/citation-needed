"""tools/skills_build.py: the project-local skill folders are built from skills/ and current."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

from citation_needed import cli

TOOL = Path(__file__).resolve().parents[1] / "tools" / "skills_build.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("skills_build", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["skills_build"] = module
    spec.loader.exec_module(module)
    return module


sb = _load()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repository holding a copy of skills/ and one unrelated skill."""
    root = tmp_path / "repo"
    shutil.copytree(sb.SKILLS_SOURCE, root / "skills", ignore=shutil.ignore_patterns("__pycache__"))
    other = root / ".claude" / "skills" / "other-skill"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("not ours\n", encoding="utf-8")
    return root


def frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n")
    block = text[4 : text.index("\n---\n", 4)]
    out: dict[str, str] = {}
    for line in block.splitlines():
        key, _, value = line.partition(": ")
        out[key] = json.loads(value) if value.startswith('"') else value
    return out


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_build_is_self_contained_and_marked(host: str) -> None:
    files = sb.build_files(host)
    assert set(files) == {f"{n}/{f}" for n in sb.SKILLS for f in ("SKILL.md", "core.md")}
    for name in sb.SKILLS:
        skill = files[f"{name}/SKILL.md"].decode("utf-8")
        front = frontmatter(skill)
        assert front["name"] == name and front["description"]
        assert ("user-invocable" in front) == (host == "claude")
        assert sb.MARKER in skill and "SKILL-MESH-GENERATED-FILE" not in skill
        assert "[core.md](core.md)" in skill and "../core.md" not in skill
        assert f"{host} entry point" in skill.lower()
        assert "\r" not in skill


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_built_core_carries_the_source_core_unchanged(host: str) -> None:
    files = sb.build_files(host)
    for name in sb.SKILLS:
        built = files[f"{name}/core.md"].decode("utf-8")
        source = (sb.SKILLS_SOURCE / name / "core.md").read_text(encoding="utf-8")
        assert sb.MARKER in built
        assert built.endswith(source.replace("\r\n", "\n"))


def test_committed_project_skill_folders_are_current() -> None:
    """The real gate: .claude/skills and .agents/skills in this repo match skills/."""
    report = sb.compare()
    assert report.stale == (), (
        f"stale generated skill files {report.stale}; run "
        "`uv run python tools/skills_build.py write` and commit the result"
    )


def test_write_then_check_round_trip(repo: Path) -> None:
    first = sb.write(repo, repo / "skills")
    assert len(first.written) == 2 * 2 * len(sb.SKILLS)  # 2 hosts x SKILL.md + core.md
    assert (repo / ".claude" / "skills" / "citation-review" / "SKILL.md").is_file()
    assert (repo / ".agents" / "skills" / "citation-sweep" / "core.md").is_file()
    assert sb.compare(repo, repo / "skills").stale == ()
    again = sb.write(repo, repo / "skills")
    assert again.written == () and again.removed == ()
    assert (repo / ".claude" / "skills" / "other-skill" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "not ours\n"


def test_check_reds_on_an_edited_or_missing_generated_file(repo: Path) -> None:
    sb.write(repo, repo / "skills")
    core = repo / ".claude" / "skills" / "citation-triage" / "core.md"
    core.write_text(core.read_text(encoding="utf-8") + "\nhand edit\n", encoding="utf-8")
    (repo / ".agents" / "skills" / "citation-distill" / "SKILL.md").unlink()
    assert set(sb.compare(repo, repo / "skills").stale) == {
        "claude/citation-triage/core.md",
        "codex/citation-distill/SKILL.md",
    }


def test_check_accepts_a_crlf_checkout(repo: Path) -> None:
    sb.write(repo, repo / "skills")
    skill = repo / ".claude" / "skills" / "citation-review" / "SKILL.md"
    skill.write_bytes(skill.read_bytes().replace(b"\n", b"\r\n"))
    assert sb.compare(repo, repo / "skills").stale == ()


def test_write_removes_a_stale_generated_file_but_never_a_hand_made_one(repo: Path) -> None:
    sb.write(repo, repo / "skills")
    leftover = repo / ".claude" / "skills" / "citation-review" / "old.md"
    leftover.write_text(f"<!-- {sb.MARKER} -->\n", encoding="utf-8")
    assert sb.write(repo, repo / "skills").removed == ("claude/citation-review/old.md",)
    hand = repo / ".claude" / "skills" / "citation-review" / "notes.md"
    hand.write_text("my notes\n", encoding="utf-8")
    with pytest.raises(sb.Refused, match="no CITATION-NEEDED-GENERATED-FILE marker"):
        sb.write(repo, repo / "skills")
    assert hand.read_text(encoding="utf-8") == "my notes\n"


def _cli_verbs() -> set[str]:
    """Every ``verb`` and ``verb sub`` the production ``cite`` parser accepts."""
    verbs: set[str] = set()

    def walk(parser: argparse.ArgumentParser, prefix: str) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    verb = f"{prefix} {name}".strip()
                    verbs.add(verb)
                    walk(child, verb)

    walk(cli._build_parser(), "")
    return verbs


# A command, not prose: `cite ...` in a code span, or after `uv run`.
CITE_COMMAND = re.compile(r"(?:`|uv run )cite ([a-z][a-z-]+)(?: ([a-z][a-z-]+))?")


def cite_commands(text: str, verbs: set[str]) -> tuple[set[str], list[str]]:
    """(commands used, commands the parser does not accept) in one skill text."""
    has_subcommands = {v.split(" ")[0] for v in verbs if " " in v}
    used: set[str] = set()
    bad: list[str] = []
    for match in CITE_COMMAND.finditer(text):
        first, second = match.group(1), match.group(2)
        verb = f"{first} {second}" if second and first in has_subcommands else first
        if verb in verbs:
            used.add(verb)
        else:
            bad.append(match.group(0))
    return used, bad


def test_every_cite_command_in_the_skills_is_a_real_cli_verb() -> None:
    verbs = _cli_verbs()
    used: set[str] = set()
    bad: list[str] = []
    for name in sb.SKILLS:
        text = (sb.SKILLS_SOURCE / name / "core.md").read_text(encoding="utf-8")
        found, wrong = cite_commands(text, verbs)
        used |= found
        bad += [f"{name}: {w}" for w in wrong]
    assert not bad, f"cite commands the CLI does not accept: {bad}"
    assert {
        "review open",
        "review commit",
        "queue list",
        "queue resolve",
        "distill generate",
    } <= used
    assert not any(
        "--project citation-needed"
        in (sb.SKILLS_SOURCE / n / "core.md").read_text(encoding="utf-8")
        for n in sb.SKILLS
    ), "project-local skills must run `uv run cite` here"


def test_the_verb_check_catches_a_misspelled_command() -> None:
    verbs = _cli_verbs()
    _, bad = cite_commands("run `cite reveiw open` then `cite queue lsit`", verbs)
    assert bad == ["`cite reveiw open", "`cite queue lsit"]


def test_check_command_exit_codes(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert sb.main(["check"]) == 0  # the committed tree
    monkeypatch.setattr(sb, "compare", lambda: sb.Report(stale=("claude/x/SKILL.md",)))
    assert sb.main(["check"]) == 1
