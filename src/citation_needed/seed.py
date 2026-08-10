"""Tracked seed-corpus import — the mechanics behind ``cite seed import`` (plan Step 7).

``seed/seed_citations.jsonl`` is the tracked, CC0-safe seed corpus: one JSON object per
line, sorted by ``natural_key`` so the file diffs cleanly. Every row was RE-DERIVED LIVE
at seed-BUILD time through the production structured-API clients
(``resolve.lookup_crossref_doi`` / ``resolve.search_crossref``; OpenAlex when keyed) from
the Phase-0 citations labeled ``verified`` in ``docs/research/choice-taxonomy-literature.md``
— stored fields come from the API's own response (title, year, venue, authors, DOI; NO
abstracts — Crossref's CC0 grant excludes them, ``docs/research/public-boundary.md`` §b),
and ``source_api`` is structurally restricted to CC0-safe providers: Semantic Scholar rows
can never enter the seed (plan D8; its Dataset License forbids redistribution).

TRUST BOUNDARY (documented here, in the verb help, and in ``seed/PROVENANCE.md``): the
import itself is OFFLINE. It validates the file's shape (whole-file reject on ANY invalid
row — nothing partial is imported from a malformed file) and then TRUSTS the tracked,
reviewed-committed contents: the ``api_structured`` echo stored on each imported citation
is the seed row's own recorded fields, not a fresh API response — verification happened at
seed-build time, not import time. That trust extends ONLY to the canonical tracked file
(:data:`DEFAULT_SEED_FILE`, compared by resolved path): its rows' notes name the file and
pin its sha256 at import time, so the audit trail records WHICH seed version was imported.
Any OTHER ``--seed-file`` path is an UNTRACKED source — the CLI refuses it without an
explicit ``--allow-untracked``, and even then the stored notes name the actual source file
and state that no verification provenance was established (never the re-derived claim).

Write discipline: citations are written ONLY through ``verify.insert_citation`` (the sole
writer / anti-fabrication gate). Dedup is the row's ``natural_key``: a row already in the
corpus is SKIPPED untouched — its ``verified_at`` is NOT refreshed, because an offline
import performs no re-verification and must not claim one (``refresh_on_conflict=False``
on the gate's atomic upsert — never a racy check-then-insert). A second import therefore
reports zero imported rows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from citation_needed import db, verify
from citation_needed.resolve import normalize_doi

#: The tracked seed corpus (reviewed-committed data; see the module docstring).
DEFAULT_SEED_FILE = db.PROJECT_ROOT / "seed" / "seed_citations.jsonl"

#: The provenance wording stored on rows imported from a non-canonical seed file — the
#: honest counterpart to the trusted-file claim (module docstring TRUST BOUNDARY).
UNTRACKED_NOTE = "untracked seed source — verification provenance not established by this import"

#: CC0-safe providers only (plan D8). 'semantic_scholar' is deliberately absent — its
#: Dataset License forbids redistribution, so S2 stays live-lookup-only and a seed row
#: attributing itself to S2 is rejected structurally, not by convention.
ALLOWED_SOURCE_APIS = ("crossref", "openalex")

#: The exact per-row key set (strict: unknown keys reject — the tracked file has a fixed,
#: reviewed schema; additions ship together with a code change here).
REQUIRED_KEYS = frozenset(
    {
        "natural_key",
        "kind",
        "title",
        "year",
        "venue",
        "authors",
        "url_or_doi",
        "resolution_method",
        "source_api",
        "category",
        "retrieved_at",
    }
)


class SeedError(RuntimeError):
    """The seed file is missing or malformed — the whole import refuses, nothing is written."""


@dataclass(frozen=True)
class SeedRow:
    """One validated seed row (fields as recorded from the structured API at build time)."""

    natural_key: str  # normalized DOI (resolve.normalize_doi form)
    title: str
    year: int
    venue: str | None
    authors: tuple[str, ...]
    url_or_doi: str
    source_api: str  # 'crossref' | 'openalex'
    category: str  # taxonomy category from choice-taxonomy-literature.md
    retrieved_at: str  # seed-build-time UTC timestamp of the live API call

    def api_echo(self) -> dict[str, Any]:
        """The row's recorded fields as the ``api_structured`` echo stored on insert.

        These fields were captured from the actual API response at seed-BUILD time; the
        offline import passes them through verbatim (the documented trust boundary).
        """
        return {
            "natural_key": self.natural_key,
            "kind": "external",
            "title": self.title,
            "year": self.year,
            "venue": self.venue,
            "authors": list(self.authors),
            "url_or_doi": self.url_or_doi,
            "resolution_method": "api_structured",
            "source_api": self.source_api,
            "category": self.category,
            "retrieved_at": self.retrieved_at,
        }


@dataclass(frozen=True)
class SeedOutcome:
    """Per-row import outcome: 'imported' (new) or 'skipped' (natural_key already present)."""

    natural_key: str
    outcome: str  # 'imported' | 'skipped'
    citation_id: int | None  # the new row's id when imported; None when skipped


@dataclass(frozen=True)
class ImportResult:
    imported: int
    skipped: int
    outcomes: tuple[SeedOutcome, ...]


def _row_error(line_no: int, message: str) -> SeedError:
    return SeedError(f"seed row {line_no}: {message} — whole file rejected, nothing imported")


def _validate_row(line_no: int, data: object) -> SeedRow:
    if not isinstance(data, dict):
        raise _row_error(line_no, "not a JSON object")
    keys = set(data)
    if keys != REQUIRED_KEYS:
        missing = sorted(REQUIRED_KEYS - keys)
        unknown = sorted(keys - REQUIRED_KEYS)
        raise _row_error(line_no, f"key set mismatch (missing {missing}, unknown {unknown})")
    if data["kind"] != "external":
        raise _row_error(line_no, f"kind must be 'external', got {data['kind']!r}")
    if data["resolution_method"] != "api_structured":
        raise _row_error(
            line_no,
            f"resolution_method must be 'api_structured', got {data['resolution_method']!r}",
        )
    source_api = data["source_api"]
    if source_api not in ALLOWED_SOURCE_APIS:
        raise _row_error(
            line_no,
            f"source_api {source_api!r} is not a CC0-safe seed provider "
            f"(allowed: {', '.join(ALLOWED_SOURCE_APIS)}; Semantic Scholar is live-lookup-only "
            "— its Dataset License forbids redistribution, plan D8)",
        )
    natural_key = data["natural_key"]
    if not isinstance(natural_key, str) or not natural_key.strip():
        raise _row_error(line_no, "natural_key must be a non-empty string")
    if natural_key != normalize_doi(natural_key):
        raise _row_error(line_no, f"natural_key {natural_key!r} is not in normalized-DOI form")
    url_or_doi = data["url_or_doi"]
    if not isinstance(url_or_doi, str) or normalize_doi(url_or_doi) != natural_key:
        raise _row_error(
            line_no,
            f"url_or_doi {url_or_doi!r} does not normalize to natural_key {natural_key!r}",
        )
    title = data["title"]
    if not isinstance(title, str) or not title.strip():
        raise _row_error(line_no, "title must be a non-empty string")
    year = data["year"]
    if not isinstance(year, int) or isinstance(year, bool):
        raise _row_error(line_no, f"year must be an integer, got {year!r}")
    venue = data["venue"]
    if venue is not None and (not isinstance(venue, str) or not venue.strip()):
        raise _row_error(line_no, "venue must be a non-empty string or null")
    authors_raw = data["authors"]
    if (
        not isinstance(authors_raw, list)
        or not authors_raw
        or not all(isinstance(a, str) and a.strip() for a in authors_raw)
    ):
        raise _row_error(line_no, "authors must be a non-empty list of non-empty strings")
    category = data["category"]
    if not isinstance(category, str) or not category.strip():
        raise _row_error(line_no, "category must be a non-empty string")
    retrieved_at = data["retrieved_at"]
    if not isinstance(retrieved_at, str) or not retrieved_at.strip():
        raise _row_error(line_no, "retrieved_at must be a non-empty string")
    return SeedRow(
        natural_key=natural_key,
        title=title,
        year=year,
        venue=venue,
        authors=tuple(authors_raw),
        url_or_doi=url_or_doi,
        source_api=source_api,
        category=category,
        retrieved_at=retrieved_at,
    )


def load_seed_rows(seed_file: str | Path = DEFAULT_SEED_FILE) -> list[SeedRow]:
    """Parse + validate the tracked seed file; ANY invalid row rejects the whole file.

    Enforces the reviewed schema per row (:data:`REQUIRED_KEYS`, CC0-safe ``source_api``,
    normalized-DOI consistency) and the file-level determinism contract: rows strictly
    ascending by ``natural_key`` (which also guarantees no duplicate keys — a duplicate
    would silently skip on a second import instead of failing loudly here).
    """
    path = Path(seed_file)
    if not path.is_file():
        raise SeedError(f"seed file does not exist: {path.as_posix()}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SeedError(f"cannot read seed file {path.as_posix()}: {exc}") from exc
    rows: list[SeedRow] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise _row_error(line_no, "blank line (the file is strictly one JSON object per line)")
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _row_error(line_no, f"not valid JSON: {exc}") from exc
        row = _validate_row(line_no, data)
        if rows and row.natural_key <= rows[-1].natural_key:
            raise _row_error(
                line_no,
                f"natural_key {row.natural_key!r} not strictly after "
                f"{rows[-1].natural_key!r} — rows must be sorted (deterministic diffs) "
                "and unique",
            )
        rows.append(row)
    if not rows:
        raise SeedError(f"seed file has no rows: {path.as_posix()}")
    return rows


def is_canonical_seed_file(seed_file: str | Path) -> bool:
    """True iff ``seed_file`` IS the tracked corpus (path equality after ``resolve()``).

    The trusted-provenance notes (seed-build-time verification claim) are reserved for
    the canonical file; every other path is an untracked source (module docstring
    TRUST BOUNDARY) and must go through the CLI's explicit ``--allow-untracked``.
    """
    try:
        return Path(seed_file).resolve() == DEFAULT_SEED_FILE.resolve()
    except OSError:  # unresolvable path can never be the tracked file
        return False


def seed_provenance_note(seed_file: str | Path, *, trusted: bool) -> str:
    """The shared notes suffix recorded on every row of one import.

    Trusted (canonical file only): names the tracked file and pins its sha256 at import
    time, so the audit trail records exactly WHICH seed data version was imported.
    Untracked: names the actual source file + :data:`UNTRACKED_NOTE` — it never carries
    the re-derived-at-seed-build-time claim.
    """
    path = Path(seed_file)
    if trusted:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return (
            "Re-derived live at seed-build time; offline import trusts the tracked "
            f"seed/seed_citations.jsonl (sha256={digest}; seed/PROVENANCE.md)."
        )
    return f"Imported from {path.as_posix()}: {UNTRACKED_NOTE}."


def import_seed(
    conn: sqlite3.Connection, rows: list[SeedRow], *, provenance_note: str
) -> ImportResult:
    """Import validated rows; every write goes through ``verify.insert_citation``.

    Idempotent by ``natural_key``: an already-present key is skipped UNTOUCHED (no
    ``verified_at`` refresh — the offline import re-verified nothing, so it claims
    nothing). Imported-vs-skipped comes from the gate's own atomic upsert
    (``refresh_on_conflict=False``; its ``created`` flag), NOT a pre-check SELECT — a
    SELECT-then-insert pair is exactly the concurrency race ``insert_citation`` was
    built to close. ``provenance_note`` (:func:`seed_provenance_note`) is appended to
    every imported row's notes. Transactions belong to the caller (``with conn:``),
    matching ``insert_citation``'s contract, so a mid-import failure writes nothing.
    """
    outcomes: list[SeedOutcome] = []
    imported = 0
    skipped = 0
    for row in rows:
        citation_id, created = verify.insert_citation(
            conn,
            kind="external",
            resolution_method="api_structured",
            title=row.title,
            doi=row.natural_key,
            api_echo=row.api_echo(),
            authors="; ".join(row.authors),
            year=row.year,
            venue=row.venue,
            keywords=row.category,
            notes=(
                f"Seed corpus row (source_api={row.source_api}; "
                f"retrieved_at={row.retrieved_at}). {provenance_note}"
            ),
            refresh_on_conflict=False,  # skipped rows stay untouched — no re-verify claim
        )
        if created:
            imported += 1
            outcomes.append(SeedOutcome(row.natural_key, "imported", citation_id))
        else:
            skipped += 1
            outcomes.append(SeedOutcome(row.natural_key, "skipped", None))
    return ImportResult(imported=imported, skipped=skipped, outcomes=tuple(outcomes))


__all__ = [
    "ALLOWED_SOURCE_APIS",
    "DEFAULT_SEED_FILE",
    "REQUIRED_KEYS",
    "UNTRACKED_NOTE",
    "ImportResult",
    "SeedError",
    "SeedOutcome",
    "SeedRow",
    "import_seed",
    "is_canonical_seed_file",
    "load_seed_rows",
    "seed_provenance_note",
]
