# citation-needed

A thin toolkit (Python + uv + SQLite) that gives every discrete choice embedded in LLM-facing
files — agent skills, memories, rules, `CLAUDE.md` files, plans — a **citation trail**: external
research literature as the primary citation class, internal workspace provenance (incidents,
investigation docs, memories) as the secondary class. A review extracts the discrete design
choices from one target artifact, cites each one (existing corpus first, live search for gaps),
classifies each as well-supported / needs-improvement / interesting, and emits an anchored 0–100
composite with a written interpretation guide. Reviews are **read-only toward targets** — output
is a breakdown doc plus database rows, never an edit to the reviewed file — and every vetted
citation persists to SQLite so the corpus compounds across reviews. "No literature found" is a
recorded, legitimate finding, not a failure.

> **Status:** plan complete ([plan.md](plan.md), 12 sections, grounded in ten investigations
> under [docs/research/](docs/research/)); implementation in progress via the tracked issues.

## Stack

| Layer | Tool | Why |
|---|---|---|
| Language / runtime | Python 3.12+, uv | Thin tool; no framework needed |
| Storage | SQLite (stdlib `sqlite3`, no ORM) + FTS5 | Corpus scale is hundreds–low-thousands of rows; auditable BM25 term matching beats an embeddings dependency at this scale |
| Details validation | pydantic | One model per artifact type mirrors `details_json`; tests assert model fields == live columns |
| HTTP | httpx | The verify gate needs SSRF-guarded resolve-before-fetch with per-redirect-hop validation |
| Parsing | PyYAML (frontmatter), stdlib `tomllib` | Artifact typing needs frontmatter; registry is TOML |
| CLI | argparse, entry point `cite` | Multi-verb, stdlib-only |
| LLM layer | Claude Code skills (`/citation-review`, `/citation-distill`, `/citation-sweep`, `/citation-triage`) | All judgment (extraction, classification, relevance) lives in the skill layer; **the CLI never calls an LLM** |
| Tests / quality | pytest, ruff, mypy | Standard gates |

## Prerequisites

- Python 3.12+ managed by [uv](https://docs.astral.sh/uv/)
- Live web access at review time (citation resolution calls Semantic Scholar, Crossref, and
  OpenAlex directly via httpx)
- Optional: a free [OpenAlex](https://openalex.org/) key in `CITATION_NEEDED_OPENALEX_KEY`
  (required since 2026-02-13 for the OpenAlex fallback tier; Semantic Scholar + Crossref work
  keyless)
- Claude Code (or a compatible harness) to run the four skills — the CLI alone is purely
  mechanical

## Setup

```powershell
# 1. install deps
uv sync

# 2. create the database (7 tables + FTS5 corpus index)
uv run cite init-db

# 3. optional: import the tracked CC0 seed corpus (Crossref-derived bibliographic
#    rows, verified live at seed-build time; idempotent — per-source terms and
#    per-row provenance in seed/PROVENANCE.md)
uv run cite seed import

# 4. optional: enable the OpenAlex fallback tier
$env:CITATION_NEEDED_OPENALEX_KEY = "<free key>"

# 5. calibrate (mandatory before the first real review; cached by fingerprint)
#    -> run /citation-review --calibrate in a Claude Code session

# quality gates
uv run pytest
uv run ruff check .
uv run mypy src
```

Back up the corpus by copying `data/citation.db` — it is produced by live search + review
judgment, so it is expensive and **not** rebuildable from the schema. `cite status` checkpoints
the WAL first so the file copy is consistent.

## The four use cases

| Use case | Skill | What it does |
|---|---|---|
| Review one target | `/citation-review <path>` | Extract → cite → classify → score → breakdown doc + DB rows |
| Distill one target | `/citation-distill <path>` | Trim/rewrite proposals where every cut is justified by a citation or a documented absence — proposals only, never edits |
| Project-wide rigor pass | `/citation-sweep <project>` | Enumerate LLM-facing files, cluster near-duplicate choices, batch-review, rank a backlog |
| Backlog triage | `/citation-triage` | Walk the queue with the operator; record keep / cut / rewrite per item |

## Key design decisions

- **Zero-touch reviews.** Reviewed targets gain zero bytes — no inline pointer lines. Output is
  a central breakdown doc (`breakdowns/<project>/<artifact-slug>.md`, gitignored) plus DB rows.
- **Two-layer split.** The CLI is deterministic and testable and never calls an LLM; extraction,
  classification, and relevance judgment run in the skill layer where the harness provides the
  model.
- **Anti-fabrication by construction.** `insert_citation()` is the only writer; a NOT NULL
  resolution record is captured from the actual fetch/API response; open-web quotes are verified
  by deterministic substring match against the raw fetched text (never an LLM judging the page);
  the fetch seam carries an SSRF guard. On any failure, nothing is inserted.
- **Calibration is a hard gate.** No real review runs until a frozen known-good anchor and a
  synthetic garbage anchor separate (≥65 / ≤35, margin ≥40, shape assertions) through the
  production path on a throwaway DB. Thresholds are never loosened to make a run pass.
- **Choice identity is semantic.** Choices are keyed by a stable `choice_key` that re-reviews
  reuse even when wording changes; removed choices are marked, never deleted, so their citations
  remain corpus assets.
- **Licensing-aware corpus.** Semantic Scholar results are used live-only (its dataset license
  forbids redistribution); the tracked seed corpus draws from Crossref (no abstracts) + OpenAlex
  (CC0) only, with per-source terms documented in `seed/PROVENANCE.md`. Code is MIT.

## Project structure

```
citation-needed/
├── plan.md                       # canonical entry plan (build steps live here)
├── schema.sql                    # canonical DDL (new DBs only)
├── migrations/                   # numbered migrations from v0.1
├── prompts/                      # versioned LLM prompt templates (calibration-fingerprinted)
├── src/citation_needed/          # cli, db, models, discover, corpus, resolve, verify,
│                                 # review, breakdown, distill, calibrate
├── fixtures/calibration/         # frozen good anchor + SYNTHETIC garbage anchor
├── seed/                         # tracked CC0 seed corpus + PROVENANCE.md
├── docs/
│   ├── interpretation-guide.md   # v1 score semantics (versioned)
│   └── research/                 # the ten Phase-0 investigation docs
├── data/                         # GITIGNORED — citation.db (the compounding corpus)
├── breakdowns/                   # GITIGNORED — review output, one doc per artifact
└── tests/
```

## License

[MIT](LICENSE). The seed corpus ships under the per-source terms documented in
`seed/PROVENANCE.md` (CC0-safe sources only).
