"""breakdown.py + `cite report` — slug rule, rendering, collisions, report round-trip.

The slug rule reproduces plan §3.2's canonical example (and neutralizes backslash as
defense-in-depth); the rendered doc labels BOTH citation classes
([external]/[internal]), shows recorded search queries on no-literature-found rows,
renders suggestions for needs-improvement choices, and is deterministic. Because the
slug is not injective, colliding artifact paths are DETECTED at write time and
diverted to a hash-discriminated sibling with a loud note — never a silent
cross-artifact overwrite — and `cite report` (through the production ``cli.main``)
locates whichever file really holds the artifact's review, erroring cleanly when no
review exists.

Workspace/commit scaffolding comes from tests/conftest.py (one source of truth).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from citation_needed import breakdown, db, review
from citation_needed.cli import main
from conftest import (
    LEVER_DOC,
    RULE_PATH,
    _commit,
    _open,
    register_artifact,
    worked_choice,
)

EXPECTED_SLUG = "--claude--rules--subagent-economy"


# ---------------------------------------------------------------------------
# Slug rule (plan §3.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "slug"),
    [
        # The plan's canonical example, byte-for-byte.
        (".claude/rules/subagent-economy.md", EXPECTED_SLUG),
        # ':' from the memory scheme and case-folding.
        (
            "memory:c--Users-abero-dev/feedback_win_capture.md",
            "memory--c--users-abero-dev--feedback_win_capture",
        ),
        ("CLAUDE.md", "claude"),
        ("docs/my plan notes.md", "docs--my--plan--notes"),
        # Interior dots survive; only the trailing .md drops.
        (
            "fixtures/good-anchor.code-quality.frozen.md",
            "fixtures--good-anchor.code-quality.frozen",
        ),
        # Backslash neutralized (defense-in-depth: a real separator to pathlib on
        # Windows — must never survive into a filename).
        ("docs\\sub\\notes.md", "docs--sub--notes"),
        ("..\\..\\outside.md", "--.--..--outside"),  # traversal shape: no separators left
    ],
)
def test_artifact_slug(path: str, slug: str) -> None:
    assert breakdown.artifact_slug(path) == slug


def test_breakdown_path_shape(tmp_path: Path) -> None:
    path = breakdown.breakdown_path(tmp_path, "coding-root", RULE_PATH)
    assert path == tmp_path / "coding-root" / f"{EXPECTED_SLUG}.md"


# ---------------------------------------------------------------------------
# Rendering — via a real CLI commit (two choices: one well-supported with both
# citation classes, one needs-improvement with a no-literature-found record)
# ---------------------------------------------------------------------------


def _two_choice_payload() -> dict[str, Any]:
    weak_choice: dict[str, Any] = {
        "choice_key": "orchestrators-delegate-reads",
        "summary": "Orchestrators delegate reads; they hold conclusions, not file dumps.",
        "quote": "An orchestrator should not Read a file inline to answer a question a "
        "sub-agent is already positioned to answer.",
        "span_start_line": 3,
        "span_end_line": 3,
        "category": "context-economy",
        "votes": ["unsupported", "unsupported", "evidence-backed"],
        "literature_searched": True,
        "literature_found": False,
        "search_queries": [
            "orchestrator delegated reads context economy",
            "LLM agent context window file dump cost",
        ],
        "citations": [],
        "suggestions": [
            "Attach the measured Read-leak share from the token-usage investigation.",
        ],
    }
    return {"choices": [worked_choice(), weak_choice]}


@pytest.fixture()
def committed(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    """Open + commit the two-choice payload through the production CLI (on top of the
    shared ``ws`` workspace fixture); returns the same paths dict."""
    run_id = _open(ws, capsys)["run_id"]
    assert _commit(ws, monkeypatch, _two_choice_payload(), run_id) == 0
    capsys.readouterr()
    return ws


def test_breakdown_written_at_canonical_path(committed: dict[str, Path]) -> None:
    doc = committed["breakdowns"] / "coding-root" / f"{EXPECTED_SLUG}.md"
    assert doc.is_file()


def test_breakdown_renders_all_required_sections(committed: dict[str, Path]) -> None:
    doc = committed["breakdowns"] / "coding-root" / f"{EXPECTED_SLUG}.md"
    text = doc.read_text(encoding="utf-8")

    # Header: artifact, run provenance, composite + band + guide version.
    assert f"# Citation review — {RULE_PATH}" in text
    assert "Reviewer model:** claude-sonnet-5" in text
    assert "content hash `" in text
    assert "(not a git repo)" in text  # nullable git sha rendered explicitly
    # Two choices: evidence-backed (+1.0) + unsupported (-0.5) -> mean 0.25 -> 62.5.
    assert "**Composite:** 62.5 / 100 — band **adequate** (interpretation guide v1)" in text

    # Per-choice blocks: summary, span, category, label + vote shares, classification.
    assert "`subagent-terse-verdict-file-detail` — well-supported" in text
    assert "`orchestrators-delegate-reads` — needs-improvement" in text
    assert f"**Span:** {RULE_PATH}:9-10" in text
    assert "**Category:** context-economy" in text
    assert "**Majority label:** evidence-backed (k=3)" in text
    assert (
        "**Vote shares:** evidence-backed 1.00 · interesting-novel 0.00 · "
        "unsupported 0.00 · contradicted 0.00" in text
    )

    # BOTH citation classes clearly labeled.
    assert "- [external] Lost in the Middle: How Language Models Use Long Contexts" in text
    assert "(supports; api_structured)" in text
    assert "- [internal] Token-usage reduction — consolidated lever map" in text
    assert f"{LEVER_DOC}:5" in text  # internal locator shows the path:line ref
    assert "(supports; internal-read)" in text

    # No-literature-found row shows the recorded search queries.
    assert "NO literature found" in text
    assert "- `orchestrator delegated reads context economy`" in text
    assert "- `LLM agent context window file dump cost`" in text

    # Suggestions render for the needs-improvement choice.
    assert "**Suggestions:**" in text
    assert "Attach the measured Read-leak share" in text

    # Footer points at the interpretation guide.
    assert "docs/interpretation-guide.md (v1)" in text


def _mini_result(artifact_path: str = "CLAUDE.md") -> review.CommitResult:
    tally = review.tally_votes(["evidence-backed", "evidence-backed", "evidence-backed"])
    choice = review.CommittedChoice(
        choice_id=1,
        choice_key="a-choice",
        reused_key=False,
        summary="A summary.",
        quote="A quote.",
        span_start_line=1,
        span_end_line=1,
        source_path=None,
        category="context-economy",
        votes=["evidence-backed", "evidence-backed", "evidence-backed"],
        tally=tally,
        citations=[],
        literature_searched=False,
        literature_found=False,
        search_queries=[],
        rationale=None,
        suggestions=[],
    )
    return review.CommitResult(
        run_id=1,
        artifact_id=1,
        artifact_path=artifact_path,
        artifact_type="claude_md",
        project="coding-root",
        reviewer_model="m",
        started_at="2026-07-21T00:00:00Z",
        finished_at="2026-07-21T00:01:00Z",
        artifact_content_hash_at_review="cafe",
        artifact_git_sha_at_review=None,
        tool_schema_version=2,
        interpretation_guide_version="v1",
        composite=100.0,
        composite_band="strong",
        choices=[choice],
        removed_keys=["z-old", "b-old"],  # deliberately unsorted input
    )


def test_breakdown_render_is_deterministic_and_orders_removed_keys() -> None:
    """render_breakdown is a pure function of CommitResult: same input -> identical
    bytes, removed keys always sorted regardless of input order."""
    result = _mini_result()
    first = breakdown.render_breakdown(result)
    assert first == breakdown.render_breakdown(result)
    assert first.index("`b-old`") < first.index("`z-old`")
    # Literature never searched renders as an explicit statement, not a blank.
    assert "not attempted" in first


def test_removed_key_renders_in_breakdown(
    committed: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A follow-up commit omitting a key lists it under 'Removed this run'."""
    run_id = _open(committed, capsys)["run_id"]
    payload = {"choices": [worked_choice()]}  # omits orchestrators-delegate-reads
    payload["choices"][0]["citations"] = []
    assert _commit(committed, monkeypatch, payload, run_id) == 0
    doc = committed["breakdowns"] / "coding-root" / f"{EXPECTED_SLUG}.md"
    text = doc.read_text(encoding="utf-8")
    assert "## Removed this run" in text
    assert "`orchestrators-delegate-reads` — not re-observed; marked `removed`" in text


# ---------------------------------------------------------------------------
# Slug collision — detected and diverted, never a silent cross-artifact overwrite
# ---------------------------------------------------------------------------

#: Two ordinary, distinct artifact paths whose §3.2 slugs collide ('/', ':', and
#: space all collapse to '--').
COLLIDE_A = "docs/release notes.md"
COLLIDE_B = "docs/release/notes.md"


def _commit_bare(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    artifact_path: str,
    choice_key: str,
) -> str:
    """Open + commit one citation-free choice for ``artifact_path``; returns stdout."""
    run_id = _open(ws, capsys, path=artifact_path)["run_id"]
    payload = {
        "choices": [
            {
                "choice_key": choice_key,
                "summary": f"The one choice recorded for {artifact_path}.",
                "quote": f"Span text for {artifact_path}.",
                "span_start_line": 1,
                "span_end_line": 1,
                "category": "context-economy",
                "votes": ["evidence-backed", "evidence-backed", "evidence-backed"],
                "literature_searched": False,
                "literature_found": False,
                "citations": [],
            }
        ]
    }
    assert _commit(ws, monkeypatch, payload, run_id) == 0
    return capsys.readouterr().out


def test_colliding_artifacts_keep_distinct_breakdowns_and_report_finds_each(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COLLIDE_A and COLLIDE_B slug identically; the second commit must NOT silently
    overwrite the first artifact's doc — it diverts to the hash-discriminated sibling
    with a loud note, and `cite report` names the right file for EACH artifact."""
    assert breakdown.artifact_slug(COLLIDE_A) == breakdown.artifact_slug(COLLIDE_B)
    register_artifact(ws, COLLIDE_A)
    register_artifact(ws, COLLIDE_B)

    out_a = _commit_bare(ws, capsys, monkeypatch, COLLIDE_A, "release-notes-choice")
    assert "COLLISION" not in out_a  # first writer takes the canonical path
    out_b = _commit_bare(ws, capsys, monkeypatch, COLLIDE_B, "release-dir-choice")
    assert "COLLISION" in out_b  # loud divert note on the colliding commit

    canonical = ws["breakdowns"] / "coding-root" / "docs--release--notes.md"
    assert canonical.is_file()
    canonical_text = canonical.read_text(encoding="utf-8")
    assert f"# Citation review — {COLLIDE_A}" in canonical_text  # A's doc survived

    # B's doc lives at the discriminated sibling, named in B's commit output.
    discriminated = [
        p
        for p in (ws["breakdowns"] / "coding-root").glob("docs--release--notes--*.md")
        if p != canonical
    ]
    assert len(discriminated) == 1
    disc_text = discriminated[0].read_text(encoding="utf-8")
    assert f"# Citation review — {COLLIDE_B}" in disc_text
    assert discriminated[0].name in out_b

    # cite report finds EACH artifact's real file.
    capsys.readouterr()
    assert (
        main(
            ["report", COLLIDE_A, "--db", str(ws["db"]), "--breakdowns-root", str(ws["breakdowns"])]
        )
        == 0
    )
    report_a = capsys.readouterr().out
    assert f"Breakdown: {canonical.as_posix()}" in report_a
    assert "file missing" not in report_a

    assert (
        main(
            ["report", COLLIDE_B, "--db", str(ws["db"]), "--breakdowns-root", str(ws["breakdowns"])]
        )
        == 0
    )
    report_b = capsys.readouterr().out
    assert f"Breakdown: {discriminated[0].as_posix()}" in report_b
    assert "file missing" not in report_b


def test_same_artifact_recommit_overwrites_its_own_doc_without_collision(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-reviewing the SAME artifact is the normal overwrite — no divert, no note."""
    register_artifact(ws, COLLIDE_A)
    _commit_bare(ws, capsys, monkeypatch, COLLIDE_A, "release-notes-choice")
    out = _commit_bare(ws, capsys, monkeypatch, COLLIDE_A, "release-notes-choice")
    assert "COLLISION" not in out
    files = list((ws["breakdowns"] / "coding-root").glob("docs--release--notes*.md"))
    assert [p.name for p in files] == ["docs--release--notes.md"]


def test_write_breakdown_diverts_on_recorded_path_mismatch(tmp_path: Path) -> None:
    """Unit-level: write_breakdown detects a foreign recorded artifact path at the
    canonical destination and diverts with a note; locate_breakdown then resolves
    each artifact to its own file."""
    first = breakdown.write_breakdown(_mini_result("docs/release notes.md"), tmp_path)
    assert first.collision_note is None
    second = breakdown.write_breakdown(_mini_result("docs/release/notes.md"), tmp_path)
    assert second.collision_note is not None
    assert "COLLISION" in second.collision_note
    assert first.path != second.path
    assert breakdown.locate_breakdown(tmp_path, "coding-root", "docs/release notes.md") == (
        first.path
    )
    assert breakdown.locate_breakdown(tmp_path, "coding-root", "docs/release/notes.md") == (
        second.path
    )


def test_write_breakdown_diverts_on_unreadable_header(tmp_path: Path) -> None:
    """A canonical destination whose header line is INVALID UTF-8 (interrupted write
    truncating a multi-byte char, non-UTF-8 hand-edit of the human-readable doc) must
    never crash with UnicodeDecodeError NOR be silently overwritten: ownership is
    unknown, so the write diverts to the discriminated sibling with a loud note, the
    unreadable file is left untouched, and locate_breakdown resolves the artifact to
    the diverted doc."""
    canonical = breakdown.breakdown_path(tmp_path, "coding-root", "CLAUDE.md")
    canonical.parent.mkdir(parents=True, exist_ok=True)
    corrupt = "# Citation review — ".encode() + b"\xff\xfe truncated header\n"
    canonical.write_bytes(corrupt)

    written = breakdown.write_breakdown(_mini_result("CLAUDE.md"), tmp_path)

    assert written.collision_note is not None
    assert "COLLISION" in written.collision_note
    assert "could not be read as UTF-8" in written.collision_note
    assert written.path != canonical
    assert canonical.read_bytes() == corrupt  # unreadable file left untouched
    assert "# Citation review — CLAUDE.md" in written.path.read_text(encoding="utf-8")
    assert breakdown.locate_breakdown(tmp_path, "coding-root", "CLAUDE.md") == written.path


# ---------------------------------------------------------------------------
# cite report — round-trip + clean errors (through cli.main)
# ---------------------------------------------------------------------------


def test_report_round_trip(committed: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            [
                "report",
                RULE_PATH,
                "--db",
                str(committed["db"]),
                "--breakdowns-root",
                str(committed["breakdowns"]),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    expected_doc = committed["breakdowns"] / "coding-root" / f"{EXPECTED_SLUG}.md"
    assert f"Breakdown: {expected_doc.as_posix()}" in out
    assert "file missing" not in out
    assert f"Artifact: {RULE_PATH} (rule, project coding-root)" in out
    assert "Composite: 62.5 / 100 — adequate (interpretation guide v1)" in out
    assert "Choices scored: 2 (well-supported 1, needs-improvement 1, interesting 0)" in out


def test_report_errors_cleanly_without_any_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "data" / "citation.db"
    assert main(["init-db", "--db", str(db_path)]) == 0
    capsys.readouterr()
    code = main(["report", "docs/unreviewed.md", "--db", str(db_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert out.startswith("error:")
    assert "no review exists" in out


def test_report_errors_cleanly_with_open_but_uncommitted_run(
    committed: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """An artifact scanned + opened but never committed still reports 'no review'."""
    conn = db.connect(committed["db"])
    try:
        conn.execute(
            "INSERT INTO artifacts (path, artifact_type, project, current_content_hash, "
            "first_seen_at) VALUES ('docs/other.md', 'plan', 'coding-root', 'cafe', "
            "'2026-07-21T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()
    capsys.readouterr()
    code = main(["report", "docs/other.md", "--db", str(committed["db"])])
    out = capsys.readouterr().out
    assert code == 1
    assert "no committed review run" in out


def test_report_missing_db_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["report", RULE_PATH, "--db", str(tmp_path / "nope.db")]) == 1
    assert "does not exist" in capsys.readouterr().out


def test_report_survives_corrupt_breakdown_header(
    committed: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`cite report` (through the production cli.main) must not traceback when the
    breakdown doc's header bytes are invalid UTF-8: the unreadable canonical file
    cannot be confirmed as this artifact's, so report degrades to the discriminated
    sibling candidate with the established 'file missing' suffix — clean output,
    exit 0, DB-backed report content intact."""
    doc = committed["breakdowns"] / "coding-root" / f"{EXPECTED_SLUG}.md"
    assert doc.is_file()
    doc.write_bytes(b"# Citation review \xff\xfe interrupted-write garbage\n")
    capsys.readouterr()
    code = main(
        [
            "report",
            RULE_PATH,
            "--db",
            str(committed["db"]),
            "--breakdowns-root",
            str(committed["breakdowns"]),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert f"{EXPECTED_SLUG}--" in out  # names the discriminated sibling candidate
    assert "file missing" in out  # sibling not written yet; re-review regenerates
    assert "Composite: 62.5 / 100" in out  # DB-backed content still renders
    assert "Traceback" not in out
