"""CLI-level Overview v1 JSON contract coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from citation_needed import db
from citation_needed.cli import main


def test_overview_json_for_absent_database_reports_uninitialized_not_zeroes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.db"

    assert main(["overview", "--json", "--db", str(missing)]) == 0

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["schema_version"] == 1
    assert payload["state"] == "uninitialized"
    assert payload["counts"] is None
    assert payload["recent_activity"] is None


def test_overview_json_for_initialized_empty_database_reports_real_zeroes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "citation.db"
    assert db.init_db(path)

    assert main(["overview", "--json", "--db", str(path)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "initialized-empty"
    assert payload["counts"]["artifacts"] == 0
    assert payload["counts"]["open_distill_queue"] == 0
    assert payload["recent_activity"] == []


def test_justify_list_json_is_a_versioned_empty_result_for_initialized_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "citation.db"
    assert db.init_db(path)

    assert main(["justify", "list", "--type", "skill", "--json", "--db", str(path)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"artifact_type": "skill", "items": [], "schema_version": 1}


def test_justify_show_errors_cleanly_for_unknown_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "citation.db"
    assert db.init_db(path)

    assert main(["justify", "show", "999", "--json", "--db", str(path)]) == 1

    assert "artifact id 999 does not exist" in capsys.readouterr().out


def test_observatory_export_cli_writes_the_paired_v1_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "citation.db"
    output_dir = tmp_path / "observatory"
    assert db.init_db(path)

    assert (
        main(
            [
                "observatory-export",
                "--db",
                str(path),
                "--out",
                str(output_dir),
                "--workspace-root",
                str(tmp_path),
            ]
        )
        == 0
    )

    assert (output_dir / "citation-overview.v1.json").is_file()
    assert (output_dir / "citation-justifications.v1.json").is_file()
    assert "Wrote" in capsys.readouterr().out
