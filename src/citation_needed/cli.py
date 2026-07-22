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
- ``cite calibrate check``  — report fingerprint-cache validity + per-component diff
  (exit 0 valid / 1 invalid)
- ``cite calibrate open``   — print the calibration context JSON (anchor paths, a
  throwaway DB with two open anchor runs, expected labels) for the skill layer
- ``cite calibrate commit`` — read the two anchor payloads from STDIN, run the 4-assertion
  gate on a throwaway DB, cache the fingerprint on PASS (exit 0 pass / 1 gate-fail /
  2 parse-fail-abort; the real DB is read via an atomic backup snapshot, never written)
- ``cite distill generate`` — mechanical distill_queue rows for a committed run's
  needs-improvement choices (contradicted -> 'rewrite', unsupported -> 'trim';
  well-supported/interesting yield no row)
- ``cite distill propose`` — read skill-drafted proposal JSON from STDIN (Windows 32K
  argv limit), upsert queue rows over the mechanical defaults (whole-payload reject)
- ``cite queue list``      — ranked distill_queue table (rank desc; default status open)
- ``cite queue resolve``   — record the operator decision on one row: --keep ->
  status 'rejected' (proposal declined, target text stays); --cut/--rewrite ->
  status 'accepted' ('applied' is out of scope — target edits happen outside
  citation-needed)
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

from citation_needed import (
    breakdown,
    calibrate,
    corpus,
    db,
    discover,
    distill,
    resolve,
    review,
    verify,
)
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
    p_open.add_argument(
        "--accept-aged",
        action="store_true",
        help="Override ONLY the 30-day calibration advisory ceiling (an A-D "
        "fingerprint mismatch still refuses; deliberate operator action).",
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

    p_calibrate = subparsers.add_parser(
        "calibrate",
        help="Anchor calibration gate: check (cache validity) / open (context JSON) / "
        "commit (run the gate on a throwaway DB; cache the fingerprint on PASS).",
    )
    calibrate_sub = p_calibrate.add_subparsers(dest="calibrate_command", required=True)

    p_cal_check = calibrate_sub.add_parser(
        "check",
        help="Report fingerprint-cache validity + per-component diff; exit 0/1.",
        description="Compares the cached calibration fingerprint (prompts hash, model "
        "id, corpus row-count/max-id, schema user_version, frozen-anchor content "
        "hash, 30-day advisory age) against current values. Exit 0 when valid, 1 "
        "when invalid/missing.",
    )
    p_cal_check.add_argument(
        "--model",
        default=None,
        help="Resolved model id to compare against fingerprint B (omitted: B is "
        "reported but not compared — the CLI cannot resolve a model itself).",
    )
    p_cal_check.add_argument(
        "--accept-aged",
        action="store_true",
        help="Treat an over-30-day (but otherwise matching) calibration as valid.",
    )
    p_cal_check.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_cal_open = calibrate_sub.add_parser(
        "open",
        help="Print the calibration context JSON (throwaway DB, anchor runs, "
        "expected labels) for the skill layer's judging pass.",
        description="Creates a throwaway snapshot of the DB (atomic sqlite3 backup; "
        "the real DB is never written), registers both frozen anchors on it, opens "
        "one review run per anchor, and prints the context JSON. Advisory for the "
        "skill layer: `cite calibrate commit` is stateless and builds its own fresh "
        "throwaway, so the default temp throwaway is removed when this command "
        "finishes (throwaway_retained=false in the JSON).",
    )
    p_cal_open.add_argument(
        "--reviewer-model",
        default="unspecified",
        help="Model identity frozen onto the anchor run rows (default: 'unspecified').",
    )
    p_cal_open.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help="Workspace root for the best-effort `git rev-parse HEAD` on the anchor "
        "runs (default: CITATION_NEEDED_WORKSPACE_ROOT env var, else the parent of "
        "the citation-needed project root).",
    )
    p_cal_open.add_argument(
        "--throwaway-dir",
        type=Path,
        default=None,
        help="Directory for the throwaway DB snapshot (default: a fresh temp dir, "
        "removed when the command finishes; pass a dir to keep it for inspection).",
    )
    p_cal_open.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_cal_commit = calibrate_sub.add_parser(
        "commit",
        help="Read the two anchor payloads from STDIN; run the 4-assertion gate; "
        "cache the fingerprint on PASS. Exit 0 pass / 1 gate-fail / 2 parse-fail.",
        description='STDIN JSON: {"good": <review-commit payload>, "garbage": '
        "<review-commit payload>} (stdin, not argv — Windows 32K argv limit). Both "
        "anchors run through the production scan+open+commit mechanics against a "
        "throwaway snapshot of the DB (atomic sqlite3 backup; the real DB is never "
        "written), so a calibration run can never poison the compounding corpus. "
        "On any gate failure NOTHING is cached and every failed assertion is "
        "reported.",
    )
    p_cal_commit.add_argument(
        "--model",
        default="unspecified",
        help="Resolved model id the anchors were judged with — calibration "
        "fingerprint B (default: 'unspecified').",
    )
    p_cal_commit.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help="Workspace root internal-read citations in the payloads are verified "
        "against (default: CITATION_NEEDED_WORKSPACE_ROOT env var, else the parent "
        "of the citation-needed project root).",
    )
    p_cal_commit.add_argument(
        "--memory-root",
        type=Path,
        default=None,
        help="Root of the per-project memory dirs for memory:-scheme internal-read "
        "citations (default: ~/.claude/projects). Mainly for hermetic tests.",
    )
    p_cal_commit.add_argument(
        "--throwaway-dir",
        type=Path,
        default=None,
        help="Directory for the throwaway DB snapshot (default: a fresh temp dir, "
        "removed when the command finishes; pass a dir to keep it for inspection).",
    )
    p_cal_commit.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_distill = subparsers.add_parser(
        "distill",
        help="Distill-proposal mechanics: generate (mechanical defaults) / "
        "propose (skill-drafted stdin payload).",
    )
    distill_sub = p_distill.add_subparsers(dest="distill_command", required=True)

    # Derived from distill.LOAD_WEIGHTS — the single source of truth; a re-typed
    # literal here is exactly the drift code-quality.md § one-source-of-truth bans.
    load_weights_text = ", ".join(
        f"{artifact_type} {weight}" for artifact_type, weight in distill.LOAD_WEIGHTS.items()
    )
    p_dist_generate = distill_sub.add_parser(
        "generate",
        help="Create/refresh mechanical-default queue rows for a committed run's "
        "needs-improvement choices.",
        description="For each needs-improvement choice of the committed run: "
        "contradicted majority -> proposal_kind 'rewrite', unsupported majority -> "
        "'trim', rank = (1 - composite/100) * load weight (per-choice composite; "
        f"load weights v1: {load_weights_text}). "
        "Justification is built from the choice's linked citation ids or its "
        "documented absence; a choice with neither rejects the whole run loudly. "
        "Well-supported and interesting choices yield no row. One row per choice: "
        "an open row is refreshed in place; a resolved row is skipped untouched.",
    )
    p_dist_generate.add_argument(
        "--run", type=int, required=True, help="Committed review run id (from `cite review open`)."
    )
    p_dist_generate.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_dist_propose = distill_sub.add_parser(
        "propose",
        help="Read skill-drafted distill proposals JSON from STDIN; upsert queue rows.",
        description="STDIN JSON (stdin, not argv — Windows 32K argv limit): "
        '{"run_id": <id>, "proposals": [{"choice_key", "proposal_kind", '
        '"justification", "justifying_citation_ids", "suggested_rewrite"}]}. '
        "Upserts over the mechanical defaults for the same choices (status stays "
        "'open'; rank stays formula-computed). Whole-payload reject on ANY invalid "
        "entry: unscored choice_key, citation id absent from the corpus, a resolved "
        "queue row, or a 'rewrite' proposal without suggested_rewrite. Output "
        "contract source: prompts/distill.v1.md.",
    )
    p_dist_propose.add_argument(
        "--run",
        type=int,
        default=None,
        help="Committed review run id (or supply run_id in the payload).",
    )
    p_dist_propose.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_queue = subparsers.add_parser(
        "queue",
        help="Distill-queue triage: list (ranked table) / resolve (record the operator decision).",
    )
    queue_sub = p_queue.add_subparsers(dest="queue_command", required=True)

    p_queue_list = queue_sub.add_parser(
        "list",
        help="Ranked distill_queue table (rank desc; default status open).",
        description="Lists queue rows ranked by urgency (rank desc, id asc): id, "
        "rank, per-choice composite + band, proposal kind, status, artifact path, "
        "choice_key, and the justification's first line.",
    )
    p_queue_list.add_argument(
        "--status",
        choices=list(distill.QUEUE_STATUSES),
        default="open",
        help="Filter by status (default: open — the triage backlog).",
    )
    p_queue_list.add_argument(
        "--project", default=None, help="Only rows whose artifact belongs to this project slug."
    )
    p_queue_list.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

    p_queue_resolve = queue_sub.add_parser(
        "resolve",
        help="Record the operator decision on one open row: --keep -> rejected; "
        "--cut/--rewrite -> accepted.",
        description="Decision -> status mapping: --keep records status 'rejected' "
        "(the proposal is declined; the target text stays). --cut and --rewrite "
        "both record status 'accepted' (the proposal proceeds; WHICH edit shape is "
        "the row's proposal_kind — run `cite distill propose` first if the kind "
        "should change). The 'applied' transition is OUT of scope: target edits "
        "happen outside citation-needed (the queue records decisions, not edits). "
        "Stores resolved_by (--by, else env USERNAME/USER) + resolved_at (pipeline "
        "clock).",
    )
    p_queue_resolve.add_argument(
        "id", type=int, help="distill_queue row id (from `cite queue list`)."
    )
    decision_group = p_queue_resolve.add_mutually_exclusive_group(required=True)
    decision_group.add_argument(
        "--keep",
        action="store_const",
        const="keep",
        dest="decision",
        help="Decline the proposal; keep the target text -> status 'rejected'.",
    )
    decision_group.add_argument(
        "--cut",
        action="store_const",
        const="cut",
        dest="decision",
        help="Accept cutting the text -> status 'accepted'.",
    )
    decision_group.add_argument(
        "--rewrite",
        action="store_const",
        const="rewrite",
        dest="decision",
        help="Accept rewriting the text -> status 'accepted'.",
    )
    p_queue_resolve.add_argument(
        "--by",
        default=None,
        help="Resolver identity recorded as resolved_by (default: env USERNAME, then USER).",
    )
    p_queue_resolve.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help=_DB_HELP)

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
    db_path: Path,
    path: str,
    reviewer_model: str,
    workspace_root: Path | None,
    accept_aged: bool,
) -> int:
    if not db_path.exists():
        print(f"error: database does not exist (run `cite init-db` first): {db_path.as_posix()}")
        return 1
    conn = db.connect(db_path)
    try:
        try:
            with conn:  # one transaction for the run-row insert
                # calibration_check is ALWAYS enabled here — the escape hatch exists
                # only for calibrate.py's own anchor runs on the throwaway DB.
                opened = review.open_review(
                    conn,
                    path,
                    reviewer_model=reviewer_model,
                    workspace_root=_default_workspace_root(workspace_root),
                    accept_aged=accept_aged,
                )
        except (review.ReviewError, calibrate.CalibrationError, sqlite3.Error) as exc:
            print(f"error: {exc}")
            return 1
    finally:
        conn.close()
    # Stdout is the machine contract (review-open.schema.json) — nothing else prints.
    print(json.dumps(opened.model_dump(), indent=2))
    return 0


def _read_stdin_json(verb: str) -> tuple[object, str | None]:
    """Read one JSON document from STDIN — the shared payload seam.

    Reads BYTES and decodes UTF-8 explicitly (RFC 8259: JSON is UTF-8). A bare
    ``sys.stdin.read()`` inherits the console codepage on Windows (cp1252/437 on this
    workspace's out-of-the-box default), silently mojibaking non-ASCII payload text
    into the DB. utf-8-sig additionally tolerates + strips a BOM (PowerShell '>' /
    Out-File can prepend one). A non-UTF-8 payload errors loudly, never corrupts.
    Returns ``(data, None)`` or ``(None, error_message)``.
    """
    raw_bytes = sys.stdin.buffer.read()
    try:
        raw = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return None, f"error: stdin must be UTF-8 — the payload could not be decoded ({exc})"
    if not raw.strip():
        return None, f"error: empty stdin — `{verb}` reads the payload JSON from STDIN"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"error: stdin is not valid JSON: {exc}"
    except RecursionError:
        # json.loads recurses per nesting level; a hostile/garbled deeply-nested payload
        # must exit through the clean error contract, never a raw traceback.
        return None, "error: stdin is not valid JSON: nesting too deep to parse"


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
    data, stdin_error = _read_stdin_json("cite review commit")
    if stdin_error is not None:
        print(stdin_error)
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


def _cmd_calibrate_check(db_path: Path, model: str | None, accept_aged: bool) -> int:
    if not db_path.exists():
        print(f"error: database does not exist (run `cite init-db` first): {db_path.as_posix()}")
        return 1
    conn = db.connect(db_path)
    try:
        try:
            check = calibrate.check_calibration(conn, model_id=model, accept_aged=accept_aged)
        except calibrate.CalibrationError as exc:
            print(f"error: {exc}")
            return 1
    finally:
        conn.close()
    cache_file = check.fingerprint_file
    print(f"Fingerprint cache: {cache_file.as_posix() if cache_file else '(none)'}")
    if check.fingerprint is not None:
        fp = check.fingerprint
        print(f"  A prompts_sha256:     {fp.prompts_sha256}")
        compared = "" if model is not None else "  (not compared — pass --model to compare)"
        print(f"  B model_id:           {fp.model_id}{compared}")
        print(f"  C corpus_fingerprint: {fp.corpus_fingerprint}")
        print(f"  D schema_user_version: {fp.schema_user_version}")
        print(f"  E anchors_sha256:     {fp.anchors_sha256}")
        age = f"{check.age_days:.1f} days old" if check.age_days is not None else "age unknown"
        print(f"  computed_at:          {fp.computed_at} ({age})")
        if fp.accepted_aged_at is not None:
            print(
                f"  accepted-aged:        last overridden {fp.accepted_aged_at} "
                f"(cache was {fp.accepted_aged_age_days} days old)"
            )
        print(f"  gate: composite(good) {fp.gate.composite_good:.1f}, ")
        print(
            f"        composite(garbage) {fp.gate.composite_garbage:.1f}, "
            f"margin {fp.gate.margin:.1f}, parse-fail {fp.gate.parse_fail_rate:.1%}"
        )
    if check.valid:
        suffix = " (aged — accepted via --accept-aged)" if check.aged else ""
        print(f"Calibration: VALID{suffix}")
        return 0
    print("Calibration: INVALID")
    for reason in check.reasons:
        print(f"  - {reason}")
    print("Re-run the gate: `cite calibrate commit` (driven by /citation-review --calibrate).")
    return 1


def _cmd_calibrate_open(
    db_path: Path,
    reviewer_model: str,
    workspace_root: Path | None,
    throwaway_dir: Path | None,
) -> int:
    try:
        context = calibrate.open_calibration(
            db_path,
            reviewer_model=reviewer_model,
            throwaway_dir=throwaway_dir,
            workspace_root=workspace_root,
        )
    except (calibrate.CalibrationError, review.ReviewError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}")
        return 1
    # Stdout is the machine contract — nothing else prints.
    print(json.dumps(context, indent=2))
    return 0


def _cmd_calibrate_commit(
    db_path: Path,
    model: str,
    workspace_root: Path | None,
    memory_root: Path | None,
    throwaway_dir: Path | None,
) -> int:
    data, stdin_error = _read_stdin_json("cite calibrate commit")
    if stdin_error is not None:
        print(stdin_error)
        return 1
    if not isinstance(data, dict) or "good" not in data or "garbage" not in data:
        print(
            'error: calibrate commit stdin must be {"good": <review-commit payload>, '
            '"garbage": <review-commit payload>}'
        )
        return 1
    try:
        good_payload = review.CommitPayload.model_validate(data["good"])
        garbage_payload = review.CommitPayload.model_validate(data["garbage"])
    except ValidationError as exc:
        print(f"error: anchor payload does not match review-commit.schema.json: {exc}")
        return 1
    try:
        result = calibrate.run_calibration(
            db_path,
            good_payload,
            garbage_payload,
            model_id=model,
            throwaway_dir=throwaway_dir,
            workspace_root=workspace_root,
            memory_root=memory_root,
        )
    except calibrate.CalibrationParseFailure as exc:
        # Distinct ABORT class: the parser is broken independent of what it scores.
        print(f"error: {exc}")
        return 2
    except (
        calibrate.CalibrationError,
        review.ReviewError,
        verify.VerificationFailed,
        sqlite3.Error,
        OSError,
    ) as exc:
        print(f"error: {exc}")
        return 1
    retained = result.throwaway_db.is_file()
    suffix = "kept (--throwaway-dir)" if retained else "temp copy, removed after the run"
    print(f"Throwaway DB: {result.throwaway_db.as_posix()} ({suffix}; real DB never written)")
    print(
        f"Anchor runs: good #{result.good_run_id} composite {result.composite_good:.1f}, "
        f"garbage #{result.garbage_run_id} composite {result.composite_garbage:.1f}, "
        f"margin {result.margin:.1f}, parse-fail rate {result.parse_fail_rate:.1%}"
    )
    for assertion in result.assertions:
        marker = "PASS" if assertion.passed else "FAIL"
        print(f"  [{marker}] {assertion.name}: {assertion.detail}")
    if result.passed:
        print(f"Calibration PASSED — fingerprint cached at {result.fingerprint_file.as_posix()}")
        return 0
    print(
        "Calibration ABORT — gate failed; nothing was cached, no real review may run. "
        "Thresholds are never loosened to make a run pass (plan D7)."
    )
    return 1


def _print_queue_writes(writes: Sequence[distill.QueueWrite]) -> None:
    for write in writes:
        print(
            f"  [{write.queue_id}] {write.choice_key} -> {write.proposal_kind}, "
            f"rank {write.rank:.2f} ({write.outcome})"
        )


def _cmd_distill_generate(db_path: Path, run_id: int) -> int:
    if not db_path.exists():
        print(f"error: database does not exist (run `cite init-db` first): {db_path.as_posix()}")
        return 1
    conn = db.connect(db_path)
    try:
        try:
            result = distill.generate_queue_rows(conn, run_id)
        except (distill.DistillError, sqlite3.Error) as exc:
            print(f"error: {exc}")
            return 1
    finally:
        conn.close()
    print(
        f"Distill queue for run #{result.run_id} ({result.artifact_path}, {result.artifact_type}):"
    )
    _print_queue_writes(result.writes)
    if result.skipped_resolved:
        print(
            f"  skipped {len(result.skipped_resolved)} resolved row(s): "
            f"{', '.join(result.skipped_resolved)} (recorded decisions stand)"
        )
    print(
        f"  {result.no_row_count} choice(s) yielded no row (well-supported/interesting); "
        f"{len(result.writes)} row(s) written"
    )
    return 0


def _cmd_distill_propose(db_path: Path, run_flag: int | None) -> int:
    if not db_path.exists():
        print(f"error: database does not exist (run `cite init-db` first): {db_path.as_posix()}")
        return 1
    data, stdin_error = _read_stdin_json("cite distill propose")
    if stdin_error is not None:
        print(stdin_error)
        return 1
    try:
        payload = distill.ProposePayload.model_validate(data)
    except ValidationError as exc:
        print(f"error: payload does not match the distill-propose contract: {exc}")
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
            result = distill.propose_queue_rows(conn, run_id, payload)
        except (distill.DistillError, sqlite3.Error) as exc:
            print(f"error: {exc}")
            return 1
    finally:
        conn.close()
    print(
        f"Upserted {len(result.writes)} proposal(s) for run #{result.run_id} "
        f"({result.artifact_path}, {result.artifact_type}); status stays 'open':"
    )
    _print_queue_writes(result.writes)
    return 0


def _cmd_queue_list(db_path: Path, status: str, project: str | None) -> int:
    if not db_path.exists():
        print(f"error: database does not exist (run `cite init-db` first): {db_path.as_posix()}")
        return 1
    conn = db.connect(db_path)
    try:
        try:
            rows = distill.list_queue(conn, status=status, project=project)
        except (distill.DistillError, sqlite3.Error) as exc:
            print(f"error: {exc}")
            return 1
    finally:
        conn.close()
    scope = f"status {status}" + (f", project {project}" if project is not None else "")
    if not rows:
        print(f"No distill-queue rows match ({scope}).")
        return 0
    print(f"{len(rows)} distill-queue row(s) ({scope}; rank desc):")
    for row in rows:
        first_line = row.justification.splitlines()[0] if row.justification else ""
        if len(first_line) > 80:
            first_line = first_line[:77] + "..."
        # Display-side staleness guard: the row's source run is no longer the
        # artifact's newest committed run (distill.py § supersession).
        marker = "  [superseded run]" if row.superseded_run else ""
        print(
            f"  [{row.queue_id}] rank {row.rank:.2f}  composite {row.composite:.1f} "
            f"({row.composite_band})  {row.proposal_kind}  {row.status}  "
            f"{row.artifact_path} :: {row.choice_key}{marker}"
        )
        print(f"      {first_line}")
    return 0


def _cmd_queue_resolve(db_path: Path, queue_id: int, decision: str, by: str | None) -> int:
    if not db_path.exists():
        print(f"error: database does not exist (run `cite init-db` first): {db_path.as_posix()}")
        return 1
    conn = db.connect(db_path)
    try:
        try:
            resolved_by = distill.default_resolver_name(by)
            outcome = distill.resolve_queue_item(conn, queue_id, decision, resolved_by=resolved_by)
        except (distill.DistillError, sqlite3.Error) as exc:
            print(f"error: {exc}")
            return 1
    finally:
        conn.close()
    print(
        f"Resolved distill-queue row #{outcome.queue_id} "
        f"({outcome.artifact_path} :: {outcome.choice_key}): --{outcome.decision} -> "
        f"status '{outcome.status}' (by {outcome.resolved_by} at {outcome.resolved_at})."
    )
    print(
        "Target edits happen outside citation-needed — the queue records decisions, "
        "not edits ('applied' is out of the CLI's scope)."
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
            return _cmd_review_open(
                args.db, args.path, args.reviewer_model, args.workspace_root, args.accept_aged
            )
        if args.review_command == "commit":
            return _cmd_review_commit(
                args.db, args.run, args.workspace_root, args.memory_root, args.breakdowns_root
            )
        raise AssertionError(f"unreachable: unknown review command {args.review_command!r}")
    if args.command == "report":
        return _cmd_report(args.db, args.path, args.breakdowns_root)
    if args.command == "calibrate":
        if args.calibrate_command == "check":
            return _cmd_calibrate_check(args.db, args.model, args.accept_aged)
        if args.calibrate_command == "open":
            return _cmd_calibrate_open(
                args.db, args.reviewer_model, args.workspace_root, args.throwaway_dir
            )
        if args.calibrate_command == "commit":
            return _cmd_calibrate_commit(
                args.db, args.model, args.workspace_root, args.memory_root, args.throwaway_dir
            )
        raise AssertionError(f"unreachable: unknown calibrate command {args.calibrate_command!r}")
    if args.command == "distill":
        if args.distill_command == "generate":
            return _cmd_distill_generate(args.db, args.run)
        if args.distill_command == "propose":
            return _cmd_distill_propose(args.db, args.run)
        raise AssertionError(f"unreachable: unknown distill command {args.distill_command!r}")
    if args.command == "queue":
        if args.queue_command == "list":
            return _cmd_queue_list(args.db, args.status, args.project)
        if args.queue_command == "resolve":
            return _cmd_queue_resolve(args.db, args.id, args.decision, args.by)
        raise AssertionError(f"unreachable: unknown queue command {args.queue_command!r}")
    raise AssertionError(f"unreachable: unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
