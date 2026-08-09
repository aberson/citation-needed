"""Bounded read-only ``observatory.v1`` producer artifact coverage."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from citation_needed import db, observatory_export
from citation_needed.cli import main
from conftest import RULE_QUOTE, _commit, _open, worked_payload, write_valid_calibration_fingerprint

_FIXTURES = Path(__file__).parent / "fixtures" / "observatory_export"
_STAMP = datetime(2026, 8, 9, tzinfo=UTC)


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in db.CANONICAL_TABLES
    }


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _assert_generic_summary_contract(payload: dict[str, object]) -> None:
    assert set(payload) == {"schema", "generated_at", "stats", "recent"}
    assert payload["schema"] == observatory_export.SCHEMA
    assert isinstance(payload["stats"], dict)
    assert isinstance(payload["recent"], list)


def _assert_generic_explorer_contract(payload: dict[str, object]) -> None:
    assert set(payload) == {"schema", "generated_at", "items"}
    assert payload["schema"] == observatory_export.SCHEMA
    items = payload["items"]
    assert isinstance(items, list)
    for item in items:
        assert isinstance(item, dict)
        assert set(item) == {"id", "label", "summary", "detail"}
        assert isinstance(item["id"], str) and item["id"].startswith("artifact-")
        assert isinstance(item["detail"], dict)


def test_initialized_empty_export_matches_versioned_contract_fixtures(tmp_path: Path) -> None:
    db_path = tmp_path / "citation.db"
    assert db.init_db(db_path)

    result = observatory_export.export_observatory_artifacts(
        db_path, tmp_path / "out", workspace_root=tmp_path, now=_STAMP
    )

    overview = _load_json(result.overview_path)
    justifications = _load_json(result.justifications_path)
    assert overview == _load_json(_FIXTURES / "empty-overview.v1.json")
    assert justifications == _load_json(_FIXTURES / "empty-justifications.v1.json")
    _assert_generic_summary_contract(overview)
    _assert_generic_explorer_contract(justifications)
    assert result.reviewed_skills_total == 0
    assert result.justifications_exported == 0


def test_export_round_trips_production_reviewed_skill_without_database_writes(
    ws: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill_path = ".claude/skills/reviewable/SKILL.md"
    skill = ws["root"] / skill_path
    skill.parent.mkdir(parents=True)
    skill.write_text(f"# Reviewable skill\n\n{RULE_QUOTE}\n", encoding="utf-8")
    assert (
        main(
            [
                "scan",
                "--db",
                str(ws["db"]),
                "--workspace-root",
                str(ws["root"]),
                "--memory-root",
                str(ws["memory"]),
            ]
        )
        == 0
    )
    write_valid_calibration_fingerprint(ws["db"])
    capsys.readouterr()
    opened = _open(ws, capsys, path=skill_path)
    assert _commit(ws, monkeypatch, worked_payload(), opened["run_id"]) == 0
    capsys.readouterr()

    conn = db.connect(ws["db"])
    try:
        counts_before = _table_counts(conn)
    finally:
        conn.close()

    result = observatory_export.export_observatory_artifacts(
        ws["db"],
        tmp_path / "out",
        workspace_root=ws["root"],
        memory_root=ws["memory"],
        now=_STAMP,
    )

    conn = db.connect(ws["db"])
    try:
        assert _table_counts(conn) == counts_before
    finally:
        conn.close()
    overview = _load_json(result.overview_path)
    justifications = _load_json(result.justifications_path)
    _assert_generic_summary_contract(overview)
    _assert_generic_explorer_contract(justifications)
    stats = overview["stats"]
    assert isinstance(stats, dict)
    assert stats["reviewed_skills_total"] == 1
    assert stats["justifications_exported"] == 1
    items = justifications["items"]
    assert isinstance(items, list) and len(items) == 1
    detail = items[0]["detail"]
    assert isinstance(detail, dict)
    assert detail["choices_total"] == 1
    choices = detail["choices"]
    assert isinstance(choices, list) and len(choices) == 1
    citation_rows = choices[0]["citations"]
    assert isinstance(citation_rows, list) and len(citation_rows) == 2
    assert all(
        row["resolution_method"] in {"api_structured", "internal-read"} for row in citation_rows
    )


def test_export_refuses_missing_database_instead_of_fabricating_empty_artifacts(
    tmp_path: Path,
) -> None:
    with pytest.raises(observatory_export.ObservatoryExportError, match="does not exist"):
        observatory_export.export_observatory_artifacts(
            tmp_path / "missing.db", tmp_path / "out", workspace_root=tmp_path, now=_STAMP
        )
    assert not (tmp_path / "out").exists()
