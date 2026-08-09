"""Tracked CC0 seed corpus validation, idempotent import, and corpus-search tests."""

import json
from pathlib import Path

import pytest

from citation_needed import corpus, db, seed
from citation_needed.cli import main


def test_tracked_seed_has_only_documented_cc0_providers_and_bibliographic_fields() -> None:
    rows = seed.load_seed()

    assert {row.provider for row in rows} == {"crossref", "openalex"}
    assert all("semanticscholar" not in row.source_url.lower() for row in rows)
    assert all("abstract" not in row.keywords.lower() for row in rows)
    assert any(row.title.startswith("Lost in the Middle") for row in rows)
    provenance = (seed.PROJECT_ROOT / "seed" / "PROVENANCE.md").read_text(encoding="utf-8")
    assert "Crossref" in provenance
    assert "OpenAlex" in provenance
    assert "Semantic Scholar" in provenance


def test_seed_import_is_idempotent_and_fts_finds_lost_in_the_middle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "citation.db"
    assert main(["init-db", "--db", str(db_path)]) == 0
    capsys.readouterr()

    assert main(["seed", "import", "--db", str(db_path)]) == 0
    assert "4 processed, 4 new, 0 existing" in capsys.readouterr().out
    assert main(["seed", "import", "--db", str(db_path)]) == 0
    assert "4 processed, 0 new, 4 existing" in capsys.readouterr().out

    conn = db.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM citations").fetchone()
        assert count is not None and count[0] == 4
        hits = corpus.corpus_search(conn, "lost in the middle")
        assert len(hits) == 1
        assert hits[0].title is not None and hits[0].title.startswith("Lost in the Middle")
    finally:
        conn.close()


def test_seed_import_cli_fails_loudly_for_missing_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["seed", "import", "--db", str(tmp_path / "missing.db")]) == 1
    assert "init-db" in capsys.readouterr().out


def test_seed_loader_rejects_semantic_scholar_and_duplicate_dois(tmp_path: Path) -> None:
    row = {
        "authors": "Ada Example",
        "doi": "10.1000/example",
        "keywords": "example metadata",
        "provider": "semantic_scholar",
        "provider_record": "paper-id",
        "schema_version": 1,
        "source_url": "https://api.semanticscholar.org/graph/v1/paper/paper-id",
        "title": "Example",
        "venue": "Example Venue",
        "year": 2020,
    }
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(seed.SeedError, match="provider"):
        seed.load_seed(path)

    row["provider"] = "crossref"
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(seed.SeedError, match="duplicates DOI"):
        seed.load_seed(path)


def test_seed_import_uses_the_existing_citation_writer(tmp_path: Path) -> None:
    """The imported row has a structured resolution record and the schema's FTS trigger fires."""
    db_path = tmp_path / "citation.db"
    assert db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        result = seed.import_seed(conn)
        assert result.inserted == 4
        row = conn.execute(
            "SELECT resolution_method, supporting_quote FROM citations WHERE natural_key = ?",
            ("10.48550/arxiv.2307.03172",),
        ).fetchone()
        assert row is not None
        assert row[0] == "api_structured"
        echo = json.loads(str(row[1]))
        assert echo["provider"] == "openalex"
    finally:
        conn.close()
