"""corpus.py — FTS5 corpus-first lookup round-trip + safe query construction + CLI.

Citations enter through the production writer (``verify.insert_citation`` — the FTS5
index syncs via the schema triggers), then :func:`corpus_search` must find them; FTS5
operators in user input must neither crash nor inject query syntax. The CLI tests
invoke the production entry (``cli.main``), never internals.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from citation_needed import corpus, db, verify
from citation_needed.cli import main


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "cite.db"
    assert main(["init-db", "--db", str(path)]) == 0
    return path


@pytest.fixture()
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(db_path)
    yield connection
    connection.close()


def _seed_citation(
    conn: sqlite3.Connection,
    *,
    title: str,
    doi: str,
    keywords: str | None = None,
) -> int:
    """Insert through the PRODUCTION writer so the FTS5 sync triggers are exercised."""
    citation_id, _created = verify.insert_citation(
        conn,
        kind="external",
        resolution_method="api_structured",
        title=title,
        doi=doi,
        api_echo={"title": title, "doi": doi},
        keywords=keywords,
    )
    conn.commit()
    return citation_id


# ---------------------------------------------------------------------------
# Round trip + ranking
# ---------------------------------------------------------------------------


def test_insert_then_corpus_search_round_trip(conn: sqlite3.Connection) -> None:
    citation_id = _seed_citation(
        conn,
        title="Lost in the Middle: How Language Models Use Long Contexts",
        doi="10.48550/arxiv.2307.03172",
        keywords="long-context retrieval position",
    )
    hits = corpus.corpus_search(conn, "how language models use long contexts")
    assert [hit.citation_id for hit in hits] == [citation_id]
    hit = hits[0]
    assert hit.natural_key == "10.48550/arxiv.2307.03172"
    assert hit.title is not None and hit.title.startswith("Lost in the Middle")
    assert isinstance(hit.score, float)


def test_bm25_ranks_denser_match_first(conn: sqlite3.Connection) -> None:
    strong = _seed_citation(
        conn,
        title="Prompt injection attacks against LLM security boundaries",
        doi="10.1/strong",
        keywords="prompt injection security",
    )
    _seed_citation(
        conn,
        title="A survey of unrelated retrieval benchmarks",
        doi="10.1/weak",
        keywords="security",
    )
    hits = corpus.corpus_search(conn, "prompt injection security")
    assert hits[0].citation_id == strong
    assert [hit.score for hit in hits] == sorted(hit.score for hit in hits)  # best first


def test_limit_bounds_results(conn: sqlite3.Connection) -> None:
    for index in range(5):
        _seed_citation(conn, title=f"Retrieval paper {index}", doi=f"10.1/r{index}")
    assert len(corpus.corpus_search(conn, "retrieval paper", limit=3)) == 3
    with pytest.raises(ValueError, match="limit"):
        corpus.corpus_search(conn, "retrieval paper", limit=0)


def test_category_filter_constrains_to_keywords(conn: sqlite3.Connection) -> None:
    security_id = _seed_citation(
        conn,
        title="Verifying quotes against fetched pages",
        doi="10.1/sec",
        keywords="security verification",
    )
    _seed_citation(
        conn,
        title="Verifying benchmark instruments end to end",
        doi="10.1/bench",
        keywords="measurement validity",
    )
    hits = corpus.corpus_search(conn, "verifying", category="security")
    assert [hit.citation_id for hit in hits] == [security_id]


# ---------------------------------------------------------------------------
# Safe query construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        'NEAR("a" "b", 3)',
        '"unbalanced quote',
        "(parens OR AND NOT))",
        "col : filter^ * -minus",
        "title:injection",
    ],
)
def test_fts5_operators_in_input_never_crash(conn: sqlite3.Connection, hostile: str) -> None:
    _seed_citation(conn, title="Safe query construction", doi="10.1/safe")
    hits = corpus.corpus_search(conn, hostile)  # must not raise, whatever it returns
    assert isinstance(hits, list)


def test_no_salient_terms_returns_empty_not_error(conn: sqlite3.Connection) -> None:
    assert corpus.corpus_search(conn, "of the and to !!! ??") == []


def test_extract_terms_drops_operators_and_stopwords() -> None:
    terms = corpus.extract_terms('NEAR("prompt" injection) AND the OR of')
    assert "near" in terms  # plain word now, quoted in the MATCH string
    assert "prompt" in terms
    assert "injection" in terms
    assert "the" not in terms
    assert "of" not in terms


def test_build_match_query_quotes_every_term() -> None:
    query = corpus.build_match_query(["prompt", "injection"], category="security")
    assert query == '("prompt" OR "injection") AND (keywords : ("security"))'


# ---------------------------------------------------------------------------
# CLI integration (production entry point)
# ---------------------------------------------------------------------------


def test_cli_corpus_search_end_to_end(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    conn = db.connect(db_path)
    try:
        _seed_citation(
            conn,
            title="Lost in the Middle: How Language Models Use Long Contexts",
            doi="10.48550/arxiv.2307.03172",
            keywords="long-context",
        )
    finally:
        conn.close()
    exit_code = main(["corpus-search", "language models long contexts", "--db", str(db_path)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 corpus hit(s)" in out
    assert "10.48550/arxiv.2307.03172" in out
    assert "Lost in the Middle" in out


def test_cli_corpus_search_no_hits(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["corpus-search", "nothing seeded yet", "--db", str(db_path)])
    assert exit_code == 0
    assert "No corpus hits." in capsys.readouterr().out


def test_cli_corpus_search_hostile_input_is_safe(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["corpus-search", 'NEAR("a" (b, 3) "', "--db", str(db_path)])
    assert exit_code == 0  # never an FTS5 syntax crash


def test_cli_corpus_search_missing_db_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["corpus-search", "anything", "--db", str(tmp_path / "missing.db")])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert out.startswith("error: database does not exist")


def test_cli_corpus_search_bad_limit_is_clean_error(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["corpus-search", "anything", "--limit", "0", "--db", str(db_path)])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert out.startswith("error:")
