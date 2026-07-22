"""Corpus-first lookup — FTS5 BM25 search over the verified citations corpus.

Every review checks the compounding corpus BEFORE any external call
(docs/research/citation-mechanics.md §e): :func:`corpus_search` runs an FTS5 ``MATCH``
over the ``citations_fts`` external-content index (schema.sql keeps it in sync with the
``citations`` table via triggers), ranked by SQLite's ``bm25()``.

Query construction is SAFE BY CONSTRUCTION: user input is never passed to FTS5 as-is.
Salient terms are extracted (alphanumeric tokens, stopwords dropped) and each term is
emitted as a double-quoted FTS5 string joined with ``OR`` — so FTS5 operators in the
input (``NEAR``, ``AND``, quotes, parens, column filters) can neither error nor inject
query syntax. Read-only: this module never writes any table.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

_TERM_RE = re.compile(r"[A-Za-z0-9]{2,}")

#: Minimal English stopword set — enough to keep OR-queries salient, deliberately small.
_STOPWORDS = frozenset(
    {
        "an", "and", "are", "as", "at", "be", "but", "by", "do", "for", "from", "how",
        "if", "in", "into", "is", "it", "its", "no", "not", "of", "on", "or", "over",
        "so", "than", "that", "the", "their", "then", "this", "to", "use", "via",
        "when", "with",
    }
)  # fmt: skip

#: Cap on extracted terms so a pasted paragraph stays a bounded OR-query.
_MAX_TERMS = 12


@dataclass(frozen=True)
class CorpusHit:
    """One BM25-ranked corpus row (``score``: SQLite ``bm25()`` — lower = better)."""

    citation_id: int
    title: str | None
    natural_key: str
    score: float


def extract_terms(text: str) -> list[str]:
    """Salient search terms: lowercased alphanumeric tokens, stopwords out, deduped,
    capped at :data:`_MAX_TERMS`. FTS5 operators/punctuation in the input never survive."""
    terms: list[str] = []
    for match in _TERM_RE.finditer(text):
        term = match.group(0).lower()
        if term in _STOPWORDS or term in terms:
            continue
        terms.append(term)
        if len(terms) == _MAX_TERMS:
            break
    return terms


def build_match_query(terms: list[str], category: str | None = None) -> str:
    """Compose the FTS5 MATCH string from ALREADY-EXTRACTED terms.

    Each term is a double-quoted FTS5 string (quotes cannot appear — extraction is
    alphanumeric-only, but embedded quotes would be doubled anyway), OR-joined so BM25
    ranks partial matches. A ``category`` adds an AND-ed ``keywords:`` column filter
    (categories live in the ``keywords`` column at insert time).
    """
    quoted = " OR ".join('"{}"'.format(term.replace('"', '""')) for term in terms)
    query = f"({quoted})"
    if category is not None:
        category_terms = extract_terms(category)
        if category_terms:
            category_quoted = " ".join(
                '"{}"'.format(term.replace('"', '""')) for term in category_terms
            )
            query = f"{query} AND (keywords : ({category_quoted}))"
    return query


def corpus_search(
    conn: sqlite3.Connection,
    query: str,
    category: str | None = None,
    limit: int = 10,
) -> list[CorpusHit]:
    """BM25-ranked FTS5 search over the citations corpus (read-only, corpus-first).

    Returns up to ``limit`` hits, best first (SQLite ``bm25()`` ascending — lower is
    better). A query yielding no salient terms returns ``[]`` — never an FTS5 error.
    Raises ``ValueError`` on a non-positive limit and lets ``sqlite3.Error`` surface
    (db raises -> cli catches).
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    terms = extract_terms(query)
    if not terms:
        return []
    match = build_match_query(terms, category)
    rows = conn.execute(
        "SELECT citations.id, citations.title, citations.natural_key, bm25(citations_fts) "
        "FROM citations_fts JOIN citations ON citations.id = citations_fts.rowid "
        "WHERE citations_fts MATCH ? "
        "ORDER BY bm25(citations_fts) LIMIT ?",
        (match, limit),
    ).fetchall()
    return [
        CorpusHit(
            citation_id=int(row[0]),
            title=row[1] if isinstance(row[1], str) else None,
            natural_key=str(row[2]),
            score=float(row[3]),
        )
        for row in rows
    ]


__all__ = ["CorpusHit", "build_match_query", "corpus_search", "extract_terms"]
