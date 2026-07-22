"""Shared Step-4 review/breakdown test scaffolding — the ONE source of truth.

tests/test_review.py and tests/test_breakdown.py both drive the production CLI
(``cli.main``) against a mini workspace; the fixture workspace, the worked-example
payload (plan §12 Appendix A), the open/commit helpers, and the offline structured-API
mock live HERE so a change to the CLI's flag shape or the canonical payload is made
once (code-quality.md § one source of truth, applied to test scaffolding).

Offline discipline: ``mock_api_lookups`` replaces the two server-side structured-API
seams (``resolve.lookup_semantic_scholar_id`` / ``resolve.lookup_crossref_doi``) with
canonical-echo fakes, because ``commit_review`` now performs the api_structured lookup
ITSELF (the anti-fabrication fix — the payload never carries the echo). The ``ws``
fixture depends on it, so every CLI-driven test in these files is offline by
construction; tests that need a failing API re-monkeypatch on top.

Stdin discipline: ``cite review commit`` reads BYTES from ``sys.stdin.buffer`` and
decodes UTF-8 explicitly, so the helpers install a bytes-backed stdin
(``io.TextIOWrapper(io.BytesIO(...))``), never a bare ``io.StringIO`` (which has no
``.buffer`` and structurally cannot exercise the decode boundary).
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from citation_needed import calibrate, db, resolve
from citation_needed.cli import main

RULE_PATH = ".claude/rules/subagent-economy.md"
LEVER_DOC = "docs/investigations/token-usage-levers-consolidated-2026-06-22.md"
ARXIV_URL = "https://arxiv.org/abs/2307.03172"
WORKED_TITLE = "Lost in the Middle: How Language Models Use Long Contexts"

_BULLET_1 = (
    "- Return only the load-bearing verdict — a PASS/BLOCKED/verdict line, counts, and "
    "at most the single most important finding. Target a handful of lines, not paragraphs."
)
_BULLET_2 = (
    "- Write any longer detail to a file (e.g. `<worktree>/.build-step/<role>-report.md`, "
    "a findings `.json`, an investigation doc) and return only its path. The orchestrator "
    "reads that file only when the verdict requires it — not eagerly."
)
RULE_QUOTE = f"{_BULLET_1}\n{_BULLET_2}"

#: Lines 1-14 of the fixture rule — the worked-example quote sits at lines 9-10.
RULE_LINES = [
    "# Subagent economy — keep the orchestrator window slim",
    "",
    "The dominant token cost is resident orchestrator context, not subagent fan-out.",
    "",
    "## Rule 1 — Subagent returns are a terse verdict; detail goes to a file",
    "",
    "When an orchestrator spawns a sub-agent, the sub-agent's prompt MUST instruct it to:",
    "",
    _BULLET_1,
    _BULLET_2,
    "",
    "## Source",
    "",
    f"- {LEVER_DOC} (Lever 2).",
]

LEVER_QUOTE = (
    "Agent returns are ~18% of a representative build-phase window "
    "(sub-agents returning ~240k chars instead of a one-line verdict)."
)
LEVER_DOC_TEXT = (
    "# Token-usage reduction — consolidated lever map (2026-06-22)\n\n"
    "## Lever 2 — subagent returns\n\n"
    f"{LEVER_QUOTE}\n"
)

SEARCH_QUERY = 'Liu et al 2023 "Lost in the Middle: How Language Models Use Long Contexts" arxiv'

#: The canonical S2 echo the offline mock serves for the worked example — the shape
#: commit_review's SERVER-SIDE lookup captures and stores (never payload-supplied).
S2_WORKED_ECHO: dict[str, Any] = {
    "paperId": "2307.03172",
    "title": WORKED_TITLE,
    "year": 2023,
    "externalIds": {"ArXiv": "2307.03172"},
}


def worked_choice(*, summary: str | None = None, quote: str | None = None) -> dict[str, Any]:
    """The Appendix-A choice; pass reworded summary/quote for the D4 test."""
    return {
        "choice_key": "subagent-terse-verdict-file-detail",
        "summary": summary
        or (
            "Sub-agent returns a terse verdict; longer detail goes to a file the "
            "orchestrator reads only when the verdict requires it, not eagerly."
        ),
        "quote": quote or RULE_QUOTE,
        "span_start_line": 9,
        "span_end_line": 10,
        "category": "context-economy",
        "votes": ["evidence-backed", "evidence-backed", "evidence-backed"],
        "literature_searched": True,
        "literature_found": True,
        "search_queries": [SEARCH_QUERY],
        "citations": [
            {
                # NOTE: no api_echo — the echo is captured server-side by commit's own
                # structured-API lookup; a payload carrying one is rejected outright.
                "kind": "external",
                "resolution_method": "api_structured",
                "title": WORKED_TITLE,
                "authors": "Nelson F. Liu, Kevin Lin, John Hewitt, Ashwin Paranjape, "
                "Michele Bevilacqua, Fabio Petroni, Percy Liang",
                "year": 2023,
                "venue": "arXiv preprint 2307.03172; later TACL 12 (2024)",
                "url": ARXIV_URL,
                "relevance_note": "Indirect support: long-context position degradation "
                "motivates keeping the orchestrator window slim.",
                "support_direction": "supports",
            },
            {
                "kind": "internal",
                "resolution_method": "internal-read",
                "title": "Token-usage reduction — consolidated lever map (2026-06-22), Lever 2",
                "workspace_path": LEVER_DOC,
                "quote": LEVER_QUOTE,
                "source_line_ref": f"{LEVER_DOC}:5",
                "relevance_note": "Direct support: the measured 18%/~240k-char leak this "
                "rule cites as its own Source.",
                "support_direction": "supports",
            },
        ],
    }


def worked_payload() -> dict[str, Any]:
    return {"choices": [worked_choice()]}


def write_valid_calibration_fingerprint(
    db_path: Path,
    model_id: str = "claude-sonnet-5",
    computed_at: datetime | None = None,
) -> Path:
    """Seed a CURRENTLY-valid calibration fingerprint cache next to ``db_path``.

    Step 5 made ``cite review open`` hard-refuse without a valid cached calibration.
    This helper mirrors the state a just-passed ``cite calibrate commit`` leaves
    behind (fingerprints A-D computed from the REAL current values), so review tests
    exercise the production check and still open — the check itself is never
    weakened or bypassed. Pass ``computed_at`` to write an aged cache.
    """
    conn = db.connect(db_path)
    try:
        corpus_fp = calibrate.corpus_fingerprint(conn)
        schema_ver = calibrate.schema_version(conn)
    finally:
        conn.close()
    moment = computed_at if computed_at is not None else datetime.now(UTC)
    fingerprint = calibrate.CalibrationFingerprint(
        prompts_sha256=calibrate.prompts_fingerprint(),
        model_id=model_id,
        corpus_fingerprint=corpus_fp,
        schema_user_version=schema_ver,
        anchors_sha256=calibrate.anchors_fingerprint(),
        computed_at=moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        gate=calibrate.GateResults(
            composite_good=95.0,
            composite_garbage=0.0,
            margin=95.0,
            good_evidence_share=0.8,
            garbage_negative_share=1.0,
            parse_fail_rate=0.0,
            passed=True,
        ),
    )
    path = calibrate.fingerprint_path(db_path)
    calibrate.write_fingerprint(path, fingerprint)  # the production atomic writer
    return path


@pytest.fixture()
def mock_api_lookups(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Offline stand-in for the SERVER-SIDE structured-API seam; records lookups.

    Returns ``{"s2": [ids...], "crossref": [dois...]}`` so a test can assert the
    lookup really was performed by commit itself (not trusted from the payload).
    """
    calls: dict[str, list[str]] = {"s2": [], "crossref": []}

    def fake_s2(paper_id: str, **_kwargs: Any) -> dict[str, Any]:
        calls["s2"].append(paper_id)
        return dict(S2_WORKED_ECHO)

    def fake_crossref(doi: str, **_kwargs: Any) -> dict[str, Any]:
        calls["crossref"].append(doi)
        return {"DOI": resolve.normalize_doi(doi), "title": [WORKED_TITLE]}

    monkeypatch.setattr(resolve, "lookup_semantic_scholar_id", fake_s2)
    monkeypatch.setattr(resolve, "lookup_crossref_doi", fake_crossref)
    return calls


@pytest.fixture()
def ws(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mock_api_lookups: dict[str, list[str]],
) -> dict[str, Path]:
    """Mini workspace + memory root + initialized/scanned DB, via the production CLI."""
    root = tmp_path / "ws"
    (root / ".claude" / "rules").mkdir(parents=True)
    (root / ".claude" / "rules" / "subagent-economy.md").write_text(
        "\n".join(RULE_LINES) + "\n", encoding="utf-8"
    )
    (root / "docs" / "investigations").mkdir(parents=True)
    (root / LEVER_DOC).write_text(LEVER_DOC_TEXT, encoding="utf-8")
    memory_root = tmp_path / "memory-root"
    memory_root.mkdir()
    db_path = tmp_path / "data" / "citation.db"
    assert main(["init-db", "--db", str(db_path)]) == 0
    assert (
        main(
            [
                "scan",
                "--db",
                str(db_path),
                "--workspace-root",
                str(root),
                "--memory-root",
                str(memory_root),
            ]
        )
        == 0
    )
    # Seed a valid calibration so `review open`'s hard gate passes for the fixture
    # workspace (mirrors a passed `cite calibrate commit`; the check still runs).
    write_valid_calibration_fingerprint(db_path)
    capsys.readouterr()  # drop setup output; tests read their own verb's output
    return {
        "root": root,
        "db": db_path,
        "memory": memory_root,
        "breakdowns": tmp_path / "breakdowns",
    }


def _open(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    path: str = RULE_PATH,
    reviewer_model: str = "claude-sonnet-5",
) -> dict[str, Any]:
    # Re-seed a currently-valid fingerprint: corpus fingerprint C changes as tests
    # commit citations, and by design corpus growth invalidates calibration — a
    # re-open mid-test mirrors the real flow's recalibrate-then-open.
    write_valid_calibration_fingerprint(ws["db"], model_id=reviewer_model)
    capsys.readouterr()  # drop any buffered output — stdout must parse as pure JSON
    assert (
        main(
            [
                "review",
                "open",
                path,
                "--reviewer-model",
                reviewer_model,
                "--db",
                str(ws["db"]),
                "--workspace-root",
                str(ws["root"]),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    data = json.loads(out)  # stdout is the machine contract — pure JSON
    assert isinstance(data, dict)
    return data


def _stdin_bytes(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    """Install a bytes-backed stdin (with a real ``.buffer``) — the production shape."""
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))


def _commit(
    ws: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    run_id: int | None,
    *,
    extra_args: list[str] | None = None,
) -> int:
    _stdin_bytes(monkeypatch, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    args = [
        "review",
        "commit",
        "--db",
        str(ws["db"]),
        "--workspace-root",
        str(ws["root"]),
        "--memory-root",
        str(ws["memory"]),
        "--breakdowns-root",
        str(ws["breakdowns"]),
    ]
    if run_id is not None:
        args += ["--run", str(run_id)]
    if extra_args:
        args += extra_args
    return main(args)


def _connect(ws: dict[str, Path]) -> sqlite3.Connection:
    return db.connect(ws["db"])


def _one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    return conn.execute(sql, params).fetchone()


def _count(conn: sqlite3.Connection, table: str) -> int:
    # Table names come from test literals, never user input.
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def register_artifact(
    ws: dict[str, Path],
    path: str,
    artifact_type: str = "plan",
    project: str = "coding-root",
) -> None:
    """Directly register an artifact row (mirrors ``cite scan``'s outcome) so review
    open/commit can run against paths the discover walk would not pick up."""
    conn = _connect(ws)
    try:
        conn.execute(
            "INSERT INTO artifacts (path, artifact_type, project, current_content_hash, "
            "first_seen_at) VALUES (?, ?, ?, 'cafe', '2026-07-21T00:00:00Z')",
            (path, artifact_type, project),
        )
        conn.commit()
    finally:
        conn.close()
