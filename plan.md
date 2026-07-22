# citation-needed — plan.md

## 1. What This Is

**citation-needed** is a thin, open-source toolkit (Python + uv + SQLite) that gives every
discrete choice embedded in LLM-facing files — skills (`SKILL.md`), memories, rules
(`.claude/rules/*.md`), `CLAUDE.md` files, and plans — a **citation trail**: external research
literature as the primary citation class, internal workspace provenance (incidents, memories,
investigation docs) as the secondary class. A review extracts the discrete design choices from
one target artifact, cites each (existing DB corpus first, live search for gaps), classifies each
as well-supported / needs-improvement / interesting, and emits per-choice dimension labels
(evidence-backed / unsupported / contradicted / interesting-novel) plus an anchored 0–100
composite with a written interpretation guide. Reviews are **read-only toward targets** — output
is a breakdown doc plus DB rows, never an edit to the reviewed file — and every vetted citation
persists to SQLite so the corpus compounds across reviews. "No literature found" is a recorded,
legitimate finding, not a failure.

Grounding: ten investigations live in [`docs/research/`](docs/research/) — nine pre-planning
plus a post-redline artifact-type-extensions survey — this plan resolves their open questions
and cites them by slug throughout.

Proposal: https://claude.ai/code/artifact/f9359e17-fbc7-487a-8dc2-1840fc7844c1

## 2. Stack

| Layer | Tool | Why |
|---|---|---|
| Language / runtime | Python 3.12+, uv | Workspace default for thin tools (x-marks-the-spot, dev-observatory precedent). |
| Storage | SQLite via stdlib `sqlite3`, no ORM | Workspace precedent (`x-marks-the-spot/CLAUDE.md:12`); corpus scale is hundreds–low-thousands of rows (research: `citation-mechanics.md` §e). |
| Full-text corpus lookup | SQLite FTS5, external-content mode | Corpus-first lookup before any web call; auditable BM25 term matching beats an embeddings dep at this scale (`citation-mechanics.md` §e). No embeddings. |
| Details validation | pydantic | One model per `artifact_type` mirrors `details_json`; `tests/test_schema.py` asserts model fields == live columns (x-marks `schema.sql:4-6` convention). |
| HTTP client | httpx | The verify gate needs resolve-before-fetch SSRF guarding and per-redirect-hop validation (ported from `x-marks-the-spot/src/xmarks/expand/verify.py:65-129`); httpx's non-following redirect loop supports this cleanly. |
| Frontmatter / config parsing | PyYAML (memory + skill frontmatter), stdlib `tomllib` (observatory `registry.toml`) | Required to type artifacts; no alternative in stdlib for YAML. |
| CLI | argparse (stdlib), entry point `cite` | Thin; no CLI-framework dep. Multi-verb pattern mirrors `observatory` CLI. |
| LLM layer | Claude Code skills (4 thin wrappers in `dev/.claude/skills/citation-*/`) | Extraction/classification/relevance judgment run in the skill layer; the CLI is purely mechanical and never calls an LLM (see §8 Key Design Decisions). |
| Tests / quality | pytest, ruff, mypy | Workspace default gates. |

## 3. Data Store

### 3.1 Database

- **Path:** `data/citation.db` — **gitignored from commit 0** along with `breakdowns/`
  (`public-boundary.md` §a: exclusion-from-day-0, never scrub-later; history rewrite is
  insufficient per `feedback_github_history_rewrite_insufficient`).
- **Schema source of truth:** `schema.sql` (executed only against a brand-new DB via
  `cite init-db`), plus **numbered migrations from v0.1** (`migrations/000N_*.sql`, applied in
  filename order by a ~30-line `db.py::migrate()` reading `PRAGMA user_version`). The corpus is
  produced by live search + review judgment — expensive, NOT rebuildable-from-seed — so
  "just rebuild from schema.sql" (x-marks's convention) explicitly does not transfer
  (`schema-draft.md` §6).
- **Concurrency:** the DB opens in **WAL mode with `busy_timeout=5000`** — a project-wide sweep
  fans out per-artifact subagents whose verify/insert calls can overlap; WAL makes concurrent
  writers safe, and every `review commit` / `insert_citation` runs inside a single transaction
  (no partial writes). Backup = copy `data/citation.db` (WAL-checkpointed via `cite status`);
  documented in §10 because the corpus is expensive, not rebuildable.
- **7 tables:** `artifacts`, `review_runs`, `choices`, `citations`, `choice_citations`, `scores`,
  `distill_queue`. Full CREATE TABLE DDL: `schema-draft.md` §7, **adopted as the v1 baseline with
  four explicit amendments** (the DDL in Step 1 is authored from baseline + this list — where
  they disagree, this list wins): (1) the four `scores` dimension columns store k-sample **vote
  shares** per §4.4, not judge-emitted continuous scores; (2) the citations verification-method
  column is named `resolution_method` with CHECK enum `('api_structured', 'web_fetch_verified',
  'internal-read')` — superseding schema-draft's `verification_method
  ('WebFetch'|'WebSearch'|'internal-read')`; (3) `artifacts.path` uses the two-scheme form above
  (workspace-relative | `memory:` prefix); (4) `choices` gains `source_path TEXT` (nullable) —
  the file a choice's quote/span was actually extracted from when it differs from
  `artifacts.path` (pointer-resolved skills/plans, CLAUDE.md `@`-imports, `evals/` sidecars);
  NULL means the artifact's own file. Summary of the load-bearing shapes:
  - **`artifacts`** — one row per reviewed file. `artifact_type IN ('memory','skill','rule',
    'claude_md','plan')` (v1 enum). The `skill` type also ingests `.claude/commands/*.md`
    slash-command files (same shape, same extractor), and a skill's `evals/` sidecar
    (evals.json statements + golden fixtures) is reviewed as part of the owning skill —
    rubric choices extract with it, recorded via `details_json`. Deferred types land by
    migration, priority-ordered per `docs/research/artifact-type-extensions.md`:
    **v1.1** = `agent_def` (`.claude/agents/*.md` — none in this workspace today, common in
    the wild) + the `agents_md` cross-tool siblings (GEMINI.md, CODEX.md, OPENCODE.md,
    `.windsurfrules`, `copilot-instructions.md` — same no-frontmatter claude_md shape);
    **later** = `reference`, `workflow_js` (+ production prompt templates bundled with it),
    `hook_prompt`, `memory_index`, `.cursor/rules/*.mdc`; **skip** = output styles, MCP
    config, `settings.json` hook wiring (no prose choices to extract). Type-specific fields
    in `details_json`
    (JSON1, pydantic-validated at write time, promotable to generated columns if hot). `path`
    UNIQUE, forward slashes: **workspace-relative** for in-tree artifacts;
    **`memory:<project-dir-slug>/<file>.md`** for memory artifacts (they live outside the
    workspace under `C:/Users/abero/.claude/projects/<slug>/memory/` and cannot be
    workspace-relative). `project` resolved against `dev/.claude/observatory/registry.toml`
    (else `coding-root` / `global`).
  - **`choices`** — durable identity = `UNIQUE(artifact_id, choice_key)` where `choice_key` is an
    LLM-assigned kebab slug the re-review is instructed to REUSE for the same underlying decision
    even if reworded; `content_hash_at_extraction` is a byte-identical fast path so the LLM
    reuse-or-mint judgment only runs on hash miss. Span lines stored as LOCATOR only, never
    identity. Not-re-observed choices become `status='removed'`, never deleted (their citations
    remain corpus assets). (`schema-draft.md` §3.)
  - **`citations`** — the deduplicated corpus. Real columns + CHECK constraints (NOT
    `details_json`): `kind IN ('external','internal')`, `CHECK (kind != 'external' OR url_or_doi
    IS NOT NULL)`, `UNIQUE(kind, natural_key)`. This is the trust-critical table — a fabricated
    citation must be **structurally impossible**, not merely discouraged (`schema-draft.md` §2;
    anti-fabrication contract in §4.2 below).
  - **`review_runs`** — frozen provenance per pass: `artifact_content_hash_at_review`,
    `artifact_git_sha_at_review`, `reviewer_model`, `tool_schema_version`. Staleness =
    `artifacts.current_content_hash` differing from the latest run's hash — the registry detects
    its own drift; targets carry no state (`context-cost-constraint.md` §6).
  - **`scores`** — per (review_run, choice): the four dimension columns (stored as k-sample vote
    shares, §4.4), derived `classification`, artifact-level composite inputs, and the first-class
    no-literature-found fields `literature_searched` / `literature_found` / `search_queries`
    (distinguishing never-checked vs checked-and-empty vs contradicted; `schema-draft.md` §5).
  - **`distill_queue`** — ranked trim/rewrite proposals: `proposal_kind` reuses
    knowledge-placement.md's tier vocabulary (`move-to-rule`, `move-to-reference`,
    `move-to-memory-pointer`, `trim`, `rewrite`, `delete-superseded`, `no-action`),
    `justification` NOT NULL (citation ids or documented absence), `status IN
    ('open','accepted','rejected','applied')`.

### 3.2 File layout of review output

- **Breakdown docs: central, one per reviewed artifact** —
  `breakdowns/<project-slug>/<artifact-slug>.md`, inside citation-needed's own repo, gitignored.
  `<project-slug>` = the registry slug (or `coding-root`/`global`); `<artifact-slug>` = the
  artifact's `path` value lowercased with every `/`, `:`, and space replaced by `--` and the
  trailing `.md` dropped (e.g. `.claude/rules/subagent-economy.md` →
  `--claude--rules--subagent-economy`). Generated by `breakdown.py`; used by `cite report` to
  locate the doc.
  Never written into a target project's tree — the only shape write-safe for owned AND not-owned
  targets, and it keeps "read-only toward targets" literal (`integration-surface.md` §d).
- **Discoverability without touching targets:** the `cite report <target-path>` CLI verb + the
  fixed breakdown path convention (mirrors the workspace's plan-location convention —
  `context-cost-constraint.md` §5). No inline pointers (§8 D1).

### 3.3 Deduplication / re-processing

- Re-review of an artifact **updates** via `choice_key` reuse; never duplicates.
- Citations dedup on `UNIQUE(kind, natural_key)` (normalized URL/DOI or workspace path).
- Corpus-first ordering: FTS5 `MATCH` (category + 3–6 salient terms, BM25-ranked) before any
  external call; per-artifact idempotency via the stored review-time content hash (re-reviewing an
  unchanged artifact re-uses prior citations without re-searching).
- A project-wide sweep **clusters near-duplicate choices across artifacts first** and resolves
  each cluster once (the lever that keeps a cold-start pass at low-hundreds of lookups instead of
  1,000–1,600; `citation-mechanics.md` §f).

## 4. The Review Pipeline (domain core)

### 4.1 Extract → cite → classify → score

1. **Ingest/type** the target (`cite scan` mechanics): detect `artifact_type`, parse frontmatter,
   resolve **pointer artifacts** (thin-wrapper SKILL.md bodies of the form "Read `<path>` and
   follow it", pointer-only plan.md, and CLAUDE.md `@path` imports) to the choice-bearing file —
   an unresolved pointer must not silently report zero choices (`corpus-survey.md` §1, §5;
   `artifact-type-extensions.md` for the `@`-import mechanics). **No double-extraction:** an
   `@`-import or pointer target that is itself a discoverable artifact (e.g. a CLAUDE.md
   importing a rule file) is recorded as a relationship and skipped — that file's choices belong
   to its own review; only non-artifact targets are inlined.
2. **Extract discrete choices** (skill layer). Type-specific units: one `##`/`###` instructional
   section per choice for skills; one named sub-rule for rules; one `##` section for CLAUDE.md;
   one `### Step N:`/`## Phase` block for plans. For a **memory**, the unit is one
   **independently-falsifiable decision** — a claim that could receive its own verdict and be
   reversed on its own. Single-decision memories (the common case) yield exactly one choice; a
   large composite memory (e.g. `user_model_preference.md`, ~150 lines) yields one choice per
   decision — its diversity-beats-stronger-model claim, its Fable-seed-points rule, and its
   re-pin-after-autoupdate convention carry different evidential standing and must score
   separately. **Over-split guard:** narrative, incident evidence, and **Why:**/**How to apply:**
   elaboration attach to their parent choice as span/provenance, never as choices of their own —
   two claims are separate only if they could plausibly receive different verdicts
   (`corpus-survey.md` §9 — the extractor must branch on type; a generic section splitter
   over-fragments memories).
3. **Cite each choice**: corpus-first FTS5 → structured APIs (Semantic Scholar default → Crossref
   DOI canonicalization → OpenAlex fallback) → WebSearch/WebFetch only for grey-literature
   gap-fill. Category-aware routing per the `choice-taxonomy-literature.md` 11-category taxonomy
   (literature-thin categories — verdict contracts, halt contracts, one-source-of-truth — expect
   internal provenance as the PRIMARY citation, and that inversion is recorded, not fought).
4. **Classify + score** per §4.4; **suggestions** are drafted for every needs-improvement choice.
5. **Persist + render**: DB rows + the breakdown doc. Zero writes to the target, ever.

### 4.2 Citation classes + anti-fabrication contract

Two clearly-labeled classes: **external** (literature; primary) and **internal** (workspace
provenance — incidents, memories, investigation docs; secondary). For three literature-thin
choice categories the classes legitimately invert (internal primary), recorded as such.

**The mechanical guard** (ported from x-marks-the-spot's `draft → verify_fact` gate,
`verify.py:216-250`; `citation-mechanics.md` §b):

- `insert_citation()` is the **only** writer touching `citations`. `resolution_method` CHECK enum
  permits only `'api_structured' | 'web_fetch_verified' | 'internal-read'` — there is no
  `llm_claimed` state to occupy.
- NOT NULL resolution record captured at insert time from the actual fetch/API response:
  locator (URL/DOI), retrieved title, access date (pipeline clock, never LLM-supplied),
  supporting quote.
- Open-web quotes are verified by **deterministic, normalized substring match against the raw
  fetched text** — never an LLM judging the page (prompt-injection-immune by construction, per
  `.claude/rules/security.md`). Structured-API hits carry the API's own JSON echo as the record.
- The fetch seam carries the SSRF (server-side request forgery) guard — resolve-before-fetch,
  refuse private/loopback ranges, re-validate every redirect hop — because candidate URLs come
  from an LLM proposal.
- On any failure (404, timeout, quote mismatch): record the choice outcome as unverified /
  no-literature-found and insert **nothing**.

### 4.3 External backends (verified terms as of 2026-07-21)

| API | Key | Limits (live-verified) | Role |
|---|---|---|---|
| Semantic Scholar Graph | Optional (free) | 5,000 req/5 min shared unauthenticated; 1–10 req/s with key | Default first search (CS/ML coverage). **Live lookup only — its Dataset License forbids redistribution, so S2-derived rows never enter the shipped seed corpus** (`public-boundary.md` §b). |
| Crossref REST | No (mailto → polite pool) | **Read `X-Rate-Limit-*` response headers at call time — never hardcode** (confirmed source conflict on static numbers; `prior-art.md` §1.3) | DOI canonicalization + bibliographic title queries. |
| OpenAlex | **Yes — free key required since 2026-02-13** | $1/day free credit ≈ 1k search calls; ID lookups effectively free | Broad-coverage fallback on S2 miss. CC0 → seed-corpus safe. |
| WebSearch/WebFetch (harness) | — | — | Grey literature only (OWASP, vendor docs, blogs). Raw-text quote verification, never the summarized answer. |

The three REST APIs are called by citation-needed's own httpx code, **not** through WebFetch
(WebFetch summarizes through a model — wrong tool for JSON; `citation-mechanics.md` §d).

### 4.4 Scoring + interpretation guide (v1, reconciled)

The `score-validity.md` categorical model is adopted (it is what the calibration gate and k≥3
voting were designed around); the `schema-draft.md` continuous columns are repurposed as vote
shares. Versioned as `interpretation_guide_version = 'v1'`; full prose guide ships at
`docs/interpretation-guide.md`.

- **Per choice:** k≥3 independent judge calls each emit ONE dimension label —
  `evidence-backed` (+1.0) | `interesting-novel` (+0.5) | `unsupported` (−0.5) |
  `contradicted` (−1.0). Majority vote decides the label (3-way split → escalate k=5, then 7).
  The four `scores` columns store the vote **shares**; a parse-failed call is force-scored
  `contradicted` and counted in the denominator, never dropped.
- **Per-choice classification (derived):** `contradicted`/`unsupported` → **needs-improvement**;
  `evidence-backed` → **well-supported**; `interesting-novel` → **interesting**. Every
  needs-improvement choice gets actionable suggestions in the breakdown.
- **Artifact composite:** mean of per-choice label weights, rescaled `(mean+1)/2×100` to 0–100.
  Bands: ≥70 **strong**, 40–69 **adequate**, 20–39 **weak**, <20 **unsupported**.
- **distill_queue rank:** `(1 − composite/100) × artifact_load_weight`, load weights v1:
  `claude_md` 3.0, `rule` 3.0 (both auto-load every session), `memory` 1.5 (index always-loaded,
  body on demand), `skill` 1.0 (loads on trigger), `plan` 0.75 — operationalizing
  knowledge-placement.md's own tier cost ordering.

### 4.5 Calibration gate (mandatory before any real review)

Per `.claude/rules/measurement-validity.md` and `score-validity.md` (adopted wholesale):

- **Good anchor:** frozen snapshot of `.claude/rules/code-quality.md` (real, incident-dense; 4/5
  choices literature-verified during Phase 0, 1/5 legitimately internal-only — a deliberate
  hard-but-fair item). Frozen copy at `fixtures/calibration/good-anchor.code-quality.frozen.md`.
- **Garbage anchor:** the synthetic 5-choice rule fixture drafted in `score-validity.md` §2b,
  frozen at `fixtures/calibration/garbage-anchor.SYNTHETIC.md` with its SYNTHETIC banner. Build
  step 5 MUST close its 3 UNVERIFIED rows with real literature checks (or swap the choices)
  before freezing — no placeholder expected-values.
- **Gate (all four assertions; any failure = ABORT, no DB writes, no target review runs):**
  `composite(good) ≥ 65` AND `composite(garbage) ≤ 35` AND margin ≥ 40 AND per-dimension shape
  (`evidence_backed_fraction(good) ≥ 0.6`; `unsupported+contradicted fraction(garbage) ≥ 0.6`).
  Parse-fail rate > 5% across the calibration run ABORTs before the score assertions.
- **Production path + throwaway DB:** calibration runs the same skill prompts and CLI verbs as a
  real review, against a copy-on-write throwaway DB (garbage-anchor "citations" must never poison
  the compounding corpus; good-anchor citations promote to the real corpus only via a deliberate
  reviewed seed step).
- **Re-calibration triggers (fingerprint-cached, not per-session):** prompt-template hash, resolved
  model id, corpus row-count/max-id, schema `user_version`; plus a 30-day advisory ceiling.
- **Pre-registration (verbatim, per the rule's scope test):** the anchored composite, computed
  from a target artifact's extracted choices via the production review pipeline, decides whether
  that artifact's choices are trustworthy as-is or flagged for revision feeding the ranked
  distill_queue; garbage must score bottom-band, good top-band, before any real composite counts.

### 4.6 The four use cases → four skills

| Use case | Skill (collision-checked) | What it does |
|---|---|---|
| 1. Review one target | `/citation-review <path>` | Full §4.1 pipeline → breakdown + DB rows. `--calibrate` mode runs the anchors. |
| 2. Distill one target | `/citation-distill <path>` | Review (or reuse fresh review) → trim/rewrite proposals where every cut is justified by a citation or a documented absence → `distill_queue` rows. Proposals only — never edits the target. |
| 3. Project-wide rigor pass | `/citation-sweep <project>` | Enumerate the project's LLM-facing files → cluster near-duplicate choices → batch reviews with per-artifact subagent fan-out (terse verdicts per subagent-economy.md) → ranked backlog into `distill_queue`. Allowed writes: DB rows + breakdown docs, NOTHING else. |
| 4. Backlog triage | `/citation-triage` | Walk `distill_queue` with the operator; record keep / cut / rewrite per item (`status` + `resolved_by`). Actual target edits happen OUTSIDE citation-needed via existing skills, informed by the recorded decisions. |

Skill SKILL.md files live at **`dev/.claude/skills/citation-<task>/`** (coding-root tree, exposed
everywhere via the one `~/.claude/skills` junction), thin-wrapping
`uv run --project citation-needed cite <verb>` — the observatory-doctor idiom
(`integration-surface.md` §a). Never under `dev/citation-needed/.claude/skills/`.

## 5. Modules

All under `src/citation_needed/` unless noted.

- **`cli.py`** — argparse entry `cite`; verbs: `init-db`, `migrate`, `status`, `scan`,
  `corpus-search`, `resolve` (read-only tiered-resolution preview, never writes the DB — added
  Step 3), `review open|commit`, `report`, `calibrate check|open|commit`,
  `distill propose`, `queue list|resolve`, `seed import`. Large JSON payloads pass via **stdin**,
  not argv (Windows 32K argv limit — `feedback_subprocess_large_arg_stdin_windows`).
- **`db.py`** — connection, `init` (schema.sql, idempotent), `migrate` (PRAGMA user_version loop).
- **`models.py`** — pydantic `details_json` models per artifact_type + row dataclasses.
- **`discover.py`** — artifact discovery: type globs, exclusions (`.venv/`, `node_modules/`,
  `.git/`, `docs/archived*`, `owned=false` registry trees), pointer detection/resolution,
  frontmatter parsing, project resolution against `registry.toml`.
- **`corpus.py`** — FTS5 external-content index + triggers; corpus-first search.
- **`resolve.py`** — S2 / Crossref / OpenAlex clients; Crossref throttles from live response
  headers; OpenAlex key via `CITATION_NEEDED_OPENALEX_KEY`.
- **`verify.py`** — the anti-fabrication gate: SSRF-guarded fetch seam (injectable for tests),
  normalize + substring quote match, `insert_citation()` sole writer.
- **`review.py`** — review_runs lifecycle; choice upsert (choice_key reuse + hash fast path +
  removed-marking); scores; the ONE implementation of label weights / composite / band /
  classification (one source of truth per code-quality.md).
- **`breakdown.py`** — renders `breakdowns/<project>/<slug>.md`.
- **`distill.py`** — proposal generation mechanics + queue rank formula.
- **`calibrate.py`** — anchors, throwaway-DB management, the 4 gate assertions, parse-fail
  threshold, fingerprint cache; review-open refuses without a valid cached calibration.
- **`prompts/`** (repo root) — the versioned LLM prompt templates (extraction, classification,
  distill) the skills load; their hash is calibration fingerprint A. Single source of truth for
  prompt text lives here, not in the SKILL.md wrappers.

## 6. API Route Contract

Not applicable — no backend/server, no ports (per `descriptor-contract.md` §3, no port is
declared or fabricated).

## 7. Project Structure

```
citation-needed/                  # nested repo (own .git via /repo-init), MIT
├── plan.md                       # this file (canonical entry plan)
├── CLAUDE.md
├── LICENSE                       # MIT, (c) 2026 Abraham Robison (claude-skills precedent)
├── README.md
├── .gitignore                    # day 0: data/, breakdowns/, *.db*, caches
├── pyproject.toml                # uv; entry point `cite`
├── schema.sql                    # canonical DDL (new DBs only)
├── migrations/                   # 000N_*.sql from v0.1
├── prompts/                      # versioned LLM prompt templates (fingerprinted)
├── src/citation_needed/          # modules per §5
├── fixtures/calibration/         # good-anchor.code-quality.frozen.md, garbage-anchor.SYNTHETIC.md
├── seed/                         # tracked CC0 seed corpus + PROVENANCE.md
├── docs/
│   ├── interpretation-guide.md   # v1 score semantics (versioned)
│   └── research/                 # the 9 Phase-0 investigation docs
├── data/                         # GITIGNORED — citation.db (the compounding corpus)
├── breakdowns/                   # GITIGNORED — <project>/<artifact-slug>.md
└── tests/                        # incl. test_schema.py round-trip
```

Plus, in the **coding-root repo** (`dev/`): `dev/.claude/skills/citation-{review,distill,sweep,
triage}/SKILL.md` — the four thin wrappers (two-repo commit hazard, §8 D9).

## 8. Key Design Decisions

- **D1 — Zero-touch reviews; no pointer lines.** Reviewed targets gain zero bulk: any line
  written into an always-loaded file is a permanent per-turn tax (83% of billed tokens above 150k
  context) and a measured staleness class (comment-drift ≈1.5× bug risk). Discoverability =
  `cite report <path>` + fixed breakdown paths, mirroring the plan-location convention. The
  inline pointer survives only as a documented manual operator action for a contradicted/
  safety-critical finding on a high-traffic file — no v1 tooling for it. (`context-cost-constraint.md`.)
- **D2 — Central, gitignored breakdowns.** Only shape write-safe for owned and not-owned targets;
  one privacy boundary instead of N. (`integration-surface.md` §d, `public-boundary.md`.)
- **D3 — Two-layer split: LLM in skills, mechanics in CLI.** The CLI never calls an LLM — no LLM
  API key, deterministic and testable; judgment (extraction, classification, relevance) lives in
  the skill layer where the harness already provides the model + subagent fan-out.
- **D4 — Choice identity = semantic `choice_key` + hash fast path.** Pure content-hash duplicates
  on LLM rewording (the common case); span anchors break on edit (the named failure). Surrogate
  integer PKs for joins; removal-not-deletion so corpus citations outlive trimmed choices.
  (`schema-draft.md` §3.)
- **D5 — `artifacts` typed via `details_json`; `citations` via real columns + CHECK.** Same
  decision procedure, opposite answers: 5 low-query-pressure types vs the 2-type trust-critical
  table where "no URL ⇒ no external citation row" must be DB-enforced. (`schema-draft.md` §1–2.)
- **D6 — Categorical per-choice scoring, versioned guide.** Majority-vote labels with fixed
  weights; parse-fail force-scored worst and counted; all cutpoints live in the versioned
  interpretation guide so revisions never silently reinterpret old rows. (`score-validity.md`.)
- **D7 — Calibration is a hard gate with ABORT semantics.** No real review, no composite, no
  corpus write until the anchors separate through the production path on a throwaway DB.
  Thresholds are never loosened just because a run misses them. (`score-validity.md` §3/§7.)
- **D8 — Semantic Scholar live-only; seed corpus from Crossref (no abstracts) + OpenAlex (CC0).**
  S2's dataset license (non-commercial, no sublicense) conflicts with an MIT repo.
  (`public-boundary.md` §b.)
- **D9 — Skills in coding-root, engine in nested repo.** Required for invoke-from-anywhere via
  the junction; accepted cost: cross-cutting changes need two commits in two repos — every such
  build step must anchor git operations twice (`working-directory.md` discipline applied per
  repo). (`integration-surface.md` §a/§c.)
- **D10 — MIT license.** The one in-workspace OSS precedent (`claude-skills/LICENSE`); compatible
  with redistributing the CC0 seed corpus; `seed/PROVENANCE.md` documents per-source data terms
  separately from the code license. (`public-boundary.md` §c.)
- **D11 — Sweep's write scope is closed.** Use case 3 may write DB rows and breakdown docs,
  nothing else. Target edits happen outside citation-needed entirely, driven by the operator from
  recorded triage decisions. (Brief constraint + `integration-surface.md` §d.)

## 9. Open Questions / Risks

| Item | Risk | Mitigation |
|---|---|---|
| choice_key reuse quality on busy artifacts (10+ choices) | Folded-into-extraction reuse judgment may mis-merge/mis-mint keys | Step 4 prototypes both fold-in and dedicated-diff-pass shapes against a 2-choice and a 10+-choice artifact before committing (`schema-draft.md` §9); acceptance test: reworded re-review produces zero duplicates |
| OpenAlex key onboarding | Fallback backend dead until key exists | Step 3 fails loud (not silent-skip) when `CITATION_NEEDED_OPENALEX_KEY` unset AND a lookup reaches the OpenAlex tier; README documents signup; S2+Crossref cover the common case keyless |
| Crossref limits drift | Hardcoded numbers go stale (verified conflict already) | Client throttles from live `X-Rate-Limit-*` headers only |
| Garbage-anchor UNVERIFIED rows | Freezing unchecked "expected" values invalidates the gate | Step 5 done-when requires closing all 3 rows with real searches or swapping choices |
| Corpus poisoning via calibration | Synthetic anchor citations leak into the compounding DB | Throwaway DB is mandatory in `calibrate` verbs; test asserts real DB untouched after a calibration run |
| Judge drift after model/prompt changes | Stale calibration silently blesses a drifted scorer | Fingerprints A–D + 30-day advisory; `review open` hard-refuses on stale fingerprints |
| Two-repo commit hazard (D9) | Skill wrapper and engine drift, or a commit lands in the wrong repo | Step 8 checklists both repos; wrong-dir guard discipline applied per repo; wrappers grep-verified against real CLI verbs |
| Pointer artifacts | Thin-wrapper SKILL.md / pointer plan.md silently yield zero choices | `discover.py` pointer resolution + a fixture test for each pointer shape (`corpus-survey.md` §1/§5) |
| Prompt-injection via fetched pages | A page instructs the verifier to pass | Deterministic substring match, never LLM-judges-the-page; security.md envelope on all fetched content |
| Junction dependency | Skills invisible if the junction breaks | Documented in CLAUDE.md ("broken skill? check junction first"); no new junction is created |
| Concurrent DB writes during sweep fan-out | Parallel per-artifact subagents overlap on insert/commit | WAL mode + `busy_timeout=5000` connection defaults (Step 1); every commit is one transaction |
| Corpus loss (expensive, not rebuildable) | Deleted/corrupted `data/citation.db` loses hours of verified review work | Backup = copy the DB file (§10); `cite status` runs a WAL checkpoint so the copy is consistent |

## 10. How to Run

```powershell
# one-time
uv sync --project citation-needed
uv run --project citation-needed cite init-db
uv run --project citation-needed cite seed import          # optional CC0 seed corpus
$env:CITATION_NEEDED_OPENALEX_KEY = "<free key>"           # optional; enables OpenAlex fallback

# calibrate (mandatory before first real review; cached by fingerprint)
# -> run /citation-review --calibrate in a Claude Code session

# review one artifact (from any project window)
# -> /citation-review .claude/rules/subagent-economy.md
uv run --project citation-needed cite report .claude/rules/subagent-economy.md

# project-wide pass + triage
# -> /citation-sweep <project-slug>   then   /citation-triage
uv run --project citation-needed cite queue list
uv run --project citation-needed cite status

# quality gates
uv run --project citation-needed pytest
uv run --project citation-needed ruff check .
uv run --project citation-needed mypy src

# backup the corpus (expensive, not rebuildable — copy after any substantial review session;
# `cite status` checkpoints the WAL first so the file copy is consistent)
Copy-Item citation-needed\data\citation.db citation-needed\data\citation.backup.db
```

After the plan pipeline completes: `/repo-init` creates the nested `.git` + public GitHub repo
(MIT). The `.gitignore` exclusions in §7 must be in the **first** commit.

## 11. Development Process

Build via `/build-phase` over the steps below. All steps are backend/CLI — `--reviewers code`
throughout (no runtime UI surface). Direct-in-tree builds with path-scoped commits are preferred
over worktrees until the nested repo exists (pre-repo-init the tree is untracked by coding-root).
Steps touching both repos (Step 8) apply the wrong-directory guard once per repo.

### Automated Steps

<!-- autofix-applied: 2026-07-21 -->
### Step 1: Scaffold, schema, migrations
- **Problem:** Create the uv package skeleton: `pyproject.toml` (py3.12+, entry `cite`), `src/citation_needed/` layout, `schema.sql` v1 (the 7 tables + FTS5 virtual table per `docs/research/schema-draft.md` §7 with the four §3.1 amendments), `db.py` init + `PRAGMA user_version` migration loop + WAL/busy_timeout connection defaults, pydantic details models, day-0 `.gitignore` (`data/`, `breakdowns/`, `*.db*`, caches), `LICENSE` (MIT), README stub.
- **Type:** code
- **Issue:** #1
- **Flags:** --reviewers deep
- **Produces:** pyproject.toml, schema.sql, migrations/, src/citation_needed/{cli,db,models}.py, tests/test_schema.py, .gitignore, LICENSE, README.md
- **Done when:** `uv run cite init-db` creates all 7 tables + FTS5 index in a fresh `data/citation.db`; `tests/test_schema.py` round-trips a golden details_json blob per artifact_type and asserts pydantic fields == live columns; pytest/ruff/mypy green.
- **Depends on:** none
- **Status:** DONE (2026-07-21)

### Step 2: Artifact discovery + typed ingestion (`cite scan`)
- **Problem:** Discover and type LLM-facing artifacts: globs for the 5 v1 types (incl. memory dirs under `C:/Users/abero/.claude/projects/*/memory/`, and `.claude/commands/*.md` ingested as `skill`), exclusions (`.venv/`, `node_modules/`, `.git/`, `docs/archived*`, `owned=false` trees via `registry.toml`), pointer detection + resolution (thin-wrapper SKILL.md, pointer plan.md, CLAUDE.md `@path` imports — mechanics in `docs/research/artifact-type-extensions.md`), frontmatter parsing, `project` resolution, `details_json` population, artifact upsert.
- **Type:** code
- **Issue:** #2
- **Flags:** --reviewers code
- **Produces:** src/citation_needed/discover.py, scan CLI verb, fixture tests for each artifact type + both pointer shapes
- **Done when:** `cite scan --project coding-root` against the real workspace registers all 5 types with counts matching spot-check fixtures; a thin-wrapper SKILL.md fixture resolves to its pointed-to file (not zero choices); a CLAUDE.md fixture with an `@path` import inlines a non-artifact target AND records-but-skips a target that is itself a scanned artifact (no double-extraction); a `.claude/commands/*.md` fixture registers as `skill`; integration test invokes the production CLI entry, not internals.
- **Depends on:** 1
- **Status:** DONE (2026-07-21)

<!-- autofix-applied: 2026-07-21 -->
### Step 3: Citation resolution + anti-fabrication verifier + FTS5 corpus
- **Problem:** Build the acquisition pipeline: `resolve.py` (S2 search, Crossref with live rate-header throttling, OpenAlex behind `CITATION_NEEDED_OPENALEX_KEY` failing loud when unset-but-reached), `verify.py` (SSRF-guarded injectable fetch seam, normalized substring quote match, `insert_citation()` sole writer with the NOT NULL resolution record), `corpus.py` (FTS5 external-content + sync triggers, `corpus-search` verb).
- **Type:** code
- **Issue:** #3
- **Flags:** --reviewers deep
- **Produces:** src/citation_needed/{resolve,verify,corpus}.py, corpus-search CLI verb, unit tests with injectable seam + one live smoke test against a real DOI
- **Done when:** a fabricated citation (quote absent from fetched text) is structurally rejected with nothing inserted; a real citation (arXiv:2307.03172) round-trips insert→FTS5-hit; SSRF fixtures (loopback/private/redirect-hop) all refuse; live Crossref call throttles from response headers.
- **Depends on:** 1
- **Status:** DONE (2026-07-21)

<!-- autofix-applied: 2026-07-21 -->
### Step 4: Review mechanics + breakdown renderer + interpretation guide
- **Problem:** `cite review open <path>` (creates review_run, emits prior choice_key/summary pairs as JSON) and `cite review commit` (stdin JSON: choices + labels + citation refs → choice upsert with key-reuse + hash fast path + removed-marking, scores with vote shares, derived classification, composite/band in `review.py` as the single implementation), breakdown renderer, `cite report <path>`, and `docs/interpretation-guide.md` v1 (all §4.4 semantics in prose). The `review open`/`review commit` stdin/stdout JSON contracts are documented as JSON Schema files under `docs/contracts/` — the single source the skills (Step 8) validate against. The extraction template's memory-type guidance encodes the §4.1 per-decision splitting rule (different-verdicts test + over-split guard), and its acceptance fixture includes one multi-decision memory that must yield >1 choice and one single-decision memory that must yield exactly 1.
- **Type:** code
- **Issue:** #4
- **Flags:** --reviewers deep
- **Produces:** src/citation_needed/{review,breakdown}.py, review/report CLI verbs, docs/contracts/review-open.schema.json, docs/contracts/review-commit.schema.json, docs/interpretation-guide.md, prompts/ v1 templates (extraction, classification)
- **Done when:** committing the worked-example JSON for `.claude/rules/subagent-economy.md` reproduces the appendix row-set exactly; a second commit with the same choice reworded reuses the choice_key (zero duplicates — the D4 acceptance test); breakdown doc renders with both citation classes labeled; `cite report` surfaces it.
- **Depends on:** 2, 3
- **Status:** DONE (2026-07-21)

<!-- autofix-applied: 2026-07-21 -->
### Step 5: Calibration fixtures + gate
- **Problem:** Freeze the good anchor (snapshot `.claude/rules/code-quality.md` into fixtures/), author the garbage anchor from `docs/research/score-validity.md` §2b **closing its 3 UNVERIFIED rows via real literature searches (or swapping those choices)** with the SYNTHETIC banner intact, and build `calibrate.py`: throwaway-DB copy, the 4 gate assertions (65/35/40/shape), parse-fail >5% ABORT, fingerprint cache (prompt hash, resolved model id, corpus fingerprint, schema version, 30-day advisory), and `review open` hard-refusal without a valid cached calibration.
- **Type:** code
- **Issue:** #5
- **Flags:** --reviewers deep
- **Produces:** fixtures/calibration/*.md, src/citation_needed/calibrate.py, calibrate CLI verbs, tests incl. a red-on-garbage self-test (a deliberately broken scorer must fail the gate)
- **Done when:** gate goes red when fed inverted anchor labels (self-test); `cite review open` refuses with a loud message when no valid calibration is cached or any fingerprint is stale; a calibration run leaves the real DB byte-identical (poisoning test); all 5 garbage-anchor choices carry a verified citation or documented search.
- **Depends on:** 4
- **Status:** DONE (2026-07-21)

### Step 6: Distill engine + queue triage verbs
- **Problem:** `distill.py`: generate trim/rewrite proposals where every proposal carries citation ids or a documented absence (`literature_searched=1, literature_found=0`), rank via `(1−composite/100) × load_weight`, plus `cite queue list` and `cite queue resolve <id> --keep|--cut|--rewrite` recording operator + timestamp.
- **Type:** code
- **Issue:** #6
- **Flags:** --reviewers code
- **Produces:** src/citation_needed/distill.py, queue CLI verbs, prompts/ distill template, tests
- **Done when:** an unsupported claude_md choice outranks an equally-unsupported skill choice (load-weight test); a well-supported choice yields no queue row; resolve round-trips status + resolved_by; justification NOT NULL enforced.
- **Depends on:** 4

### Step 7: Seed corpus + provenance
- **Problem:** Build the tracked CC0 seed corpus from the Phase-0 verified citations — Crossref-sourced fields (no abstracts) + OpenAlex-sourced rows only; S2-derived entries re-derived through Crossref/OpenAlex or excluded — with `seed/PROVENANCE.md` naming per-source license terms, and an idempotent `cite seed import`.
- **Type:** code
- **Issue:** #7
- **Flags:** --reviewers code
- **Produces:** seed/seed_citations.jsonl, seed/PROVENANCE.md, seed import CLI verb, tests
- **Done when:** fresh DB + `seed import` twice → no duplicates; FTS5 `corpus-search "lost in the middle"` hits the seeded row; PROVENANCE.md lists every source with its license basis; zero S2-attributed rows present.
- **Depends on:** 3

### Step 8: The four skills (two-repo step)
- **Problem:** Author `dev/.claude/skills/citation-{review,distill,sweep,triage}/SKILL.md` as thin wrappers over `uv run --project citation-needed cite <verb>` (observatory-doctor idiom): citation-review embeds the §4.1 flow + calibrate mode + stdin JSON contracts; citation-sweep embeds near-duplicate clustering + per-artifact subagent fan-out with terse-verdict returns (subagent-economy.md); citation-distill and citation-triage wrap their verbs. NOTE: SKILL.md files land in the CODING-ROOT repo; any engine tweaks land in citation-needed — two scoped commits, wrong-dir guard applied per repo.
- **Type:** code
- **Issue:** #8
- **Flags:** --reviewers code
- **Produces:** 4 SKILL.md files under dev/.claude/skills/, minor CLI adjustments if contract gaps surface
- **Done when:** each SKILL.md's embedded commands grep-match real CLI verbs (`cite --help` output); frontmatter is the standard 3 fields spelled `user-invocable` (no `argument:`/`user-invokable` drift per `corpus-survey.md` §8); junction-exposure verified (`ls ~/.claude/skills/citation-review/`).
- **Depends on:** 4, 5, 6

### Step 9: End-to-end smoke — calibrate, then one real review
- **Problem:** Exercise the full production path once with real components (no mocks): run calibration through the skill+CLI pipeline to a green gate, then a real `/citation-review` of `.claude/rules/subagent-economy.md` with live corpus-first lookup and live web verification.
- **Type:** code
- **Issue:** #9
- **Flags:** --reviewers code
- **Produces:** a real breakdown doc, real DB rows, a short smoke report in docs/
- **Done when:** calibration passes the 4-assertion gate on real anchors; the review yields ≥2 choices with both citation classes represented; `git status` of the coding-root repo shows zero modifications to the reviewed target; `cite report` renders the result.
- **Depends on:** 5, 8

### Step 10: Observation run — small real sweep + findings
- **Problem:** Run `/citation-sweep` over one bounded real scope (the 13 root `.claude/rules/*.md` files) with per-artifact subagent fan-out; capture an observation findings doc: wall-clock + per-API call counts and failures, near-duplicate cluster ratio, corpus-hit-rate curve across the pass, queue output sanity. File issues for anything surfaced rather than fixing in-step.
- **Type:** code
- **Issue:** #10
- **Flags:** --reviewers code
- **Produces:** docs/observation-run-1.md, distill_queue rows for the rules corpus
- **Done when:** the sweep completes all 13 artifacts (or documents each abstention); findings doc contains the four metric families; zero unhandled exceptions (failures are recorded outcomes, not crashes); ranked queue is non-empty and ordered by the §4.4 rank formula.
- **Depends on:** 6, 9

### Manual Steps
(These run after /build-phase completes. Operator drives.)

### Step M1: Operator UAT — read one breakdown against its target
- **Source step:** Step 9
- **Issue:** #11
- **Commands:**
  ```powershell
  uv run --project citation-needed cite report .claude/rules/subagent-economy.md
  # then open the breakdown doc path it prints, side-by-side with the rule file
  ```
- **What to look for:**
  | Check | Expected outcome |
  |---|---|
  | Choices extracted | Match the rule's actual discrete decisions (≈2 for this file), no over-fragmenting |
  | Citation labels | Every external citation resolvable (click one URL); internal citations carry path:line; classes clearly labeled |
  | No-literature rows | Recorded as searched-and-empty with the query strings shown, not blank |
  | Scores vs guide | Composite + band read sensibly against docs/interpretation-guide.md |
  | Target untouched | `git diff .claude/rules/subagent-economy.md` is empty |

### Step M2: citation-triage session over the sweep queue
- **Source step:** Step 10
- **Issue:** #12
- **Commands:**
  ```powershell
  uv run --project citation-needed cite queue list
  # then, in a Claude Code session: /citation-triage
  ```
- **What to look for:**
  | Check | Expected outcome |
  |---|---|
  | Ranking order | claude_md/rule findings outrank skill/plan findings at equal composite |
  | Justifications | Every item cites evidence or a documented absence — no bare "looks trimmable" |
  | Resolution round-trip | keep/cut/rewrite decisions persist (`cite queue list` reflects statuses) |
  | Scope discipline | Nothing in the session offered to edit a target file directly |

Please run M1 next (after the automated steps complete).

## 12. Appendix

### A. Worked example (the brief's canonical case)

Choice from `.claude/rules/subagent-economy.md`: "sub-agent returns a terse verdict; detail goes
to a file the orchestrator reads only on failure."

| Field | Value |
|---|---|
| choice_key | `subagent-terse-verdict-file-detail` |
| citations | [external] Liu et al. 2023, "Lost in the Middle" (arXiv:2307.03172 — long-context position degradation → keep orchestrator context slim); [internal] `docs/investigations/token-usage-levers-consolidated-2026-06-22.md:61-64`, Lever 2 (the measured 18%/~240k-char leak) |
| support_direction | supports (both) |
| label / classification | evidence-backed → well-supported |
| suggestions | none |
| distill_queue | no row (well-supported ⇒ no proposal) |

Full INSERT row-set with real captured provenance (content hash, git sha):
`docs/research/schema-draft.md` §8.

### B. v1 artifact-type extraction units (from `docs/research/corpus-survey.md` §9)

| artifact_type | Choice unit | Note |
|---|---|---|
| skill | one `##`/`###` instructional section; the `evals/` sidecar's rubric choices extract with the owning skill | resolve thin-wrapper pointers first; `.claude/commands/*.md` ingests as this type |
| rule | one named `##` sub-rule (rule + incident + source-memory triad) | the triad IS pre-existing internal provenance |
| claude_md | one `##` section | highest load weight (auto-loads every session) |
| plan | one `### Step N:` / `## Phase` block | resolve pointer-only plans to phase files |
| memory | one choice per independently-falsifiable decision (single-decision files — the common case — yield one) | nested `metadata:` frontmatter; `metadata.type` is a review-priority signal; over-split guard per §4.1 |

### C. Choice-category taxonomy + literature density (from `docs/research/choice-taxonomy-literature.md`)

11 categories; well-covered: prompt phrasing, context economy, fan-out/diversity, LLM-as-judge,
prompt injection. Moderate: measurement validity, doc minimalism. Thin (internal-provenance-
primary expected): verdict/output contracts, autonomy/halt contracts, one-source-of-truth.
Fast-churning (re-verify per use): memory/retrieval. Seed citations for the corpus are in that
doc with per-citation verification labels (verified / UNVERIFIED-CONTENT / no-literature-found).

### Decision Inventory

Canonical ID registry for the proposal artifact (append-only; a reversed decision flips its
status to `changed <date>`, never disappears).

| ID | P/D | Choice (short) | Status |
|---|---|---|---|
| P1 | P | Open source, thin toolkit, skills other projects invoke | stands |
| P2 | P | Python + uv + SQLite | stands |
| P3 | P | Live search at review time; vetted citations persist + compound | stands |
| P4 | P | Two citation classes: external primary, internal secondary | stands |
| P5 | P | 4 dimensions + anchored composite + written interpretation guide | stands |
| P6 | P | Reviews read-only toward targets; breakdown + DB rows out | stands |
| P7 | P | Four use cases: review / distill / sweep / triage | stands |
| P8 | P | No fabricated/padded citations; no-literature-found is a result | stands |
| P9 | P | Scorer blocked until good/garbage anchors separate | stands |
| P10 | P | New nested repo at dev/citation-needed/ | stands |
| P11 | P | The 7-table set (typed artifacts; external\|internal citations) | stands |
| D1 | D | Zero-touch registry; no pointer tooling in v1 | stands |
| D2 | D | Central gitignored breakdowns/<project>/<slug>.md | stands |
| D3 | D | Skills in coding-root junction tree; citation-* names; `cite` wrapper | stands |
| D4 | D | Sweep write scope closed: DB + breakdowns only | stands |
| D5 | D | Choice identity = semantic choice_key + hash fast path; removal-not-deletion | changed 2026-07-21 (extraction granularity refined — see D17) |
| D6 | D | Categorical k≥3 majority scoring; weights +1/+0.5/−0.5/−1; bands 70/40/20; load weights 3/3/1.5/1/0.75 | stands |
| D7 | D | Calibration gate 65/35/40 + shape; 5% parse-fail ABORT; fingerprint cache; throwaway DB | stands |
| D8 | D | Anchors: frozen code-quality.md (good) + synthetic fixture (garbage, ships publicly) | stands |
| D9 | D | S2 live-only; seed corpus = Crossref (no abstracts) + OpenAlex (CC0) | stands |
| D10 | D | MIT license | stands |
| D11 | D | artifacts via details_json; citations via real columns + CHECK | stands |
| D12 | D | v1 artifact types = 5; reference/workflow/hook/agents_md/memory_index deferred | stands |
| D13 | D | LLM only in skill layer; CLI never calls a model; stdin JSON contracts in docs/contracts/ | stands |
| D14 | D | CLI entry `cite`, argparse stdlib-only | stands |
| D15 | D | Steps 1/3/4/5 build under --reviewers deep | stands |
| D16 | D | WAL + busy_timeout=5000; migrations from v0.1; file-copy backup | stands |
| D17 | D | Memory extraction = one choice per independently-falsifiable decision; over-split guard (operator-requested 2026-07-21) | stands |
| D18 | D | Type roadmap: v1 folds commands + evals-rubrics into `skill` and resolves CLAUDE.md `@`-imports; v1.1 = agent_def + cross-tool agents_md siblings; later = cursor-rules, prompt templates (with workflow_js); skip = output styles, MCP, settings hooks | stands |

### D. Reuse map (from `docs/research/prior-art.md`)

Reuse as-is: judge-core doctrine, review-proof evidence discipline, S2/OpenAlex/Crossref APIs.
Imitate: lesson-harvest dedup+idempotency+capped-dropped-list (→ sweep + queue), memory-distill
checklist round (→ classification), review-deep evidence shape (→ breakdown findings),
score-skill golden-gate (→ calibration), plan-trim/test-prune confirm-gates (→ triage),
x-marks-the-spot verify gate (→ `verify.py`). Build fresh: the SQLite corpus schema, the
choice-extraction prompts, the dual-class citation model. Skip: promptfoo/Inspect/Evals/DeepEval/
Braintrust, scite.ai (sales-gated), prompt linters (complementary axis).
