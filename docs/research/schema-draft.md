# citation-needed — SQLite schema draft

Status: pre-implementation research (no `plan.md` exists yet for this project). Read-only
investigation; nothing outside this file was modified. Written 2026-07-21 against workspace HEAD
`8c95f95faba483a63bc332a415feb9e2015635eb` (branch `switchboard-offload-plan`).

## 0. Verdict (read this first)

**7 tables**, exactly the required set, no extras: `artifacts`, `choices`, `citations`,
`choice_citations`, `review_runs`, `scores`, `distill_queue`.

**Choice identity mechanism (recommended): semantic key with a hash fast-path.** Each choice gets
a durable `choice_key` (an LLM-assigned kebab slug, unique per artifact) that a re-review is
instructed to *reuse* for anything still substantively the same decision, even if reworded —
because the extraction step is itself an LLM judgment call and a raw content hash breaks the
instant the extractor rephrases a sentence identically-in-meaning. A `content_hash_at_extraction`
column gives a cheap exact-match short-circuit for the common case (unrelated part of the file
changed, this choice's span didn't), so the LLM-mediated reuse-or-mint judgment is only needed
when the hash *doesn't* match. Span (start/end line) is stored but demoted to a **locator**, never
the identity key — the requirement explicitly needs identity to survive edits, and line numbers are
the first thing an edit invalidates. Full argument: §3.

**Two "argue, don't silently pick" calls, and they resolve in opposite directions** (§1, §4):
`artifacts` typing → JSON `details_json` column (many types, low cross-type query pressure, no
ORM). `citations` typing → real nullable columns + `CHECK` constraints (only 2 types, and this is
the trust-critical table where "does every external citation have a resolvable URL" must be a
DB-enforced invariant, not a JSON-buried convention). Same decision procedure, different answer,
because the inputs (type cardinality × query/constraint pressure) differ.

Full CREATE TABLE statements: §7. Worked example against a real workspace file with one verified
external citation and one verified internal citation: §8.

---

## 1. `artifacts` — typing decision (subtype tables vs JSON column)

The prompt requires this table be **TYPED** for memories vs skills vs rules vs CLAUDE.md files vs
plans, and requires the subtype-vs-JSON choice be argued, not defaulted.

### What the five types actually look like, inspected directly

- **memory** — real YAML frontmatter: `name`, `description`, `metadata: {node_type, type
  (feedback|user|project), originSessionId, modified}`. Verified against two live memory files:
  `C:/Users/abero/.claude/projects/c--Users-abero-dev/memory/feedback_win_capture_when_worth_it.md:1-7`
  and `.../memory/user_model_preference.md:1-6`. Also carries a **scope** axis not in the
  frontmatter itself: global (`.../c--Users-abero-dev/memory/`) vs per-project
  (`.../c--Users-abero-dev-Alpha4Gate/memory/`, `.../c--Users-abero-dev-toybox/memory/`) — derived
  from the directory name, not a file field.
- **skill** — real frontmatter, different shape: `name`, `description`, `user-invocable`. Verified
  against `.claude/skills/session-wrap/SKILL.md:1-4`.
- **rule** — no frontmatter at all in the files inspected (`.claude/rules/subagent-economy.md`,
  `.claude/rules/knowledge-placement.md`, `.claude/rules/security.md`) — just an H1 and prose, plus
  an informal `## Source` / `## Source memories` closing section naming the memories that motivated
  the rule (e.g. `subagent-economy.md:29-31`). The only "structured" fact worth capturing is that
  source-memory list.
- **CLAUDE.md** — no frontmatter; the load-bearing structural fact is root-vs-per-project (root
  `CLAUDE.md` auto-loads for every session in this tree; a project `CLAUDE.md` is that project's
  own). Per `knowledge-placement.md` §Tier decision tree, CLAUDE.md-inline is explicitly called out
  as "the most expensive tier" — worth a flag since it changes how urgently an unsupported choice
  here should surface in `distill_queue`.
- **plan** — per `.claude/rules/plan-and-issue-flow.md` and
  `.claude/rules/descriptor-contract.md` §4, plans carry `### Step N:` blocks
  (Problem/Type/Issue/Flags bullets) or `## Phase` headings, and a load-bearing distinction between
  a plan with **inline steps** vs a **pointer-only** plan (index that only links to sub-plans,
  which the dev-observatory observer can't extract a built/total ratio from — `descriptor-contract.md`
  §4: "yields a goal but no built/total ratio").

### The argument

The five type-specific field sets are **small (1–4 fields), non-overlapping, and never jointly
queried across types** — a citation-needed review always operates on *one* artifact at a time; the
tool never needs "every memory AND every skill where field X = Y" as a single cross-type SQL
predicate; per-type filtering ("all memories with `type=feedback` not reviewed in 90 days") is
always scoped to one type already, which a `WHERE artifact_type='memory' AND
json_extract(details_json,'$.node_type_type')=...` handles without a JOIN.

Given that shape, five subtype tables (`artifact_memory`, `artifact_skill`, `artifact_rule`,
`artifact_claude_md`, `artifact_plan`) buy schema-level `CHECK`/`NOT NULL` enforcement per type, but
cost 5 CREATE TABLEs, 5 migration surfaces, and a polymorphic-association pattern to join back to
`choices` — exactly the ORM-shaped ceremony the project brief rules out ("no heavyweight ORM,"
"thin toolkit"). A single `details_json TEXT` column (SQLite's JSON1 extension — `json_extract`,
`->`, `->>`, `json_valid()` — ships in the standard CPython `sqlite3` build, so this isn't a
functionality trade, only an enforcement-layer trade) keeps one table, one migration surface, and
still supports targeted queries.

**Direct internal precedent for exactly this trade already exists in the workspace and already
made the JSON-for-variable-secondary-shape call with confidence:**
`x-marks-the-spot/src/xmarks/schema.sql:231-239` — `score_snapshots.top_factors_json TEXT` holds a
per-row variable-shape payload alongside real, constrained columns (`band INTEGER CHECK (band
BETWEEN 1 AND 5)`) for the parts that ARE queried/constrained directly. That project is also a
thin uv+SQLite tool with **zero ORM**, confirmed in `x-marks-the-spot/CLAUDE.md:12` ("Storage:
SQLite via stdlib `sqlite3` (no ORM)").

**What is lost:** a malformed `details_json` for a given `artifact_type` (e.g. a memory row missing
`node_type`) is only caught in the Python/pydantic validation layer at write time, not by a DB
`CHECK` constraint. Mitigation, borrowed directly from the same x-marks-the-spot convention
(`x-marks-the-spot/CLAUDE.md:12` — "pydantic models mirror it" — and the schema.sql header,
`x-marks-the-spot/src/xmarks/schema.sql:4-6`, "the pydantic models in models.py mirror it, and
tests/test_schema.py asserts model fields == live columns so the two cannot drift"): define one
pydantic model per `artifact_type` (`MemoryDetails`, `SkillDetails`, `RuleDetails`,
`ClaudeMdDetails`, `PlanDetails`), validate against it before any `details_json` write, and add a
`tests/test_details_schema.py` that round-trips a golden JSON blob per type through the model.

**Escape hatch if a field gets hot.** If a specific `details_json` field later needs real SQL
filtering/indexing at scale, SQLite generated columns promote just that field without a schema
fork:

```sql
ALTER TABLE artifacts ADD COLUMN memory_kind TEXT
    GENERATED ALWAYS AS (json_extract(details_json, '$.node_type_type')) VIRTUAL;
CREATE INDEX idx_artifacts_memory_kind ON artifacts (memory_kind)
    WHERE artifact_type = 'memory';
```

### `details_json` shape per type (illustrative, validated by the pydantic models above)

| artifact_type | fields |
|---|---|
| `memory` | `node_type`, `memory_kind` (`feedback`\|`user`\|`project`), `origin_session_id`, `memory_scope` (`global`\|`project`), `frontmatter_modified` |
| `skill` | `user_invocable` (bool), `has_evals` (bool — an `evals/` dir present) |
| `rule` | `source_memory_paths` (JSON array — the informal `## Source` list) |
| `claude_md` | `scope` (`root`\|`project`), `project_slug` |
| `plan` | `plan_kind` (`root`\|`master`\|`feature`), `is_pointer_only` (bool, per `descriptor-contract.md` §4), `step_count`, `phase_count` |

### `project` column

Populated from path against the dev-observatory registry (`.claude/observatory/registry.toml`,
per `descriptor-contract.md` §2) where the path falls under a registered project; `'coding-root'`
for workspace-root-owned files (root rules/skills/CLAUDE.md, per `working-directory.md`'s
coding-root-vs-project vocabulary); for memory files, the project directory's suffix
(`c--Users-abero-dev-Alpha4Gate` → `Alpha4Gate`) or `'global'` for the root memory dir.

---

## 2. `citations` — typing decision (the opposite answer, and why)

`citations.kind IN ('external', 'internal')` — only **2** types, not 5, and this table is the one
whose entire job is trustworithiness: "never fabricate a citation" (per this task's hard rules) is
exactly the kind of invariant a DB `CHECK` constraint should enforce, not a JSON convention nothing
checks. `WHERE kind='external' AND url_or_doi IS NULL` must be **structurally impossible**, not
merely discouraged — the SQLite equivalent of x-marks-the-spot's own quarantine-by-schema pattern
(`x-marks-the-spot/src/xmarks/schema.sql:261-265`, the `facts_verified` view: "status != 'verified'
is structurally invisible here; the invariant is enforced by schema, not convention"). Real
columns + `CHECK (kind != 'external' OR url_or_doi IS NOT NULL)` (see §7) buys that; a JSON blob
would not (a bug in the writer could silently omit `url_or_doi` inside JSON and nothing would
refuse the insert).

This is the same decision procedure as §1 (type cardinality × query/constraint pressure) producing
the opposite conclusion, worth stating explicitly: `artifacts` has 5 types, low cross-type query
pressure, and no correctness invariant riding on any one field → JSON. `citations` has 2 types,
high query pressure (every review, every distill-queue rank calculation reads citation quality),
and a hard correctness invariant (resolvable references only) → real columns + `CHECK`.

---

## 3. (a) Stable choice identity across re-reviews

### Options considered

1. **Content-hash of the extracted choice text.** Rejected as the *sole* mechanism. Extraction is
   an LLM step; the same underlying decision can legitimately come out reworded between two review
   runs of the same artifact ("sub-agent returns terse verdict" vs "subagent must return a terse
   verdict") even with **zero** change to the artifact file itself. A pure hash treats that as a
   new choice → duplicate row, violating "must update/supersede rows, not duplicate them." This is
   the *common* case for an LLM-extraction pipeline, not an edge case.
2. **Semantic key, LLM-assigned and reuse-instructed.** A kebab slug (`choice_key`) the extractor
   mints per choice. The re-review prompt is fed the *prior* run's `(choice_key, summary)` pairs for
   this artifact and instructed: reuse a prior key verbatim for anything still the same decision
   (even reworded); mint a new key only for a genuinely new choice; anything not re-observed is
   marked `removed`. This is not a new mechanism bolted on — the extractor is already reading the
   whole artifact and reasoning about each choice; recognizing "is this the same choice as last
   run" is a marginal judgment on top of work already being done, not a separate system.
3. **Span anchors (line ranges / heading-path).** Rejected as the identity key — this is the
   *literal* failure mode the requirement names ("must survive the artifact being edited between
   reviews"): any edit above a span shifts every line number below it; a heading rename breaks a
   heading-path key. Retained as a **locator only** (`span_start_line`/`span_end_line` — where to
   point a human to re-read the choice today), explicitly non-identity-bearing.

### Recommendation: semantic key, with a hash fast-path

`choice_key` (semantic, LLM-assigned, reuse-instructed) is the identity, enforced by `UNIQUE
(artifact_id, choice_key)`. `content_hash_at_extraction` (sha256 of the literal extracted span) is
stored alongside as a **cheap exact-match short-circuit**: if a re-review's freshly extracted span
hashes identically to the stored one, the match is confirmed with zero LLM judgment (the overwhelmingly
common case — most re-reviews touch an unrelated part of a multi-choice file, so most choices'
spans are byte-identical run to run). Only a hash **mismatch** requires the LLM
reuse-or-mint judgment described above. This two-tier design is cheap in the common case and
correct in the target case (rewording) the requirement is actually worried about.

**Surrogate PK, not the natural key, for FK targets.** `choices.id` (an autoincrement integer)
is what `citations`/`scores`/`distill_queue` reference — not `choice_key` directly — so an operator
relabeling a slug later doesn't cascade a rewrite through every join table.

**Removal, not deletion.** A choice not re-observed in a re-review is marked `status='removed'` +
`superseded_at`, never hard-deleted. This matters because the citations attached to it remain real,
reusable corpus data (a paper doesn't stop existing because a workspace rule got trimmed) —
directly serving the brief's "every vetted citation persists to SQLite so the corpus compounds
across reviews."

---

## 4. (b) Provenance columns

Three levels, each carrying the requirement's three facts (content hash / git sha / timestamp):

- **`artifacts`** — `current_content_hash`, `current_git_sha`, `last_reviewed_at`: "what do we
  currently believe about this artifact," refreshed each review.
- **`review_runs`** — `artifact_content_hash_at_review`, `artifact_git_sha_at_review`,
  `started_at`/`finished_at`: the frozen snapshot **this specific run** actually reviewed. This is
  the row that answers "was this artifact edited since the last review" (`current_content_hash !=`
  the hash on the most recent `review_runs` row for it).
- **`citations`** (internal kind only) — `source_git_sha`, `source_line_ref`: internal documents
  drift too (`token-usage-levers-consolidated-2026-06-22.md` could be edited after being cited), so
  the citation's own provenance is refreshed at `verified_at` whenever a later review reuses it,
  not treated as a one-time fact.

`review_run_id` is threaded onto `choices` (`first_extracted_review_run_id`,
`last_confirmed_review_run_id`) and `choice_citations` (`first_linked_review_run_id`,
`last_confirmed_review_run_id`) so the full history of *when* a choice or a link was
established/reconfirmed is reconstructable without a separate time-series table per row.

---

## 5. (c) "No literature found" as a first-class result

A choice that got a genuine, honest, exhaustive-enough search and came back empty must be
**indistinguishable from a completed review**, not absent data. This lives on `scores`, not as a
missing `citations`/`choice_citations` row (a null result must never fabricate a placeholder
citation — there is no URL to store):

- `scores.literature_searched` (0/1) — was an external search actually attempted for this choice.
- `scores.literature_found` (0/1) — did it return anything usable.
- `scores.search_queries` — the actual query strings tried, so the null result is auditable, not a
  black box (this run's own worked example literally used the query `Liu et al 2023 "Lost in the
  Middle: How Language Models Use Long Contexts" arxiv` — see §8).
- `scores.unsupported` (0..1, the required dimension) — how much of the "this choice lacks backing"
  signal is driving the composite down.

This distinguishes three states that a naive "citations missing" heuristic would conflate:
**(1)** never checked (`literature_searched=0`) — a data gap, not a finding; **(2)** checked, found
nothing (`literature_searched=1, literature_found=0`) — the first-class "no literature found"
result the brief asks for; **(3)** checked, found something that argues against the choice
(`literature_found=1`, `scores.contradicted` high) — a different, worse outcome than (2). A query
like `WHERE literature_searched=1 AND literature_found=0` retrieves exactly the audit-worthy set of
"we looked, nothing exists yet" choices — the operator-triage signal for "someone should either
write this up as original research or soften the confidence of this choice."

---

## 6. (d) Schema versioning / migration approach

**Rejected: blindly copy x-marks-the-spot's "just rebuild from schema.sql" convention.** That
project's DB is a disposable cache — `x-marks-the-spot/CLAUDE.md:15` states "DB is derived from
git-tracked seed YAML"; `schema.sql` can be edited and the whole DB rebuilt because the source of
truth (`seeds/*.yaml`) is checked into git and cheap to re-run. **citation-needed's corpus does not
have that property.** Its rows come from live web search + review-session judgment (hours of
work, API calls, verification), not from a git-tracked seed file — rebuilding it is expensive, not
free. Copying the "rebuild-from-schema.sql" convention here would be copying a convention whose
justification doesn't transfer; flagging this rather than defaulting to the nearest internal
precedent.

**Recommended approach, tailored to that asymmetry:**

1. **`schema.sql`** is still the single canonical CREATE-TABLE source (following the "one source of
   truth" convention itself, `.claude/rules/code-quality.md` §"One source of truth for
   data-shape constants") — but it is only ever *executed* against a brand-new, empty DB
   (`citation-needed db init`), same idempotent `CREATE TABLE IF NOT EXISTS` shape already used in
   this workspace at `agora/ledger/db.py:8-22`.
2. **From v0.1 — not deferred — any change to an existing DB ships as a numbered migration file**:
   `migrations/0002_add_composite_band.sql`, applied in filename order by a small (~30-line, no
   framework) `db.py::migrate(conn)` that reads `PRAGMA user_version`, applies every pending
   numbered file inside its own transaction, and bumps `user_version` to that file's number at the
   end. `review_runs.tool_schema_version` (see §7) records the `user_version` a given review ran
   under, so a corpus spanning multiple schema versions stays legible.
3. **No ORM, no Alembic** — the loop in (2) *is* the entire migration mechanism, matching the
   project brief's explicit "thin uv+SQLite tool (no heavyweight ORM)" constraint.
4. **`tests/test_schema.py`**, mirroring `x-marks-the-spot/src/xmarks/schema.sql:4-6`'s own
   convention (pydantic models mirror the DB; a test asserts fields == live columns): assert each
   `details_json` pydantic model's fields round-trip through a golden blob, and that a fresh
   `db init` + full migration replay land on an identical `PRAGMA table_info` for every table —
   catching schema.sql/migrations drift mechanically, the same "assert `is`, not just `==`" spirit
   `.claude/rules/code-quality.md` calls for at the data-shape-constant level, applied here at the
   DB/model boundary.

---

## 7. CREATE TABLE statements

Creation order matters (no forward FK references): `artifacts` → `review_runs` → `choices` →
`citations` → `choice_citations` → `scores` → `distill_queue`.

```sql
-- citation-needed — schema.sql (DRAFT v0)
-- Single source of truth for a brand-new DB only (see §6 for why migrations,
-- not schema.sql edits, own every change to an existing corpus).
-- stdlib sqlite3, no ORM — mirrors x-marks-the-spot/src/xmarks/schema.sql's own convention.

PRAGMA foreign_keys = ON;

-- ============================================================
-- artifacts — one row per LLM-facing file this tool has reviewed.
-- TYPING: hybrid (see §1). Common columns are real; type-specific
-- fields live in details_json (JSON1-queryable), validated at the
-- pydantic layer, promotable to a generated column if one gets hot.
-- ============================================================
CREATE TABLE IF NOT EXISTS artifacts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    path                 TEXT NOT NULL UNIQUE,     -- workspace-relative, forward slashes
    artifact_type        TEXT NOT NULL CHECK (artifact_type IN
                              ('memory', 'skill', 'rule', 'claude_md', 'plan')),
    project              TEXT NOT NULL,            -- registry slug, or 'coding-root' / 'global'
    is_active            INTEGER NOT NULL DEFAULT 1,   -- 0 once the file is deleted/moved
    current_content_hash TEXT,                     -- sha256 of file bytes, refreshed each review
    current_git_sha      TEXT,                     -- HEAD sha at last review
    first_seen_at        TEXT NOT NULL,            -- ISO 8601 UTC
    last_reviewed_at     TEXT,
    details_json         TEXT,                     -- type-specific fields; see §1 table
    CHECK (details_json IS NULL OR json_valid(details_json))
);
CREATE INDEX IF NOT EXISTS idx_artifacts_type_project ON artifacts (artifact_type, project);

-- ============================================================
-- review_runs — one row per review pass over one artifact.
-- Carries provenance (b): content hash + git sha + timestamps AT
-- REVIEW TIME, frozen, independent of artifacts' "current" fields.
-- ============================================================
CREATE TABLE IF NOT EXISTS review_runs (
    id                               INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id                      INTEGER NOT NULL REFERENCES artifacts (id),
    started_at                      TEXT NOT NULL,
    finished_at                     TEXT,
    artifact_content_hash_at_review TEXT NOT NULL,
    artifact_git_sha_at_review      TEXT,               -- NULL only if workspace was dirty
    reviewer_model                  TEXT NOT NULL,       -- e.g. 'claude-sonnet-5'
    tool_schema_version              INTEGER NOT NULL,   -- PRAGMA user_version this run ran under
    status                          TEXT NOT NULL DEFAULT 'completed'
                                        CHECK (status IN ('completed', 'aborted')),
    notes                           TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_runs_artifact ON review_runs (artifact_id, started_at);

-- ============================================================
-- choices — one durable row per distinct decision extracted from
-- an artifact. Re-reviews UPDATE via choice_key match (§3); they
-- never insert a duplicate for the same underlying decision.
-- ============================================================
CREATE TABLE IF NOT EXISTS choices (
    id                             INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id                    INTEGER NOT NULL REFERENCES artifacts (id),
    choice_key                     TEXT NOT NULL,        -- kebab slug; the identity (§3)
    summary                        TEXT NOT NULL,
    quote_or_span                  TEXT,                 -- literal extracted text, for audit
    span_start_line                INTEGER,               -- LOCATOR only, never identity (§3)
    span_end_line                  INTEGER,
    content_hash_at_extraction     TEXT NOT NULL,         -- sha256(quote_or_span); fast-path match
    status                         TEXT NOT NULL DEFAULT 'active'
                                       CHECK (status IN ('active', 'superseded', 'removed')),
    first_extracted_review_run_id  INTEGER NOT NULL REFERENCES review_runs (id),
    last_confirmed_review_run_id   INTEGER NOT NULL REFERENCES review_runs (id),
    superseded_at                  TEXT,
    UNIQUE (artifact_id, choice_key)
);
CREATE INDEX IF NOT EXISTS idx_choices_artifact ON choices (artifact_id, status);
CREATE INDEX IF NOT EXISTS idx_choices_hash ON choices (content_hash_at_extraction);

-- ============================================================
-- citations — the deduplicated corpus. TYPING: real columns +
-- CHECK, not JSON (§2) — this is the trust-critical table.
-- One row per distinct source; reused across many choices/artifacts.
-- ============================================================
CREATE TABLE IF NOT EXISTS citations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                 TEXT NOT NULL CHECK (kind IN ('external', 'internal')),
    natural_key          TEXT NOT NULL,   -- normalized URL/DOI (external) or workspace path (internal)
    title                TEXT,
    -- external-only (NULL when kind='internal'):
    authors              TEXT,
    year                 INTEGER,
    venue                TEXT,
    url_or_doi           TEXT,
    -- internal-only (NULL when kind='external'):
    workspace_path       TEXT,
    -- shared verification provenance, refreshed whenever a later review reuses this row:
    verified_at          TEXT NOT NULL,
    verification_method  TEXT NOT NULL,   -- 'WebFetch' | 'WebSearch' | 'internal-read'
    source_git_sha       TEXT,            -- internal only: workspace sha at last verification
    source_line_ref      TEXT,            -- internal only: 'path:line' at last verification (drifts)
    notes                TEXT,
    UNIQUE (kind, natural_key),
    CHECK (kind != 'external' OR url_or_doi IS NOT NULL),   -- never fabricate: DB-enforced
    CHECK (kind != 'internal' OR workspace_path IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_citations_kind ON citations (kind);

-- ============================================================
-- choice_citations — join table. Per-link relevance note is
-- REQUIRED (never a bare join), plus a supports/contradicts axis
-- so a citation can argue FOR or AGAINST a choice.
-- ============================================================
CREATE TABLE IF NOT EXISTS choice_citations (
    choice_id                    INTEGER NOT NULL REFERENCES choices (id),
    citation_id                  INTEGER NOT NULL REFERENCES citations (id),
    relevance_note               TEXT NOT NULL,
    support_direction            TEXT NOT NULL CHECK (support_direction IN
                                      ('supports', 'contradicts', 'tangential')),
    first_linked_review_run_id   INTEGER NOT NULL REFERENCES review_runs (id),
    last_confirmed_review_run_id INTEGER NOT NULL REFERENCES review_runs (id),
    PRIMARY KEY (choice_id, citation_id)
);

-- ============================================================
-- scores — per (review_run, choice). The 4 required dimensions,
-- the anchored composite + band, and (c) the first-class
-- "no literature found" fields.
-- ============================================================
CREATE TABLE IF NOT EXISTS scores (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    review_run_id                INTEGER NOT NULL REFERENCES review_runs (id),
    choice_id                    INTEGER NOT NULL REFERENCES choices (id),
    evidence_backed              REAL NOT NULL CHECK (evidence_backed BETWEEN 0 AND 1),
    unsupported                  REAL NOT NULL CHECK (unsupported BETWEEN 0 AND 1),
    contradicted                 REAL NOT NULL CHECK (contradicted BETWEEN 0 AND 1),
    interesting_novel            REAL NOT NULL CHECK (interesting_novel BETWEEN 0 AND 1),
    classification               TEXT NOT NULL CHECK (classification IN
                                      ('well-supported', 'needs-improvement', 'interesting')),
    composite                    REAL NOT NULL,
    composite_band               TEXT NOT NULL,
    interpretation_guide_version TEXT NOT NULL,
    rationale                    TEXT,
    literature_searched          INTEGER NOT NULL DEFAULT 0,
    literature_found             INTEGER NOT NULL DEFAULT 0,
    search_queries               TEXT,    -- (c): queries tried, present even on a null result
    UNIQUE (review_run_id, choice_id)
);
CREATE INDEX IF NOT EXISTS idx_scores_choice ON scores (choice_id);

-- ============================================================
-- distill_queue — citation-justified trim/distill proposals,
-- ranked for operator backlog triage. proposal_kind reuses
-- knowledge-placement.md's own tier vocabulary (dogfooding it).
-- ============================================================
CREATE TABLE IF NOT EXISTS distill_queue (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    choice_id                INTEGER NOT NULL REFERENCES choices (id),
    artifact_id              INTEGER NOT NULL REFERENCES artifacts (id),  -- denormalized for query speed
    review_run_id            INTEGER NOT NULL REFERENCES review_runs (id),
    proposal_kind            TEXT NOT NULL CHECK (proposal_kind IN
                                 ('move-to-rule', 'move-to-reference', 'move-to-skill-trigger',
                                  'move-to-memory-pointer', 'trim', 'rewrite',
                                  'delete-superseded', 'no-action')),
    rank                     REAL NOT NULL,      -- higher = more urgent; see §7.1 formula
    justification            TEXT NOT NULL,
    justifying_citation_ids  TEXT,               -- JSON array of citations.id
    status                   TEXT NOT NULL DEFAULT 'open'
                                 CHECK (status IN ('open', 'accepted', 'rejected', 'applied')),
    created_at               TEXT NOT NULL,
    resolved_at              TEXT,
    resolved_by              TEXT,
    CHECK (justifying_citation_ids IS NULL OR json_valid(justifying_citation_ids))
);
CREATE INDEX IF NOT EXISTS idx_distill_queue_status_rank ON distill_queue (status, rank DESC);

PRAGMA user_version = 1;
```

### 7.1 Composite / band / classification / ranking formulas (proposed, versioned)

Stored in `scores.interpretation_guide_version` so a later revision of the cutpoints doesn't
silently reinterpret old rows.

**`composite`** (v0): `evidence_backed − contradicted − 0.3·unsupported + 0.2·interesting_novel`,
clamped to `[0, 1]`.

**`composite_band`** (v0, mirrors x-marks-the-spot's own "ordinal bands, never raw decimals for
display" convention — `x-marks-the-spot/CLAUDE.md:51`, and structurally `score_snapshots.band` vs
`.logodds_raw` at `x-marks-the-spot/src/xmarks/schema.sql:231-239`):

| composite | band |
|---|---|
| ≥ 0.70 | `strong` |
| 0.40 – 0.69 | `adequate` |
| 0.15 – 0.39 | `weak` |
| < 0.15 | `unsupported` |

**`classification`** (v0, priority-ordered — first match wins):
1. `needs-improvement` if `unsupported ≥ 0.5` or `contradicted ≥ 0.4`
2. `well-supported` if `evidence_backed ≥ 0.6` and `contradicted < 0.2`
3. `interesting` if `interesting_novel ≥ 0.6` and `evidence_backed < 0.4`
4. else `needs-improvement` (default)

**`distill_queue.rank`** (v0): `(1 − composite) × artifact_load_weight`, where
`artifact_load_weight` is higher for `claude_md` (auto-loaded every session — the most expensive
tier per `knowledge-placement.md` §Tier decision tree item 1) than for a `skill` (loads only on
trigger) — so an unsupported CLAUDE.md-inline choice outranks an equally-unsupported skill choice
for triage attention, operationalizing the tier tree's own cost ordering rather than inventing a
separate weighting scheme.

---

## 8. Worked example: `subagent-economy.md` Rule 1

Target choice: `.claude/rules/subagent-economy.md:9-10` ("sub-agent returns a terse verdict; detail
goes to a file the orchestrator reads only on failure/when needed"). Real provenance values below
were captured this run: `git rev-parse HEAD` → `8c95f95faba483a63bc332a415feb9e2015635eb`;
`sha256sum .claude/rules/subagent-economy.md` →
`b7b78480aa69dcd965e6f4b3b1e94fb232be5dc44f389301c2f2700a790bc94b`.

```sql
-- 1 row: the artifact
INSERT INTO artifacts
    (id, path, artifact_type, project, is_active, current_content_hash, current_git_sha,
     first_seen_at, last_reviewed_at, details_json)
VALUES
    (1, '.claude/rules/subagent-economy.md', 'rule', 'coding-root', 1,
     'b7b78480aa69dcd965e6f4b3b1e94fb232be5dc44f389301c2f2700a790bc94b',
     '8c95f95faba483a63bc332a415feb9e2015635eb',
     '2026-07-21T00:00:00Z', '2026-07-21T20:04:00Z',
     '{"source_memory_paths":["docs/investigations/token-usage-levers-consolidated-2026-06-22.md",
       "docs/investigations/high-context-usage-2026-06-22.md"]}');

-- 1 row: the review run
INSERT INTO review_runs
    (id, artifact_id, started_at, finished_at, artifact_content_hash_at_review,
     artifact_git_sha_at_review, reviewer_model, tool_schema_version, status, notes)
VALUES
    (1, 1, '2026-07-21T20:00:00Z', '2026-07-21T20:04:00Z',
     'b7b78480aa69dcd965e6f4b3b1e94fb232be5dc44f389301c2f2700a790bc94b',
     '8c95f95faba483a63bc332a415feb9e2015635eb',
     'claude-sonnet-5', 1, 'completed',
     'Illustrative pass for the schema-draft worked example.');

-- 1 row: the choice (quote is the literal text of subagent-economy.md:9-10)
INSERT INTO choices
    (id, artifact_id, choice_key, summary, quote_or_span, span_start_line, span_end_line,
     content_hash_at_extraction, status, first_extracted_review_run_id, last_confirmed_review_run_id)
VALUES
    (1, 1, 'subagent-terse-verdict-file-detail',
     'Sub-agent returns a terse verdict; longer detail goes to a file the orchestrator reads only when the verdict requires it, not eagerly.',
     '- Return only the load-bearing verdict — a PASS/BLOCKED/verdict line, counts, and at most the single most important finding. Target a handful of lines, not paragraphs.
- Write any longer detail to a file (e.g. `<worktree>/.build-step/<role>-report.md`, a findings `.json`, an investigation doc) and return only its path. The orchestrator reads that file only when the verdict requires it (e.g. on BLOCKED/NEEDS-WORK, to feed findings back to the developer) — not eagerly.',
     9, 10,
     'e3fd2e64e3c8b200a744dd3446c0c2e4557b131d2d8b1b366cad86e2a2f7a4f7',   -- sha256 of the quote text above, UTF-8 + trailing newline
     'active', 1, 1);

-- 2 rows: citations (1 external, verified this run; 1 internal, verified this run)
INSERT INTO citations
    (id, kind, natural_key, title, authors, year, venue, url_or_doi, workspace_path,
     verified_at, verification_method, source_git_sha, source_line_ref, notes)
VALUES
    (1, 'external', 'https://arxiv.org/abs/2307.03172',
     'Lost in the Middle: How Language Models Use Long Contexts',
     'Nelson F. Liu, Kevin Lin, John Hewitt, Ashwin Paranjape, Michele Bevilacqua, Fabio Petroni, Percy Liang',
     2023, 'arXiv preprint 2307.03172 (v1 2023-07-06, v3 2023-11-20); later TACL 12 (2024), https://aclanthology.org/2024.tacl-1.9/',
     'https://arxiv.org/abs/2307.03172', NULL,
     '2026-07-21T20:01:00Z', 'WebFetch', NULL, NULL,
     'Verified this run: WebSearch located arXiv:2307.03172, WebFetch confirmed title/authors/submission date directly from arxiv.org/abs/2307.03172.'),
    (2, 'internal', 'docs/investigations/token-usage-levers-consolidated-2026-06-22.md',
     'Token-usage reduction — consolidated lever map (2026-06-22), Lever 2',
     NULL, NULL, NULL, NULL,
     'docs/investigations/token-usage-levers-consolidated-2026-06-22.md',
     '2026-07-21T20:02:00Z', 'internal-read',
     '8c95f95faba483a63bc332a415feb9e2015635eb',
     'docs/investigations/token-usage-levers-consolidated-2026-06-22.md:61-64',
     'The source subagent-economy.md itself names in its own "## Source" footer (subagent-economy.md:31).');

-- 2 rows: choice_citations links
INSERT INTO choice_citations
    (choice_id, citation_id, relevance_note, support_direction,
     first_linked_review_run_id, last_confirmed_review_run_id)
VALUES
    (1, 1,
     'Indirect support: establishes LLMs under-attend to / degrade on information placed mid-long-context, which motivates why an ever-growing resident tool_result is costly beyond token price — not a direct empirical test of the terse-verdict-vs-file pattern itself.',
     'supports', 1, 1),
    (1, 2,
     'Direct support: the actual measurement this rule cites as its own Source — 18% of a representative build-phase window is Agent returns averaging ~240k chars instead of a one-line verdict (token-usage-levers-consolidated-2026-06-22.md:61-64).',
     'supports', 1, 1);

-- 1 row: the score
INSERT INTO scores
    (id, review_run_id, choice_id, evidence_backed, unsupported, contradicted, interesting_novel,
     classification, composite, composite_band, interpretation_guide_version, rationale,
     literature_searched, literature_found, search_queries)
VALUES
    (1, 1, 1, 0.75, 0.0, 0.0, 0.15, 'well-supported', 0.78, 'strong', 'v0',
     'Strong internal-empirical backing (the workspace''s own measured leak, citation 2) plus supportive-but-indirect external literature (citation 1 motivates the practice generally; it does not directly benchmark verdict-vs-file as an intervention). composite = 0.75 - 0 - 0.3*0 + 0.2*0.15 = 0.78.',
     1, 1, 'Liu et al 2023 "Lost in the Middle: How Language Models Use Long Contexts" arxiv');

-- distill_queue: NO row for this choice — classification='well-supported' → proposal_kind
-- would be 'no-action', and the v0 pipeline only writes a row when a choice is NOT
-- well-supported (an operator backlog entry for a fine choice is noise, not signal).
```

**Illustrative-only** (not a real finding — shown purely to demonstrate the row shape for the
opposite case, a choice the pipeline judged unsupported):

```sql
-- HYPOTHETICAL, for shape illustration only — not derived from a real review.
INSERT INTO distill_queue
    (choice_id, artifact_id, review_run_id, proposal_kind, rank, justification,
     justifying_citation_ids, status, created_at)
VALUES
    (99, 12, 7, 'move-to-memory-pointer', 0.61,
     'Hypothetical: an unsupported CLAUDE.md-inline claim found no external literature and no internal measurement backing it; propose demoting it to a memory pointer per knowledge-placement.md tier 5.',
     NULL, 'open', '2026-07-21T20:05:00Z');
```

---

## 9. Open questions for whoever plans this project next

- Whether `choice_key` minting/reuse should be a dedicated LLM sub-step (fed the prior run's key
  list) or folded into the same extraction pass that reads the artifact — folding it in is cheaper
  per `.claude/rules/subagent-economy.md` itself, but a dedicated diff-pass may be more reliable for
  artifacts with many (10+) choices where reuse judgment gets harder to hold in one pass.
  Recommend prototyping both against `subagent-economy.md` (2 choices) and a busier artifact (e.g.
  `.claude/rules/plan-and-issue-flow.md`) before committing.
  small enough not to need it yet, but the corpus-is-expensive asymmetry (§6) means don't defer past v0.1.
- `project` resolution against the observatory registry (`descriptor-contract.md` §2) needs a real
  parser once this project exists — not designed here, only assumed as a lookup.
- The `composite`/`band`/`classification`/`rank` formulas in §7.1 are a starting proposal (flagged
  `v0` precisely so they're revisable) — calibrating them needs `measurement-validity.md`'s own
  "calibrate with anchors before comparing candidates" discipline: feed the pipeline a frozen
  known-well-supported choice and a frozen known-unsupported choice and assert the ordering holds,
  before trusting comparative rankings across many artifacts.

---

## Sources

**External (web, verified this run):**
- [Lost in the Middle: How Language Models Use Long Contexts (arXiv:2307.03172)](https://arxiv.org/abs/2307.03172) — Liu, Lin, Hewitt, Paranjape, Bevilacqua, Petroni, Liang, 2023. Verified via WebSearch + WebFetch this run; confirmed title/authors/submission date (v1 2023-07-06, v3 2023-11-20).
- [Lost in the Middle — ACL Anthology / TACL 12 (2024)](https://aclanthology.org/2024.tacl-1.9/) — the later peer-reviewed venue for the same paper, surfaced by the same search.

**Internal workspace (path:line):**
- `.claude/rules/subagent-economy.md:3-31` — the target rule (Rule 1 at :9-10, Source footer at :31).
- `docs/investigations/token-usage-levers-consolidated-2026-06-22.md:56-71` (Lever 2), specifically `:61-64` — the internal citation used in §8.
- `.claude/rules/knowledge-placement.md` — Tier decision tree (referenced §1, §7.1); "Capturing a win vs a regression" five-gate test (methodological analogue for `distill_queue` triage discipline).
- `.claude/rules/descriptor-contract.md` §1–§4 — scrapable-artifact conventions informing `artifacts.details_json` for `plan` and `claude_md` types; §2 (registry) informing `artifacts.project`.
- `.claude/rules/measurement-validity.md` — "calibrate with anchors before comparing candidates," cited in §9 for how the composite/band formulas should eventually be validated.
- `.claude/rules/plan-and-issue-flow.md` — `### Step N:` plan-step shape informing the `plan` artifact type.
- `.claude/rules/code-quality.md` § "One source of truth for data-shape constants" — cited in §6 for the schema.sql/migrations discipline.
- `.claude/rules/working-directory.md` — coding-root vs project vocabulary, informing `artifacts.project` defaults.
- `x-marks-the-spot/src/xmarks/schema.sql:1-10, :231-239, :261-265` — the workspace's own thin uv+SQLite-no-ORM precedent: header conventions, `top_factors_json` (JSON-for-variable-payload precedent, §1), `score_snapshots.band`/`logodds_raw` split (ordinal-band-over-raw-decimal precedent, §7.1), `facts_verified` view (schema-enforced quarantine precedent, §2).
- `x-marks-the-spot/CLAUDE.md:12, :15, :51` — "no ORM," "DB derived from git-tracked seed YAML" (the rebuild-cheap property citation-needed's corpus lacks, §6), "Scores display as 5-level ordinal bands, never decimals."
- `agora/ledger/db.py:8-22` — `CREATE TABLE IF NOT EXISTS` idempotent-init convention, reused in §7.
- Live memory files (frontmatter shape verified directly, informing the `memory` artifact type in §1): `C:/Users/abero/.claude/projects/c--Users-abero-dev/memory/feedback_win_capture_when_worth_it.md:1-7`, `.../memory/user_model_preference.md:1-6`.
- `.claude/skills/session-wrap/SKILL.md:1-4` — SKILL.md frontmatter shape verified directly, informing the `skill` artifact type in §1.
