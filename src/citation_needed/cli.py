"""``cite`` — argparse CLI entry point.

Purely mechanical verbs (the CLI never calls an LLM). v1 verbs:

- ``cite init-db``       — execute schema.sql against a brand-new DB (no-op if initialized)
- ``cite migrate``       — apply pending migrations/000N_*.sql in filename order
- ``cite status``        — per-table row counts + schema version; WAL-checkpoints the DB
- ``cite scan``          — discover + type LLM-facing artifacts, upsert into ``artifacts``
- ``cite corpus-search`` — FTS5 BM25 lookup over the verified citations corpus (read-only)
- ``cite resolve``       — tiered live resolution (S2 -> Crossref -> OpenAlex); READ-ONLY,
  writes nothing — citations are inserted only through review flows
- ``cite review open``   — create a review_runs row with frozen provenance; print the
  run id + prior choice_key/summary pairs as JSON (review-open.schema.json)
- ``cite review commit`` — read the payload JSON from STDIN (Windows 32K argv limit —
  review-commit.schema.json), persist choices/scores/citations in one transaction,
  render the breakdown doc
- ``cite report``        — locate the breakdown doc for a target path + terse summary
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from citation_needed import breakdown, corpus, db, discover, resolve, review, verify
from citation_needed.models import DETAILS_MODELS

_DB_HELP = "Path to the SQLite database (default: data/citation.db under the project root)."


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cite",
        description="Citation trail for choices embedded in LLM-facing files.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_init = subparsers.add_parser("init-db", help="Create a new DB from schema.sql.")
    p_init.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_migrate = subparsers.add_parser("migrate", help="Apply pending numbered migrations.")
    p_migrate.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_status = subparsers.add_parser(
        "status", help="Per-table row counts + schema version; WAL-checkpoints the DB."
    )
    p_status.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_scan = subparsers.add_parser(
        "scan", help="Discover + type LLM-facing artifacts and upsert them into the DB."
    )
    p_scan.add_argument(
        "--project",
        default=None,
        help="Only upsert artifacts resolving to this project slug "
        "(a registry slug, 'coding-root', or 'global').",
    )
    p_scan.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help="Workspace root to scan (default: CITATION_NEEDED_WORKSPACE_ROOT env var, "
        "else the parent of the citation-needed project root).",
    )
    p_scan.add_argument(
        "--memory-root",
        type=Path,
        default=None,
        help="Root of the per-project memory dirs (default: ~/.claude/projects). "
        "Mainly for hermetic tests.",
    )
    p_scan.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_corpus = subparsers.add_parser(
        "corpus-search",
        help="FTS5 BM25 search over the verified citations corpus (read-only).",
        description="Corpus-first lookup: BM25-ranked FTS5 MATCH over citations_fts. "
        "User input is term-extracted, never passed to FTS5 raw. Read-only.",
    )
    p_corpus.add_argument("query", help="Free-text query; salient terms are extracted.")
    p_corpus.add_argument(
        "--category",
        default=None,
        help="Constrain hits to citations whose keywords mention this category.",
    )
    p_corpus.add_argument(
        "--limit", type=int, default=10, help="Maximum hits to return (default: 10)."
    )
    p_corpus.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_resolve = subparsers.add_parser(
        "resolve",
        help="Tiered live citation resolution (Semantic Scholar -> Crossref DOI "
        "canonicalization -> OpenAlex fallback). READ-ONLY: prints the result and "
        "writes NOTHING — citations enter the DB only through review flows "
        "(verify.insert_citation).",
        description="Resolve a query against the structured APIs: Semantic Scholar "
        "first, Crossref canonicalization when a DOI is found, OpenAlex fallback on an "
        "S2 miss (requires CITATION_NEEDED_OPENALEX_KEY). Shows a corpus-first FTS5 "
        "preview when the DB exists. This verb NEVER writes the database — the "
        "citations table is written only by review flows through the anti-fabrication "
        "gate (verify.insert_citation).",
    )
    p_resolve.add_argument("query", help="Free-text bibliographic query.")
    p_resolve.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_review = subparsers.add_parser(
        "review", help="Review-run lifecycle: open (frozen provenance) / commit (stdin JSON)."
    )
    review_sub = p_review.add_subparsers(dest="review_command", required=True)

    p_open = review_sub.add_parser(
        "open",
        help="Create a review_runs row with frozen provenance; print JSON to stdout.",
        description="Creates a review run for an already-scanned artifact and prints "
        "the review-open.schema.json JSON: run_id, artifact info (stored content hash, "
        "best-effort git HEAD sha, tool schema version), and the prior "
        "choice_key/summary pairs the skill layer feeds back for key REUSE.",
    )
    p_open.add_argument(
        "path",
        help="Artifact path as stored (workspace-relative forward-slash, or memory:<slug>/...).",
    )
    p_open.add_argument(
        "--reviewer-model",
        default="unspecified",
        help="Model identity frozen onto the run row (default: 'unspecified').",
    )
    p_open.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help="Workspace root for the best-effort `git rev-parse HEAD` "
        "(default: CITATION_NEEDED_WORKSPACE_ROOT env var, else the parent of the "
        "citation-needed project root).",
    )
    p_open.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_commit = review_sub.add_parser(
        "commit",
        help="Read the commit payload JSON from STDIN; persist rows + render the breakdown.",
        description="Reads the review-commit.schema.json payload from STDIN (stdin, not "
        "argv — Windows 32K argv limit), upserts choices by (artifact_id, choice_key), "
        "writes vote-share scores, links/inserts citations through the anti-fabrication "
        "gate (web_fetch_verified entries are re-fetched and re-verified server-side), "
        "stamps the composite on the run, and renders the breakdown doc. All DB writes "
        "happen in ONE transaction.",
    )
    p_commit.add_argument(
        "--run",
        type=int,
        default=None,
        help="Review run id from `cite review open` (or supply run_id in the payload).",
    )
    p_commit.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help="Workspace root internal-read citations are verified against "
        "(default: CITATION_NEEDED_WORKSPACE_ROOT env var, else the parent of the "
        "citation-needed project root).",
    )
    p_commit.add_argument(
        "--memory-root",
        type=Path,
        default=None,
        help="Root of the per-project memory dirs that memory:-scheme internal-read "
        "citations are confined to (default: ~/.claude/projects). Mainly for hermetic "
        "tests.",
    )
    p_commit.add_argument(
        "--breakdowns-root",
        type=Path,
        default=None,
        help="Where breakdown docs render (default: breakdowns/ under the project root).",
    )
    p_commit.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_report = subparsers.add_parser(
        "report",
        help="Locate the breakdown doc for a target path + print a terse summary.",
        description="Resolves the breakdown path via the plan §3.2 slug convention and "
        "prints composite, band, choice count, and per-classification counts from the "
        "latest committed review run. Errors cleanly when no review exists.",
    )
    p_report.add_argument(
        "path",
        help="Target artifact path as stored (workspace-relative, or memory:<slug>/...).",
    )
    p_report.add_argument(
        "--breakdowns-root",
        type=Path,
        default=None,
        help="Where breakdown docs live (default: breakdowns/ under the project root).",
    )
    p_report.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    return parser


def _cmd_init_db(db_path: Path) -> int:
    created = db.init_db(db_path)
    if created:
        conn = db.connect(db_path)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
        print(f"Initialized new database at {db_path.as_posix()} (schema v{version}).")
    else:
        print(f"Database at {db_path.as_posix()} already has tables — init-db is a no-op.")
        print("Schema changes to an existing DB ship as migrations (see migrations/README.md).")
    return 0


def _cmd_migrate(db_path: Path) -> int:
    try:
        applied = db.migrate(db_path)
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        # FileNotFoundError: no DB yet; ValueError: bad filename / trailing incomplete
        # statement; sqlite3.Error: broken migration SQL. All exit clean, never traceback.
        print(f"error: {exc}")
        return 1
    if applied:
        versions = ", ".join(str(v) for v in applied)
        print(f"Applied {len(applied)} migration(s): {versions}.")
    else:
        print("Database is up to date — no pending migrations.")
    return 0


def _cmd_status(db_path: Path) -> int:
    if not db_path.exists():
        print(f"error: database does not exist (run `cite init-db` first): {db_path.as_posix()}")
        return 1
    conn = db.connect(db_path)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        print(f"Database: {db_path.as_posix()}")
        print(f"Schema version (PRAGMA user_version): {version}")
        for table in db.CANONICAL_TABLES:
            try:
                # table names come from the trusted CANONICAL_TABLES constant, never user input
                count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.OperationalError:
                print(f"  {table:<16} MISSING")
                continue
            print(f"  {table:<16} {count} row(s)")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return 0


def _cmd_scan(
    db_path: Path,
    workspace_root: Path | None,
    memory_root: Path | None,
    project_filter: str | None,
) -> int:
    if not db_path.exists():
        print(f"error: database does not exist (run `cite init-db` first): {db_path.as_posix()}")
        return 1
    root = workspace_root if workspace_root is not None else discover.default_workspace_root()
    if not root.is_dir():
        print(f"error: workspace root does not exist: {root.as_posix()}")
        return 1

    try:
        report = discover.scan_workspace(root, memory_root=memory_root)
    except OSError as exc:
        # scan_workspace degrades per-entry; a root-level filesystem failure still
        # surfaces here as the established clean contract (discover raises, cli catches).
        print(f"error: {exc}")
        return 1
    selected = [
        artifact
        for artifact in report.artifacts
        if project_filter is None or artifact.project == project_filter
    ]

    counts: dict[str, Counter[str]] = {}
    conn = db.connect(db_path)
    try:
        try:
            with conn:  # one transaction for the whole upsert pass
                for artifact in selected:
                    status = discover.upsert_artifact(conn, artifact)
                    counts.setdefault(artifact.artifact_type, Counter())[status] += 1
        except (ValidationError, sqlite3.Error) as exc:
            # ValidationError: a details builder produced a shape its model rejects;
            # sqlite3.Error: constraint/IO failure mid-upsert (the transaction rolls
            # back). Both exit clean, never traceback (db raises -> cli catches).
            print(f"error: {exc}")
            return 1
    finally:
        conn.close()

    print(f"Scanned workspace: {report.workspace_root.as_posix()}")
    if project_filter is not None:
        print(
            f"Project filter: {project_filter} "
            f"({len(selected)} of {len(report.artifacts)} discovered artifact(s))"
        )
    else:
        print(f"Discovered {len(report.artifacts)} artifact(s).")
    print("Per-type counts:")
    for artifact_type in sorted(DETAILS_MODELS):
        type_counts = counts.get(artifact_type, Counter())
        total = sum(type_counts.values())
        print(
            f"  {artifact_type:<10} {total} ({type_counts['new']} new, "
            f"{type_counts['updated']} updated, {type_counts['unchanged']} unchanged)"
        )
    if report.notes:
        print("Pointer/frontmatter notes:")
        for note in report.notes:
            print(f"  {note}")
    print(
        f"Exclusions: {report.excluded_dir_count} excluded dir subtree(s) "
        f"(.venv/node_modules/.git/docs archived); "
        f"not-owned tree(s) skipped: {', '.join(report.not_owned_skipped) or 'none'}; "
        f"{report.memory_index_skipped} memory index file(s) (MEMORY.md) skipped"
    )
    return 0


def _cmd_corpus_search(db_path: Path, query: str, category: str | None, limit: int) -> int:
    if not db_path.exists():
        print(f"error: database does not exist (run `cite init-db` first): {db_path.as_posix()}")
        return 1
    conn = db.connect(db_path)
    try:
        try:
            hits = corpus.corpus_search(conn, query, category=category, limit=limit)
        except (ValueError, sqlite3.Error) as exc:
            # ValueError: bad limit; sqlite3.Error: missing/corrupt FTS index.
            print(f"error: {exc}")
            return 1
    finally:
        conn.close()
    if not hits:
        print("No corpus hits.")
        return 0
    print(f"{len(hits)} corpus hit(s) (bm25: lower = better):")
    for hit in hits:
        title = hit.title or "(untitled)"
        print(f"  [{hit.citation_id}] bm25={hit.score:.3f}  {hit.natural_key}  {title}")
    return 0


def _print_resolution(result: resolve.ResolutionResult) -> None:
    print(f"Tiers tried: {', '.join(result.tiers_tried)}")
    if not result.resolved or result.hit is None:
        print("No resolution found (a legitimate no-literature-found outcome).")
        return
    hit = result.hit
    print(f"Tier: {result.tier}")
    print(f"Title: {hit.title}")
    if hit.year is not None:
        print(f"Year: {hit.year}")
    if hit.authors:
        print(f"Authors: {'; '.join(hit.authors)}")
    if hit.doi:
        print(f"DOI: {hit.doi}")
    if hit.arxiv_id:
        print(f"ArXiv: {hit.arxiv_id}")
    if hit.url:
        print(f"URL: {hit.url}")
    if hit.abstract_snippet:
        print(f"Abstract: {hit.abstract_snippet}")
    if result.crossref_echo is not None:
        print("Crossref canonicalization: ok (JSON echo captured).")
    for note in result.notes:
        print(f"Note: {note}")


def _cmd_resolve(db_path: Path, query: str) -> int:
    print(f"Query: {query}")
    if db_path.exists():
        conn = db.connect(db_path)
        try:
            try:
                corpus_hits = corpus.corpus_search(conn, query, limit=3)
            except sqlite3.Error as exc:
                corpus_hits = []
                print(f"Corpus (FTS5): unavailable ({exc}); continuing to live resolution.")
            else:
                if corpus_hits:
                    print(
                        f"Corpus (FTS5): {len(corpus_hits)} existing hit(s) — "
                        "see `cite corpus-search` before spending live calls."
                    )
                else:
                    print("Corpus (FTS5): 0 hits.")
        finally:
            conn.close()
    else:
        print("Corpus (FTS5): database not found — skipping corpus-first preview.")
    try:
        result = resolve.resolve_citation(query)
    except resolve.ResolutionError as exc:
        # Includes OpenAlexKeyMissing — loud and actionable, never a silent skip.
        print(f"error: {exc}")
        return 1
    _print_resolution(result)
    print(
        "Read-only: nothing was written. Citations enter the DB only through review "
        "flows (the verify.insert_citation anti-fabrication gate)."
    )
    return 0


def _default_workspace_root(flag: Path | None) -> Path:
    return flag if flag is not None else discover.default_workspace_root()


def _default_breakdowns_root(flag: Path | None) -> Path:
    return flag if flag is not None else db.PROJECT_ROOT / "breakdowns"


def _cmd_review_open(
    db_path: Path, path: str, reviewer_model: str, workspace_root: Path | None
) -> int:
    if not db_path.exists():
        print(f"error: database does not exist (run `cite init-db` first): {db_path.as_posix()}")
        return 1
    conn = db.connect(db_path)
    try:
        try:
            with conn:  # one transaction for the run-row insert
                opened = review.open_review(
                    conn,
                    path,
                    reviewer_model=reviewer_model,
                    workspace_root=_default_workspace_root(workspace_root),
                )
        except (review.ReviewError, sqlite3.Error) as exc:
            print(f"error: {exc}")
            return 1
    finally:
        conn.close()
    # Stdout is the machine contract (review-open.schema.json) — nothing else prints.
    print(json.dumps(opened.model_dump(), indent=2))
    return 0


def _cmd_review_commit(
    db_path: Path,
    run_flag: int | None,
    workspace_root: Path | None,
    memory_root: Path | None,
    breakdowns_root: Path | None,
) -> int:
    if not db_path.exists():
        print(f"error: database does not exist (run `cite init-db` first): {db_path.as_posix()}")
        return 1
    # Read BYTES and decode UTF-8 explicitly (RFC 8259: JSON is UTF-8). A bare
    # sys.stdin.read() inherits the console codepage on Windows (cp1252/437 on this
    # workspace's out-of-the-box default), silently mojibaking non-ASCII payload text
    # into the DB. utf-8-sig additionally tolerates + strips a BOM (PowerShell '>' /
    # Out-File can prepend one). A non-UTF-8 payload errors loudly, never corrupts.
    raw_bytes = sys.stdin.buffer.read()
    try:
        raw = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        print(f"error: stdin must be UTF-8 — the payload could not be decoded ({exc})")
        return 1
    if not raw.strip():
        print("error: empty stdin — `cite review commit` reads the payload JSON from STDIN")
        return 1
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: stdin is not valid JSON: {exc}")
        return 1
    except RecursionError:
        # json.loads recurses per nesting level; a hostile/garbled deeply-nested payload
        # must exit through the clean error contract, never a raw traceback.
        print("error: stdin is not valid JSON: nesting too deep to parse")
        return 1
    try:
        payload = review.CommitPayload.model_validate(data)
    except ValidationError as exc:
        print(f"error: payload does not match docs/contracts/review-commit.schema.json: {exc}")
        return 1
    if run_flag is not None and payload.run_id is not None and run_flag != payload.run_id:
        print(f"error: --run {run_flag} conflicts with payload run_id {payload.run_id}")
        return 1
    run_id = run_flag if run_flag is not None else payload.run_id
    if run_id is None:
        print("error: no run id — pass --run <id> or include run_id in the payload")
        return 1

    conn = db.connect(db_path)
    try:
        try:
            result = review.commit_review(
                conn,
                run_id,
                payload,
                workspace_root=_default_workspace_root(workspace_root),
                memory_root=memory_root,
            )
        except (review.ReviewError, verify.VerificationFailed, sqlite3.Error) as exc:
            # ReviewError: lifecycle/tie/link violations; VerificationFailed: the
            # anti-fabrication gate refused a citation; sqlite3.Error: constraint/IO.
            # The ONE transaction rolled back — nothing was written.
            print(f"error: {exc}")
            return 1
    finally:
        conn.close()

    try:
        written = breakdown.write_breakdown(result, _default_breakdowns_root(breakdowns_root))
    except OSError as exc:
        print(
            f"error: DB rows for run {result.run_id} are committed, but the breakdown "
            f"doc could not be written: {exc}"
        )
        return 1
    doc_path = written.path
    counts = result.classification_counts()
    if written.collision_note is not None:
        print(written.collision_note)
    print(f"Committed review run #{result.run_id} for {result.artifact_path}")
    print(
        f"Composite: {result.composite:.1f} / 100 — {result.composite_band} "
        f"(interpretation guide {result.interpretation_guide_version})"
    )
    print(
        f"Choices: {len(result.choices)} scored (well-supported {counts['well-supported']}, "
        f"needs-improvement {counts['needs-improvement']}, "
        f"interesting {counts['interesting']}); {len(result.removed_keys)} removed"
    )
    print(f"Breakdown: {doc_path.as_posix()}")
    return 0


def _cmd_report(db_path: Path, target_path: str, breakdowns_root: Path | None) -> int:
    if not db_path.exists():
        print(f"error: database does not exist (run `cite init-db` first): {db_path.as_posix()}")
        return 1
    path = target_path.replace("\\", "/").strip()
    conn = db.connect(db_path)
    try:
        artifact = conn.execute(
            "SELECT id, artifact_type, project FROM artifacts WHERE path = ?", (path,)
        ).fetchone()
        if artifact is None:
            print(
                f"error: no review exists for {path} — the artifact is not registered "
                "(run `cite scan`, then `cite review open` + `cite review commit`)"
            )
            return 1
        run = conn.execute(
            "SELECT id, finished_at, composite, composite_band, interpretation_guide_version "
            "FROM review_runs WHERE artifact_id = ? AND composite IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (int(artifact[0]),),
        ).fetchone()
        if run is None:
            print(
                f"error: no review exists for {path} — no committed review run "
                "(run `cite review open`, then `cite review commit`)"
            )
            return 1
        counts = Counter(
            {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT classification, COUNT(*) FROM scores WHERE review_run_id = ? "
                    "GROUP BY classification",
                    (int(run[0]),),
                )
            }
        )
    finally:
        conn.close()
    # locate_breakdown checks BOTH candidates — the canonical §3.2 slug path and the
    # hash-discriminated sibling a slug collision diverts to (see breakdown.py).
    doc = breakdown.locate_breakdown(
        _default_breakdowns_root(breakdowns_root), str(artifact[2]), path
    )
    suffix = (
        "" if doc.is_file() else "  (file missing — gitignored output; re-review to regenerate)"
    )
    total = sum(counts.values())
    print(f"Breakdown: {doc.as_posix()}{suffix}")
    print(f"Artifact: {path} ({artifact[1]}, project {artifact[2]})")
    print(f"Latest review: run #{run[0]}, committed {run[1]}")
    print(f"Composite: {float(run[2]):.1f} / 100 — {run[3]} (interpretation guide {run[4]})")
    print(
        f"Choices scored: {total} (well-supported {counts['well-supported']}, "
        f"needs-improvement {counts['needs-improvement']}, "
        f"interesting {counts['interesting']})"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``cite`` console script. Returns a process exit code."""
    args = _build_parser().parse_args(argv)
    if args.command == "init-db":
        return _cmd_init_db(args.db)
    if args.command == "migrate":
        return _cmd_migrate(args.db)
    if args.command == "status":
        return _cmd_status(args.db)
    if args.command == "scan":
        return _cmd_scan(args.db, args.workspace_root, args.memory_root, args.project)
    if args.command == "corpus-search":
        return _cmd_corpus_search(args.db, args.query, args.category, args.limit)
    if args.command == "resolve":
        return _cmd_resolve(args.db, args.query)
    if args.command == "review":
        if args.review_command == "open":
            return _cmd_review_open(args.db, args.path, args.reviewer_model, args.workspace_root)
        if args.review_command == "commit":
            return _cmd_review_commit(
                args.db, args.run, args.workspace_root, args.memory_root, args.breakdowns_root
            )
        raise AssertionError(f"unreachable: unknown review command {args.review_command!r}")
    if args.command == "report":
        return _cmd_report(args.db, args.path, args.breakdowns_root)
    raise AssertionError(f"unreachable: unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
