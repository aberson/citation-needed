# Corpus survey — LLM-facing artifact classes in `dev/`

Scope: `c:/Users/abero/dev` (multi-project workspace, Windows 11) plus the per-project memory
trees under `C:/Users/abero/.claude/projects/*/memory/`. Method: `Glob`/`find`/`grep` counts and
structural sampling (no full-corpus read) via the Bash tool, October... (run date 2026-07-21).
Counts are approximate (shell-glob based) and exclude vendored/build noise (`node_modules/`,
`.venv/`) except where called out as its own finding.

All workspace-file citations below are `path:line` relative to `c:/Users/abero/dev` unless a
different root is given. One external source was fetched and verified this run (§8).

---

## 1. Skills — `.claude/skills/*/SKILL.md`

### Count
- **Root workspace** (`dev/.claude/skills/`): **50** skill directories, all with a `SKILL.md`.
- **Per-project** (`<project>/.claude/skills/*/SKILL.md`), owned projects only: Alpha4Gate 9,
  pinchy_mchire 10, void_furnace 8, pokemon-go-tools 5, toybox 2, brickomancer 1, on-brand 1,
  shake_spear 5 (wrapper-shaped, see below) + 14 more inside its two `projects/*/` story
  subfolders (28 total, generated/templated copies — not independently-authored skills).
- **Public-mirror project** `claude-skills/` (curated subset repo, separate from `dev-root`
  `.claude/skills/`): 46 `SKILL.md` files — a **parallel, hand-curated corpus of the same
  artifacts**, per `CLAUDE.md:` "claude-skills public mirror ... sync = per-file merge."
- **Third-party / not-owned** (`career-ops/`, `owned = false` in
  `.claude/observatory/registry.toml:` `slug = "career-ops" ... owned = false`): carries its own
  `SKILL.md` **mirrored across 7 different agent-tool conventions** in parallel —
  `.claude/skills/`, `.agents/skills/`, `.antigravitycli/skills/`, `.grok/skills/`,
  `.kimi/skills/`, `.opencode/skills/`, `.qwen/skills/`, plus a `plugins/*/skill.md` set and a
  root `AGENTS.md` (the cross-tool CLAUDE.md analogue). This is evidence of a **cross-tool
  artifact family** citation-needed's taxonomy should reserve a slot for (tool-agnostic skill
  mirrors + `AGENTS.md`), even though this specific project is out of scope for review
  (`owned=false`) per the workspace's own write-block convention
  (`.claude/rules/descriptor-contract.md` §2).
- Vendored noise found and excluded from all counts above: `SKILL.md` also ships **inside
  installed Python packages** — e.g.
  `Alpha4Gate/.venv/Lib/site-packages/fastapi/.agents/skills/fastapi/SKILL.md`,
  `brickomancer/.venv/Lib/site-packages/typer/.agents/skills/typer/SKILL.md`. A corpus-scan
  script for citation-needed **must exclude `.venv/`, `node_modules/`, `.git/`** or it will
  ingest third-party package skill files as if they were workspace choices.

### Structural anatomy
Frontmatter (YAML between `---` markers), sampled across all 50 root `SKILL.md` files
(`.claude/skills/plan-review/SKILL.md:1-5` is representative):

| Field observed | Count (of 50 root skills) |
|---|---|
| `name` | 50 |
| `description` | 50 |
| `user-invocable: true` | 44 |
| `argument:` (nonstandard — not in the official schema, see §8) | 4 — `context-slim`, `lesson-harvest`, `tier-escalate`, `tier-offload` |
| `user-invokable:` (typo of `user-invocable`) | 1 — `.claude/skills/claude-oauth-auth/SKILL.md` |
| `argument-hint:` | 1 |

None of the newer official fields (`allowed-tools`, `disallowed-tools`, `context`, `agent`,
`model`, `effort`, `when_to_use`, `disable-model-invocation`, `hooks`, `paths`, `shell`) are used
anywhere in this workspace's skills — confirmed by a `grep -rl` over every `SKILL.md` for each
field name (zero hits). This is itself a citable **choice**: the corpus consistently uses the
minimal 3-field frontmatter (`name`/`description`/`user-invocable`) and has not adopted the
platform's newer invocation-control surface.

Body structure (from reading `plan-review`, `plan-wrap`, `build-phase`, `session-wrap`): a `# Title`
heading, an `## Arguments` table (flag / required / default / description columns) when the skill
takes flags, then numbered `## N. <topic>` or `### <topic>` sections that are the actual
instructions — these numbered sections are exactly what a citation review should extract as
discrete **choices** (e.g. plan-review's "### 1. Data persistence" through its ~20+ numbered
checks, `.claude/skills/plan-review/SKILL.md:19-40+`).

Subdirectories, counted across all 50 root skill folders (`find .claude/skills -maxdepth 3 -type d`):
- **`evals/`** — 42 of 50 skills. Canonical shape: `evals.json` (skill name, version,
  `passing_threshold`, `categories[]` → `evals[]` with `id` + a `statement` framed to name what
  would make it grade FALSE — see `.claude/skills/plan-review/evals/evals.json:1-8`) +
  `test_scenarios.json` (`scenarios[]` with `id`/`name`/`description`/`context` — a synthetic
  transcript-context builder) + `golden/` (32 of the 42 `evals/` dirs) + a `results.tsv` run log.
  `golden/*.md` files are labeled reference outputs (e.g.
  `.claude/skills/plan-wrap/evals/golden/good_all_done.md:1-3` — HTML-comment-prefixed with a
  scenario pointer and a one-line "what this anchors" note; `bad_*` variants are negative
  anchors). This evals/golden pairing is the **measurement-validity "calibrate with anchors"**
  discipline (`.claude/rules/measurement-validity.md` § Calibrate with anchors) already
  implemented at the skill level — a structural precedent citation-needed's own eval harness
  should follow.
- **`scripts/`** — 5 skills carry an executable helper (Python/PowerShell), sometimes with its
  own `__pycache__`/`.pytest_cache`/`.ruff_cache`/`.mypy_cache` (build noise, not content).
- **`references/`** — NOT used at the per-skill level at dev-root (zero `.claude/skills/*/references/`
  dirs found there); two per-project skills do have one:
  `pokemon-go-tools/.claude/skills/pokemon-go-list/references/` and `.../pokemon-go-ui/references/`.
  The dev-root workspace instead centralizes shared reference material at the top level —
  see §3.
- Transient run-state dirs (not corpus content, but present and must be excluded from a scan):
  `judge-motion/.judge-motion/<timestamp>-<label>/` (33 timestamped run folders),
  `skill-iterate/tmp/iter{1..5}` + `tmp/prompts_iter{1..5}`, `plan-wrap/evals/golden-archived-*`.

### Size range
`SKILL.md` bodies range from short (~20-40 lines for a thin wrapper, see shake_spear below) to
several hundred lines for a heavily-flagged orchestrator skill (`build-phase`, `session-wrap`).
Not separately measured line-by-line in this pass; `evals.json`/`test_scenarios.json` sidecars
commonly run several hundred lines to ~1-2K given ~30 assertions × verbose `statement` text.

### The "thin wrapper" sub-shape (structurally distinct from a full skill)
`shake_spear/.claude/skills/character-keeper/SKILL.md` (and its 4 siblings, plus the 14 generated
copies inside each `projects/<story>/.claude/skills/*/`) carry only
`name`/`description`/`user-invocable` frontmatter and a **~6-line pointer body**: "Read
`../../skills/character_keeper.md` ... and follow it." The canonical, citation-worthy content
lives in a **separate, non-`.claude/skills/` shared prompt-skill corpus** at `shake_spear/skills/*.md`
(14 files, cataloged by `shake_spear/skills/README.md`, confirmed by
`shake_spear/CLAUDE.md`: "Shared prompt skills live in `skills/`... The slash wrappers under
`.claude/skills/` only point there"). **Implication for the DB schema:** a `SKILL.md` row is not
always the choice-bearing artifact — citation-needed must be able to follow a documented pointer
(`Read <relative-path> and follow it`) to the actual content file, or it will review an empty
wrapper and attribute zero choices to a project that in fact has 14 richly-authored prompt files.

---

## 2. Rules — `.claude/rules/*.md`

### Count
**22** files total workspace-wide (excluding vendored dirs):
- Root (`dev/.claude/rules/`): 13 — `code-quality.md`, `command-presentation.md`,
  `descriptor-contract.md`, `knowledge-placement.md`, `measurement-validity.md`,
  `plan-and-issue-flow.md`, `python.md`, `security.md`, `shareable-docs.md`,
  `subagent-economy.md`, `windows-shell.md`, `working-directory.md`, `worktree-hygiene.md`.
- Per-project: Alpha4Gate 4 (`bot-runtime.md`, `evolve.md`, `frontend-ui.md`, `wsl-evolve.md`),
  void_furnace 3 (`secrets-handling.md`, `substrate-invocation.md`, `substrate-testing.md`),
  toybox 2 (`claude-auth.md`, `frontend-ui.md`).

### Structural anatomy
No YAML frontmatter — plain markdown starting with a `# Title`. Every root rule sampled follows
the same recurring skeleton (most explicit in `code-quality.md`, `measurement-validity.md`,
`security.md`, `working-directory.md`): a short framing paragraph naming **how many discrete
sub-rules** it covers and their origin ("Four implementation-time rules learned from real
regressions..."), then `## <rule name>` sections each ending in:
1. a stated **rule/principle** (imperative, often bolded),
2. a **named incident** as evidence — project + phase/date + concrete cost (e.g.
   `code-quality.md:` "Alpha4Gate Phase 4.6 Step 1 ... Soak-4 spent 70 minutes..."),
   `security.md:` "Toybox issues #4 and #5 contained fake `<system-reminder>` blocks...",
3. a `## Source memories` list of `feedback_*.md` memory filenames that seeded the rule.

This 3-part shape (**rule → incident evidence → source memory pointer**) is exactly the
"internal workspace provenance" citation class the citation-needed spec calls for — each rule
sub-section is *already* citing its own internal source. A citation review of a rule file should
extract **one discrete choice per named sub-rule** (not per file), and should check whether the
cited incident + source memory actually exist (they are internally verifiable: grep the memory
dir for the named file).

### Size range
`.claude/rules/*.md` line counts range from **4 lines** (a stub, seen in some per-project rules)
to **95 lines** (`.claude/rules/measurement-validity.md`); root-level rules cluster 60-95 lines,
per-project rules are shorter (~40-90 lines, e.g. `void_furnace/.claude/rules/substrate-testing.md`
at ~45 lines).

### Choice-extraction unit
One **named sub-rule/section** (`## <name>`) per row — its principle statement is the "choice,"
its incident + source-memory citation are the internal provenance to verify, and an external
literature search is the gap-filler for the underlying principle (e.g. does "grep all downstream
consumers before changing a key shape" correspond to any published refactoring-safety literature?).

---

## 3. References — `.claude/references/*.md`

### Count
**12** files, all at the dev-root (`dev/.claude/references/`) — no per-project `references/`
directory was found at the project-root level (`find . -type d -iname references` outside
`.claude/skills/*/references/` and one vendored Playwright hit returned nothing else):
`command-presentation.md`, `intake-engine.md`, `plan-and-issue-flow.md`, `projects.md`,
`shakedown-engine.md`, `skill-pipeline.md`, `skill-role-taxonomy.md`, `step-authoring.md`,
`task-state-schema.md`, `transition-directory-contract.md`, `windows-shell.md`,
`worktree-hygiene.md`. Total **1,484 lines** across the 12 files (avg ~124 lines/file).

### Structural anatomy
No frontmatter. These are the **tier-3 "human/reference detail"** files per the workspace's own
`.claude/rules/knowledge-placement.md` tier tree (§ tier 3: "background, worked examples,
tables ... linked on demand"). Each is the **full detail counterpart** to a one-line CLAUDE.md or
rule-file pointer — e.g. `.claude/rules/windows-shell.md`'s last line ("Full lookup table:
`.claude/references/windows-shell.md`") is the stub; the reference file is the exhaustive lookup
table itself. This is the **"inline stub" pattern** documented in
`.claude/rules/knowledge-placement.md` § The inline stub. Content shape varies by file purpose:
lookup tables (`windows-shell.md`), numbered landmine catalogs (`worktree-hygiene.md`), full
format specs (`plan-and-issue-flow.md`, `step-authoring.md`), or schema docs
(`task-state-schema.md`).

### Choice-extraction unit
Reference files are mostly **derived/expanded detail of a rule's already-cited choices** — a
citation review would usually treat the *owning rule file* as the choice-bearing unit and the
reference file as supporting elaboration, rather than re-extracting duplicate choices from both.
Exception: reference files with their own standalone taxonomy content not owned by any rule (e.g.
`skill-role-taxonomy.md`, `projects.md`) should be reviewed as primary artifacts in their own
right.

---

## 4. CLAUDE.md chain

### Count
- Root: `c:/Users/abero/dev/CLAUDE.md` — **130 lines**.
- Per-project (project-root `CLAUDE.md`, one level under `dev/`, excluding `.venv`/`node_modules`):
  **29** files.
- Workspace-wide including nested (e.g. `shake_spear/projects/example_kids_story/CLAUDE.md`):
  **34** files.

### Structural anatomy
No YAML frontmatter (plain markdown). The root `CLAUDE.md` follows a stable `## <Topic>` section
skeleton: Environment, Conventions, Skill pipeline, Projects, Control plane, Session wrap & commit
discipline, Parallel session safety, Compact Instructions, Session Resume, Pointers — each a dense
paragraph or bullet list, frequently **cross-referencing a `.claude/rules/*.md` file** rather than
restating it (per its own `.claude/rules/knowledge-placement.md` § One owner per contract). Sampled
per-project files (`void_furnace/CLAUDE.md`, `shake_spear/CLAUDE.md`) converge on a similar but not
identical skeleton: `## Project overview`, `## Stack`, `## Commands`/`## Package manager`,
`## Directory layout`, `## Gotchas`, `## Rules` (links to project's own `.claude/rules/*.md`),
`## Pointers`. This "Commands"/"Stack" heading convention is exactly what
`.claude/rules/descriptor-contract.md` §1 requires for dev-observatory scrapability — confirming
CLAUDE.md structure is already partly standardized by an existing internal contract, itself a
citable internal-provenance source for any "why is Commands a heading" choice.

Nested-project `CLAUDE.md` (e.g. `shake_spear/projects/example_kids_story/CLAUDE.md`) is a
**smaller, scoped variant** — "Read first" file list + "Hard rules" + "Default behavior" — no
Stack/Commands section, because the parent project (not the story) owns the toolchain. This is a
third structural sub-shape (**scoped-child CLAUDE.md**) distinct from root and project-root.

### Size range
9 lines (`songs/CLAUDE.md`, `studying/CLAUDE.md` stubs) up to 181 lines
(`brickomancer/CLAUDE.md`); most substantive project files cluster 100-180 lines. void_furnace's
CLAUDE.md is denser still per-paragraph (long inline phase-history prose, see the system-reminder
excerpt fetched during this survey — a single "Directory layout" bullet for
`src/void_furnace/readiness/` runs to several hundred words of embedded history) — a structural
outlier worth flagging: CLAUDE.md prose that has accumulated project-history narrative rather than
staying a stable "durable fact" file is exactly the drift `context-slim` (an existing sibling
skill) already targets, and would itself be a "needs-improvement" choice under citation-needed's
own rubric (too much volatile narrative in an always-loaded tier-1 file, per
`.claude/rules/knowledge-placement.md` tier 1).

### Choice-extraction unit
Each `##`-level section is one discrete choice-bearing unit (e.g. "Model = Opus 4.8 default" in
the root CLAUDE.md Environment section is a single, densely-justified architectural choice
citation-needed should be able to isolate and cite independently of its neighbors).

---

## 5. Plan docs — `plan.md` / `master_plan.md` / `documentation/*-plan.md`

### Count
- `plan.md` or `master_plan.md` **at a project root** (the canonical entry-plan location per
  `.claude/rules/descriptor-contract.md` §4): 12 root-level `plan.md` files sampled directly
  (`aberson.github.io`, `always-best-estimates`, `applied_learning`, `b2_project_goblin`,
  `coding_without_pants`, `measure-twice`, `on-brand`, `pta_finance`, `shake_spear`,
  `void_furnace`, `walkies`, `x-marks-the-spot`).
- **All** `plan.md`/`master_plan.md` workspace-wide (any depth, any project, excluding
  vendored/archive dirs): **61**.
- **All** `documentation/*-plan.md`-shaped feature/phase plans workspace-wide: **88**.
- Combined plan-shaped corpus scanned for heading convention (98 files, `plan.md` +
  `master_plan.md` + `documentation/*-plan.md` + `documentation/plans/*.md`, archive-excluded):
  **32** use the `### Step N:` convention, **9** use `## Phase` headings — confirming both
  conventions named in `.claude/rules/descriptor-contract.md` §4 are live in the corpus
  simultaneously (not one project's quirk), plus a majority that use neither inline (see the
  pointer-plan pattern below).

### Structural anatomy
Two structurally distinct plan shapes coexist:

1. **Inline-step plan** — the canonical shape `descriptor-contract.md` §4 wants: a
   `## 1. What This Is` (or equivalent labeled objective) section, then `### Step N: <title>`
   blocks each with `**Problem:**`/`**Type:**`/`**Issue:**`/`**Flags:**` bullets (per
   `.claude/rules/plan-and-issue-flow.md`: "Plan steps need `### Step N:` + Problem/Type/Issue/Flags
   bullets") and a `**Status:** DONE` marker once built. Size range: 755-1,287 lines for the
   larger root `plan.md` files sampled directly (this Read tool truncates around 25K tokens, i.e.
   roughly this range, per the note below).
2. **Pointer/index plan** — a short `plan.md` (as little as **9 lines**) that only links out to
   sub-plans, e.g. `void_furnace/plan.md:1-26`: "# void_furnace plan — moved ... The old
   monolithic `plan.md` was split during /plan-review round 2 because its 26.7K-token size
   exceeded the Read tool's 25K ceiling" — pointing to `documentation/master_plan.md` +
   `documentation/plans/phase-{0..4}-*.md` + `documentation/appendix/`. Per
   `.claude/rules/descriptor-contract.md` §4, a pure pointer plan "yields a goal but no
   built/total ratio" for dev-observatory's scraper — the same limitation applies to a citation
   review: **the entry `plan.md` is not where the choices live**; the reviewer must resolve to the
   linked phase files.

### Choice-extraction unit
Per inline-step plan: one `### Step N:` block = one candidate choice bundle (its `**Problem:**`
statement is the choice's "why," its body is the "what," `**Type:**`/`**Flags:**` are metadata).
For pointer plans, the citation-needed corpus-loader must (a) detect the "moved"/pointer shape
(e.g. via a regex on the first heading + a link-only body) and (b) recursively resolve to the
linked phase docs rather than reporting "plan has 0 extractable choices."

---

## 6. Memory files — `C:/Users/abero/.claude/projects/*/memory/`

### Count
**13** per-project memory directories exist. Fact-file counts (`*.md` excluding `MEMORY.md`
itself):

| Project memory dir | Fact files |
|---|---|
| `c--Users-abero-dev` (main/root) | 183 |
| `c--Users-abero-dev-Alpha4Gate` | 120 |
| `c--Users-abero-dev-void-furnace` | 61 |
| `c--Users-abero-dev-toybox` | 45 |
| `c--Users-abero-dev-pta-finance` | 16 |
| `c--Users-abero-dev-brickomancer` | 10 |
| `c--Users-abero-dev-always-best-estimates` | 6 |
| `c--Users-abero-dev-career-ops` | 6 |
| `c--Users-abero-dev-workspace` | 4 |
| `c--Users-abero-OneDrive-Desktop-coding-root` | 4 |
| `c--Users-abero-dev-songs` | 2 |
| `c--Users-abero-dev-walkies` | 2 |
| `C--Users-abero-dev-on-brand` | 0 (dir exists, empty) |

**Total ≈ 459** per-fact memory files workspace-wide, plus one `MEMORY.md` index per directory
(13 indexes).

### Structural anatomy — the key taxonomy divergence from skills/rules
Every per-fact memory file (sampled: `feedback_win_capture_when_worth_it.md`,
`reference_buildphase_subagent_tightening.md`) carries **YAML frontmatter with a nested
`metadata:` map**, distinct from a `SKILL.md`'s flat frontmatter:

```yaml
---
name: feedback_win_capture_when_worth_it
description: "..."
metadata:
  node_type: memory
  type: feedback            # or: project | reference | user
  originSessionId: <uuid>
  modified: <ISO8601>        # present on ~12 of 183 sampled; most lack it
---
```

`metadata.type` distribution in the main dev memory dir (183 files): **feedback 74, project 37,
reference 5, user 2** (the remainder either lack a parsed `type:` line or sit outside this simple
awk scan — treat as approximate). Body convention (not enforced by schema, but consistent by
practice): a **problem/decision narrative paragraph**, often with **inline citations to external
literature by author/year or arXiv ID** embedded directly in prose (e.g.
`feedback_win_capture_when_worth_it.md:24-47` cites "Baron & Hershey d~0.8-1.1", "Ellis & Davidi
2005", "Power of Noise (SIGIR 2024)", "Selective Memory arXiv 2603.15994", "ExpeL +11/+19pt arXiv
2308.10144", "Voyager arXiv 2305.16291", "Reflexion arXiv 2303.11366") — this is direct evidence
the workspace **already produces exactly the artifact citation-needed wants to generalize**: a
memory file today is a hand-authored, ad hoc external-literature-cited justification for a
decision. citation-needed's job is to make this systematic (SQLite-backed, structured
evidence-backed/unsupported/contradicted classes) rather than free-text-embedded-in-prose. None of
those specific citations were re-verified in this run — they are reported here only as an
**existing structural pattern** (citations already embedded in the corpus), not as citations this
survey vouches for.

A `[[wikilink]]`-style cross-reference convention links memory files to each other (e.g.
`feedback_win_capture_when_worth_it.md:54`: "Links: [[feedback_memory_cleanup_default_delete]]
... and the measurement-validity rule"). `MEMORY.md` itself is a **pure index** — one bullet per
fact file, grouped under `## <category>` headings, each bullet a `[title](filename.md)` link plus
a terse one-line hook (`C:/Users/abero/.claude/projects/c--Users-abero-dev/memory/MEMORY.md:5-15`).

A runtime behavior observed directly in this session: reading an aged memory file
(`reference_buildphase_subagent_tightening.md`, ~29 days old) auto-prepended a
`<system-reminder>` staleness warning ("Memories are point-in-time observations, not live
state... Verify against current code before asserting as fact.") — this is a platform-level
freshness gate on the memory artifact class specifically, with no equivalent observed on
skills/rules/CLAUDE.md/plans.

### Size range
9-10 lines (short one-liners, e.g. `user_observer_shorthand.md`) to 146-150 lines (the longest
sampled, `task-state-per-session-model.md`, `ui-review-loop.md`, `user_model_preference.md`);
total 4,717 lines across 183 files in the main dev memory dir alone (avg ~26 lines/file — much
shorter than rules or CLAUDE.md sections).

### Why memories are NOT the same shape as skills (explicit contrast, per the assignment)
| Axis | Skills (`SKILL.md`) | Memory fact files |
|---|---|---|
| Frontmatter | Flat (`name`, `description`, `user-invocable`) | Nested (`metadata: {node_type, type, originSessionId, modified}`) |
| Purpose | **Procedure** — invoked to perform a task (`/plan-review`) | **Observation** — a point-in-time fact/decision record, never invoked |
| Body shape | Structured: Arguments table + numbered instructional sections | Free-text narrative paragraph(s), often with inline ad hoc citations |
| Lives alongside | `evals/` (self-testing harness), optional `scripts/`/`references/` | Nothing — a fact file has no sidecar tests or scripts |
| Cross-links | None observed (skills don't link to each other by convention) | `[[wikilink]]` convention + an index (`MEMORY.md`) that every file is registered in |
| Freshness signal | None (a skill is either current or edited) | Platform-injected staleness `<system-reminder>` on read, age-gated |
| Discrete "choice" unit | One `##`/`###` instructional section | The **entire file is usually one choice/decision** (rarely subdivides further) |
| Where it's consumed | Loaded on-demand when the skill fires (tier 2 per `knowledge-placement.md`) | `MEMORY.md` index loaded **every session** (tier 1); bodies loaded on click-through |

**This means citation-needed's DB schema cannot use one `artifact` table with one `choice`
extraction rule for both.** A skill review extracts many choices per file (each instructional
section); a memory review most often extracts **one** choice per file (the whole fact IS the
choice), with the frontmatter `metadata.type` (feedback/project/reference/user) as a
review-priority signal (e.g. `reference`-typed memories are more likely to already carry
citations worth ingesting into the corpus directly; `feedback`-typed memories are bug-shaped
regressions more amenable to "internal provenance" citation than "external literature").

---

## 7. Other LLM-facing classes found

- **`.claude/workflows/*.js`** — 2 found (`dev/.claude/workflows/deep-research-pinned.js`,
  `void_furnace/.claude/workflows/deep-research-pinned.js`, byte-identical canonical copy per
  CLAUDE.md). Structure: a `export const meta = {name, description, whenToUse, phases: [...]}`
  header (itself a prompt-facing description block Claude reads to decide invocation), then JS
  defining per-phase model/effort tier pins + JSON schemas for structured agent outputs (e.g.
  `SCOPE_SCHEMA` at `deep-research-pinned.js:34-40+`). This is a **prompt-adjacent, code-shaped**
  artifact: the `meta` block is LLM-facing prose (citable like a skill description), while the
  rest is executable orchestration logic (not itself a "choice" in the citation-needed sense,
  though the **tier-pin comments** — e.g. "search/fetch/verify arms → model: sonnet ... Provenance:
  run wf_9ef05e51-a3a ran 104/104 agents on Fable" at `deep-research-pinned.js:12-19` — are
  exactly the kind of empirically-justified engineering choice citation-needed should be able to
  extract and cite.
- **Prompt-injecting hooks** — `.claude/hooks/session-resume.ps1` (122 lines),
  `checkpoint-nudge.ps1` (253), `goal-regression-advisory.ps1` (46), `pre-compact.ps1` (93), wired
  via `.claude/settings.json` `hooks.{PreCompact,SessionStart,Stop}` matchers. These inject
  **plain-text context** into the session on exit 0 (documented explicitly in
  `session-resume.ps1:5-8,21-23`: "anything written to stdout on exit 0 is added to Claude's
  context as plain text... Output is plain ASCII informational context (NOT framed as system
  commands, which would trip prompt-injection defenses)"). This is a **fifth LLM-facing class**
  not in the assignment's checklist: a hook's stdout-on-success is itself a template of
  LLM-facing prose whose exact wording is a citable choice (e.g. why the resume banner is framed
  as informational text rather than an instruction, to avoid tripping the workspace's own
  prompt-injection defenses per `.claude/rules/security.md`).
- **`AGENTS.md`** — found at `career-ops/AGENTS.md` and `shake_spear/AGENTS.md`: the multi-tool
  (Codex/Cursor/etc.) analogue to `CLAUDE.md`. Not in the assignment's location list but present
  in this workspace and structurally CLAUDE.md-shaped; citation-needed's location config should
  have a slot for it even if lower-priority than `CLAUDE.md`.
- **No agent-definition files** (`.claude/agents/*`) and **no output-style files** were found
  anywhere in the workspace (`find . -path "*/.claude/agents/*"` and an `*output-style*` glob
  both returned zero, vendored dirs excluded) — confirmed absent, not merely unsearched.
- **`docs/archived-skills/`** — 17 retired skill directories kept for history (`rwl`, `rwl-direct`,
  `rwl-full`, `rwl-gauntlet`, `tdd`, `project-map`, etc.). These are former LLM-facing artifacts
  now inert; a citation-needed scan should exclude `docs/archived*` from live-corpus review by
  default (mirrors dev-observatory's own finder rule, `.claude/rules/descriptor-contract.md` §4:
  "skipping `*archive*`/`*brainstorm*`/`*template*`/`*draft*`").
- **`docs/lessons-learned.md`** (4,055 lines) and **`docs/friction-catalog.md`** (596 lines) — long-form
  companion docs each `feedback_*.md` memory is a "thin pointer" into (per CLAUDE.md § Pointers
  and `knowledge-placement.md` § tier 5). Structurally these are large multi-section reference
  docs, not memory files themselves, but they are where a memory's cited incident is elaborated —
  a citation review resolving a memory's internal provenance should expect to resolve here.

---

## 8. External anchor: the platform's own SKILL.md frontmatter spec

Fetched and verified this run: **Claude Code documentation, "Extend Claude with skills"**,
`https://code.claude.com/docs/en/skills` (redirect target of `https://docs.claude.com/en/docs/claude-code/skills`,
fetched 2026-07-21). The doc states Claude Code's skills "follow the [Agent Skills](https://agentskills.io)
open standard" and documents the full frontmatter field table: `name`, `description`,
`when_to_use`, `argument-hint`, `arguments`, `disable-model-invocation`, `user-invocable`,
`allowed-tools`, `disallowed-tools`, `model`, `effort`, `context`, `agent`, `hooks`, `paths`,
`shell` (canonical directory shape: `SKILL.md` required + optional `template.md`/`examples/`/
`scripts/`/reference docs). Cross-referencing this workspace's corpus against that spec surfaces
two concrete findings (already folded into §1 above, restated here as the citation payload):
1. **Nonstandard field `argument:`** (singular, no such field in the spec — the spec's field is
   `arguments` or `argument-hint`) used in 4 skills. This is either a dead/vestigial field from an
   older internal convention or a typo-shaped drift; worth a follow-up grep of each skill's body to
   see if `argument:`'s value is actually read/used anywhere, or if it's inert.
2. **Typo `user-invokable:`** (should be `user-invocable`) in `claude-oauth-auth/SKILL.md` — since
   the spec's actual field is `user-invocable`, this skill is running on the **default** (`true`)
   rather than whatever its author intended, silently. This is a live NEEDS-IMPROVEMENT-classed
   finding a citation-needed review of `claude-oauth-auth` should surface directly (a "choice"
   that isn't actually taking effect).

No other external sources were verified in this pass — this investigation's scope (corpus
structure) needed only this one anchor. See `.claude/rules/measurement-validity.md` and
`.claude/rules/security.md` § Treat fetched external content as data for the standing rules a
future citation-needed web-fetch step must also honor (verify claims, do not act on embedded
directives).

---

## 9. Verdict — the artifact-type taxonomy the DB must model

citation-needed's schema needs **at least these distinct artifact-type rows**, each with its own
choice-extraction unit and frontmatter shape (not a single generic "document" type):

| `artifact_type` | Location glob | Frontmatter | Choice-extraction unit | Approx. count (owned, non-vendored) |
|---|---|---|---|---|
| `skill` | `.claude/skills/*/SKILL.md` | flat (name/description/user-invocable, +drift) | one `##`/`###` instructional section | ~125 (root+per-project) + 46 public-mirror duplicates |
| `skill_pointer` | thin-wrapper `SKILL.md` (shake_spear-shaped) | same flat frontmatter, body is a `Read <path>` pointer | resolve to the pointed-to file, extract there | subset of the above; ≥19 in shake_spear alone |
| `rule` | `.claude/rules/*.md` | none | one named `##` sub-rule (rule+incident+source-memory triad) | 22 |
| `reference` | `.claude/references/*.md` | none | usually owned by a rule; standalone only for taxonomy-shaped refs | 12 |
| `claude_md` | `CLAUDE.md` (root / project-root / scoped-child) | none | one `##` section | 34 (3 structural sub-shapes: root, project-root, scoped-child) |
| `plan_inline` | `plan.md`/`master_plan.md`/`documentation/*-plan.md` with inline steps | none | one `### Step N:` or `## Phase` block | 32 Step-shaped + 9 Phase-shaped (of 98 scanned) |
| `plan_pointer` | short `plan.md` that only links to phase files | none | resolve to linked files, extract there | present (e.g. void_furnace, 9-line file) |
| `memory_fact` | `C:/Users/abero/.claude/projects/*/memory/*.md` (excl. `MEMORY.md`) | **nested** `metadata:{node_type,type,originSessionId,modified}` | usually the **whole file** is one choice | ~459 |
| `memory_index` | `MEMORY.md` | none | not a choice source itself — a routing index into `memory_fact` rows | 13 |
| `workflow_js` | `.claude/workflows/*.js` | JS `meta = {...}` object | the `meta` block + tier-pin comments | 2 |
| `hook_prompt` | `.claude/hooks/*.ps1` wired in `settings.json` | none (PS comments document the injection contract) | the literal injected-text template | 4 |
| `agents_md` (out-of-taxonomy-but-present) | `AGENTS.md` | none | same as `claude_md` | 2 (both in non-owned/lower-priority projects) |

**The load-bearing structural fact:** memory files are NOT skill-shaped. A skill's frontmatter is
flat and its body is a multi-section, multi-choice procedure with a self-contained eval harness
sitting beside it (`evals/`); a memory fact's frontmatter is a **nested metadata object**
(`node_type`/`type`/`originSessionId`/`modified`), it has no sidecar tests, it is usually a
**single** choice/decision per file rather than several, it participates in a cross-file
`[[wikilink]]` graph that skills do not, and it alone carries a platform-injected staleness
warning on read. A generic `artifact(path, body) → choices[]` extractor built for skills will
either over-fragment memory files (finding non-existent sub-sections) or silently swallow their
one real choice under the wrong shape. The DB's `artifact_type` column must drive **type-specific
extraction logic**, not just a display label.
