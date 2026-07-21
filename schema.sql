-- citation-needed — schema.sql (canonical DDL, v1)
--
-- Executed ONLY against a brand-new DB (via `cite init-db`). The corpus is produced by live
-- search + review judgment — expensive, NOT rebuildable-from-seed — so every change to an
-- existing DB after v0.1 ships as a numbered migration in migrations/ (applied in filename
-- order by db.migrate() reading PRAGMA user_version). Never edit an existing DB by editing
-- this file. See plan.md §3.1 and docs/research/schema-draft.md §6.
--
-- Baseline: docs/research/schema-draft.md §7, adopted with the four plan.md §3.1 amendments
-- (amendments WIN on conflict):
--   (1) the four scores dimension columns store k-sample VOTE SHARES (REAL 0..1), named
--       *_share — not judge-emitted continuous scores (plan.md §4.4);
--   (2) citations verification column is resolution_method with CHECK enum
--       ('api_structured', 'web_fetch_verified', 'internal-read');
--   (3) artifacts.path uses the two-scheme form (workspace-relative | 'memory:' prefix),
--       UNIQUE, forward slashes;
--   (4) choices gains source_path (nullable) — the file a quote/span was actually extracted
--       from when it differs from artifacts.path (pointer-resolved skills/plans, CLAUDE.md
--       @-imports, evals/ sidecars); NULL means the artifact's own file.
--
-- The pydantic models in src/citation_needed/models.py mirror details_json per artifact_type,
-- and tests/test_schema.py asserts the DETAILS_MODELS registry covers exactly the
-- artifact_type CHECK enum below, so the two cannot drift.

PRAGMA foreign_keys = ON;  -- per-connection; db.connect() re-asserts this on every connection

-- ============================================================
-- artifacts — one row per LLM-facing file this tool has reviewed.
-- TYPING: hybrid (schema-draft.md §1). Common columns are real;
-- type-specific fields live in details_json (JSON1-queryable),
-- validated at the pydantic layer, promotable to a generated
-- column if one gets hot.
-- ============================================================
CREATE TABLE IF NOT EXISTS artifacts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Two-scheme form (plan.md §3.1 amendment 3): workspace-relative for in-tree artifacts;
    -- 'memory:<project-dir-slug>/<file>.md' for memory artifacts (they live outside the
    -- workspace). Always forward slashes — enforced below.
    path                 TEXT NOT NULL UNIQUE,
    artifact_type        TEXT NOT NULL CHECK (artifact_type IN
                              ('memory', 'skill', 'rule', 'claude_md', 'plan')),
    project              TEXT NOT NULL,            -- registry slug, or 'coding-root' / 'global'
    is_active            INTEGER NOT NULL DEFAULT 1,   -- 0 once the file is deleted/moved
    current_content_hash TEXT,                     -- sha256 of file bytes, refreshed each review
    current_git_sha      TEXT,                     -- HEAD sha at last review
    first_seen_at        TEXT NOT NULL,            -- ISO 8601 UTC
    last_reviewed_at     TEXT,
    details_json         TEXT,                     -- type-specific fields; pydantic-validated
    CHECK (details_json IS NULL OR json_valid(details_json)),
    CHECK (path NOT LIKE '%\%')                    -- forward slashes only (amendment 3)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_type_project ON artifacts (artifact_type, project);

-- ============================================================
-- review_runs — one row per review pass over one artifact.
-- Frozen provenance: content hash + git sha + timestamps AT
-- REVIEW TIME, independent of artifacts' "current" fields.
-- ============================================================
CREATE TABLE IF NOT EXISTS review_runs (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id                     INTEGER NOT NULL REFERENCES artifacts (id),
    started_at                      TEXT NOT NULL,
    finished_at                     TEXT,
    artifact_content_hash_at_review TEXT NOT NULL,
    artifact_git_sha_at_review      TEXT,               -- NULL only if workspace was dirty
    reviewer_model                  TEXT NOT NULL,      -- e.g. 'claude-sonnet-5'
    tool_schema_version             INTEGER NOT NULL,   -- PRAGMA user_version this run ran under
    status                          TEXT NOT NULL DEFAULT 'completed'
                                        CHECK (status IN ('completed', 'aborted')),
    notes                           TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_runs_artifact ON review_runs (artifact_id, started_at);

-- ============================================================
-- choices — one durable row per distinct decision extracted from
-- an artifact. Re-reviews UPDATE via choice_key match; they never
-- insert a duplicate for the same underlying decision. Span lines
-- are LOCATOR only, never identity (schema-draft.md §3).
-- ============================================================
CREATE TABLE IF NOT EXISTS choices (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id                   INTEGER NOT NULL REFERENCES artifacts (id),
    choice_key                    TEXT NOT NULL,        -- kebab slug; the identity
    summary                       TEXT NOT NULL,
    quote_or_span                 TEXT,                 -- literal extracted text, for audit
    span_start_line               INTEGER,              -- LOCATOR only, never identity
    span_end_line                 INTEGER,
    -- File the quote/span was actually extracted from when it differs from artifacts.path
    -- (pointer-resolved skills/plans, CLAUDE.md @-imports, evals/ sidecars). NULL means the
    -- artifact's own file. (plan.md §3.1 amendment 4)
    source_path                   TEXT,
    content_hash_at_extraction    TEXT NOT NULL,        -- sha256(quote_or_span); fast-path match
    status                        TEXT NOT NULL DEFAULT 'active'
                                      CHECK (status IN ('active', 'superseded', 'removed')),
    first_extracted_review_run_id INTEGER NOT NULL REFERENCES review_runs (id),
    last_confirmed_review_run_id  INTEGER NOT NULL REFERENCES review_runs (id),
    superseded_at                 TEXT,
    UNIQUE (artifact_id, choice_key)
);
CREATE INDEX IF NOT EXISTS idx_choices_artifact ON choices (artifact_id, status);
CREATE INDEX IF NOT EXISTS idx_choices_hash ON choices (content_hash_at_extraction);

-- ============================================================
-- citations — the deduplicated corpus. TYPING: real columns +
-- CHECK, not JSON (schema-draft.md §2) — this is the trust-
-- critical table: a fabricated citation must be STRUCTURALLY
-- impossible, not merely discouraged. insert_citation() is the
-- sole writer (plan.md §4.2).
-- ============================================================
CREATE TABLE IF NOT EXISTS citations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL CHECK (kind IN ('external', 'internal')),
    natural_key       TEXT NOT NULL,   -- normalized URL/DOI (external) or workspace path (internal)
    title             TEXT,
    -- external-only (NULL when kind='internal'):
    authors           TEXT,
    year              INTEGER,
    venue             TEXT,
    url_or_doi        TEXT,
    -- internal-only (NULL when kind='external'):
    workspace_path    TEXT,
    -- shared verification provenance, refreshed whenever a later review reuses this row:
    verified_at       TEXT NOT NULL,
    -- Amendment 2 (plan.md §3.1): resolution_method supersedes schema-draft's
    -- verification_method. There is no 'llm_claimed' state to occupy.
    resolution_method TEXT NOT NULL CHECK (resolution_method IN
                          ('api_structured', 'web_fetch_verified', 'internal-read')),
    -- Resolution-record text captured at insert time from the actual fetch/API response
    -- (plan.md §4.2); indexed by citations_fts for corpus-first lookup
    -- (docs/research/citation-mechanics.md §e):
    supporting_quote  TEXT,            -- deterministically substring-matched against fetched text
    keywords          TEXT,            -- salient terms for FTS5 corpus-first search
    source_git_sha    TEXT,            -- internal only: workspace sha at last verification
    source_line_ref   TEXT,            -- internal only: 'path:line' at last verification (drifts)
    notes             TEXT,
    UNIQUE (kind, natural_key),
    CHECK (kind != 'external' OR url_or_doi IS NOT NULL),   -- never fabricate: DB-enforced
    CHECK (kind != 'internal' OR workspace_path IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_citations_kind ON citations (kind);

-- ============================================================
-- citations_fts — FTS5 external-content index over the citations
-- corpus (corpus-first BM25 lookup before any web call;
-- citation-mechanics.md §e). External-content mode stores only
-- the index, never a second copy of the text; the triggers below
-- keep it in sync with the content table.
-- ============================================================
CREATE VIRTUAL TABLE IF NOT EXISTS citations_fts USING fts5(
    title, supporting_quote, keywords, notes,
    content='citations', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS citations_fts_ai AFTER INSERT ON citations BEGIN
    INSERT INTO citations_fts (rowid, title, supporting_quote, keywords, notes)
    VALUES (new.id, new.title, new.supporting_quote, new.keywords, new.notes);
END;

CREATE TRIGGER IF NOT EXISTS citations_fts_ad AFTER DELETE ON citations BEGIN
    INSERT INTO citations_fts (citations_fts, rowid, title, supporting_quote, keywords, notes)
    VALUES ('delete', old.id, old.title, old.supporting_quote, old.keywords, old.notes);
END;

CREATE TRIGGER IF NOT EXISTS citations_fts_au AFTER UPDATE ON citations BEGIN
    INSERT INTO citations_fts (citations_fts, rowid, title, supporting_quote, keywords, notes)
    VALUES ('delete', old.id, old.title, old.supporting_quote, old.keywords, old.notes);
    INSERT INTO citations_fts (rowid, title, supporting_quote, keywords, notes)
    VALUES (new.id, new.title, new.supporting_quote, new.keywords, new.notes);
END;

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
-- scores — per (review_run, choice). Amendment 1 (plan.md §3.1 /
-- §4.4): the four dimension columns store k-sample VOTE SHARES
-- (fraction of judge votes per label, 0..1; parse-failed calls
-- force-scored 'contradicted' and kept in the denominator) — NOT
-- judge-emitted continuous scores. classification is DERIVED from
-- the majority label. First-class no-literature-found fields
-- distinguish never-checked vs checked-and-empty.
-- ============================================================
CREATE TABLE IF NOT EXISTS scores (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    review_run_id                INTEGER NOT NULL REFERENCES review_runs (id),
    choice_id                    INTEGER NOT NULL REFERENCES choices (id),
    evidence_backed_share        REAL NOT NULL CHECK (evidence_backed_share BETWEEN 0 AND 1),
    interesting_novel_share      REAL NOT NULL CHECK (interesting_novel_share BETWEEN 0 AND 1),
    unsupported_share            REAL NOT NULL CHECK (unsupported_share BETWEEN 0 AND 1),
    contradicted_share           REAL NOT NULL CHECK (contradicted_share BETWEEN 0 AND 1),
    classification               TEXT NOT NULL CHECK (classification IN
                                     ('well-supported', 'needs-improvement', 'interesting')),
    composite                    REAL NOT NULL,
    composite_band               TEXT NOT NULL,
    interpretation_guide_version TEXT NOT NULL,
    rationale                    TEXT,
    literature_searched          INTEGER NOT NULL DEFAULT 0,
    literature_found             INTEGER NOT NULL DEFAULT 0,
    search_queries               TEXT,    -- queries tried, present even on a null result
    UNIQUE (review_run_id, choice_id)
);
CREATE INDEX IF NOT EXISTS idx_scores_choice ON scores (choice_id);

-- ============================================================
-- distill_queue — citation-justified trim/distill proposals,
-- ranked for operator backlog triage. proposal_kind reuses
-- knowledge-placement.md's own tier vocabulary (v1 enum per
-- plan.md §3.1 — no 'move-to-skill-trigger' in v1).
-- ============================================================
CREATE TABLE IF NOT EXISTS distill_queue (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    choice_id               INTEGER NOT NULL REFERENCES choices (id),
    artifact_id             INTEGER NOT NULL REFERENCES artifacts (id),  -- denormalized
    review_run_id           INTEGER NOT NULL REFERENCES review_runs (id),
    proposal_kind           TEXT NOT NULL CHECK (proposal_kind IN
                                ('move-to-rule', 'move-to-reference', 'move-to-memory-pointer',
                                 'trim', 'rewrite', 'delete-superseded', 'no-action')),
    rank                    REAL NOT NULL,      -- higher = more urgent; plan.md §4.4 formula
    justification           TEXT NOT NULL,      -- citation ids or documented absence
    justifying_citation_ids TEXT,               -- JSON array of citations.id
    status                  TEXT NOT NULL DEFAULT 'open'
                                CHECK (status IN ('open', 'accepted', 'rejected', 'applied')),
    created_at              TEXT NOT NULL,
    resolved_at             TEXT,
    resolved_by             TEXT,
    CHECK (justifying_citation_ids IS NULL OR json_valid(justifying_citation_ids))
);
CREATE INDEX IF NOT EXISTS idx_distill_queue_status_rank ON distill_queue (status, rank DESC);

PRAGMA user_version = 1;
