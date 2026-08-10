"""Step-7 seed corpus + `cite seed import` — the tracked file and its offline import.

Acceptance (plan Step 7 done-when), all driven through the production CLI (`cli.main`):
fresh DB + import twice -> no duplicates; FTS5 corpus-search "lost in the middle" hits
the seeded Liu et al. TACL row; PROVENANCE.md lists every source_api present in the
JSONL; zero S2-attributed rows anywhere; every row passes schema validation; rows are
deterministically ordered; imported rows satisfy the citations CHECK constraints
(kind='external' + url_or_doi present). Offline by construction: `seed import` performs
no network I/O — the live re-derivation happened at seed-BUILD time (the trust boundary
seed/PROVENANCE.md documents).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from citation_needed import db, seed, verify
from citation_needed.cli import main
from citation_needed.resolve import normalize_doi

SEED_FILE = db.PROJECT_ROOT / "seed" / "seed_citations.jsonl"
PROVENANCE = db.PROJECT_ROOT / "seed" / "PROVENANCE.md"

#: The Step-7 acceptance row: Liu et al. published-venue (TACL 2024) record — the
#: arXiv:2307.03172 paper's Crossref-registered DOI.
LIU_NATURAL_KEY = "10.1162/tacl_a_00638"
LIU_TITLE = "Lost in the Middle: How Language Models Use Long Contexts"


def _raw_rows() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in SEED_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _fresh_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "data" / "citation.db"
    assert main(["init-db", "--db", str(db_path)]) == 0
    return db_path


def _import(db_path: Path) -> None:
    """Run the canonical-file import through the production CLI; assert it succeeds."""
    assert main(["seed", "import", "--db", str(db_path)]) == 0


def _citations(db_path: Path) -> list[sqlite3.Row]:
    conn = db.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM citations ORDER BY natural_key").fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The tracked file itself (reviewed-committed data)
# ---------------------------------------------------------------------------


def test_seed_file_schema_valid() -> None:
    """Every row parses, carries exactly the required fields, and self-agrees."""
    rows = seed.load_seed_rows(SEED_FILE)  # whole-file validation is the loader's job
    assert rows, "seed file must not be empty"
    for raw in _raw_rows():
        assert set(raw) == set(seed.REQUIRED_KEYS)
        assert raw["kind"] == "external"
        assert raw["resolution_method"] == "api_structured"
        assert isinstance(raw["title"], str) and raw["title"].strip()
        assert isinstance(raw["year"], int)
        assert isinstance(raw["authors"], list) and raw["authors"]
        assert normalize_doi(raw["url_or_doi"]) == raw["natural_key"]
        # No abstracts, ever — Crossref's CC0 grant excludes them (public-boundary §b);
        # the schema has no such field and no row may smuggle one in.
        assert "abstract" not in {key.lower() for key in raw}


def test_seed_file_deterministic_ordering() -> None:
    keys = [raw["natural_key"] for raw in _raw_rows()]
    assert keys == sorted(keys), "rows must be sorted by natural_key (clean diffs)"
    assert len(keys) == len(set(keys)), "natural_key must be unique within the file"


def test_seed_file_zero_s2_attributed_rows() -> None:
    """Plan D8: Semantic Scholar is live-lookup-only; no S2-attributed row may ship."""
    for raw in _raw_rows():
        assert raw["source_api"] in seed.ALLOWED_SOURCE_APIS
        assert raw["source_api"] not in ("semantic_scholar", "s2")
    assert "semantic_scholar" not in seed.ALLOWED_SOURCE_APIS
    assert "s2" not in seed.ALLOWED_SOURCE_APIS


def test_seed_contains_the_liu_lost_in_the_middle_row() -> None:
    by_key = {raw["natural_key"]: raw for raw in _raw_rows()}
    liu = by_key[LIU_NATURAL_KEY]
    assert liu["title"] == LIU_TITLE
    assert any("Liu" in author for author in liu["authors"])
    assert liu["source_api"] == "crossref"


def test_provenance_lists_every_source_api_and_license_basis() -> None:
    text = PROVENANCE.read_text(encoding="utf-8")
    for raw in _raw_rows():
        assert raw["source_api"].lower() in text.lower(), (
            f"PROVENANCE.md must name source_api {raw['source_api']!r}"
        )
    # License basis + the exclusion rationale must be stated, not implied.
    assert "CC0" in text
    assert "MIT" in text
    assert "Semantic Scholar" in text
    assert "abstract" in text.lower()  # the no-abstracts policy


# ---------------------------------------------------------------------------
# `cite seed import` — idempotent, offline, through the anti-fabrication gate
# ---------------------------------------------------------------------------


def test_import_twice_no_duplicates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = _fresh_db(tmp_path)
    expected = len(_raw_rows())

    _import(db_path)
    first_out = capsys.readouterr().out
    assert f"Imported {expected} new citation(s); skipped 0" in first_out
    assert len(_citations(db_path)) == expected

    _import(db_path)
    second_out = capsys.readouterr().out
    assert f"Imported 0 new citation(s); skipped {expected}" in second_out
    assert len(_citations(db_path)) == expected  # count identical — zero duplicates


def test_second_import_leaves_rows_untouched(tmp_path: Path) -> None:
    """A skipped row is untouched: verified_at is NOT refreshed (import re-verified
    nothing, so it must not claim a fresh verification)."""
    db_path = _fresh_db(tmp_path)
    _import(db_path)
    before = {(row["natural_key"], row["verified_at"]) for row in _citations(db_path)}
    _import(db_path)
    after = {(row["natural_key"], row["verified_at"]) for row in _citations(db_path)}
    assert before == after


def test_import_skips_a_real_review_row_completely_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Collision with a RICHER row from a real review (different resolution_method,
    authors, venue, notes) at a seed natural_key: the seed import must count it skipped,
    create no duplicate, and leave EVERY column (verified_at included) byte-identical —
    weaker seed data can never clobber already-verified review provenance."""
    db_path = _fresh_db(tmp_path)
    fetched = "Long contexts: performance is often highest at the beginning or end."
    conn = db.connect(db_path)
    try:
        with conn:
            citation_id, created = verify.insert_citation(
                conn,
                kind="external",
                resolution_method="web_fetch_verified",
                title="Lost in the Middle (review-committed record)",
                doi=LIU_NATURAL_KEY,
                supporting_quote="performance is often highest",
                fetch_result=verify.FetchResult(
                    final_url="https://example.org/liu2024",
                    fetched_text=fetched,
                    fetch_time="2026-07-01T00:00:00Z",
                    hops=0,
                ),
                authors="Liu, Nelson F.; and colleagues (review formatting)",
                venue="TACL (verified page)",
                keywords="review-keywords",
                notes="Committed by a real review run — richer than any seed row.",
            )
        assert created is True
        conn.row_factory = sqlite3.Row
        before = dict(
            conn.execute("SELECT * FROM citations WHERE id = ?", (citation_id,)).fetchone()
        )
    finally:
        conn.close()

    capsys.readouterr()
    _import(db_path)
    out = capsys.readouterr().out
    expected_new = len(_raw_rows()) - 1
    assert f"Imported {expected_new} new citation(s); skipped 1" in out
    assert f"{LIU_NATURAL_KEY} skipped" in out

    rows = _citations(db_path)
    assert len(rows) == len(_raw_rows())  # no duplicate row for the shared natural_key
    after = {row["natural_key"]: dict(row) for row in rows}[LIU_NATURAL_KEY]
    assert after == before  # all 15 columns identical — the review row was never touched


def test_concurrent_seed_imports_are_race_free(tmp_path: Path) -> None:
    """Two threads import the same seed at the same instant (the Step-3 Barrier shape).

    The pre-fix SELECT-then-insert pre-check misclassified the loser's rows; the atomic
    upsert (refresh_on_conflict=False) must give one thread all-imported and the other
    all-skipped — no exception, no duplicate, accurate counts on both sides."""
    db_path = _fresh_db(tmp_path)
    rows = seed.load_seed_rows(SEED_FILE)
    note = seed.seed_provenance_note(SEED_FILE, trusted=True)
    barrier = threading.Barrier(2)
    results: list[seed.ImportResult] = []
    errors: list[Exception] = []

    def worker() -> None:
        connection = db.connect(db_path)
        try:
            barrier.wait(timeout=10)
            with connection:
                results.append(seed.import_seed(connection, rows, provenance_note=note))
        except Exception as exc:  # the assertion below IS "no exception escaped"
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == [], f"concurrent seed import raised: {errors!r}"
    total = len(rows)
    # WAL serializes the two write transactions: one imports everything, the other
    # skips everything — and each side's counts come from the atomic upsert, not a
    # stale pre-check read.
    assert sorted((r.imported, r.skipped) for r in results) == [(0, total), (total, 0)]
    assert len(_citations(db_path)) == total


def test_canonical_import_notes_pin_file_and_sha256(tmp_path: Path) -> None:
    """Trusted-path audit trail: every imported row's notes name the canonical file and
    pin its sha256 at import time, so WHICH seed data version was imported is on record."""
    db_path = _fresh_db(tmp_path)
    _import(db_path)
    digest = hashlib.sha256(SEED_FILE.read_bytes()).hexdigest()
    rows = _citations(db_path)
    assert rows
    for row in rows:
        assert "seed/seed_citations.jsonl" in row["notes"]
        assert f"sha256={digest}" in row["notes"]
        assert "Re-derived live at seed-build time" in row["notes"]


def test_corpus_search_hits_the_seeded_liu_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """FTS5 acceptance: `cite corpus-search "lost in the middle"` finds the seeded row."""
    db_path = _fresh_db(tmp_path)
    _import(db_path)
    capsys.readouterr()
    assert main(["corpus-search", "lost in the middle", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert LIU_NATURAL_KEY in out
    assert LIU_TITLE in out


def test_import_respects_citations_check_constraints(tmp_path: Path) -> None:
    """Every imported row satisfies the trust-critical CHECKs and the gate's contract."""
    db_path = _fresh_db(tmp_path)
    _import(db_path)
    rows = _citations(db_path)
    assert rows
    for row in rows:
        assert row["kind"] == "external"
        assert row["url_or_doi"] is not None  # the DB CHECK, observed
        assert row["url_or_doi"] == f"https://doi.org/{row['natural_key']}"
        assert row["resolution_method"] == "api_structured"
        assert row["verified_at"]  # pipeline-clock stamp, NOT NULL
        echo = json.loads(row["supporting_quote"])  # the stored resolution record
        assert echo["source_api"] in seed.ALLOWED_SOURCE_APIS
        assert row["workspace_path"] is None  # external rows carry no internal locator


def test_db_has_zero_s2_attributed_rows_after_import(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    _import(db_path)
    for row in _citations(db_path):
        echo = json.loads(row["supporting_quote"])
        assert echo["source_api"] not in ("semantic_scholar", "s2")


# ---------------------------------------------------------------------------
# Whole-file reject: a malformed seed imports NOTHING
# ---------------------------------------------------------------------------


def _valid_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "natural_key": "10.1000/example.1",
        "kind": "external",
        "title": "An Example Paper",
        "year": 2024,
        "venue": "Example Proceedings",
        "authors": ["Ada Example"],
        "url_or_doi": "https://doi.org/10.1000/example.1",
        "resolution_method": "api_structured",
        "source_api": "crossref",
        "category": "example-category",
        "retrieved_at": "2026-07-22T00:00:00Z",
    }
    row.update(overrides)
    return row


def _write_seed(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    return path


def test_loader_rejects_s2_source_api(tmp_path: Path) -> None:
    bad = _write_seed(tmp_path / "bad.jsonl", [_valid_row(source_api="semantic_scholar")])
    with pytest.raises(seed.SeedError, match="source_api"):
        seed.load_seed_rows(bad)


def test_loader_rejects_unsorted_rows(tmp_path: Path) -> None:
    second = _valid_row(
        natural_key="10.1000/example.0", url_or_doi="https://doi.org/10.1000/example.0"
    )
    bad = _write_seed(tmp_path / "bad.jsonl", [_valid_row(), second])
    with pytest.raises(seed.SeedError, match="sorted"):
        seed.load_seed_rows(bad)


def test_loader_rejects_natural_key_url_mismatch(tmp_path: Path) -> None:
    bad = _write_seed(
        tmp_path / "bad.jsonl", [_valid_row(url_or_doi="https://doi.org/10.9999/other")]
    )
    with pytest.raises(seed.SeedError, match="does not normalize"):
        seed.load_seed_rows(bad)


def test_loader_rejects_missing_and_unknown_keys(tmp_path: Path) -> None:
    missing = _valid_row()
    del missing["venue"]
    with pytest.raises(seed.SeedError, match="key set mismatch"):
        seed.load_seed_rows(_write_seed(tmp_path / "missing.jsonl", [missing]))
    with pytest.raises(seed.SeedError, match="key set mismatch"):
        seed.load_seed_rows(
            _write_seed(tmp_path / "unknown.jsonl", [_valid_row(abstract="smuggled text")])
        )


def test_loader_rejects_non_object_json_line(tmp_path: Path) -> None:
    """Valid JSON that is not an object (an array line) is malformed input, typed."""
    bad = tmp_path / "bad.jsonl"
    bad.write_text('["not", "an", "object"]\n', encoding="utf-8")
    with pytest.raises(seed.SeedError, match="not a JSON object"):
        seed.load_seed_rows(bad)


def test_loader_rejects_blank_line(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps(_valid_row(), sort_keys=True) + "\n\n", encoding="utf-8")
    with pytest.raises(seed.SeedError, match="blank line"):
        seed.load_seed_rows(bad)


def test_loader_rejects_invalid_json_syntax(tmp_path: Path) -> None:
    """Truncated/garbage JSON raises the typed SeedError, never a bare JSONDecodeError."""
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"natural_key": "10.1000/example.1", truncated\n', encoding="utf-8")
    with pytest.raises(seed.SeedError, match="not valid JSON"):
        seed.load_seed_rows(bad)


def test_cli_rejects_malformed_seed_file_and_imports_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = _fresh_db(tmp_path)
    bad = _write_seed(
        tmp_path / "bad.jsonl", [_valid_row(), _valid_row(source_api="semantic_scholar")]
    )
    # Second row is invalid -> whole-file reject BEFORE any write (clean error contract).
    # --allow-untracked so the MALFORMED reject (not the untracked gate) is what fires.
    capsys.readouterr()
    args = ["seed", "import", "--db", str(db_path), "--seed-file", str(bad), "--allow-untracked"]
    assert main(args) == 1
    out = capsys.readouterr().out
    assert "error:" in out and "seed row 2" in out
    assert len(_citations(db_path)) == 0


def test_cli_errors_cleanly_on_missing_seed_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = _fresh_db(tmp_path)
    capsys.readouterr()
    missing = tmp_path / "nope.jsonl"
    args = ["seed", "import", "--db", str(db_path), "--seed-file", str(missing)]
    assert main([*args, "--allow-untracked"]) == 1
    out = capsys.readouterr().out
    assert "error:" in out and "does not exist" in out


def test_cli_errors_cleanly_without_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No DB yet -> the standard init-db hint and exit 1, never a traceback."""
    capsys.readouterr()
    assert main(["seed", "import", "--db", str(tmp_path / "absent" / "no.db")]) == 1
    assert "init-db" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Untracked --seed-file: the honest trust boundary (bugs-review HIGH finding)
# ---------------------------------------------------------------------------


def test_untracked_seed_file_refused_without_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fabricated (non-canonical) --seed-file without --allow-untracked: refused
    loudly, exit 1, NOTHING written — seed-verified provenance is not claimable for
    arbitrary files."""
    db_path = _fresh_db(tmp_path)
    fabricated = _write_seed(tmp_path / "fabricated.jsonl", [_valid_row()])
    capsys.readouterr()
    assert main(["seed", "import", "--db", str(db_path), "--seed-file", str(fabricated)]) == 1
    out = capsys.readouterr().out
    assert "error:" in out and "--allow-untracked" in out
    assert "not the tracked seed corpus" in out
    assert len(_citations(db_path)) == 0


def test_untracked_import_records_untracked_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With --allow-untracked the import proceeds, but every row's notes name the actual
    source file + the untracked wording — NEVER the re-derived-at-seed-build claim."""
    db_path = _fresh_db(tmp_path)
    fabricated = _write_seed(tmp_path / "fabricated.jsonl", [_valid_row()])
    capsys.readouterr()
    args = ["seed", "import", "--db", str(db_path), "--seed-file", str(fabricated)]
    assert main([*args, "--allow-untracked"]) == 0
    out = capsys.readouterr().out
    assert seed.UNTRACKED_NOTE in out  # the closing Note line states it too
    rows = _citations(db_path)
    assert len(rows) == 1
    notes = rows[0]["notes"]
    assert fabricated.as_posix() in notes  # the actual source file is named
    assert "untracked seed source — verification provenance not established" in notes
    assert "Re-derived live at seed-build time" not in notes


def test_is_canonical_seed_file_is_path_equality_after_resolve(tmp_path: Path) -> None:
    """Unit contract for the gate: the canonical file (however spelled) is trusted;
    any other path — even a byte-identical copy — is not."""
    dotted = SEED_FILE.parent / ".." / "seed" / "seed_citations.jsonl"
    assert seed.is_canonical_seed_file(SEED_FILE)
    assert seed.is_canonical_seed_file(dotted)  # same file via a non-normalized spelling
    copy = tmp_path / "seed_citations.jsonl"
    copy.write_bytes(SEED_FILE.read_bytes())
    assert not seed.is_canonical_seed_file(copy)  # identical bytes, wrong provenance


def test_verb_help_documents_the_trust_boundary(capsys: pytest.CaptureFixture[str]) -> None:
    """The verb help must carry the trust boundary: verified at seed-BUILD time; the
    offline import trusts the tracked, reviewed-committed file."""
    with pytest.raises(SystemExit) as excinfo:
        main(["seed", "import", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "BUILD time" in out
    assert "trusts the tracked file" in out
    assert "PROVENANCE.md" in out
