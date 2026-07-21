# citation-needed — CLAUDE.md

## Project overview

Thin, open-source toolkit (Python + uv + SQLite) that gives every discrete choice embedded in
LLM-facing files (skills, memories, rules, CLAUDE.md files, plans) a citation trail — external
research literature primary, internal workspace provenance secondary. Reviews are read-only
toward targets: output is a breakdown doc + DB rows; every vetted citation persists so the corpus
compounds across reviews.

## Stack

| Layer | Tool |
|---|---|
| Language / runtime | Python 3.12+, uv |
| Storage | SQLite via stdlib `sqlite3` (no ORM) + FTS5 corpus index; pydantic mirrors `details_json` |
| HTTP | httpx (SSRF-guarded fetch seam in `verify.py`) |
| Parsing | PyYAML (frontmatter), stdlib `tomllib` (observatory registry) |
| CLI | argparse, entry point `cite` |
| LLM layer | Claude Code skills `/citation-review`, `/citation-distill`, `/citation-sweep`, `/citation-triage` — thin wrappers in `dev/.claude/skills/citation-*/` (coding-root repo, junction-exposed); the CLI never calls an LLM |
| Tests / quality | pytest, ruff, mypy |

## Commands

```
uv sync --project citation-needed
uv run --project citation-needed cite init-db
uv run --project citation-needed cite status
uv run --project citation-needed cite scan --project <slug>
uv run --project citation-needed cite report <target-path>
uv run --project citation-needed cite queue list
uv run --project citation-needed cite seed import
uv run --project citation-needed pytest
uv run --project citation-needed ruff check .
uv run --project citation-needed mypy src
```

No server, no ports.

## Directory layout

```
plan.md                  # canonical entry plan (### Step N: blocks)
schema.sql               # canonical DDL (new DBs only); migrations/ own all changes after v0.1
src/citation_needed/     # cli, db, models, discover, corpus, resolve, verify, review,
                         # breakdown, distill, calibrate
prompts/                 # versioned LLM prompt templates (hash = calibration fingerprint A)
fixtures/calibration/    # frozen good anchor + SYNTHETIC garbage anchor
seed/                    # tracked CC0 seed corpus + PROVENANCE.md
docs/                    # interpretation-guide.md + research/ (9 Phase-0 investigations)
data/                    # GITIGNORED — citation.db (the compounding corpus)
breakdowns/              # GITIGNORED — <project>/<artifact-slug>.md review output
tests/
```

## Architecture summary

Two layers. The **CLI** (`cite`) is purely mechanical: DB + migrations, artifact discovery/typing
(incl. pointer-artifact resolution), FTS5 corpus-first lookup, structured-API citation resolution
(Semantic Scholar live-only; Crossref throttled from live rate headers; OpenAlex behind
`CITATION_NEEDED_OPENALEX_KEY`), the anti-fabrication verify gate (`insert_citation()` sole
writer; NOT NULL resolution record; deterministic substring quote match; SSRF guard), scoring
math, breakdown rendering, calibration gate + fingerprint cache. The **skill layer** does all LLM
judgment (choice extraction, classification via k≥3 majority vote, relevance) and drives the CLI
via stdin JSON. Calibration (frozen good/garbage anchors, 65/35/40 + shape assertions, throwaway
DB) hard-gates every real review; `cite review open` refuses on stale fingerprints.

## Current state

Plan written (plan.md, 12 sections), no code yet. Phase 0 research complete in
`docs/research/`. Next: plan-review → plan-redline → plan-wrap → repo-init → build-phase.

## Environment requirements

- Windows 11 (workspace default); paths in tool calls absolute, forward slashes in DB rows.
- uv-managed Python 3.12+ (no system pip).
- Optional env var `CITATION_NEEDED_OPENALEX_KEY` (free key, required since 2026-02-13 for the
  OpenAlex fallback tier; S2 + Crossref work keyless).
- Live web access at review time (WebSearch/WebFetch in the harness + direct httpx API calls).
- The nested repo boundary: this project gets its own `.git` (via /repo-init). Anchor all git
  operations here — from `dev/` root they land in coding-root. The four SKILL.md wrappers live in
  the CODING-ROOT repo (`dev/.claude/skills/citation-*/`) — cross-cutting changes need two
  commits in two repos.
- Reviews must never write into reviewed targets; breakdown docs + DB rows only.
