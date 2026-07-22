"""review.py — lifecycle, scoring math, contract sync, worked-example acceptance.

Covers the Step 4 acceptance targets through the PRODUCTION CLI entry (``cli.main``,
stdin via a bytes-backed stream — the production ``sys.stdin.buffer`` shape):

- the plan §12 Appendix A worked example for ``.claude/rules/subagent-economy.md``
  reproduces the schema-draft §8 row-set (load-bearing fields, amended to §4.4 vote
  shares) — one artifact, one run, one choice, one external api_structured + one
  internal internal-read citation, both links, one scores row, zero distill rows;
- D4: a second commit with the same choice REWORDED reuses the choice_key — zero
  duplicate choices;
- removed-marking: a third commit omitting the key flips it to ``status='removed'``
  with its citations still linked — scoped to that artifact only;
- anti-fabrication at the commit seam: api_structured echoes are captured by the
  CLI's OWN server-side structured-API lookup (title-matched; payload echoes are
  rejected outright), web_fetch_verified re-fetches server-side, internal-read paths
  are confined to the workspace/memory root (traversal refuses the whole payload);
- stdin is decoded as explicit UTF-8 bytes (BOM tolerated; mojibake impossible) —
  including a REAL subprocess pipe round-trip;
- vote-share math incl. ``parse-failed`` force-scored contradicted; tie rejection;
  composite/band edge values exact THROUGH composite_from_labels (70 / 40 / 20);
- contract sync: the docs/contracts/*.schema.json files match the pydantic mirrors
  field-for-field, and docs/interpretation-guide.md mentions the four labels + the
  band cutpoints (the code/guide sync the review.py docstring promises).

Shared scaffolding (mini workspace, worked payload, open/commit helpers, the offline
structured-API mock) lives in tests/conftest.py — one source of truth.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from citation_needed import db, resolve, review, verify
from citation_needed.cli import main
from conftest import (
    _BULLET_1,
    _BULLET_2,
    ARXIV_URL,
    LEVER_DOC,
    RULE_PATH,
    RULE_QUOTE,
    S2_WORKED_ECHO,
    SEARCH_QUERY,
    WORKED_TITLE,
    _commit,
    _connect,
    _count,
    _one,
    _open,
    _stdin_bytes,
    register_artifact,
    worked_choice,
    worked_payload,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = PROJECT_ROOT / "docs" / "contracts"


# ---------------------------------------------------------------------------
# review open — frozen provenance + prior pairs (through cli.main)
# ---------------------------------------------------------------------------


def test_review_open_emits_contract_json(
    ws: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    data = _open(ws, capsys)
    opened = review.OpenOutput.model_validate(data)  # the review-open.schema.json mirror
    assert opened.run_id == 1
    assert opened.reviewer_model == "claude-sonnet-5"
    assert opened.artifact.path == RULE_PATH
    assert opened.artifact.artifact_type == "rule"
    assert opened.artifact.project == "coding-root"
    assert opened.artifact.git_sha is None  # the fixture workspace is not a git repo
    assert opened.artifact.tool_schema_version == 2
    assert opened.prior_choices == []

    conn = _connect(ws)
    try:
        stored_hash = _one(
            conn, "SELECT current_content_hash FROM artifacts WHERE path = ?", (RULE_PATH,)
        )[0]
        run = _one(
            conn,
            "SELECT artifact_content_hash_at_review, artifact_git_sha_at_review, "
            "reviewer_model, tool_schema_version, finished_at, composite "
            "FROM review_runs WHERE id = ?",
            (opened.run_id,),
        )
    finally:
        conn.close()
    assert opened.artifact.content_hash == stored_hash
    assert run[0] == stored_hash  # frozen FROM the stored hash
    assert run[1] is None
    assert run[2] == "claude-sonnet-5"
    assert int(run[3]) == 2
    assert run[4] is None and run[5] is None  # not committed yet


def test_review_open_unregistered_artifact_errors(
    ws: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "review",
            "open",
            "docs/never-scanned.md",
            "--db",
            str(ws["db"]),
            "--workspace-root",
            str(ws["root"]),
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert out.startswith("error:")
    assert "not registered" in out


def test_review_open_missing_db_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["review", "open", RULE_PATH, "--db", str(tmp_path / "nope.db")]) == 1
    assert "does not exist" in capsys.readouterr().out


def test_review_open_returns_prior_pairs_after_commit(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _open(ws, capsys)["run_id"]
    assert _commit(ws, monkeypatch, worked_payload(), run_id) == 0
    capsys.readouterr()
    data = _open(ws, capsys)
    opened = review.OpenOutput.model_validate(data)
    assert [(p.choice_key, p.status) for p in opened.prior_choices] == [
        ("subagent-terse-verdict-file-detail", "active")
    ]


# ---------------------------------------------------------------------------
# The worked example (plan §12.A) — reproduces schema-draft §8's row-set shape
# ---------------------------------------------------------------------------


def test_worked_example_commit_reproduces_appendix_rowset(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_api_lookups: dict[str, list[str]],
) -> None:
    run_id = _open(ws, capsys)["run_id"]
    assert _commit(ws, monkeypatch, worked_payload(), run_id) == 0
    out = capsys.readouterr().out
    assert f"Committed review run #{run_id} for {RULE_PATH}" in out
    assert "Composite: 100.0 / 100 — strong" in out
    # The api_structured echo came from OUR server-side lookup (the payload has none).
    assert mock_api_lookups["s2"] == ["ARXIV:2307.03172"]

    conn = _connect(ws)
    try:
        # artifacts: refreshed "current" belief
        artifact = _one(
            conn,
            "SELECT id, artifact_type, project, last_reviewed_at, current_content_hash "
            "FROM artifacts WHERE path = ?",
            (RULE_PATH,),
        )
        assert artifact is not None
        assert artifact[1] == "rule" and artifact[2] == "coding-root"
        assert artifact[3] is not None

        # review_runs: frozen provenance + the committed composite
        run = _one(
            conn,
            "SELECT artifact_content_hash_at_review, reviewer_model, status, finished_at, "
            "composite, composite_band, interpretation_guide_version "
            "FROM review_runs WHERE id = ?",
            (run_id,),
        )
        assert run[0] == artifact[4]
        assert run[1] == "claude-sonnet-5"
        assert run[2] == "completed" and run[3] is not None
        assert float(run[4]) == 100.0  # all votes evidence-backed -> (1+1)/2*100
        assert run[5] == "strong"
        assert run[6] == "v1"

        # choices: exactly one durable row
        assert _count(conn, "choices") == 1
        choice = _one(
            conn,
            "SELECT id, choice_key, summary, span_start_line, span_end_line, status, "
            "content_hash_at_extraction, first_extracted_review_run_id, "
            "last_confirmed_review_run_id, source_path FROM choices",
        )
        assert choice[1] == "subagent-terse-verdict-file-detail"
        assert "terse verdict" in str(choice[2])
        assert (choice[3], choice[4]) == (9, 10)
        assert choice[5] == "active"
        # Independent expected value (hashlib inline, NOT review._content_hash — the
        # assertion must be able to catch a bug inside _content_hash itself).
        assert str(choice[6]) == hashlib.sha256(RULE_QUOTE.encode("utf-8")).hexdigest()
        assert choice[7] == run_id and choice[8] == run_id
        assert choice[9] is None

        # citations: 1 external api_structured + 1 internal internal-read
        assert _count(conn, "citations") == 2
        external = _one(
            conn,
            "SELECT natural_key, title, url_or_doi, resolution_method, supporting_quote, "
            "year FROM citations WHERE kind = 'external'",
        )
        assert external[0] == ARXIV_URL  # normalized URL natural key
        assert external[1] == WORKED_TITLE
        assert external[2] == ARXIV_URL
        assert external[3] == "api_structured"
        # The stored echo is the API's own JSON, captured by OUR lookup at commit time.
        assert json.loads(str(external[4])) == S2_WORKED_ECHO
        assert int(external[5]) == 2023
        internal = _one(
            conn,
            "SELECT natural_key, workspace_path, resolution_method, supporting_quote, "
            "source_line_ref FROM citations WHERE kind = 'internal'",
        )
        assert internal[0] == LEVER_DOC and internal[1] == LEVER_DOC
        assert internal[2] == "internal-read"
        assert internal[3] == worked_choice()["citations"][1]["quote"]
        assert internal[4] == f"{LEVER_DOC}:5"

        # choice_citations: both links, both supports, run-threaded
        links = conn.execute(
            "SELECT citation_id, support_direction, relevance_note, "
            "first_linked_review_run_id, last_confirmed_review_run_id "
            "FROM choice_citations WHERE choice_id = ? ORDER BY citation_id",
            (int(choice[0]),),
        ).fetchall()
        assert len(links) == 2
        for link in links:
            assert link[1] == "supports"
            assert str(link[2]).strip()
            assert link[3] == run_id and link[4] == run_id

        # scores: the four vote-share columns per §4.4 (amendment 1)
        score = _one(
            conn,
            "SELECT evidence_backed_share, interesting_novel_share, unsupported_share, "
            "contradicted_share, classification, composite, composite_band, "
            "interpretation_guide_version, literature_searched, literature_found, "
            "search_queries FROM scores WHERE review_run_id = ?",
            (run_id,),
        )
        assert (
            float(score[0]),
            float(score[1]),
            float(score[2]),
            float(score[3]),
        ) == (1.0, 0.0, 0.0, 0.0)
        assert score[4] == "well-supported"
        assert float(score[5]) == 100.0 and score[6] == "strong" and score[7] == "v1"
        assert (int(score[8]), int(score[9])) == (1, 1)
        assert json.loads(str(score[10])) == [SEARCH_QUERY]

        # distill_queue: NOTHING writes this table in Step 4 (Step 6 owns proposals),
        # so this is a premature-write guard for the whole review pipeline — it is NOT
        # evidence that a well-supported classification suppresses a proposal row
        # (that discriminating behavior lands with Step 6 and needs its own test).
        assert _count(conn, "distill_queue") == 0
    finally:
        conn.close()


def test_d4_reworded_recommit_reuses_choice_key_zero_duplicates(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_1 = _open(ws, capsys)["run_id"]
    assert _commit(ws, monkeypatch, worked_payload(), run_1) == 0

    reworded_quote = f"{_BULLET_1}\n{_BULLET_2}\n(reworded extraction of the same span)"
    reworded = {
        "choices": [
            worked_choice(
                summary="Subagent must return a terse verdict and park detail in a file "
                "read only on failure.",
                quote=reworded_quote,
            )
        ]
    }
    # Citations by natural_key this time — existing-corpus links, no new rows.
    reworded["choices"][0]["citations"] = [
        {
            "kind": "external",
            "natural_key": ARXIV_URL,
            "relevance_note": "Still supports: long-context degradation.",
            "support_direction": "supports",
        },
        {
            "kind": "internal",
            "natural_key": LEVER_DOC,
            "relevance_note": "Still supports: the measured leak.",
            "support_direction": "supports",
        },
    ]
    capsys.readouterr()
    run_2 = _open(ws, capsys)["run_id"]
    assert run_2 != run_1
    assert _commit(ws, monkeypatch, reworded, run_2) == 0

    conn = _connect(ws)
    try:
        assert _count(conn, "choices") == 1  # D4: zero duplicates
        choice = _one(
            conn,
            "SELECT summary, status, first_extracted_review_run_id, "
            "last_confirmed_review_run_id, content_hash_at_extraction FROM choices",
        )
        assert "park detail" in str(choice[0])  # summary updated in place
        assert choice[1] == "active"
        assert choice[2] == run_1  # first extraction preserved
        assert choice[3] == run_2  # confirmed by the re-review
        # Hash refreshed to the REWORDED quote's sha256 (independent hashlib expected).
        assert str(choice[4]) == hashlib.sha256(reworded_quote.encode("utf-8")).hexdigest()
        assert _count(conn, "citations") == 2  # links reused, no new corpus rows
        assert _count(conn, "choice_citations") == 2
        assert _count(conn, "scores") == 2  # one per (run, choice)
    finally:
        conn.close()


def test_removed_marking_keeps_citations_linked(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_1 = _open(ws, capsys)["run_id"]
    assert _commit(ws, monkeypatch, worked_payload(), run_1) == 0

    replacement = {
        "choices": [
            {
                "choice_key": "orchestrators-delegate-reads",
                "summary": "Orchestrators delegate file reads to sub-agents and keep "
                "conclusions, not dumps.",
                "quote": "Rule 2 — Orchestrators delegate reads; they hold conclusions.",
                "span_start_line": 12,
                "span_end_line": 12,
                "category": "context-economy",
                "votes": ["unsupported", "unsupported", "evidence-backed"],
                "literature_searched": True,
                "literature_found": False,
                "search_queries": ["orchestrator context delegation empirical"],
                "citations": [],
                "suggestions": ["Back the delegation claim with a measured incident."],
            }
        ]
    }
    capsys.readouterr()
    run_2 = _open(ws, capsys)["run_id"]
    assert _commit(ws, monkeypatch, replacement, run_2) == 0
    out = capsys.readouterr().out
    assert "1 removed" in out

    conn = _connect(ws)
    try:
        removed = _one(
            conn,
            "SELECT id, status, superseded_at FROM choices WHERE choice_key = ?",
            ("subagent-terse-verdict-file-detail",),
        )
        assert removed[1] == "removed"  # never deleted
        assert removed[2] is not None
        # Its citations remain linked corpus assets.
        still_linked = _one(
            conn,
            "SELECT COUNT(*) FROM choice_citations WHERE choice_id = ?",
            (int(removed[0]),),
        )
        assert int(still_linked[0]) == 2
        assert _count(conn, "citations") == 2
        new_choice = _one(
            conn,
            "SELECT status FROM choices WHERE choice_key = ?",
            ("orchestrators-delegate-reads",),
        )
        assert new_choice[0] == "active"
        # Run-2 composite covers only run-2's choices: majority unsupported -> 25.0/weak.
        run = _one(
            conn,
            "SELECT composite, composite_band FROM review_runs WHERE id = ?",
            (run_2,),
        )
        assert float(run[0]) == 25.0 and run[1] == "weak"
    finally:
        conn.close()


def _bare_choice(choice_key: str, summary: str = "A standalone claim.") -> dict[str, Any]:
    """A minimal citation-free choice for scoping/plumbing tests."""
    return {
        "choice_key": choice_key,
        "summary": summary,
        "quote": f"The literal span backing {choice_key}.",
        "span_start_line": 1,
        "span_end_line": 1,
        "category": "context-economy",
        "votes": ["evidence-backed", "evidence-backed", "evidence-backed"],
        "literature_searched": False,
        "literature_found": False,
        "citations": [],
    }


def test_removed_marking_scoped_to_artifact(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two artifacts share a choice_key (legal — uniqueness is (artifact_id,
    choice_key)); re-reviewing ONE artifact without that key marks only ITS row
    removed. A regression that widened the removal query past the run's artifact_id
    would flip the second artifact's row too and fail here."""
    register_artifact(ws, "docs/second.md")
    run_a = _open(ws, capsys)["run_id"]
    assert _commit(ws, monkeypatch, {"choices": [_bare_choice("shared-key")]}, run_a) == 0
    capsys.readouterr()
    run_b = _open(ws, capsys, path="docs/second.md")["run_id"]
    assert _commit(ws, monkeypatch, {"choices": [_bare_choice("shared-key")]}, run_b) == 0
    capsys.readouterr()
    # Re-review the FIRST artifact, omitting shared-key.
    run_c = _open(ws, capsys)["run_id"]
    assert _commit(ws, monkeypatch, {"choices": [_bare_choice("a-new-key")]}, run_c) == 0

    conn = _connect(ws)
    try:
        rows = conn.execute(
            "SELECT a.path, c.status FROM choices c JOIN artifacts a ON a.id = c.artifact_id "
            "WHERE c.choice_key = 'shared-key' ORDER BY a.path",
        ).fetchall()
    finally:
        conn.close()
    assert [(str(r[0]), str(r[1])) for r in rows] == [
        (RULE_PATH, "removed"),  # the re-reviewed artifact's row
        ("docs/second.md", "active"),  # the OTHER artifact's row is untouched
    ]


# ---------------------------------------------------------------------------
# Failure modes through the CLI — loud, and NOTHING written (one transaction)
# ---------------------------------------------------------------------------


def _assert_nothing_committed(ws: dict[str, Path], run_id: int) -> None:
    conn = _connect(ws)
    try:
        assert _count(conn, "choices") == 0
        assert _count(conn, "scores") == 0
        assert _count(conn, "citations") == 0
        assert _count(conn, "choice_citations") == 0
        run = _one(conn, "SELECT finished_at, composite FROM review_runs WHERE id = ?", (run_id,))
        assert run[0] is None and run[1] is None
    finally:
        conn.close()


def test_commit_tie_rejected_loudly_nothing_written(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    payload["choices"][0]["votes"] = [
        "evidence-backed",
        "evidence-backed",
        "contradicted",
        "contradicted",
    ]
    assert _commit(ws, monkeypatch, payload, run_id) == 1
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "tie" in out and "escalated" in out
    _assert_nothing_committed(ws, run_id)


def test_commit_fabricated_internal_quote_rejected_nothing_written(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    payload["choices"][0]["citations"][1]["quote"] = "a quote that is NOT in the lever doc"
    assert _commit(ws, monkeypatch, payload, run_id) == 1
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "not found" in out
    _assert_nothing_committed(ws, run_id)


def test_commit_rejects_caller_supplied_fetched_text(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract deliberately has no field for fetched page text — extra='forbid'
    (additionalProperties: false) rejects an 'I already fetched it' claim outright."""
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    payload["choices"][0]["citations"][0] = {
        "kind": "external",
        "resolution_method": "web_fetch_verified",
        "title": "Some page",
        "url": "https://example.com/post",
        "quote": "a quote",
        "fetched_text": "attacker-supplied page text containing a quote",
        "relevance_note": "n/a",
        "support_direction": "supports",
    }
    assert _commit(ws, monkeypatch, payload, run_id) == 1
    out = capsys.readouterr().out
    assert "review-commit.schema.json" in out
    assert "fetched_text" in out
    _assert_nothing_committed(ws, run_id)


def test_commit_rejects_caller_supplied_api_echo(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract deliberately has NO api_echo field — the echo is captured
    server-side by commit's own structured-API lookup, so a payload claiming 'here is
    the API's response' is rejected outright (the fabrication hole this closes)."""
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    payload["choices"][0]["citations"][0]["api_echo"] = {
        "paperId": "fake",
        "title": "totally made up",
        "year": 1999,
    }
    assert _commit(ws, monkeypatch, payload, run_id) == 1
    out = capsys.readouterr().out
    assert "review-commit.schema.json" in out
    assert "api_echo" in out
    _assert_nothing_committed(ws, run_id)


# ---------------------------------------------------------------------------
# api_structured — SERVER-SIDE lookup, title match, loud failure (fix for the
# fabrication hole: the payload supplies only a locator + claimed title)
# ---------------------------------------------------------------------------


def test_commit_api_structured_doi_locator_uses_crossref(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_api_lookups: dict[str, list[str]],
) -> None:
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    entry = payload["choices"][0]["citations"][0]
    entry.pop("url")
    entry["doi"] = "https://doi.org/10.48550/arXiv.2307.03172"
    assert _commit(ws, monkeypatch, payload, run_id) == 0
    assert mock_api_lookups["crossref"] == ["https://doi.org/10.48550/arXiv.2307.03172"]
    assert mock_api_lookups["s2"] == []  # DOI routes to Crossref, not S2
    conn = _connect(ws)
    try:
        row = _one(
            conn,
            "SELECT natural_key, url_or_doi, supporting_quote FROM citations "
            "WHERE kind = 'external'",
        )
    finally:
        conn.close()
    assert row[0] == "10.48550/arxiv.2307.03172"  # normalized DOI natural key
    assert row[1] == "https://doi.org/10.48550/arxiv.2307.03172"
    assert json.loads(str(row[2]))["title"] == [WORKED_TITLE]  # the Crossref echo


def test_commit_api_structured_title_mismatch_rejects_whole_payload(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    payload["choices"][0]["citations"][0]["title"] = "A Fabricated Title The API Never Returned"
    assert _commit(ws, monkeypatch, payload, run_id) == 1
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "title mismatch" in out
    _assert_nothing_committed(ws, run_id)


def test_commit_api_structured_title_match_is_normalized(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Casefold + whitespace-collapse — a case/spacing variant of the true title is
    the same title, not a mismatch."""
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    payload["choices"][0]["citations"][0]["title"] = (
        "  lost IN the   middle: how language\nmodels use long contexts "
    )
    assert _commit(ws, monkeypatch, payload, run_id) == 0


def test_commit_api_structured_unreachable_api_rejects_loudly(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable structured API is a LOUD whole-payload reject — never a silent
    insert of an unverified citation."""

    def boom(paper_id: str, **_kwargs: Any) -> dict[str, Any]:
        raise resolve.ResolutionError("connection refused")

    monkeypatch.setattr(resolve, "lookup_semantic_scholar_id", boom)
    run_id = _open(ws, capsys)["run_id"]
    assert _commit(ws, monkeypatch, worked_payload(), run_id) == 1
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "lookup failed" in out
    _assert_nothing_committed(ws, run_id)


def test_commit_api_structured_unresolvable_locator_rejected(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    payload["choices"][0]["citations"][0]["url"] = "https://example.com/some-blog-post"
    assert _commit(ws, monkeypatch, payload, run_id) == 1
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "not resolvable server-side" in out
    _assert_nothing_committed(ws, run_id)


def test_commit_web_fetch_verified_refetches_server_side(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched_urls: list[str] = []

    def fake_fetch(url: str, *args: Any, **kwargs: Any) -> verify.FetchResult:
        fetched_urls.append(url)
        return verify.FetchResult(
            final_url=url,
            fetched_text="blog post body: keep the orchestrator window slim, always.",
            fetch_time="2026-07-21T00:00:00Z",
            hops=0,
        )

    monkeypatch.setattr(verify, "fetch_url", fake_fetch)
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    payload["choices"][0]["citations"] = [
        {
            "kind": "external",
            "resolution_method": "web_fetch_verified",
            "title": "Keep the orchestrator window slim",
            "url": "https://example.com/window-slim",
            "quote": "keep the orchestrator window slim",
            "relevance_note": "Grey-literature support.",
            "support_direction": "supports",
        }
    ]
    assert _commit(ws, monkeypatch, payload, run_id) == 0
    assert fetched_urls == ["https://example.com/window-slim"]  # OUR fetch, not the caller's

    conn = _connect(ws)
    try:
        row = _one(
            conn,
            "SELECT resolution_method, supporting_quote FROM citations WHERE kind = 'external'",
        )
        assert row[0] == "web_fetch_verified"
        assert row[1] == "keep the orchestrator window slim"
    finally:
        conn.close()


def test_commit_web_fetch_quote_mismatch_rejects_whole_payload(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch(url: str, *args: Any, **kwargs: Any) -> verify.FetchResult:
        return verify.FetchResult(
            final_url=url,
            fetched_text="page text that does not contain the claimed words",
            fetch_time="2026-07-21T00:00:00Z",
            hops=0,
        )

    monkeypatch.setattr(verify, "fetch_url", fake_fetch)
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    payload["choices"][0]["citations"] = [
        {
            "kind": "external",
            "resolution_method": "web_fetch_verified",
            "title": "Some page",
            "url": "https://example.com/post",
            "quote": "a fabricated supporting quote",
            "relevance_note": "n/a",
            "support_direction": "supports",
        }
    ]
    assert _commit(ws, monkeypatch, payload, run_id) == 1
    assert "quote not found" in capsys.readouterr().out
    _assert_nothing_committed(ws, run_id)


# ---------------------------------------------------------------------------
# internal-read path confinement — traversal refuses the whole payload
# ---------------------------------------------------------------------------


def _internal_entry(workspace_path: str, quote: str) -> dict[str, Any]:
    return {
        "kind": "internal",
        "resolution_method": "internal-read",
        "title": "Claimed internal provenance",
        "workspace_path": workspace_path,
        "quote": quote,
        "relevance_note": "n/a",
        "support_direction": "supports",
    }


@pytest.mark.parametrize(
    "escape_path",
    [
        "../outside-secrets.env",  # relative traversal out of workspace_root
        "sub/../../outside-secrets.env",  # interior traversal
        "memory:../outside-secrets.env",  # memory-scheme slug traversal
        "memory:proj/../../../outside-secrets.env",  # memory-scheme file traversal
    ],
)
def test_commit_internal_read_escape_refused_nothing_written(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    escape_path: str,
) -> None:
    """A '../' escape (workspace or memory scheme) is refused BY THE CONFINEMENT CHECK
    even though the target file exists and contains the quote — internal citations are
    workspace/memory provenance only, never an arbitrary-file read oracle."""
    outside = ws["root"].parent / "outside-secrets.env"
    outside.write_text("API_TOKEN=super-secret-value\n", encoding="utf-8")
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    payload["choices"][0]["citations"] = [
        _internal_entry(escape_path, "API_TOKEN=super-secret-value")
    ]
    assert _commit(ws, monkeypatch, payload, run_id) == 1
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "escapes" in out or "memory locator" in out
    _assert_nothing_committed(ws, run_id)


def test_commit_internal_read_absolute_path_refused(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absolute workspace_path RESETS a pathlib join (Path(root) / 'C:/x' discards
    root) — the resolve()+is_relative_to confinement refuses it outright."""
    outside = ws["root"].parent / "outside-secrets.env"
    outside.write_text("API_TOKEN=super-secret-value\n", encoding="utf-8")
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    payload["choices"][0]["citations"] = [
        _internal_entry(str(outside), "API_TOKEN=super-secret-value")
    ]
    assert _commit(ws, monkeypatch, payload, run_id) == 1
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "escapes" in out
    _assert_nothing_committed(ws, run_id)


def test_commit_internal_read_memory_scheme_confined_read_works(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legitimate memory: locator maps to <memory_root>/<slug>/memory/<file> and
    verifies against the REAL file — confinement blocks escapes, not the feature."""
    note = ws["memory"] / "c--users-x-dev" / "memory" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("A memory-recorded incident backs this rule.\n", encoding="utf-8")
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    payload["choices"][0]["citations"] = [
        _internal_entry(
            "memory:c--users-x-dev/note.md", "A memory-recorded incident backs this rule."
        )
    ]
    assert _commit(ws, monkeypatch, payload, run_id) == 0
    conn = _connect(ws)
    try:
        row = _one(
            conn,
            "SELECT natural_key, resolution_method FROM citations WHERE kind = 'internal'",
        )
    finally:
        conn.close()
    assert row[0] == "memory:c--users-x-dev/note.md"
    assert row[1] == "internal-read"


# ---------------------------------------------------------------------------
# stdin decode — explicit UTF-8 bytes (the real-subprocess mojibake repro)
# ---------------------------------------------------------------------------

_EM_DASH_SUMMARY = "Choices carry an em dash — and a non-Latin word: 日本語."


def test_commit_stdin_utf8_roundtrip_through_real_subprocess(
    ws: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """REAL subprocess pipe (bytes on OS-level stdin, not io.StringIO): a UTF-8
    payload containing an em dash + a non-Latin word must round-trip byte-identically
    into the DB regardless of the console codepage (the empirically-reproduced
    mojibake was 'â€”' stored with exit 0 under cp1252/437 defaults)."""
    run_id = _open(ws, capsys)["run_id"]
    choice = _bare_choice("em-dash-choice", summary=_EM_DASH_SUMMARY)
    choice["quote"] = "Span — 日本語 span."
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "citation_needed.cli",
            "review",
            "commit",
            "--run",
            str(run_id),
            "--db",
            str(ws["db"]),
            "--workspace-root",
            str(ws["root"]),
            "--memory-root",
            str(ws["memory"]),
            "--breakdowns-root",
            str(ws["breakdowns"]),
        ],
        input=json.dumps({"choices": [choice]}, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout.decode(errors="replace") + proc.stderr.decode(
        errors="replace"
    )
    conn = _connect(ws)
    try:
        row = _one(conn, "SELECT summary, quote_or_span FROM choices")
    finally:
        conn.close()
    assert row[0] == _EM_DASH_SUMMARY  # byte-identical round trip — no mojibake
    assert row[1] == "Span — 日本語 span."


def test_commit_stdin_non_utf8_bytes_error_cleanly(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cp1252-encoded bytes (an em dash as 0x97) are NOT UTF-8 — the commit refuses
    loudly instead of mojibaking into the DB."""
    raw = '{"choices": [{"summary": "em dash — here"}]}'.encode("cp1252")
    _stdin_bytes(monkeypatch, raw)
    assert main(["review", "commit", "--run", "1", "--db", str(ws["db"])]) == 1
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "stdin must be UTF-8" in out


def test_commit_stdin_utf8_bom_tolerated(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A UTF-8 BOM (PowerShell '>' / Out-File emit one) is stripped, not a JSON error."""
    run_id = _open(ws, capsys)["run_id"]
    payload = {"choices": [_bare_choice("bom-choice")]}
    raw = b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8")
    _stdin_bytes(monkeypatch, raw)
    assert (
        main(
            [
                "review",
                "commit",
                "--run",
                str(run_id),
                "--db",
                str(ws["db"]),
                "--workspace-root",
                str(ws["root"]),
                "--breakdowns-root",
                str(ws["breakdowns"]),
            ]
        )
        == 0
    )


def test_commit_deeply_nested_stdin_errors_cleanly(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5000-deep nested array overflows json.loads' recursion — the CLI must exit
    through the clean error contract, never a raw RecursionError traceback."""
    _stdin_bytes(monkeypatch, b"[" * 5000 + b"]" * 5000)
    assert main(["review", "commit", "--run", "1", "--db", str(ws["db"])]) == 1
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "not valid JSON" in out


def test_commit_run_bookkeeping_errors(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _open(ws, capsys)["run_id"]

    # Unknown run id.
    assert _commit(ws, monkeypatch, worked_payload(), 999) == 1
    assert "does not exist" in capsys.readouterr().out

    # --run vs payload run_id conflict.
    payload = worked_payload()
    payload["run_id"] = run_id + 41
    assert _commit(ws, monkeypatch, payload, run_id) == 1
    assert "conflicts" in capsys.readouterr().out

    # No run id at all.
    assert _commit(ws, monkeypatch, worked_payload(), None) == 1
    assert "no run id" in capsys.readouterr().out

    # Double commit.
    assert _commit(ws, monkeypatch, worked_payload(), run_id) == 0
    capsys.readouterr()
    assert _commit(ws, monkeypatch, worked_payload(), run_id) == 1
    assert "already committed" in capsys.readouterr().out


def test_commit_bad_stdin_errors(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stdin_bytes(monkeypatch, b"")
    assert main(["review", "commit", "--run", "1", "--db", str(ws["db"])]) == 1
    assert "empty stdin" in capsys.readouterr().out

    _stdin_bytes(monkeypatch, b"{not json")
    assert main(["review", "commit", "--run", "1", "--db", str(ws["db"])]) == 1
    assert "not valid JSON" in capsys.readouterr().out


def test_commit_unknown_existing_citation_link_errors(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    payload["choices"][0]["citations"] = [
        {
            "kind": "external",
            "natural_key": "https://nowhere.example/none",
            "relevance_note": "n/a",
            "support_direction": "supports",
        }
    ]
    assert _commit(ws, monkeypatch, payload, run_id) == 1
    assert "no existing external citation" in capsys.readouterr().out
    _assert_nothing_committed(ws, run_id)


# ---------------------------------------------------------------------------
# Scoring math — the single implementation (pure-function tests)
# ---------------------------------------------------------------------------


def test_label_weights_and_classification_map() -> None:
    assert review.LABEL_WEIGHTS == {
        "evidence-backed": 1.0,
        "interesting-novel": 0.5,
        "unsupported": -0.5,
        "contradicted": -1.0,
    }
    assert review.CLASSIFICATION_BY_LABEL == {
        "evidence-backed": "well-supported",
        "interesting-novel": "interesting",
        "unsupported": "needs-improvement",
        "contradicted": "needs-improvement",
    }


def test_tally_votes_shares_and_majority() -> None:
    tally = review.tally_votes(
        ["evidence-backed", "evidence-backed", "unsupported", "interesting-novel"]
    )
    assert tally.evidence_backed_share == 0.5
    assert tally.unsupported_share == 0.25
    assert tally.interesting_novel_share == 0.25
    assert tally.contradicted_share == 0.0
    assert tally.majority_label == "evidence-backed"
    assert tally.classification == "well-supported"


def test_tally_votes_parse_failed_forced_to_contradicted_in_denominator() -> None:
    tally = review.tally_votes(["evidence-backed", "parse-failed", "evidence-backed"])
    assert tally.contradicted_share == pytest.approx(1 / 3)
    assert tally.evidence_backed_share == pytest.approx(2 / 3)
    assert tally.majority_label == "evidence-backed"

    # Enough parse-fails flip the majority itself.
    flipped = review.tally_votes(["parse-failed", "parse-failed", "unsupported"])
    assert flipped.contradicted_share == pytest.approx(2 / 3)
    assert flipped.majority_label == "contradicted"
    assert flipped.classification == "needs-improvement"


def test_tally_votes_tie_raises() -> None:
    with pytest.raises(review.TieError, match="escalated"):
        review.tally_votes(["evidence-backed", "evidence-backed", "contradicted", "contradicted"])
    with pytest.raises(review.TieError):
        review.tally_votes(["evidence-backed", "unsupported", "interesting-novel"])


def test_tally_votes_k_minimum_and_unknown_label() -> None:
    with pytest.raises(review.ReviewError, match="k >= 3"):
        review.tally_votes(["evidence-backed", "evidence-backed"])
    with pytest.raises(review.ReviewError, match="unknown vote label"):
        review.tally_votes(["evidence-backed", "evidence-backed", "meh"])


def test_composite_edge_values_exact() -> None:
    # mean weight 0.4 -> exactly 70.0 (band edge, strong side)
    seventy = review.composite_from_labels(["evidence-backed"] * 3 + ["unsupported"] * 2)
    assert seventy == 70.0
    assert review.band_of(seventy) == "strong"
    # mean weight -0.2 -> exactly 40.0 (band edge, adequate side)
    forty = review.composite_from_labels(
        ["interesting-novel", "interesting-novel", "unsupported", "unsupported", "contradicted"]
    )
    assert forty == 40.0
    assert review.band_of(forty) == "adequate"
    # mean weight -0.6 -> exactly 20.0 (band edge, weak side) — THROUGH the real
    # summation path, not a hardcoded float into band_of: a regression that broke
    # exact representability at the 20-edge would land at 19.999... and fail here.
    twenty = review.composite_from_labels(["contradicted"] + ["unsupported"] * 4)
    assert twenty == 20.0
    assert review.band_of(twenty) == "weak"


def test_band_boundaries() -> None:
    assert review.band_of(100.0) == "strong"
    assert review.band_of(70.0) == "strong"
    assert review.band_of(69.999) == "adequate"
    assert review.band_of(40.0) == "adequate"
    assert review.band_of(39.999) == "weak"
    assert review.band_of(20.0) == "weak"
    assert review.band_of(19.999) == "unsupported"
    assert review.band_of(0.0) == "unsupported"


def test_composite_requires_choices() -> None:
    with pytest.raises(review.ReviewError, match="at least one"):
        review.composite_from_labels([])


def test_parse_failed_shares_persist_through_cli(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _open(ws, capsys)["run_id"]
    payload = worked_payload()
    payload["choices"][0]["votes"] = ["evidence-backed", "parse-failed", "evidence-backed"]
    assert _commit(ws, monkeypatch, payload, run_id) == 0
    conn = _connect(ws)
    try:
        score = _one(
            conn,
            "SELECT evidence_backed_share, contradicted_share, classification FROM scores",
        )
        assert float(score[0]) == pytest.approx(2 / 3)
        assert float(score[1]) == pytest.approx(1 / 3)
        assert score[2] == "well-supported"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Payload validation (the pydantic mirror of review-commit.schema.json)
# ---------------------------------------------------------------------------


def test_payload_literature_searched_requires_queries() -> None:
    choice = worked_choice()
    choice["search_queries"] = []
    with pytest.raises(ValidationError, match="search_queries"):
        review.CommitPayload.model_validate({"choices": [choice]})


def test_payload_duplicate_choice_keys_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate choice_key"):
        review.CommitPayload.model_validate({"choices": [worked_choice(), worked_choice()]})


def test_payload_votes_minimum_and_enum() -> None:
    choice = worked_choice()
    choice["votes"] = ["evidence-backed", "evidence-backed"]
    with pytest.raises(ValidationError):
        review.CommitPayload.model_validate({"choices": [choice]})
    choice["votes"] = ["evidence-backed", "evidence-backed", "excellent"]
    with pytest.raises(ValidationError):
        review.CommitPayload.model_validate({"choices": [choice]})


def test_payload_choice_key_must_be_kebab() -> None:
    choice = worked_choice()
    choice["choice_key"] = "Not A Slug"
    with pytest.raises(ValidationError, match="kebab"):
        review.CommitPayload.model_validate({"choices": [choice]})


def test_payload_citation_shape_rules() -> None:
    base = {"relevance_note": "n", "support_direction": "supports"}
    # New record without a method.
    with pytest.raises(ValidationError, match="resolution_method"):
        review.CitationEntry.model_validate({**base, "kind": "external", "title": "t"})
    # api_structured without a locator (the lookup is server-side, off the locator).
    with pytest.raises(ValidationError, match="locator"):
        review.CitationEntry.model_validate(
            {
                **base,
                "kind": "external",
                "resolution_method": "api_structured",
                "title": "t",
            }
        )
    # api_echo is not a payload field at all (captured server-side; extra='forbid').
    with pytest.raises(ValidationError, match="api_echo"):
        review.CitationEntry.model_validate(
            {
                **base,
                "kind": "external",
                "resolution_method": "api_structured",
                "title": "t",
                "url": "https://arxiv.org/abs/2307.03172",
                "api_echo": {"title": "t"},
            }
        )
    # natural_key link without kind.
    with pytest.raises(ValidationError, match="requires kind"):
        review.CitationEntry.model_validate({**base, "natural_key": "https://x.test/a"})
    # Link + new-record fields mixed.
    with pytest.raises(ValidationError, match="must not carry"):
        review.CitationEntry.model_validate(
            {**base, "citation_id": 1, "resolution_method": "api_structured"}
        )
    # internal-read must be kind='internal'.
    with pytest.raises(ValidationError, match="internal"):
        review.CitationEntry.model_validate(
            {
                **base,
                "kind": "external",
                "resolution_method": "internal-read",
                "title": "t",
                "workspace_path": "docs/x.md",
                "quote": "q",
            }
        )


def test_golden_commit_payload_validates_through_the_mirror() -> None:
    """The worked-example payload IS the golden payload for review-commit.schema.json —
    validated through the pydantic mirror (the documented no-new-dependency route)."""
    payload = review.CommitPayload.model_validate(worked_payload())
    assert payload.choices[0].choice_key == "subagent-terse-verdict-file-detail"
    assert payload.choices[0].citations[0].resolution_method == "api_structured"
    assert payload.choices[0].citations[1].resolution_method == "internal-read"


# ---------------------------------------------------------------------------
# Contract schema sync — docs/contracts/*.schema.json == the pydantic mirrors.
# No-new-dependency route (Step 4 item 5): pydantic models validate every real
# payload/output; these tests assert the .json files' properties/required/enums
# equal the model definitions, so the schemas and the code cannot drift.
# ---------------------------------------------------------------------------


def _load_schema(name: str) -> dict[str, Any]:
    data = json.loads((CONTRACTS_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


_CONTRACT_PAIRS: list[tuple[type[BaseModel], str, str]] = [
    (review.CommitPayload, "review-commit.schema.json", "CommitPayload"),
    (review.ChoiceEntry, "review-commit.schema.json", "ChoiceEntry"),
    (review.CitationEntry, "review-commit.schema.json", "CitationEntry"),
    (review.OpenOutput, "review-open.schema.json", "OpenOutput"),
    (review.OpenArtifact, "review-open.schema.json", "OpenArtifact"),
    (review.PriorChoice, "review-open.schema.json", "PriorChoice"),
]


@pytest.mark.parametrize(
    ("model", "schema_file", "def_name"),
    _CONTRACT_PAIRS,
    ids=[pair[2] for pair in _CONTRACT_PAIRS],
)
def test_contract_schema_matches_pydantic_mirror(
    model: type[BaseModel], schema_file: str, def_name: str
) -> None:
    schema = _load_schema(schema_file)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    definition = schema["$defs"][def_name]
    # extra="forbid" <-> additionalProperties: false
    assert definition["additionalProperties"] is False
    assert set(definition["properties"]) == set(model.model_fields)
    required = {name for name, field in model.model_fields.items() if field.is_required()}
    assert set(definition.get("required", [])) == required


def test_contract_enums_match_code_constants() -> None:
    commit_schema = _load_schema("review-commit.schema.json")
    votes_enum = set(commit_schema["$defs"]["ChoiceEntry"]["properties"]["votes"]["items"]["enum"])
    assert votes_enum == set(review.LABEL_WEIGHTS) | {review.PARSE_FAILED_LABEL}
    direction_enum = set(
        commit_schema["$defs"]["CitationEntry"]["properties"]["support_direction"]["enum"]
    )
    assert direction_enum == {"supports", "contradicts", "tangential"}
    method_enum = set(
        commit_schema["$defs"]["CitationEntry"]["properties"]["resolution_method"]["enum"]
    ) - {None}
    assert method_enum == {"api_structured", "web_fetch_verified", "internal-read"}
    # The fabrication-hole fix, structurally: neither echo/page-text field exists.
    citation_properties = set(commit_schema["$defs"]["CitationEntry"]["properties"])
    assert "api_echo" not in citation_properties
    assert "fetched_text" not in citation_properties


def test_actual_open_output_validates_against_open_contract(
    ws: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The CODE'S ACTUAL open output validates against review-open.schema.json via its
    pydantic mirror (OpenOutput, extra='forbid') + the field-sync test above — the
    documented no-new-dependency validation route."""
    data = _open(ws, capsys)
    opened = review.OpenOutput.model_validate(data)
    assert json.loads(opened.model_dump_json()) == data  # lossless round-trip


def test_interpretation_guide_in_sync_with_code() -> None:
    """The minimal guide/code sync the review.py docstring promises: the versioned
    prose names all four dimension labels, the three band cutpoints, and the current
    guide version. A cutpoint or label rename that skips the guide fails here."""
    guide = (PROJECT_ROOT / "docs" / "interpretation-guide.md").read_text(encoding="utf-8")
    for label in review.LABEL_WEIGHTS:
        assert label in guide, f"guide does not mention the {label!r} label"
    for cutpoint in ("70", "40", "20"):
        assert cutpoint in guide, f"guide does not mention the {cutpoint} band cutpoint"
    assert review.INTERPRETATION_GUIDE_VERSION in guide


# ---------------------------------------------------------------------------
# Migration 0002 — a v1 DB migrates to the same review_runs shape as a fresh init
# ---------------------------------------------------------------------------

_V1_REVIEW_RUNS_DDL = """
CREATE TABLE review_runs (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id                     INTEGER NOT NULL,
    started_at                      TEXT NOT NULL,
    finished_at                     TEXT,
    artifact_content_hash_at_review TEXT NOT NULL,
    artifact_git_sha_at_review      TEXT,
    reviewer_model                  TEXT NOT NULL,
    tool_schema_version             INTEGER NOT NULL,
    status                          TEXT NOT NULL DEFAULT 'completed'
                                        CHECK (status IN ('completed', 'aborted')),
    notes                           TEXT
);
PRAGMA user_version = 1;
"""


def test_migration_0002_converges_with_fresh_schema(tmp_path: Path) -> None:
    fresh_path = tmp_path / "fresh.db"
    assert db.init_db(fresh_path) is True
    legacy_path = tmp_path / "legacy.db"
    legacy = db.connect(legacy_path)
    try:
        legacy.executescript(_V1_REVIEW_RUNS_DDL)
        legacy.commit()
    finally:
        legacy.close()

    assert db.migrate(legacy_path) == [2]

    def columns(path: Path) -> list[tuple[str, str, int, int]]:
        conn = db.connect(path)
        try:
            return [
                (str(r[1]), str(r[2]).upper(), int(r[3]), int(r[5]))
                for r in conn.execute("PRAGMA table_info(review_runs)")
            ]
        finally:
            conn.close()

    assert columns(legacy_path) == columns(fresh_path)


# ---------------------------------------------------------------------------
# git_head_sha — best-effort, nullable
# ---------------------------------------------------------------------------


def test_git_head_sha_none_outside_a_repo(tmp_path: Path) -> None:
    assert review.git_head_sha(tmp_path) is None


def test_git_head_sha_captures_head_in_a_repo(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    repo = tmp_path / "repo"
    repo.mkdir()
    init = subprocess.run(
        ["git", "init", "-q", str(repo)], capture_output=True, text=True, check=False
    )
    commit = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@test.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if init.returncode != 0 or commit.returncode != 0:
        pytest.skip(f"git fixture setup failed: {init.stderr or commit.stderr}")
    sha = review.git_head_sha(repo)
    assert sha is not None
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
