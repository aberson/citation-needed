"""Tracked CC0 seed-corpus loader and idempotent importer.

The seed is intentionally a small, redistributable bootstrap for corpus-first lookup, not a
replacement for the review-time verification path.  It contains bibliographic metadata only from
the providers documented in ``seed/PROVENANCE.md``: Crossref and OpenAlex.  In particular, it
contains no Semantic Scholar fields and no Crossref abstracts, whose rights are not covered by the
Crossref CC0 metadata grant.

Rows still enter SQLite exclusively through :func:`citation_needed.verify.insert_citation`, the
production citation writer.  Re-running an import therefore uses the existing natural-key upsert
and cannot create duplicate citations or bypass the resolution-record invariant.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from citation_needed import verify
from citation_needed.resolve import normalize_doi

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = PROJECT_ROOT / "seed" / "seed_citations.jsonl"
_SCHEMA_VERSION = 1
_ALLOWED_PROVIDERS = frozenset({"crossref", "openalex"})
_MAX_ROWS = 128
_MAX_LINE_BYTES = 16 * 1024
_REQUIRED_KEYS = frozenset(
    {
        "authors",
        "doi",
        "keywords",
        "provider",
        "provider_record",
        "schema_version",
        "source_url",
        "title",
        "venue",
        "year",
    }
)


class SeedError(ValueError):
    """Raised for malformed, unsafe, or internally inconsistent tracked seed data."""


@dataclass(frozen=True)
class SeedCitation:
    """One strict, bibliographic-only row of the tracked seed corpus."""

    provider: str
    provider_record: str
    source_url: str
    doi: str
    title: str
    authors: str
    year: int
    venue: str
    keywords: str


@dataclass(frozen=True)
class SeedImportResult:
    """Transparent result of one idempotent import attempt."""

    processed: int
    inserted: int
    existing: int


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SeedError(f"{label} must be a non-empty string")
    return value.strip()


def _parse_row(raw: object, line_number: int) -> SeedCitation:
    if not isinstance(raw, dict):
        raise SeedError(f"seed line {line_number} must be a JSON object")
    keys = frozenset(raw)
    if keys != _REQUIRED_KEYS:
        missing = sorted(_REQUIRED_KEYS - keys)
        unknown = sorted(keys - _REQUIRED_KEYS)
        raise SeedError(
            f"seed line {line_number} has wrong keys; missing={missing}, unknown={unknown}"
        )
    if raw["schema_version"] != _SCHEMA_VERSION:
        raise SeedError(
            f"seed line {line_number} has unsupported schema_version {raw['schema_version']!r}"
        )
    provider = _required_string(raw["provider"], f"seed line {line_number}.provider")
    if provider not in _ALLOWED_PROVIDERS:
        raise SeedError(
            f"seed line {line_number}.provider must be one of {sorted(_ALLOWED_PROVIDERS)}"
        )
    year = raw["year"]
    if not isinstance(year, int) or isinstance(year, bool) or not 1800 <= year <= 2100:
        raise SeedError(f"seed line {line_number}.year must be an integer in [1800, 2100]")
    doi = _required_string(raw["doi"], f"seed line {line_number}.doi")
    try:
        normalized_doi = normalize_doi(doi)
    except ValueError as exc:
        raise SeedError(f"seed line {line_number}.doi is invalid: {doi!r}") from exc
    return SeedCitation(
        provider=provider,
        provider_record=_required_string(
            raw["provider_record"], f"seed line {line_number}.provider_record"
        ),
        source_url=_required_string(raw["source_url"], f"seed line {line_number}.source_url"),
        doi=normalized_doi,
        title=_required_string(raw["title"], f"seed line {line_number}.title"),
        authors=_required_string(raw["authors"], f"seed line {line_number}.authors"),
        year=year,
        venue=_required_string(raw["venue"], f"seed line {line_number}.venue"),
        keywords=_required_string(raw["keywords"], f"seed line {line_number}.keywords"),
    )


def load_seed(path: str | Path = SEED_PATH) -> tuple[SeedCitation, ...]:
    """Load the strict JSONL seed, rejecting every malformed/duplicate row loudly."""
    file_path = Path(path)
    if not file_path.is_file():
        raise SeedError(f"seed corpus file not found: {str(path)!r}")
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SeedError(f"could not read seed corpus {str(path)!r}: {exc}") from exc
    if not lines:
        raise SeedError("seed corpus must contain at least one JSONL row")
    if len(lines) > _MAX_ROWS:
        raise SeedError(f"seed corpus has too many rows ({len(lines)} > {_MAX_ROWS})")

    rows: list[SeedCitation] = []
    seen_dois: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise SeedError(f"seed line {line_number} is blank; JSONL rows must be explicit")
        if len(line.encode("utf-8")) > _MAX_LINE_BYTES:
            raise SeedError(f"seed line {line_number} exceeds {_MAX_LINE_BYTES} byte limit")
        try:
            raw: Any = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SeedError(f"seed line {line_number} is not valid JSON: {exc}") from exc
        row = _parse_row(raw, line_number)
        if row.doi in seen_dois:
            raise SeedError(f"seed line {line_number} duplicates DOI {row.doi!r}")
        seen_dois.add(row.doi)
        rows.append(row)
    return tuple(rows)


def _api_echo(row: SeedCitation) -> dict[str, Any]:
    """Construct the stored structured-resolution record from a documented provider response."""
    return {
        "authors": row.authors,
        "doi": row.doi,
        "provider": row.provider,
        "provider_record": row.provider_record,
        "source_url": row.source_url,
        "title": row.title,
        "venue": row.venue,
        "year": row.year,
    }


def import_seed(conn: sqlite3.Connection, path: str | Path = SEED_PATH) -> SeedImportResult:
    """Import all seed rows via the sole production writer, without duplicating natural keys."""
    rows = load_seed(path)
    inserted = 0
    existing = 0
    with conn:
        for row in rows:
            natural_key = normalize_doi(row.doi)
            prior = conn.execute(
                "SELECT 1 FROM citations WHERE kind = 'external' AND natural_key = ?",
                (natural_key,),
            ).fetchone()
            verify.insert_citation(
                conn,
                kind="external",
                resolution_method="api_structured",
                title=row.title,
                doi=row.doi,
                api_echo=_api_echo(row),
                authors=row.authors,
                year=row.year,
                venue=row.venue,
                keywords=row.keywords,
                notes=(
                    f"Tracked CC0 seed metadata from {row.provider}; "
                    "see seed/PROVENANCE.md. No abstract stored."
                ),
            )
            if prior is None:
                inserted += 1
            else:
                existing += 1
    return SeedImportResult(processed=len(rows), inserted=inserted, existing=existing)


__all__ = [
    "SEED_PATH",
    "SeedCitation",
    "SeedError",
    "SeedImportResult",
    "import_seed",
    "load_seed",
]
