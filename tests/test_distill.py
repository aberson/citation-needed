"""distill.py — mechanical queue generation, propose upsert, triage verbs, rank math.

Covers the Step 6 acceptance targets through the PRODUCTION CLI entry (``cli.main``):

- load-weight test: an unsupported claude_md choice outranks an equally-unsupported
  skill choice (2.25 vs 0.75 — exact values); memory tier exact through the real
  generate path (1.125);
- a well-supported (and an interesting) choice yields NO queue row;
- justification NOT NULL enforced: a needs-improvement choice with neither linked
  citations nor a recorded literature search REJECTS the whole generate run loudly
  (nothing written — including rows for the run's other, justifiable choices);
- corrupted scores.search_queries fails loud (DistillError naming run + choice),
  never silently defaults to [];
- mechanical defaults: contradicted majority -> 'rewrite', unsupported -> 'trim';
- rank math exact: (1 - composite/100) * load weight, per-choice composite;
- re-generate/re-propose upsert: one row per choice, refreshed in place;
- supersession (queue lifecycle): a re-review commit purges — in its own
  transaction, before any distill verb runs — every open row the new run no longer
  justifies (choice reclassified well-supported, or removed from the artifact);
  resolved rows are immutable audit and survive; `cite queue list` flags rows whose
  source run is no longer the artifact's newest committed run ([superseded run]);
- `cite distill propose`: whole-payload reject (unscored key, unknown citation id,
  duplicate choice_key in one payload, resolved row, 'rewrite' without
  suggested_rewrite), suggested_rewrite appended;
- `cite queue list`: rank-desc ordering + --status/--project filters;
- `cite queue resolve`: keep->rejected / cut,rewrite->accepted, resolved_by
  (--by, else env USERNAME/USER) + resolved_at round-trip; resolved rows immutable;
- fingerprint-A scoping: ONLY the scorer templates (extraction + classification)
  feed calibration fingerprint A — adding/editing prompts/distill.v*.md never
  invalidates a calibration;
- drift guards: docs/interpretation-guide.md's load-weight table matches
  distill.LOAD_WEIGHTS; LOAD_WEIGHTS covers exactly the artifact_type enum.

Shared scaffolding (mini workspace, worked payload, open/commit helpers, the offline
structured-API mock) lives in tests/conftest.py — one source of truth.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from citation_needed import calibrate, db, distill
from citation_needed.cli import main
from citation_needed.models import DETAILS_MODELS
from conftest import (
    LEVER_DOC,
    LEVER_QUOTE,
    _commit,
    _connect,
    _count,
    _open,
    _stdin_bytes,
    register_artifact,
    worked_payload,
)

_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ---------------------------------------------------------------------------
# Payload builders + CLI helpers
# ---------------------------------------------------------------------------


def _choice(
    key: str,
    votes: list[str],
    *,
    searched: bool = True,
    found: bool = False,
    queries: list[str] | None = None,
    citations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "choice_key": key,
        "summary": f"Test choice {key}.",
        "quote": f"Some asserted convention for {key}.",
        "span_start_line": 1,
        "span_end_line": 1,
        "category": "doc-minimalism",
        "votes": votes,
        "literature_searched": searched,
        "literature_found": found,
        "search_queries": (
            queries if queries is not None else (["test query one"] if searched else [])
        ),
        "citations": citations or [],
    }


def _unsupported_choice(key: str = "asserted-convention") -> dict[str, Any]:
    return _choice(key, ["unsupported"] * 3)


def _contradicting_internal_citation() -> dict[str, Any]:
    return {
        "kind": "internal",
        "resolution_method": "internal-read",
        "title": "Token-usage lever map, Lever 2",
        "workspace_path": LEVER_DOC,
        "quote": LEVER_QUOTE,
        "relevance_note": "Measured workspace evidence arguing against the choice.",
        "support_direction": "contradicts",
    }


def _contradicted_choice(key: str = "contradicted-convention") -> dict[str, Any]:
    return _choice(
        key,
        ["contradicted"] * 3,
        found=True,
        citations=[_contradicting_internal_citation()],
    )


def _supported_choice(key: str = "supported-convention") -> dict[str, Any]:
    """Evidence-backed 3/3 — the re-review flip payload for the supersession tests."""
    citation = dict(
        _contradicting_internal_citation(),
        support_direction="supports",
        relevance_note="Measured workspace evidence supporting the choice.",
    )
    return _choice(key, ["evidence-backed"] * 3, found=True, citations=[citation])


def _review_and_commit(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    choices: list[dict[str, Any]],
) -> int:
    opened = _open(ws, capsys, path=path)
    assert _commit(ws, monkeypatch, {"choices": choices}, opened["run_id"]) == 0
    capsys.readouterr()
    return int(opened["run_id"])


def _generate(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    run_id: int,
    expect: int = 0,
) -> str:
    code = main(["distill", "generate", "--run", str(run_id), "--db", str(ws["db"])])
    out = capsys.readouterr().out
    assert code == expect, out
    return out


def _propose(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    run_id: int | None = None,
    expect: int = 0,
) -> str:
    _stdin_bytes(monkeypatch, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    args = ["distill", "propose", "--db", str(ws["db"])]
    if run_id is not None:
        args += ["--run", str(run_id)]
    code = main(args)
    out = capsys.readouterr().out
    assert code == expect, out
    return out


def _queue_list(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    *flags: str,
    expect: int = 0,
) -> str:
    code = main(["queue", "list", "--db", str(ws["db"]), *flags])
    out = capsys.readouterr().out
    assert code == expect, out
    return out


def _queue_resolve(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    queue_id: int,
    decision_flag: str,
    by: str | None = None,
    expect: int = 0,
) -> str:
    args = ["queue", "resolve", str(queue_id), decision_flag, "--db", str(ws["db"])]
    if by is not None:
        args += ["--by", by]
    code = main(args)
    out = capsys.readouterr().out
    assert code == expect, out
    return out


def _queue_rows(ws: dict[str, Path]) -> list[dict[str, Any]]:
    conn = _connect(ws)
    try:
        rows = conn.execute(
            "SELECT q.id, c.choice_key, q.proposal_kind, q.rank, q.justification, "
            "q.justifying_citation_ids, q.status, q.created_at, q.resolved_at, "
            "q.resolved_by, a.artifact_type FROM distill_queue q "
            "JOIN choices c ON c.id = q.choice_id "
            "JOIN artifacts a ON a.id = q.artifact_id ORDER BY q.id"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": int(r[0]),
            "choice_key": str(r[1]),
            "proposal_kind": str(r[2]),
            "rank": float(r[3]),
            "justification": str(r[4]),
            "justifying_citation_ids": r[5],
            "status": str(r[6]),
            "created_at": str(r[7]),
            "resolved_at": r[8],
            "resolved_by": r[9],
            "artifact_type": str(r[10]),
        }
        for r in rows
    ]


def _rows_by_key(ws: dict[str, Path]) -> dict[str, dict[str, Any]]:
    return {row["choice_key"]: row for row in _queue_rows(ws)}


# ---------------------------------------------------------------------------
# generate — acceptance targets
# ---------------------------------------------------------------------------


def test_load_weight_unsupported_claude_md_outranks_equal_skill(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "CLAUDE.md", artifact_type="claude_md")
    register_artifact(ws, ".claude/skills/foo/SKILL.md", artifact_type="skill")
    run_cm = _review_and_commit(ws, capsys, monkeypatch, "CLAUDE.md", [_unsupported_choice("cm")])
    run_sk = _review_and_commit(
        ws, capsys, monkeypatch, ".claude/skills/foo/SKILL.md", [_unsupported_choice("sk")]
    )
    _generate(ws, capsys, run_cm)
    _generate(ws, capsys, run_sk)
    rows = _rows_by_key(ws)
    # Equally unsupported (per-choice composite 25.0 both) — the tier decides:
    assert rows["cm"]["rank"] == 2.25  # (1 - 0.25) * 3.0
    assert rows["sk"]["rank"] == 0.75  # (1 - 0.25) * 1.0
    assert rows["cm"]["rank"] > rows["sk"]["rank"]


def test_well_supported_and_interesting_choices_yield_no_row(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Well-supported: the worked example (evidence-backed 3/3) on the scanned rule.
    opened = _open(ws, capsys)
    assert _commit(ws, monkeypatch, worked_payload(), opened["run_id"]) == 0
    capsys.readouterr()
    out = _generate(ws, capsys, int(opened["run_id"]))
    assert "0 row(s) written" in out
    assert "1 choice(s) yielded no row" in out
    # Interesting: interesting-novel 3/3 also yields no row.
    register_artifact(ws, "plans/plan.md", artifact_type="plan")
    run_id = _review_and_commit(
        ws,
        capsys,
        monkeypatch,
        "plans/plan.md",
        [_choice("novel-idea", ["interesting-novel"] * 3)],
    )
    out = _generate(ws, capsys, run_id)
    assert "0 row(s) written" in out
    conn = _connect(ws)
    try:
        assert _count(conn, "distill_queue") == 0
    finally:
        conn.close()


def test_generate_mechanical_mapping_and_justifications(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run_id = _review_and_commit(
        ws,
        capsys,
        monkeypatch,
        "rules/extra.md",
        [_contradicted_choice("bad-claim"), _unsupported_choice("bare-claim")],
    )
    _generate(ws, capsys, run_id)
    rows = _rows_by_key(ws)
    # contradicted majority -> 'rewrite', justified by the linked citation id(s):
    contradicted = rows["bad-claim"]
    assert contradicted["proposal_kind"] == "rewrite"
    cited_ids = json.loads(contradicted["justifying_citation_ids"])
    assert len(cited_ids) == 1
    assert "citation id(s)" in contradicted["justification"]
    assert "1 contradicts" in contradicted["justification"]
    # unsupported majority -> 'trim', justified by the documented absence:
    unsupported = rows["bare-claim"]
    assert unsupported["proposal_kind"] == "trim"
    assert unsupported["justifying_citation_ids"] is None
    assert "Documented absence" in unsupported["justification"]
    assert '"test query one"' in unsupported["justification"]
    assert unsupported["status"] == "open"
    assert _ISO_UTC_RE.match(unsupported["created_at"])


def test_generate_rejects_unjustifiable_choice_whole_run(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    # One justifiable needs-improvement choice + one with NEITHER citations nor a
    # recorded search: the WHOLE run rejects and nothing is written.
    run_id = _review_and_commit(
        ws,
        capsys,
        monkeypatch,
        "rules/extra.md",
        [
            _unsupported_choice("fine-claim"),
            _choice("black-box", ["unsupported"] * 3, searched=False),
        ],
    )
    out = _generate(ws, capsys, run_id, expect=1)
    assert "error:" in out
    assert "'black-box'" in out
    assert "literature_searched=0" in out
    assert "No rows were written" in out
    conn = _connect(ws)
    try:
        assert _count(conn, "distill_queue") == 0
    finally:
        conn.close()


def test_generate_requires_committed_run(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    opened = _open(ws, capsys)  # open but never committed
    out = _generate(ws, capsys, int(opened["run_id"]), expect=1)
    assert "not committed" in out
    out = _generate(ws, capsys, 9999, expect=1)
    assert "does not exist" in out


def test_rank_math_exact_values(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    register_artifact(ws, "plans/plan.md", artifact_type="plan")
    run_rule = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_contradicted_choice("dead-wrong")]
    )
    run_plan = _review_and_commit(
        ws, capsys, monkeypatch, "plans/plan.md", [_unsupported_choice("plan-claim")]
    )
    _generate(ws, capsys, run_rule)
    _generate(ws, capsys, run_plan)
    rows = _rows_by_key(ws)
    # contradicted: per-choice composite 0.0 -> (1 - 0) * 3.0 = 3.0 exactly
    assert rows["dead-wrong"]["rank"] == 3.0
    # unsupported: per-choice composite 25.0 -> (1 - 0.25) * 0.75 = 0.5625 exactly
    assert rows["plan-claim"]["rank"] == 0.5625
    # The formula function is the same implementation the rows came from:
    assert distill.queue_rank(0.0, "rule") == 3.0
    assert distill.queue_rank(25.0, "plan") == 0.5625
    assert distill.queue_rank(25.0, "claude_md") == 2.25


def test_memory_tier_rank_exact_through_generate(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The 1.5 memory tier computed end-to-end through the real generate path —
    # existence is drift-guarded elsewhere; this pins the VALUE the formula yields.
    register_artifact(ws, "memory/MEMORY.md", artifact_type="memory")
    run_id = _review_and_commit(
        ws, capsys, monkeypatch, "memory/MEMORY.md", [_unsupported_choice("memory-claim")]
    )
    _generate(ws, capsys, run_id)
    row = _rows_by_key(ws)["memory-claim"]
    assert row["artifact_type"] == "memory"
    # unsupported: per-choice composite 25.0 -> (1 - 0.25) * 1.5 = 1.125 exactly
    assert row["rank"] == 1.125
    assert distill.queue_rank(25.0, "memory") == 1.125


def test_regenerate_refreshes_no_duplicate_rows(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run_id = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("bare-claim")]
    )
    out = _generate(ws, capsys, run_id)
    assert "(created)" in out
    first = _queue_rows(ws)
    out = _generate(ws, capsys, run_id)
    assert "(refreshed)" in out
    second = _queue_rows(ws)
    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["created_at"] == first[0]["created_at"]  # creation time survives


def test_generate_fails_loud_on_corrupted_search_queries(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # scores.search_queries is written once as json.dumps(list); anything else is a
    # corrupted row and must raise a DistillError naming run + choice — never
    # silently read back as queries=[] (calibrate.py's json.loads discipline).
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run_id = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("bare-claim")]
    )

    def _corrupt(value: str) -> None:
        conn = _connect(ws)
        try:
            conn.execute(
                "UPDATE scores SET search_queries = ? WHERE review_run_id = ?",
                (value, run_id),
            )
            conn.commit()
        finally:
            conn.close()

    _corrupt("{not json")
    out = _generate(ws, capsys, run_id, expect=1)
    assert "error:" in out
    assert "'bare-claim'" in out
    assert f"#{run_id}" in out
    assert "not valid JSON" in out
    assert _queue_rows(ws) == []  # nothing written
    # Valid JSON of the WRONG shape is equally corrupted — fail loud, not [].
    _corrupt('{"a": 1}')
    out = _generate(ws, capsys, run_id, expect=1)
    assert "not a JSON array" in out
    assert _queue_rows(ws) == []


# ---------------------------------------------------------------------------
# Supersession — stale open rows die with the re-review commit (queue lifecycle)
# ---------------------------------------------------------------------------


def test_reclassified_choice_purges_stale_open_row_at_commit(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run1 = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("bare-claim")]
    )
    _generate(ws, capsys, run1)
    assert _rows_by_key(ws)["bare-claim"]["status"] == "open"
    # Re-review scores the SAME choice_key well-supported: the stale open 'trim'
    # row dies WITH the commit itself — no distill verb runs in between, so there
    # is no window where `cite queue list` recommends against superseded evidence.
    _review_and_commit(ws, capsys, monkeypatch, "rules/extra.md", [_supported_choice("bare-claim")])
    assert _queue_rows(ws) == []
    out = _queue_list(ws, capsys)
    assert "No distill-queue rows match (status open)." in out


def test_removed_choice_purges_stale_open_row_at_commit(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run1 = _review_and_commit(
        ws,
        capsys,
        monkeypatch,
        "rules/extra.md",
        [_unsupported_choice("dead-claim"), _unsupported_choice("kept-claim")],
    )
    _generate(ws, capsys, run1)
    assert len(_queue_rows(ws)) == 2
    # The re-review payload omits 'dead-claim' entirely -> commit marks the choice
    # status='removed' AND purges its open row in the same transaction. 'kept-claim'
    # is still needs-improvement, so its open row survives — visibly flagged as
    # [superseded run] until a generate refreshes it against the new run.
    run2 = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("kept-claim")]
    )
    rows = _rows_by_key(ws)
    assert list(rows) == ["kept-claim"]
    assert rows["kept-claim"]["status"] == "open"
    out = _queue_list(ws, capsys)
    assert "dead-claim" not in out
    assert "kept-claim" in out
    assert "[superseded run]" in out
    # A FULL re-review cycle (commit + generate) leaves zero stale actionables:
    _generate(ws, capsys, run2)
    out = _queue_list(ws, capsys)
    assert "1 distill-queue row(s)" in out
    assert "[superseded run]" not in out


def test_resolved_row_survives_reclassifying_re_review(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run1 = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("bare-claim")]
    )
    _generate(ws, capsys, run1)
    queue_id = _queue_rows(ws)[0]["id"]
    _queue_resolve(ws, capsys, queue_id, "--cut", by="alice")
    # Re-review flips the choice to well-supported: the RESOLVED row is immutable
    # audit — supersession deletes open rows only, never a recorded decision.
    _review_and_commit(ws, capsys, monkeypatch, "rules/extra.md", [_supported_choice("bare-claim")])
    rows = _queue_rows(ws)
    assert len(rows) == 1
    assert rows[0]["id"] == queue_id
    assert rows[0]["status"] == "accepted"
    assert rows[0]["resolved_by"] == "alice"


# ---------------------------------------------------------------------------
# distill propose — the skill-drafted override path
# ---------------------------------------------------------------------------


def test_propose_overrides_mechanical_default_upsert(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run_id = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("bare-claim")]
    )
    _generate(ws, capsys, run_id)
    before = _queue_rows(ws)[0]
    assert before["proposal_kind"] == "trim"
    payload = {
        "run_id": run_id,
        "proposals": [
            {
                "choice_key": "bare-claim",
                "proposal_kind": "move-to-reference",
                "justification": "Documented absence: nothing backs the always-load "
                "placement; reference detail belongs in docs/.",
                "suggested_rewrite": "See docs/reference.md for the worked detail.",
            }
        ],
    }
    out = _propose(ws, capsys, monkeypatch, payload)
    assert "(refreshed)" in out
    rows = _queue_rows(ws)
    assert len(rows) == 1  # no duplicate row for the same choice
    after = rows[0]
    assert after["id"] == before["id"]
    assert after["proposal_kind"] == "move-to-reference"
    assert after["status"] == "open"  # propose never moves status
    assert after["rank"] == before["rank"]  # rank stays formula-computed
    assert after["justification"].endswith(
        "Suggested rewrite:\nSee docs/reference.md for the worked detail."
    )


def test_propose_can_queue_a_scored_well_supported_choice(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The skill layer's judgment may exceed the mechanical rule (e.g. a 'no-action'
    # record). Any choice SCORED in the run is proposable.
    opened = _open(ws, capsys)
    assert _commit(ws, monkeypatch, worked_payload(), opened["run_id"]) == 0
    capsys.readouterr()
    payload = {
        "proposals": [
            {
                "choice_key": "subagent-terse-verdict-file-detail",
                "proposal_kind": "no-action",
                "justification": "Examined for distill: well-supported by its linked "
                "citations; leave as-is.",
            }
        ],
        "run_id": int(opened["run_id"]),
    }
    _propose(ws, capsys, monkeypatch, payload)
    rows = _queue_rows(ws)
    assert len(rows) == 1
    assert rows[0]["proposal_kind"] == "no-action"
    assert rows[0]["rank"] == 0.0  # composite 100 -> (1 - 1) * 3.0


def test_propose_rewrite_requires_suggested_rewrite(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run_id = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("bare-claim")]
    )
    payload = {
        "proposals": [
            {
                "choice_key": "bare-claim",
                "proposal_kind": "rewrite",
                "justification": "Needs a rewrite.",
            }
        ]
    }
    out = _propose(ws, capsys, monkeypatch, payload, run_id=run_id, expect=1)
    assert "suggested_rewrite" in out
    assert _queue_rows(ws) == []


def test_propose_whole_payload_reject_on_any_invalid_entry(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run_id = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("bare-claim")]
    )
    _generate(ws, capsys, run_id)
    before = _queue_rows(ws)
    # One valid entry + one unscored choice_key: NOTHING changes.
    payload = {
        "proposals": [
            {
                "choice_key": "bare-claim",
                "proposal_kind": "delete-superseded",
                "justification": "Superseded by the newer rule.",
            },
            {
                "choice_key": "never-scored",
                "proposal_kind": "trim",
                "justification": "Ghost entry.",
            },
        ]
    }
    out = _propose(ws, capsys, monkeypatch, payload, run_id=run_id, expect=1)
    assert "whole payload" in out
    assert "'never-scored'" in out
    assert "No rows were written" in out
    assert _queue_rows(ws) == before  # the valid entry did not land either


def test_propose_payload_duplicate_choice_keys_rejected(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The sibling of test_review.py's duplicate-key commit guard: two proposals for
    # one choice_key reject the WHOLE payload — the only thing standing between a
    # duplicated key and a double INSERT past the pre-computed existing_by_key map.
    entry = {
        "choice_key": "bare-claim",
        "proposal_kind": "trim",
        "justification": "Documented absence.",
    }
    with pytest.raises(ValidationError, match="duplicate choice_key"):
        distill.ProposePayload.model_validate({"proposals": [entry, dict(entry)]})
    # And through the production CLI seam (rejected before any run/DB lookup):
    out = _propose(
        ws, capsys, monkeypatch, {"run_id": 1, "proposals": [entry, dict(entry)]}, expect=1
    )
    assert "does not match the distill-propose contract" in out
    assert "duplicate choice_key" in out
    assert _queue_rows(ws) == []


def test_propose_unknown_citation_id_rejects(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run_id = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("bare-claim")]
    )
    payload = {
        "proposals": [
            {
                "choice_key": "bare-claim",
                "proposal_kind": "trim",
                "justification": "Cites a citation that does not exist.",
                "justifying_citation_ids": [4242],
            }
        ]
    }
    out = _propose(ws, capsys, monkeypatch, payload, run_id=run_id, expect=1)
    assert "4242" in out
    assert "does not exist in the corpus" in out
    assert _queue_rows(ws) == []


def test_propose_on_resolved_row_rejects(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run_id = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("bare-claim")]
    )
    _generate(ws, capsys, run_id)
    queue_id = _queue_rows(ws)[0]["id"]
    _queue_resolve(ws, capsys, queue_id, "--cut", by="alice")
    payload = {
        "proposals": [
            {
                "choice_key": "bare-claim",
                "proposal_kind": "rewrite",
                "justification": "Trying to overwrite a recorded decision.",
                "suggested_rewrite": "New text.",
            }
        ]
    }
    out = _propose(ws, capsys, monkeypatch, payload, run_id=run_id, expect=1)
    assert "already has a resolved queue row" in out
    assert _queue_rows(ws)[0]["proposal_kind"] == "trim"  # untouched


def test_propose_run_bookkeeping_and_stdin_errors(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run_id = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("bare-claim")]
    )
    entry = {
        "choice_key": "bare-claim",
        "proposal_kind": "trim",
        "justification": "Documented absence.",
    }
    # --run conflicts with payload run_id:
    out = _propose(
        ws,
        capsys,
        monkeypatch,
        {"run_id": run_id + 1, "proposals": [entry]},
        run_id=run_id,
        expect=1,
    )
    assert "conflicts with payload run_id" in out
    # No run id at all:
    out = _propose(ws, capsys, monkeypatch, {"proposals": [entry]}, expect=1)
    assert "no run id" in out
    # Empty stdin:
    _stdin_bytes(monkeypatch, b"")
    assert main(["distill", "propose", "--db", str(ws["db"])]) == 1
    assert "empty stdin" in capsys.readouterr().out
    # Unknown fields reject (extra="forbid" — the review-commit discipline):
    bad = {"proposals": [dict(entry, rank=99.0)], "run_id": run_id}
    out = _propose(ws, capsys, monkeypatch, bad, expect=1)
    assert "does not match the distill-propose contract" in out
    assert _queue_rows(ws) == []


# ---------------------------------------------------------------------------
# queue list — ordering + filters
# ---------------------------------------------------------------------------


def _seed_ranked_rows(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, dict[str, Any]]:
    """Four artifacts across tiers -> ranks 3.0 (rule), 2.25 (claude_md),
    0.75 (skill), 0.5625 (plan)."""
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    register_artifact(ws, "CLAUDE.md", artifact_type="claude_md")
    register_artifact(ws, ".claude/skills/foo/SKILL.md", artifact_type="skill", project="proj-x")
    register_artifact(ws, "plans/plan.md", artifact_type="plan")
    for path, choices in (
        ("rules/extra.md", [_contradicted_choice("rule-claim")]),
        ("CLAUDE.md", [_unsupported_choice("cm-claim")]),
        (".claude/skills/foo/SKILL.md", [_unsupported_choice("skill-claim")]),
        ("plans/plan.md", [_unsupported_choice("plan-claim")]),
    ):
        run_id = _review_and_commit(ws, capsys, monkeypatch, path, choices)
        _generate(ws, capsys, run_id)
    return _rows_by_key(ws)


def test_queue_list_ordering_and_filters(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _seed_ranked_rows(ws, capsys, monkeypatch)
    out = _queue_list(ws, capsys)
    assert "4 distill-queue row(s) (status open; rank desc):" in out
    listed_ids = [int(m) for m in re.findall(r"\[(\d+)\] rank", out)]
    expected_order = [
        rows["rule-claim"]["id"],  # 3.0
        rows["cm-claim"]["id"],  # 2.25
        rows["skill-claim"]["id"],  # 0.75
        rows["plan-claim"]["id"],  # 0.5625
    ]
    assert listed_ids == expected_order
    # The requested columns are present on the top row:
    assert "rank 3.00" in out
    assert "composite 0.0 (unsupported)" in out
    assert "rewrite" in out
    assert "rules/extra.md :: rule-claim" in out
    assert "Mechanical default" in out  # one-line justification
    # --project filter:
    out = _queue_list(ws, capsys, "--project", "proj-x")
    assert "1 distill-queue row(s)" in out
    assert "skill-claim" in out
    assert "cm-claim" not in out
    # --status filter: resolve one, then it leaves 'open' and appears under 'accepted'.
    _queue_resolve(ws, capsys, rows["plan-claim"]["id"], "--cut", by="alice")
    out = _queue_list(ws, capsys)
    assert "3 distill-queue row(s)" in out
    assert "plan-claim" not in out
    out = _queue_list(ws, capsys, "--status", "accepted")
    assert "1 distill-queue row(s) (status accepted; rank desc):" in out
    assert "plan-claim" in out


def test_queue_list_empty_message(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _queue_list(ws, capsys)
    assert "No distill-queue rows match (status open)." in out


# ---------------------------------------------------------------------------
# queue resolve — the keep/cut/rewrite -> status mapping
# ---------------------------------------------------------------------------


def test_status_by_decision_mapping_pinned() -> None:
    # The documented contract (interpretation-guide § resolution mapping): keep
    # declines the proposal (target text stays); cut/rewrite accept it. 'applied'
    # is out of the CLI's scope — target edits happen outside citation-needed.
    assert distill.STATUS_BY_DECISION == {
        "keep": "rejected",
        "cut": "accepted",
        "rewrite": "accepted",
    }
    assert distill.QUEUE_STATUSES == ("open", "accepted", "rejected", "applied")


def test_resolve_round_trips_status_resolved_by_at(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run_id = _review_and_commit(
        ws,
        capsys,
        monkeypatch,
        "rules/extra.md",
        [
            _unsupported_choice("a-claim"),
            _unsupported_choice("b-claim"),
            _unsupported_choice("c-claim"),
        ],
    )
    _generate(ws, capsys, run_id)
    rows = _rows_by_key(ws)
    out = _queue_resolve(ws, capsys, rows["a-claim"]["id"], "--keep", by="alice")
    assert "--keep -> status 'rejected'" in out
    out = _queue_resolve(ws, capsys, rows["b-claim"]["id"], "--cut", by="alice")
    assert "--cut -> status 'accepted'" in out
    out = _queue_resolve(ws, capsys, rows["c-claim"]["id"], "--rewrite", by="bob")
    assert "--rewrite -> status 'accepted'" in out
    resolved = _rows_by_key(ws)
    assert resolved["a-claim"]["status"] == "rejected"
    assert resolved["b-claim"]["status"] == "accepted"
    assert resolved["c-claim"]["status"] == "accepted"
    for key, who in (("a-claim", "alice"), ("b-claim", "alice"), ("c-claim", "bob")):
        assert resolved[key]["resolved_by"] == who
        assert _ISO_UTC_RE.match(str(resolved[key]["resolved_at"]))


def test_resolve_default_resolver_from_env(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run_id = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("bare-claim")]
    )
    _generate(ws, capsys, run_id)
    queue_id = _queue_rows(ws)[0]["id"]
    monkeypatch.setenv("USERNAME", "operator-bob")
    _queue_resolve(ws, capsys, queue_id, "--cut")
    assert _queue_rows(ws)[0]["resolved_by"] == "operator-bob"


def test_resolve_without_any_identity_errors(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run_id = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("bare-claim")]
    )
    _generate(ws, capsys, run_id)
    queue_id = _queue_rows(ws)[0]["id"]
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    out = _queue_resolve(ws, capsys, queue_id, "--cut", expect=1)
    assert "no resolver identity" in out
    assert _queue_rows(ws)[0]["status"] == "open"  # nothing recorded


def test_resolve_unknown_id_and_already_resolved_errors(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run_id = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("bare-claim")]
    )
    _generate(ws, capsys, run_id)
    queue_id = _queue_rows(ws)[0]["id"]
    out = _queue_resolve(ws, capsys, 9999, "--keep", by="alice", expect=1)
    assert "does not exist" in out
    _queue_resolve(ws, capsys, queue_id, "--cut", by="alice")
    out = _queue_resolve(ws, capsys, queue_id, "--keep", by="alice", expect=1)
    assert "already resolved" in out
    assert _queue_rows(ws)[0]["status"] == "accepted"  # first decision stands


def test_resolved_rows_skipped_on_regenerate(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_artifact(ws, "rules/extra.md", artifact_type="rule")
    run_id = _review_and_commit(
        ws, capsys, monkeypatch, "rules/extra.md", [_unsupported_choice("bare-claim")]
    )
    _generate(ws, capsys, run_id)
    queue_id = _queue_rows(ws)[0]["id"]
    _queue_resolve(ws, capsys, queue_id, "--keep", by="alice")
    out = _generate(ws, capsys, run_id)
    assert "skipped 1 resolved row(s): bare-claim" in out
    rows = _queue_rows(ws)
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected"  # the recorded decision stands


# ---------------------------------------------------------------------------
# Fingerprint A scoping — WHICH templates feed calibration fingerprint A
# ---------------------------------------------------------------------------


def test_fingerprint_a_scoped_to_scorer_templates(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "extraction.v1.md").write_text("extract", encoding="utf-8")
    (prompts / "classification.v1.md").write_text("classify", encoding="utf-8")
    before = calibrate.prompts_fingerprint(prompts)
    # Adding a distill template does NOT invalidate a calibration — distill prompts
    # draft proposals from already-committed scores; they cannot move a composite.
    (prompts / "distill.v1.md").write_text("draft proposals", encoding="utf-8")
    assert calibrate.prompts_fingerprint(prompts) == before
    (prompts / "distill.v1.md").write_text("edited proposals", encoding="utf-8")
    assert calibrate.prompts_fingerprint(prompts) == before
    # Editing a SCORER template still invalidates:
    (prompts / "classification.v1.md").write_text("classify v2", encoding="utf-8")
    assert calibrate.prompts_fingerprint(prompts) != before
    # The scope constant is pinned — exactly the scorer families feed A:
    assert calibrate.FINGERPRINT_A_TEMPLATE_STEMS == ("classification", "extraction")


def test_fingerprint_a_fails_loud_with_only_distill_templates(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "distill.v1.md").write_text("draft proposals", encoding="utf-8")
    with pytest.raises(calibrate.CalibrationError, match="no prompt templates"):
        calibrate.prompts_fingerprint(prompts)


def test_real_prompts_dir_distill_template_outside_fingerprint_a() -> None:
    # The repo really ships prompts/distill.v1.md, and fingerprint A really
    # excludes it — the in-scope set is exactly the two scorer templates.
    assert (calibrate.PROMPTS_DIR / "distill.v1.md").is_file()
    names = [file.name for file in calibrate.fingerprint_a_files()]
    assert names == ["classification.v1.md", "extraction.v1.md"]


# ---------------------------------------------------------------------------
# Drift guards — one source of truth for the weight map
# ---------------------------------------------------------------------------


def test_load_weights_cover_exactly_the_artifact_type_enum() -> None:
    assert set(distill.LOAD_WEIGHTS) == set(DETAILS_MODELS)


def test_interpretation_guide_load_weight_table_matches_code() -> None:
    text = (db.PROJECT_ROOT / "docs" / "interpretation-guide.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| `(\w+)` \| \*\*([0-9.]+)\*\* \|", text, flags=re.M)
    parsed = {name: float(weight) for name, weight in rows}
    assert parsed == distill.LOAD_WEIGHTS, (
        "docs/interpretation-guide.md's load-weight table drifted from "
        "distill.LOAD_WEIGHTS (the single source of truth)"
    )


def test_queue_rank_rejects_out_of_range_composite_and_unknown_type() -> None:
    with pytest.raises(distill.DistillError, match=re.escape("0..100")):
        distill.queue_rank(101.0, "rule")
    with pytest.raises(distill.DistillError, match="no load weight"):
        distill.queue_rank(50.0, "novel-type")


def test_all_four_distill_and_queue_verbs_error_cleanly_without_db(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "nope" / "citation.db"
    for args in (
        ["distill", "generate", "--run", "1", "--db", str(missing)],
        # propose checks the db BEFORE reading stdin, so no stdin stub is needed:
        ["distill", "propose", "--db", str(missing)],
        ["queue", "list", "--db", str(missing)],
        ["queue", "resolve", "1", "--keep", "--db", str(missing)],
    ):
        assert main(args) == 1
        assert "database does not exist" in capsys.readouterr().out


def _direct_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "citation.db"
    db.init_db(db_path)
    return db.connect(db_path)


def test_list_queue_rejects_unknown_status(tmp_path: Path) -> None:
    conn = _direct_db(tmp_path)
    try:
        with pytest.raises(distill.DistillError, match="unknown status"):
            distill.list_queue(conn, status="bogus")
    finally:
        conn.close()
