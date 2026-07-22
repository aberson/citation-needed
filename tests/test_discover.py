"""Discovery + typed ingestion (`cite scan`) — fixture tests + CLI integration.

A tmp-dir mini-workspace exercises every Step 2 requirement: all 5 artifact types, both
pointer shapes (thin-wrapper SKILL.md, pointer-only plan) plus CLAUDE.md ``@path`` imports
(artifact target recorded-and-skipped vs plain-doc target inlined), the commands-dir
``skill`` ingestion, MEMORY.md index exclusion, `.venv`/`node_modules`/`.git`/
``docs/archived*`` exclusions, an ``owned=false`` registry tree, and re-scan idempotency.
The integration tests invoke the production CLI entry (``cli.main``), never internals.
Never scans the real workspace — hermetic tmp fixture only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from citation_needed import db, discover
from citation_needed.cli import main
from citation_needed.models import MemoryDetails, PlanDetails, SkillDetails

SESSION_ID = "740ce8b1-8ae8-41e5-a58b-7245a7601fee"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def mini_workspace(tmp_path: Path) -> dict[str, Path | str]:
    ws = tmp_path / "ws"
    mem = tmp_path / "memroot"
    ws.mkdir()

    _write(
        ws / ".claude" / "observatory" / "registry.toml",
        '[[project]]\nslug = "projA"\npath = "projA"\nowned = true\n\n'
        '[[project]]\nslug = "notowned"\npath = "notowned"\nowned = false\n',
    )

    _write(ws / ".claude" / "rules" / "one-rule.md", "# One rule\n\nAlways do the thing.\n")

    _write(
        ws / ".claude" / "skills" / "real-skill" / "SKILL.md",
        "---\nname: real-skill\ndescription: A real skill with body sections.\n"
        "user-invocable: true\n---\n# Real skill\n\n## 1. First instruction\n\n"
        "Do the first thing carefully.\n\n## 2. Second instruction\n\n"
        "Then do the second thing.\n",
    )
    _write(ws / ".claude" / "skills" / "real-skill" / "evals" / "evals.json", "{}\n")

    _write(
        ws / ".claude" / "skills" / "thin-wrap" / "SKILL.md",
        "---\nname: thin-wrap\ndescription: Thin wrapper.\nuser-invocable: true\n---\n"
        "Read `../../shared/core.md` (relative to this file) and follow it.\n",
    )
    _write(ws / ".claude" / "shared" / "core.md", "# Core\n\nThe real instructions live here.\n")

    _write(
        ws / ".claude" / "skills" / "rule-wrap" / "SKILL.md",
        "---\nname: rule-wrap\ndescription: Wrapper onto an artifact.\n---\n"
        "Read `../../rules/one-rule.md` and follow it.\n",
    )

    _write(
        ws / ".claude" / "skills" / "broken-wrap" / "SKILL.md",
        "---\nname: broken-wrap\ndescription: Wrapper with a dead target.\n---\n"
        "Read `./missing-target.md` and follow it.\n",
    )

    _write(
        ws / ".claude" / "commands" / "foo.md",
        "---\nname: foo\ndescription: Legacy slash command.\n---\n# Foo command\n\n"
        "Run the deploy steps in order.\n",
    )

    _write(
        ws / "CLAUDE.md",
        "# Fixture workspace\n\nStanding rule: @.claude/rules/one-rule.md\n\n"
        "Background reading: @docs/notes.md\n\nGone: @missing/nowhere.md\n\n"
        "Contact someone@example.com about access.\n\n"
        "```python\n@decorator_looking_thing.md\n```\n",
    )
    _write(ws / "docs" / "notes.md", "# Notes\n\nPlain background doc — not an artifact.\n")
    _write(ws / "docs" / "archived-junk" / "CLAUDE.md", "# Must never be ingested\n")

    _write(
        ws / "projA" / "plan.md",
        "# projA plan\n\n## 1. What This Is\n\nA fixture project plan.\n\n"
        "### Step 1: Do a thing\n- **Problem:** Something.\n- **Type:** code\n\n"
        "### Step 2: Do another\n- **Problem:** More.\n- **Type:** code\n",
    )
    _write(ws / "projA" / "CLAUDE.md", "# projA\n\n## Stack\n\nuv\n")

    _write(
        ws / "projB" / "plan.md",
        "# projB plan — moved\n\nThe plan was split. See [Phase 1](docs/phase-1-plan.md) and\n"
        "[missing](docs/gone-plan.md).\n",
    )
    _write(
        ws / "projB" / "docs" / "phase-1-plan.md",
        "# Phase 1\n\n### Step 1: Real step\n- **Problem:** X.\n",
    )

    _write(ws / "notowned" / "CLAUDE.md", "# Not owned — must never be ingested\n")
    _write(ws / ".venv" / "CLAUDE.md", "# Vendored — must never be ingested\n")
    _write(ws / "node_modules" / "CLAUDE.md", "# Vendored — must never be ingested\n")
    _write(ws / ".git" / "CLAUDE.md", "# VCS internals — must never be ingested\n")

    ws_slug = discover.workspace_memory_slug(ws.resolve())
    _write(mem / ws_slug / "memory" / "MEMORY.md", "# Project Memory\n\n- index only\n")
    _write(
        mem / ws_slug / "memory" / "feedback_fixture_thing.md",
        '---\nname: feedback_fixture_thing\ndescription: "A fixture memory."\n'
        "metadata:\n  node_type: memory\n  type: feedback\n"
        f"  originSessionId: {SESSION_ID}\n  modified: 2026-07-21T00:00:00Z\n---\n"
        "The decision: prefer X over Y because Z.\n",
    )
    _write(
        mem / ws_slug / "memory" / "broken_fm.md",
        "---\nname: [unclosed\nmetadata: {\n---\nBody anyway.\n",
    )
    _write(
        mem / f"{ws_slug}-projA" / "memory" / "project_note.md",
        "---\nname: project_note\nmetadata:\n  node_type: memory\n  type: project\n---\n"
        "A projA-scoped memory.\n",
    )
    # owned=false pruning has a SECOND, independently-coded implementation on the memory
    # path (_memory_project_and_scope slug matching, not the walk's resolved-path dict) —
    # this dir exercises it plus the not_owned_skipped dedup against the walk's entry.
    _write(
        mem / f"{ws_slug}-notowned" / "memory" / "private_note.md",
        "---\nname: private_note\nmetadata:\n  node_type: memory\n  type: project\n---\n"
        "A not-owned project's private memory — must never be ingested.\n",
    )
    _write(
        mem / "c--somewhere-else-entirely" / "memory" / "other_tree.md",
        "---\nname: other_tree\nmetadata:\n  node_type: memory\n  type: reference\n---\n"
        "Belongs to a tree outside the workspace.\n",
    )

    return {"ws": ws, "mem": mem, "db": tmp_path / "citation.db", "ws_slug": ws_slug}


def _scan_args(fx: dict[str, Path | str], *extra: str) -> list[str]:
    return [
        "scan",
        "--workspace-root",
        str(fx["ws"]),
        "--memory-root",
        str(fx["mem"]),
        "--db",
        str(fx["db"]),
        *extra,
    ]


def _rows(db_path: Path) -> dict[str, tuple[str, str, dict[str, object]]]:
    conn = db.connect(db_path)
    try:
        return {
            row[0]: (row[1], row[2], json.loads(row[3]) if row[3] else {})
            for row in conn.execute(
                "SELECT path, artifact_type, project, details_json FROM artifacts"
            )
        }
    finally:
        conn.close()


def _count_line(artifact_type: str, total: int, new: int, updated: int, unchanged: int) -> str:
    return f"  {artifact_type:<10} {total} ({new} new, {updated} updated, {unchanged} unchanged)"


# ---------------------------------------------------------------------------
# Integration: the production CLI entry, end-to-end against the fixture
# ---------------------------------------------------------------------------


def test_cli_scan_end_to_end(
    mini_workspace: dict[str, Path | str], capsys: pytest.CaptureFixture[str]
) -> None:
    fx = mini_workspace
    assert main(["init-db", "--db", str(fx["db"])]) == 0
    capsys.readouterr()

    assert main(_scan_args(fx)) == 0
    out = capsys.readouterr().out

    # Per-type counts (rule 1; skills: real, thin, rule-wrap, broken-wrap + command = 5;
    # claude_md: root + projA = 2; plans: projA + projB = 2; memory: 3 in-ws + 1 outside).
    assert _count_line("rule", 1, 1, 0, 0) in out
    assert _count_line("skill", 5, 5, 0, 0) in out
    assert _count_line("claude_md", 2, 2, 0, 0) in out
    assert _count_line("plan", 2, 2, 0, 0) in out
    assert _count_line("memory", 4, 4, 0, 0) in out

    # Pointer notes + exclusion counts surface in scan output.
    assert "UNRESOLVED pointer: .claude/skills/broken-wrap/SKILL.md" in out
    assert "UNRESOLVED import: CLAUDE.md -> missing/nowhere.md" in out
    assert "pointer-only plan: projB/plan.md -> 1 linked target(s)" in out
    assert "not-owned tree(s) skipped: notowned" in out
    assert "4 excluded dir subtree(s)" in out
    assert "1 memory index file(s) (MEMORY.md) skipped" in out


def test_scan_db_rows_per_type(mini_workspace: dict[str, Path | str]) -> None:
    fx = mini_workspace
    assert main(["init-db", "--db", str(fx["db"])]) == 0
    assert main(_scan_args(fx)) == 0
    rows = _rows(Path(str(fx["db"])))
    ws_slug = str(fx["ws_slug"])

    assert rows[".claude/rules/one-rule.md"][:2] == ("rule", "coding-root")

    artifact_type, project, details = rows[".claude/skills/real-skill/SKILL.md"]
    assert (artifact_type, project) == ("skill", "coding-root")
    assert details["name"] == "real-skill"
    assert details["user_invocable"] is True
    assert details["command_style"] == "skill_dir"
    assert details["has_evals"] is True
    assert details["is_pointer"] is False

    # Thin wrapper resolves to its pointed-to file (never zero choices) — the target is
    # NOT itself an artifact, so it is marked for inlining.
    details = rows[".claude/skills/thin-wrap/SKILL.md"][2]
    assert details["is_pointer"] is True
    assert details["pointer_target"] == ".claude/shared/core.md"
    assert details["pointer_target_is_artifact"] is False
    assert details["resolution_base"] == "file"

    # Wrapper onto a scanned artifact: relationship recorded, no double-extraction.
    details = rows[".claude/skills/rule-wrap/SKILL.md"][2]
    assert details["pointer_target"] == ".claude/rules/one-rule.md"
    assert details["pointer_target_is_artifact"] is True

    # Dead pointer target: recorded, never silently zero (and no resolution base).
    details = rows[".claude/skills/broken-wrap/SKILL.md"][2]
    assert details["is_pointer"] is True
    assert details["pointer_unresolved"] is True
    assert "pointer_target" not in details
    assert "resolution_base" not in details

    # .claude/commands/*.md ingests as skill (same shape, same extractor).
    artifact_type, _project, details = rows[".claude/commands/foo.md"]
    assert artifact_type == "skill"
    assert details["command_style"] == "commands_dir"

    # Root CLAUDE.md @path imports: artifact target recorded-and-skipped; plain doc
    # inlined; missing target unresolved; fenced/email tokens never matched.
    artifact_type, project, details = rows["CLAUDE.md"]
    assert (artifact_type, project) == ("claude_md", "coding-root")
    assert details["scope"] == "root"
    assert details["artifact_imports"] == [".claude/rules/one-rule.md"]
    assert details["inline_targets"] == ["docs/notes.md"]
    assert details["pointer_unresolved"] == ["missing/nowhere.md"]
    assert "decorator_looking_thing" not in json.dumps(details)
    assert "example.com" not in json.dumps(details)

    artifact_type, project, details = rows["projA/CLAUDE.md"]
    assert (artifact_type, project) == ("claude_md", "projA")
    assert details["scope"] == "project"

    artifact_type, project, details = rows["projA/plan.md"]
    assert (artifact_type, project) == ("plan", "projA")
    assert details["plan_kind"] == "root"
    assert details["is_pointer_only"] is False
    assert details["step_count"] == 2

    # Pointer-only plan: linked targets recorded, missing link surfaced.
    details = rows["projB/plan.md"][2]
    assert details["is_pointer_only"] is True
    assert details["linked_targets"] == ["projB/docs/phase-1-plan.md"]
    assert details["pointer_unresolved"] == ["docs/gone-plan.md"]
    # The linked sub-plan is not itself an entry plan — no separate artifact row.
    assert "projB/docs/phase-1-plan.md" not in rows

    # Memory: two-scheme path, nested-metadata frontmatter, index + scope mapping.
    artifact_type, project, details = rows[f"memory:{ws_slug}/feedback_fixture_thing.md"]
    assert (artifact_type, project) == ("memory", "coding-root")
    assert details["node_type"] == "memory"
    assert details["memory_kind"] == "feedback"
    assert details["origin_session_id"] == SESSION_ID
    assert details["memory_scope"] == "global"
    assert str(details["frontmatter_modified"]).startswith("2026-07-21")

    artifact_type, project, details = rows[f"memory:{ws_slug}-projA/project_note.md"]
    assert (artifact_type, project) == ("memory", "projA")
    assert details["memory_scope"] == "project"

    assert rows["memory:c--somewhere-else-entirely/other_tree.md"][1] == "global"

    # Malformed frontmatter tolerated + recorded, never a crash.
    details = rows[f"memory:{ws_slug}/broken_fm.md"][2]
    assert details["frontmatter_error"]

    # Explicit exclusions: MEMORY.md index, owned=false tree (workspace walk AND the
    # independently-coded memory-dir path), vendored/VCS dirs, docs/archived*.
    assert not any("MEMORY.md" in path for path in rows)
    assert "notowned/CLAUDE.md" not in rows
    assert f"memory:{ws_slug}-notowned/private_note.md" not in rows
    assert not any(
        path.startswith((".venv/", "node_modules/", ".git/", "docs/archived")) for path in rows
    )


def test_rescan_unchanged_then_updated(
    mini_workspace: dict[str, Path | str], capsys: pytest.CaptureFixture[str]
) -> None:
    fx = mini_workspace
    assert main(["init-db", "--db", str(fx["db"])]) == 0
    assert main(_scan_args(fx)) == 0
    capsys.readouterr()

    # Second scan: everything unchanged, nothing rewritten.
    assert main(_scan_args(fx)) == 0
    out = capsys.readouterr().out
    assert _count_line("rule", 1, 0, 0, 1) in out
    assert _count_line("skill", 5, 0, 0, 5) in out
    assert _count_line("memory", 4, 0, 0, 4) in out

    conn = db.connect(Path(str(fx["db"])))
    try:
        old_hash = conn.execute(
            "SELECT current_content_hash FROM artifacts WHERE path = '.claude/rules/one-rule.md'"
        ).fetchone()[0]
    finally:
        conn.close()

    _write(
        Path(str(fx["ws"])) / ".claude" / "rules" / "one-rule.md",
        "# One rule\n\nAlways do the thing.\n\nNow with a new clause.\n",
    )
    assert main(_scan_args(fx)) == 0
    out = capsys.readouterr().out
    assert _count_line("rule", 1, 0, 1, 0) in out

    conn = db.connect(Path(str(fx["db"])))
    try:
        new_hash = conn.execute(
            "SELECT current_content_hash FROM artifacts WHERE path = '.claude/rules/one-rule.md'"
        ).fetchone()[0]
        count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    finally:
        conn.close()
    assert new_hash != old_hash
    assert count == 14  # 1 rule + 5 skills + 2 claude_md + 2 plans + 4 memories, no dupes


def test_scan_project_filter_upserts_only_that_project(
    mini_workspace: dict[str, Path | str], capsys: pytest.CaptureFixture[str]
) -> None:
    fx = mini_workspace
    assert main(["init-db", "--db", str(fx["db"])]) == 0
    assert main(_scan_args(fx, "--project", "projA")) == 0
    out = capsys.readouterr().out
    assert "Project filter: projA" in out
    rows = _rows(Path(str(fx["db"])))
    assert rows, "expected projA artifacts"
    assert all(row[1] == "projA" for row in rows.values())
    assert set(rows) == {
        "projA/CLAUDE.md",
        "projA/plan.md",
        f"memory:{fx['ws_slug']}-projA/project_note.md",
    }


def test_scan_project_filter_coding_root_registers_all_five_types(
    mini_workspace: dict[str, Path | str],
) -> None:
    """The acceptance-target shape: `cite scan --project coding-root` covers all 5 types."""
    fx = mini_workspace
    assert main(["init-db", "--db", str(fx["db"])]) == 0
    assert main(_scan_args(fx, "--project", "coding-root")) == 0
    rows = _rows(Path(str(fx["db"])))
    assert all(row[1] == "coding-root" for row in rows.values())
    assert {row[0] for row in rows.values()} == {"rule", "skill", "claude_md", "plan", "memory"}


def test_scan_workspace_root_env_var(
    mini_workspace: dict[str, Path | str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = mini_workspace
    monkeypatch.setenv(discover.WORKSPACE_ROOT_ENV, str(fx["ws"]))
    assert main(["init-db", "--db", str(fx["db"])]) == 0
    capsys.readouterr()
    assert main(["scan", "--memory-root", str(fx["mem"]), "--db", str(fx["db"])]) == 0
    out = capsys.readouterr().out
    assert f"Scanned workspace: {Path(str(fx['ws'])).resolve().as_posix()}" in out
    assert _count_line("rule", 1, 1, 0, 0) in out


def test_scan_missing_db_exits_nonzero(
    mini_workspace: dict[str, Path | str], capsys: pytest.CaptureFixture[str]
) -> None:
    fx = mini_workspace
    assert main(_scan_args(fx)) == 1
    assert "does not exist" in capsys.readouterr().out


def test_scan_missing_workspace_root_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "citation.db"
    assert main(["init-db", "--db", str(db_path)]) == 0
    capsys.readouterr()
    assert main(["scan", "--workspace-root", str(tmp_path / "nope"), "--db", str(db_path)]) == 1
    assert "workspace root does not exist" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Unit: entry-plan finder (descriptor-contract §4 mirror)
# ---------------------------------------------------------------------------


def test_find_entry_plan_root_plan_beats_master(tmp_path: Path) -> None:
    _write(tmp_path / "plan.md", "# p\n")
    _write(tmp_path / "master_plan.md", "# m\n")
    assert discover.find_entry_plan(tmp_path) == tmp_path / "plan.md"


def test_find_entry_plan_master_when_no_plan(tmp_path: Path) -> None:
    _write(tmp_path / "master_plan.md", "# m\n")
    assert discover.find_entry_plan(tmp_path) == tmp_path / "master_plan.md"


def test_find_entry_plan_subdir_before_glob(tmp_path: Path) -> None:
    _write(tmp_path / "plans" / "plan.md", "# p\n")
    _write(tmp_path / "docs" / "feature-plan.md", "# f\n")
    assert discover.find_entry_plan(tmp_path) == tmp_path / "plans" / "plan.md"


def test_find_entry_plan_glob_skips_archive_template_draft_brainstorm(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "archive-old-plan.md", "# a\n")
    _write(tmp_path / "docs" / "template_plan.md", "# t\n")
    _write(tmp_path / "docs" / "draft-x-plan.md", "# d\n")
    _write(tmp_path / "docs" / "brainstorm-plan.md", "# b\n")
    _write(tmp_path / "docs" / "feature-plan.md", "# f\n")
    assert discover.find_entry_plan(tmp_path) == tmp_path / "docs" / "feature-plan.md"


def test_find_entry_plan_none(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "notes.md", "# n\n")
    assert discover.find_entry_plan(tmp_path) is None


# ---------------------------------------------------------------------------
# Unit: pointer detection, imports, frontmatter, project resolution
# ---------------------------------------------------------------------------


def test_detect_skill_pointer_variants() -> None:
    assert (
        discover.detect_skill_pointer("Read `../../skills/core.md` and follow it.\n")
        == "../../skills/core.md"
    )
    assert (
        discover.detect_skill_pointer("Read skills/core.md, then follow the instructions.\n")
        == "skills/core.md"
    )
    assert discover.detect_skill_pointer("# Title\n\nRead `a/b.md` and follow it.\n") == "a/b.md"


def test_detect_skill_pointer_rejects_real_bodies() -> None:
    # No follow-shape at all.
    assert discover.detect_skill_pointer("Do the thing. Then do the other thing.\n") is None
    # Pointer-shaped line buried in a long real body is not a thin wrapper.
    long_body = "Read `a/b.md` and follow it.\n" + "More instructions.\n" * 20
    assert discover.detect_skill_pointer(long_body) is None
    assert discover.detect_skill_pointer("") is None


def test_extract_imports_filters_noise() -> None:
    text = (
        "Rule: @.claude/rules/x.md and @docs/notes.md and @AGENTS.md\n"
        "Email someone@example.com stays out.\n"
        "Deps: colorjs.io, culori / @adobe/leonardo-contrast-colors | contrast gate\n"
        "Scoped npm package @scope/some-pkg-name stays out too.\n"
        "```\n@fenced/path.md\n```\n"
        "Inline `@span/path.md` stays out too.\n"
    )
    assert discover.extract_imports(text) == [
        ".claude/rules/x.md",
        "docs/notes.md",
        "AGENTS.md",
    ]


def test_parse_frontmatter_malformed_is_tolerated() -> None:
    frontmatter, body, error = discover.parse_frontmatter(
        "---\nname: [unclosed\nmetadata: {\n---\nBody anyway.\n"
    )
    assert frontmatter == {}
    assert body == "Body anyway.\n"
    assert error is not None


def test_parse_frontmatter_missing_is_not_an_error() -> None:
    frontmatter, body, error = discover.parse_frontmatter("# Just a body\n")
    assert frontmatter == {}
    assert body == "# Just a body\n"
    assert error is None


def test_resolve_project_longest_prefix_wins() -> None:
    registry = [
        discover.RegistryEntry(slug="outer", path="a", owned=True),
        discover.RegistryEntry(slug="inner", path="a/b", owned=True),
    ]
    assert discover.resolve_project("a/b/c.md", registry) == "inner"
    assert discover.resolve_project("a/x.md", registry) == "outer"
    assert discover.resolve_project("elsewhere/x.md", registry) == "coding-root"


def test_workspace_memory_slug() -> None:
    assert discover.workspace_memory_slug(Path("C:/Users/x/dev")) == "C--Users-x-dev"


# ---------------------------------------------------------------------------
# Unit: scan_workspace report-level facts (memory index exclusion is explicit)
# ---------------------------------------------------------------------------


def test_scan_workspace_memory_index_exclusion_is_explicit(
    mini_workspace: dict[str, Path | str],
) -> None:
    fx = mini_workspace
    report = discover.scan_workspace(Path(str(fx["ws"])), memory_root=Path(str(fx["mem"])))
    assert report.memory_index_skipped == 1
    assert not any("MEMORY.md" in artifact.path for artifact in report.artifacts)
    # Both the workspace walk AND the memory scan hit the notowned project; the list is
    # deduped, and no artifact from the not-owned memory dir survives.
    assert report.not_owned_skipped == ["notowned"]
    assert not any("-notowned/" in artifact.path for artifact in report.artifacts)
    assert report.excluded_dir_count == 4  # .venv, node_modules, .git, docs/archived-junk


def test_scan_workspace_missing_registry_degrades(tmp_path: Path) -> None:
    ws = tmp_path / "bare"
    _write(ws / ".claude" / "rules" / "r.md", "# r\n")
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    assert [a.artifact_type for a in report.artifacts] == ["rule"]
    assert report.artifacts[0].project == "coding-root"
    assert any("registry not found" in note for note in report.notes)
    assert any("memory root not found" in note for note in report.notes)


# ---------------------------------------------------------------------------
# Iteration 2 (review-driven): pointer resolution bases, scan resilience,
# hash policy, case-insensitivity, pointer-only heuristic, nested registry
# paths, skill frontmatter-error wiring
# ---------------------------------------------------------------------------


def test_pointer_resolves_relative_to_project_root(tmp_path: Path) -> None:
    """The shake_spear shape: a pointer written relative to the 'workshop root', not the
    SKILL.md's own directory, still resolves (file-relative tried first, then ancestors),
    and the resolving ancestor is recorded as resolution_base for auditability."""
    ws = tmp_path / "ws"
    _write(
        ws / "workshop" / ".claude" / "skills" / "keeper" / "SKILL.md",
        "---\nname: keeper\ndescription: Thin wrapper.\n---\n"
        "Read `skills/keeper_core.md` (relative to the workshop root) and follow it"
        " exactly.\n",
    )
    _write(ws / "workshop" / "skills" / "keeper_core.md", "# The real instructions\n")
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    details = next(a for a in report.artifacts if a.path.endswith("SKILL.md")).details
    assert isinstance(details, SkillDetails)
    assert details.is_pointer is True
    assert details.pointer_target == "workshop/skills/keeper_core.md"
    assert details.pointer_unresolved is None
    assert details.resolution_base == "workshop"


def test_pointer_backslash_token_normalized(tmp_path: Path) -> None:
    """A Windows-authored backslash pointer token resolves like its forward-slash twin.

    Platform caveat (test-quality review, accepted): on Windows, WindowsPath treats a
    backslash as a separator anyway, so this test only exercises the explicit
    ``.replace("\\\\", "/")`` normalization when run on a POSIX runner — on this
    project's Windows dev box it is documentation-grade coverage. Kept because the
    normalization ALSO feeds the resolution-policy prefix checks (``./``/``../``/bare),
    which the resolution_base assertion below does exercise on every platform."""
    ws = tmp_path / "ws"
    _write(
        ws / ".claude" / "skills" / "bs-wrap" / "SKILL.md",
        "---\nname: bs-wrap\n---\nRead `..\\..\\shared\\core.md` and follow it.\n",
    )
    _write(ws / ".claude" / "shared" / "core.md", "# Core\n")
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    details = next(a for a in report.artifacts if a.path.endswith("SKILL.md")).details
    assert isinstance(details, SkillDetails)
    assert details.pointer_target == ".claude/shared/core.md"
    # The target exists file-relative, so the first-priority base wins and is recorded.
    assert details.resolution_base == "file"


def test_plan_discovery_survives_bad_toplevel_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad top-level dir records a SCAN ERROR note and never zeroes plan discovery
    for its siblings (the silent-fallthrough-in-loop class)."""
    ws = tmp_path / "ws"
    _write(ws / "aproj" / "plan.md", "# a\n\n### Step 1: X\n- **Problem:** P.\n")
    _write(ws / "zproj" / "plan.md", "# z\n\n### Step 1: Y\n- **Problem:** Q.\n")
    (ws / "boom").mkdir()
    real_is_dir = Path.is_dir

    def fake_is_dir(self: Path, **kwargs: object) -> bool:
        if self.name == "boom":
            raise PermissionError("locked by AV")
        return real_is_dir(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "is_dir", fake_is_dir)
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    plan_paths = {a.path for a in report.artifacts if a.artifact_type == "plan"}
    assert {"aproj/plan.md", "zproj/plan.md"} <= plan_paths
    assert any("SCAN ERROR" in note and "boom" in note for note in report.notes)


def test_memory_scan_survives_bad_project_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One flaky memory project dir is skipped loudly; its siblings still ingest.

    The fault is injected on the ``memory`` SUBDIR probe of the flaky project dir — NOT
    on the project dir itself, which the earlier ``_safe_is_dir`` filter in
    ``_scan_memory`` would absorb before the target guard ever ran — so the raise
    originates INSIDE ``_scan_memory_project_dir`` and only the per-project-dir
    try/except in ``_scan_memory`` can catch it. Removing that guard makes this test
    CRASH (uncaught PermissionError out of scan_workspace), not just fail an assert."""
    ws = tmp_path / "ws"
    ws.mkdir()
    mem = tmp_path / "mem"
    slug = discover.workspace_memory_slug(ws.resolve())
    _write(
        mem / slug / "memory" / "note.md",
        "---\nname: note\nmetadata:\n  node_type: memory\n  type: feedback\n---\nBody.\n",
    )
    (mem / "flaky-proj" / "memory").mkdir(parents=True)
    real_is_dir = Path.is_dir

    def fake_is_dir(self: Path, **kwargs: object) -> bool:
        if self.name == "memory" and self.parent.name == "flaky-proj":
            raise PermissionError("locked")
        return real_is_dir(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "is_dir", fake_is_dir)
    report = discover.scan_workspace(ws, memory_root=mem)
    assert any(a.path == f"memory:{slug}/note.md" for a in report.artifacts)
    assert any("SCAN ERROR (memory)" in note and "flaky-proj" in note for note in report.notes)


def test_rescan_line_ending_flip_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CRLF<->LF-only drift (checkout autocrlf, editor rewrite) never flips 'updated';
    a real content change still does."""
    ws = tmp_path / "ws"
    rule = ws / ".claude" / "rules" / "r.md"
    rule.parent.mkdir(parents=True)
    rule.write_bytes(b"# R\n\nDo the thing.\n")
    db_path = tmp_path / "citation.db"
    assert main(["init-db", "--db", str(db_path)]) == 0
    args = [
        "scan",
        "--workspace-root",
        str(ws),
        "--memory-root",
        str(tmp_path / "no-mem"),
        "--db",
        str(db_path),
    ]
    assert main(args) == 0
    capsys.readouterr()

    rule.write_bytes(b"# R\r\n\r\nDo the thing.\r\n")
    assert main(args) == 0
    assert _count_line("rule", 1, 0, 0, 1) in capsys.readouterr().out

    rule.write_bytes(b"# R\r\n\r\nDo the OTHER thing.\r\n")
    assert main(args) == 0
    assert _count_line("rule", 1, 0, 1, 0) in capsys.readouterr().out


def test_resolve_project_is_case_insensitive() -> None:
    """Registry-path casing that drifts from the on-disk dir still matches (Windows)."""
    registry = [discover.RegistryEntry(slug="toybox", path="Toybox", owned=True)]
    assert discover.resolve_project("toybox/x.md", registry) == "toybox"
    assert discover.resolve_project("Toybox/x.md", registry) == "toybox"
    assert discover.resolve_project("toyboxx/x.md", registry) == "coding-root"


def test_excluded_dir_names_case_insensitive(tmp_path: Path) -> None:
    """A differently-cased vendored dir (.Venv, Node_Modules) is still pruned."""
    ws = tmp_path / "ws"
    _write(ws / ".Venv" / "CLAUDE.md", "# vendored\n")
    _write(ws / "Node_Modules" / "CLAUDE.md", "# vendored\n")
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    assert report.artifacts == []
    assert report.excluded_dir_count == 2


def test_substantive_plan_without_steps_is_not_pointer_only(tmp_path: Path) -> None:
    """A long prose/architecture plan that simply doesn't use Step-N/Phase headings is
    real content — never classified as a pointer stub."""
    ws = tmp_path / "ws"
    body = (
        "# Big plan\n\n## Codebase Summary\n\n"
        + "Prose line describing the architecture in detail.\n" * 80
        + "\nOne incidental link: [notes](docs/notes.md)\n"
    )
    _write(ws / "proj" / "plan.md", body)
    _write(ws / "proj" / "docs" / "notes.md", "# notes\n")
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    details = next(a for a in report.artifacts if a.path == "proj/plan.md").details
    assert isinstance(details, PlanDetails)
    assert details.is_pointer_only is False
    assert details.linked_targets is None


def test_link_dense_index_plan_is_pointer_only(tmp_path: Path) -> None:
    """A genuine index/master plan stays pointer-only past the short-stub cap when its
    link density says 'index'."""
    ws = tmp_path / "ws"
    links = "".join(f"- [Part {i}](docs/part-{i}-plan.md)\n" for i in range(12))
    body = (
        "# Master plan and index\n\nThis file is the master plan and index.\n\n"
        + links
        + "Context prose line.\n" * 60
    )
    _write(ws / "proj" / "master_plan.md", body)
    for i in range(12):
        _write(ws / "proj" / "docs" / f"part-{i}-plan.md", f"# Part {i}\n")
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    details = next(a for a in report.artifacts if a.path == "proj/master_plan.md").details
    assert isinstance(details, PlanDetails)
    assert details.is_pointer_only is True
    assert details.linked_targets is not None
    assert len(details.linked_targets) == 12


def test_memory_slug_matching_is_case_insensitive(tmp_path: Path) -> None:
    """Drive-letter/case drift between the computed workspace slug and the real memory
    dir names (the docstring's own documented production condition) still maps."""
    ws = tmp_path / "ws"
    _write(
        ws / ".claude" / "observatory" / "registry.toml",
        '[[project]]\nslug = "projA"\npath = "projA"\nowned = true\n',
    )
    mem = tmp_path / "mem"
    slug = discover.workspace_memory_slug(ws.resolve())
    mangled = slug.swapcase()
    assert mangled != slug  # vacuous unless the casing actually differs
    _write(
        mem / mangled / "memory" / "global_note.md",
        "---\nmetadata:\n  type: feedback\n---\nBody.\n",
    )
    _write(
        mem / f"{mangled}-PROJA" / "memory" / "proj_note.md",
        "---\nmetadata:\n  type: project\n---\nBody.\n",
    )
    report = discover.scan_workspace(ws, memory_root=mem)
    by_path = {a.path: a for a in report.artifacts}
    global_art = by_path[f"memory:{mangled}/global_note.md"]
    assert global_art.project == "coding-root"
    assert isinstance(global_art.details, MemoryDetails)
    assert global_art.details.memory_scope == "global"
    proj_art = by_path[f"memory:{mangled}-PROJA/proj_note.md"]
    assert proj_art.project == "projA"
    assert isinstance(proj_art.details, MemoryDetails)
    assert proj_art.details.memory_scope == "project"


def test_skill_frontmatter_error_recorded(tmp_path: Path) -> None:
    """The SKILL extractor's own malformed-frontmatter wiring (field + note), distinct
    from the memory extractor's already-covered path."""
    ws = tmp_path / "ws"
    _write(
        ws / ".claude" / "skills" / "bad-fm" / "SKILL.md",
        "---\nname: [unclosed\nmetadata: {\n---\nA body line that is not a pointer.\n",
    )
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    details = next(a for a in report.artifacts if a.path.endswith("SKILL.md")).details
    assert isinstance(details, SkillDetails)
    assert details.frontmatter_error
    assert details.name is None
    assert any("frontmatter error: .claude/skills/bad-fm/SKILL.md" in note for note in report.notes)


def test_plan_discovery_for_nested_registry_path(tmp_path: Path) -> None:
    """An owned registry path deeper than one level (not a top-level child) is found via
    the registry branch alone."""
    ws = tmp_path / "ws"
    _write(
        ws / ".claude" / "observatory" / "registry.toml",
        '[[project]]\nslug = "projC"\npath = "libs/projC"\nowned = true\n',
    )
    _write(ws / "libs" / "projC" / "plan.md", "# projC\n\n### Step 1: X\n- **Problem:** P.\n")
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    plans = {a.path: a for a in report.artifacts if a.artifact_type == "plan"}
    assert "libs/projC/plan.md" in plans
    assert plans["libs/projC/plan.md"].project == "projC"


def test_scan_registry_unreadable_degrades_loudly(
    mini_workspace: dict[str, Path | str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry that vanishes/locks between the existence check and the read (TOCTOU)
    degrades with a loud note — never a crash."""
    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "registry.toml":
            raise PermissionError("registry locked")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    fx = mini_workspace
    report = discover.scan_workspace(Path(str(fx["ws"])), memory_root=Path(str(fx["mem"])))
    assert any("registry unreadable" in note for note in report.notes)
    # Degraded, not silently empty: with no registry there are no owned=false
    # exclusions, so the notowned tree ingests as coding-root.
    assert any(a.path == "notowned/CLAUDE.md" for a in report.artifacts)


def test_scan_registry_non_utf8_degrades_loudly(
    mini_workspace: dict[str, Path | str],
) -> None:
    """A registry.toml whose bytes are not UTF-8 (interrupted write, cp1252 re-save)
    is a PARSE failure, not a crash: same loud-note degrade as malformed TOML — never
    an uncaught UnicodeDecodeError (the bug-shape sibling of breakdown.py's
    unreadable-header guard)."""
    fx = mini_workspace
    registry = Path(str(fx["ws"])) / ".claude" / "observatory" / "registry.toml"
    registry.write_bytes(b'[[project]]\nslug = "proj\xff\xfeA"\npath = "projA"\n')
    report = discover.scan_workspace(Path(str(fx["ws"])), memory_root=Path(str(fx["mem"])))
    assert any("registry parse error" in note for note in report.notes)
    # Degraded exactly like malformed TOML: no owned=false exclusions survive, so the
    # notowned tree ingests as coding-root.
    assert any(a.path == "notowned/CLAUDE.md" for a in report.artifacts)


# ---------------------------------------------------------------------------
# Iteration 3 (review-driven): the enumeration-seam invariant (fault injection at the
# os.scandir layer — the layer BENEATH the guarded seam, so ANY regression back to
# pathlib glob/rglob/os.walk, whose internals swallow the same OSError silently, fails
# these tests on the missing SCAN ERROR note), pointer-resolution policy bounds, and
# the oversized-read guard.
# ---------------------------------------------------------------------------


def _patch_scandir_to_fail_for(monkeypatch: pytest.MonkeyPatch, dir_name: str) -> None:
    """Make os.scandir raise PermissionError for any directory named ``dir_name``.

    Injecting at os.scandir (not at a discover helper) makes these tests
    implementation-independent: pathlib's glob/rglob and os.walk all sit on os.scandir,
    so a reintroduction of any of them cannot dodge the fault — it can only differ in
    whether the failure surfaces LOUDLY (the invariant) or vanishes (the bug-shape)."""
    real_scandir = os.scandir

    def fake_scandir(path: Any = ".", *args: Any, **kwargs: Any) -> Any:
        if Path(os.fspath(path)).name == dir_name:
            raise PermissionError("locked by AV")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", fake_scandir)


def test_plan_glob_enumeration_failure_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE invariant for the 'enumeration silently swallows OSError' bug-shape, plan-glob
    path: a failing docs/ dir on the `*-plan.md` fallback branch must complete the scan,
    drop ONLY that project's plan artifact, and record a loud SCAN ERROR note FROM THE
    PLAN-GLOB ENUMERATION ITSELF (with pathlib.glob this exact fault produced None with
    zero notes and zero exceptions — the walk's own note for the same dir is asserted
    separately and must not satisfy this invariant)."""
    ws = tmp_path / "ws"
    _write(
        ws / "aproj" / "docs" / "feature-plan.md",
        "# f\n\n### Step 1: X\n- **Problem:** P.\n",
    )
    _write(ws / "zproj" / "plan.md", "# z\n\n### Step 1: Y\n- **Problem:** Q.\n")
    _patch_scandir_to_fail_for(monkeypatch, "docs")
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    plan_paths = {a.path for a in report.artifacts if a.artifact_type == "plan"}
    assert "zproj/plan.md" in plan_paths  # scan completed; the sibling is intact
    assert not any(path.startswith("aproj/") for path in plan_paths)  # artifact missing…
    # …LOUDLY, and specifically from the glob layer (a walk-context note alone would
    # mean find_entry_plan still swallows):
    assert any("SCAN ERROR (plan glob)" in note and "docs" in note for note in report.notes)
    assert any("SCAN ERROR (walk)" in note and "docs" in note for note in report.notes)


def test_memory_rglob_enumeration_failure_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE invariant for the 'enumeration silently swallows OSError' bug-shape, memory
    recursive path: a failing NESTED subdir under a memory/ tree must complete the scan,
    keep the sibling memory files, drop only the unreachable subtree, and record a loud
    SCAN ERROR note (with rglob this exact fault silently vanished the subtree)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    mem = tmp_path / "mem"
    slug = discover.workspace_memory_slug(ws.resolve())
    _write(
        mem / slug / "memory" / "aaa_good.md",
        "---\nmetadata:\n  type: feedback\n---\nBody.\n",
    )
    _write(
        mem / slug / "memory" / "zzz_bad_subdir" / "inner.md",
        "---\nmetadata:\n  type: feedback\n---\nBody.\n",
    )
    _patch_scandir_to_fail_for(monkeypatch, "zzz_bad_subdir")
    report = discover.scan_workspace(ws, memory_root=mem)
    paths = {a.path for a in report.artifacts}
    assert f"memory:{slug}/aaa_good.md" in paths  # scan completed; sibling ingested
    assert not any("inner.md" in path for path in paths)  # unreachable subtree missing…
    assert any(
        "SCAN ERROR (memory files)" in note and "zzz_bad_subdir" in note for note in report.notes
    )


def test_bare_filename_pointer_is_file_relative_only(tmp_path: Path) -> None:
    """A dead bare-filename pointer must NOT false-resolve to an unrelated same-named
    file at an ancestor (the reproduced ws/README.md case): bare names resolve ONLY
    against the pointing file's own directory — a miss is loudly unresolved, and a hit
    records resolution_base='file'."""
    ws = tmp_path / "ws"
    _write(ws / "README.md", "# unrelated root readme\n")
    _write(
        ws / "subproj" / ".claude" / "skills" / "thing" / "SKILL.md",
        "---\nname: thing\n---\nRead `README.md` and follow it.\n",
    )
    _write(
        ws / "subproj" / ".claude" / "skills" / "sib" / "SKILL.md",
        "---\nname: sib\n---\nRead `notes.md` and follow it.\n",
    )
    _write(ws / "subproj" / ".claude" / "skills" / "sib" / "notes.md", "# the real notes\n")
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    by_path = {a.path: a.details for a in report.artifacts}
    dead = by_path["subproj/.claude/skills/thing/SKILL.md"]
    assert isinstance(dead, SkillDetails)
    assert dead.is_pointer is True
    assert dead.pointer_unresolved is True  # NOT silently resolved to ws/README.md
    assert dead.pointer_target is None
    assert dead.resolution_base is None
    assert any("UNRESOLVED pointer" in note and "thing/SKILL.md" in note for note in report.notes)
    live = by_path["subproj/.claude/skills/sib/SKILL.md"]
    assert isinstance(live, SkillDetails)
    assert live.pointer_target == "subproj/.claude/skills/sib/notes.md"
    assert live.resolution_base == "file"


def test_multicomponent_pointer_stops_at_project_root(tmp_path: Path) -> None:
    """The ancestor walk never passes the artifact's project root — neither for a
    registry-registered project (projR) nor a marker-detected one (projM, CLAUDE.md).
    The same-named file beyond the boundary must NOT resolve; the pointer is loudly
    unresolved instead of confidently wrong."""
    ws = tmp_path / "ws"
    _write(
        ws / ".claude" / "observatory" / "registry.toml",
        '[[project]]\nslug = "projR"\npath = "projR"\nowned = true\n',
    )
    _write(ws / "docs" / "help.md", "# beyond the project boundary\n")
    _write(
        ws / "projR" / ".claude" / "skills" / "a" / "SKILL.md",
        "---\nname: a\n---\nRead `docs/help.md` and follow it.\n",
    )
    _write(ws / "projM" / "CLAUDE.md", "# projM\n")
    _write(
        ws / "projM" / ".claude" / "skills" / "b" / "SKILL.md",
        "---\nname: b\n---\nRead `docs/help.md` and follow it.\n",
    )
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    by_path = {a.path: a.details for a in report.artifacts}
    for path in ("projR/.claude/skills/a/SKILL.md", "projM/.claude/skills/b/SKILL.md"):
        details = by_path[path]
        assert isinstance(details, SkillDetails), path
        assert details.pointer_unresolved is True, path
        assert details.pointer_target is None, path


def test_parent_anchored_pointer_resolves_via_ancestor_within_project(tmp_path: Path) -> None:
    """The shake_spear NESTED-project shape (live in the real workspace, e.g.
    shake_spear/projects/dev_dispatches): a `../../skills/x.md` token authored relative
    to the nested project's root — not the SKILL.md's own dir — resolves via the bounded
    ancestor walk, to a target still inside the project root, with the base recorded."""
    ws = tmp_path / "ws"
    _write(ws / "workshop" / "CLAUDE.md", "# workshop\n")  # marker -> stop = workshop
    _write(
        ws / "workshop" / "projects" / "sub" / ".claude" / "skills" / "keeper" / "SKILL.md",
        "---\nname: keeper\n---\nRead `../../skills/keeper_core.md` and follow it.\n",
    )
    _write(ws / "workshop" / "skills" / "keeper_core.md", "# the real instructions\n")
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    details = next(a for a in report.artifacts if a.path.endswith("SKILL.md")).details
    assert isinstance(details, SkillDetails)
    assert details.pointer_target == "workshop/skills/keeper_core.md"
    assert details.pointer_unresolved is None
    assert details.resolution_base == "workshop/projects/sub"


def test_short_prose_plan_with_incidental_link_not_pointer_only(tmp_path: Path) -> None:
    """A SHORT real plan — substantive prose, one incidental link, no Step/Phase units —
    is real content: the short-stub branch alone must not reclassify it as 'nothing
    here, look elsewhere' (the iteration-2 heuristic's residual false-positive class)."""
    ws = tmp_path / "ws"
    body = (
        "# Small plan\n\n## Overview\n\n"
        + "A real design decision, described in prose.\n" * 20
        + "\nSee also the [related design doc](docs/other.md).\n"
    )
    _write(ws / "proj" / "plan.md", body)
    _write(ws / "proj" / "docs" / "other.md", "# other\n")
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    details = next(a for a in report.artifacts if a.path == "proj/plan.md").details
    assert isinstance(details, PlanDetails)
    assert details.is_pointer_only is False
    assert details.linked_targets is None


def test_tiny_pointer_stub_plan_is_still_pointer_only(tmp_path: Path) -> None:
    """The genuine 'plan moved' stub (a few lines, a link) stays pointer-only under the
    tightened short-stub cap."""
    ws = tmp_path / "ws"
    _write(
        ws / "proj" / "plan.md",
        "# Moved\n\nThe plan now lives at [the real plan](docs/real-plan.md).\n",
    )
    _write(ws / "proj" / "docs" / "real-plan.md", "# real\n\n### Step 1: X\n- **Problem:** P.\n")
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    details = next(a for a in report.artifacts if a.path == "proj/plan.md").details
    assert isinstance(details, PlanDetails)
    assert details.is_pointer_only is True
    assert details.linked_targets == ["proj/docs/real-plan.md"]


def test_read_size_cap_skips_oversized_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grossly oversized candidate (mis-extensioned blob) is skipped with a loud note
    instead of being read fully into memory (resource-exhaustion guard on _read)."""
    monkeypatch.setattr(discover, "_MAX_READ_BYTES", 16)
    ws = tmp_path / "ws"
    _write(ws / ".claude" / "rules" / "big.md", "x" * 64)
    _write(ws / ".claude" / "rules" / "small.md", "# ok\n")
    report = discover.scan_workspace(ws, memory_root=tmp_path / "no-mem")
    assert [a.path for a in report.artifacts] == [".claude/rules/small.md"]
    assert any(
        "unreadable/oversized file skipped" in note and "big.md" in note for note in report.notes
    )
