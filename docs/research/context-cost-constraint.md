# Context-cost constraint: pointer line vs. zero-touch registry

**Question.** citation-needed reviews an LLM-facing artifact (a `SKILL.md`, `CLAUDE.md`, rules
file, or plan) and persists a citation trail to SQLite. Should the review also write a one-line
progressive-disclosure pointer into the reviewed target (design **a**), or should the target
never be touched at all — DB rows + breakdown docs only (design **b**)? This doc validates the
premise that reviewed artifacts must gain ~zero bulk, argues from the workspace's own primary
sources plus verified external literature, and recommends one default.

## 1. The premise, restated precisely

The concern is not disk space. It is **resident-context cost**: every `CLAUDE.md` in the walk
from cwd to the workspace root, every `.claude/rules/*.md` at each level, and every project's
`MEMORY.md` index loads on **every conversational turn** for that project, forever, for the
lifetime of the file — not once at review time. Anything citation-needed writes into one of
these targets is not a one-time cost; it is a **recurring per-turn tax** for as long as the line
exists. This is the exact quantity `.claude/rules/subagent-economy.md` and
`.claude/rules/knowledge-placement.md` are built to protect.

## 2. Primary sources (workspace)

### 2.1 `.claude/rules/knowledge-placement.md` — the tier decision tree

> "1. Needed **every session / turn**? → CLAUDE.md inline. Auto-loaded always — the most
> expensive tier, so keep it to durable, cross-cutting facts.
> 2. Needed only in a **named situation**? → a `.claude/rules/*.md` ... or a **skill**.
> 3. **Human / reference** detail (background, worked examples, tables)? → `.claude/references/`
> or `docs/`, linked on demand."
> — `c:/Users/abero/dev/.claude/rules/knowledge-placement.md:10-15`

A citation breakdown is, by definition, **tier 3**: background/provenance a future editor
consults when they are about to touch or challenge a specific choice — not something needed on
every turn the file loads. The tier tree's own logic routes it to `docs/` / `.claude/references/`,
linked on demand, not to an always-loaded inline marker.

The same file's "inline stub" section is the closest existing precedent for design (a), and it is
narrower than a blanket default:

> "When detail moves out to a rule / skill / reference, leave behind only the **trigger
> condition** plus any **safety-critical fact** — nothing else."
> — `knowledge-placement.md:43-44`

This licenses an inline pointer only when the thing left behind is a **trigger condition +
safety-critical fact**, not a general "see also" breadcrumb. "This rule has a citation
breakdown" is not, by itself, a safety-critical fact for 99% of choices; it becomes one only when
the review outcome itself is safety-relevant (§6).

The tree also states the "one owner per contract" rule:

> "Every contract is stated in full **exactly once**; every other mention is a one-line
> cross-reference ... Duplicates drift."
> — `knowledge-placement.md:36-39`

A pointer comment ("citations: see breakdown `<slug>`") and the DB row it points at are two
representations of the same fact — "this artifact has a citation trail." Design (a) creates that
duplication on every review; design (b) has exactly one owner (the registry, keyed by target
path/commit).

### 2.2 `.claude/rules/subagent-economy.md` — resident cost is the dominant lever

> "The dominant token cost in this workspace is **resident orchestrator context**, not subagent
> fan-out ... **83% of billed tokens are spent above 150k context**."
> — `subagent-economy.md:3`

> "Both are resident *forever* once they land."
> — `subagent-economy.md:3`

This rule is literally about Agent-returns and inline `Read`s, not CLAUDE.md pointer lines — but
the mechanism it targets (small additions that become permanently resident and get paid for on
every subsequent turn) is exactly the shape of a pointer comment embedded in an always-loaded
rules/CLAUDE.md file. The workspace's own measured finding
(`docs/investigations/token-usage-levers-consolidated-2026-06-22.md:36-44`) is that 83.2% of
billed tokens are paid above 150k context, and that resident bloat — not fan-out — is the lever
worth pulling first. A pointer line is a small instance of the same anti-pattern the rule exists
to prevent, at the opposite end of the file's lifecycle (permanent, not per-turn).

### 2.3 `.claude/skills/context-slim/SKILL.md` — the existing auto-loaded-context auditor

context-slim is the workspace's standing answer to "does this content deserve to be
always-loaded?" Its rules-file classification is directly on point:

> "**KEEP AS-IS** — always-relevant safety or quality rules ... **STUB** — relevant on some turns
> but over-detailed ... **MOVE OUT** — only needed on rare task types; remove from rules/
> entirely, add a one-line pointer in the nearest CLAUDE.md."
> — `.claude/skills/context-slim/SKILL.md:74-76`

A citation-provenance pointer is not a safety/quality rule (KEEP AS-IS bar), so under
context-slim's own taxonomy it is at best a MOVE-OUT candidate — and MOVE-OUT's own "one-line
pointer" is reserved for things "needed on rare task types," which describes a citation lookup
well. Critically, context-slim also treats **broken references in always-loaded files as a
defect class it actively hunts for**:

> "Also scan each collected CLAUDE.md for `.claude/rules/<filename>.md` link references ...
> Verify each target file exists. Record any missing targets ... `(MISSING — referenced but not
> found)`."
> — `context-slim/SKILL.md:30`, and Phase 3's "Broken reference check" (`SKILL.md:103`)

This is the workspace already on record that a stale/dangling pointer sitting in an always-loaded
file is a tracked failure mode, not a hypothetical one. Every citation-needed pointer written
into a target is a new instance of exactly the kind of reference context-slim has to keep
checking isn't broken — i.e., design (a) adds ongoing auditing surface, not just token bulk.

### 2.4 CLAUDE.md's own "Plan location" convention — the precedent for (b)

The workspace has already solved an isomorphic discoverability problem — "how does tooling find
a per-project artifact without a pointer sprinkled through the codebase" — and solved it with a
**path convention**, not an embedded marker:

> "Each project keeps ONE canonical entry plan named `plan.md` or `master_plan.md`, at the
> project root or under `plans/`/`docs`/`documentation/` ... This is the discoverable path the
> control plane's observer + tooling read; a plan kept elsewhere/otherwise-named is invisible to
> them."
> — `c:/Users/abero/dev/CLAUDE.md:17`

dev-observatory finds every project's plan by checking a small set of conventional locations
(`descriptor-contract.md` §4), not by grepping for a pointer comment placed inside unrelated
files. This is the direct architectural analogue of design (b): a fixed, documented registry
location that tooling (and a human who knows the convention) checks, with zero footprint in the
artifacts being described.

## 3. External literature (verified this run)

These are general software-engineering and LLM findings, not citation-needed-specific, but they
sharpen two of the load-bearing sub-claims: (i) resident-context cost is a reliability problem,
not just a dollar-cost problem, and (ii) embedded pointers/comments that can drift out of sync
with their target are an empirically common, measured failure mode — supporting the staleness
argument with more than intuition.

- **Chroma, "Context Rot: How Increasing Input Tokens Impacts LLM Performance"** (Hong,
  Troynikov, Huber; Jul 2025). Across 18 SOTA models (GPT-4.1, Claude 4, Gemini 2.5, Qwen3),
  reliability drops as input length grows non-uniformly, "even for tasks as simple as retrieval
  and text replication." <https://www.trychroma.com/research/context-rot> — Verified via
  WebSearch this run (title, authors, date, and finding cross-confirmed across independent
  summaries).
  — **Relevance:** every token added to an always-loaded CLAUDE.md/rules file is not
  cost-neutral background noise; it is measured to degrade the reliability of everything else
  the model does in that context, reinforcing why "approximately zero bulk" is the right
  constraint rather than "small is probably fine."

- **Liu, Lin, Hewitt, Paranjape, Bevilacqua, Petroni, Liang, "Lost in the Middle: How Language
  Models Use Long Contexts"** (arXiv:2307.03172; TACL 2024). Performance is highest when relevant
  information is at the start or end of context and "degrades significantly" as more context is
  added around it, even for models built for long context.
  <https://arxiv.org/abs/2307.03172> — Verified via WebSearch this run (abstract, authors, venue
  cross-confirmed).
  — **Relevance:** an inline pointer line sits inside the reviewed artifact itself — i.e., inside
  the same context region as the load-bearing instructions the file exists to carry — adding
  dilution risk to the very content it is supposed to sit beside, at zero benefit if nobody is
  actively auditing that file's provenance that turn.

- **"Context Rot in AI-Assisted Software Development: Repurposing Documentation Consistency for
  AI Configuration Artifacts"** (arXiv:2606.09090). Examines staleness specifically in AI
  coding-agent configuration artifacts (CLAUDE.md-style files, agent rules) as they drift from
  the codebase they describe, and proposes repurposing documentation-consistency detection
  methods (comparing artifact state against the described code/reality) to catch it.
  <https://arxiv.org/pdf/2606.09090> — Verified via WebFetch this run (fetched and summarized the
  actual PDF).
  — **Relevance:** this is the closest literature analogue to citation-needed's own target class
  (CLAUDE.md/rules/skills). Its proposed fix is external consistency-checking against a tracked
  state (registry-shaped), not embedding more content into the artifact — independent support for
  design (b)'s shape over design (a)'s.

- **"Investigating the Impact of Code Comment Inconsistency on Bug Introducing"**
  (arXiv:2409.10781). Commits where a comment is not updated consistently with its code
  ("inconsistent changes") are "around 1.5 times more likely to lead to a bug-introducing commit
  than consistent changes," with the risk concentrated in the window right after the divergence
  first appears. <https://arxiv.org/abs/2409.10781> — Verified via WebFetch this run (fetched and
  summarized the abstract/findings).
  — **Relevance:** a citation pointer left in a target and never revisited is structurally the
  same shape as a stale code comment — text that once matched the artifact's state and no longer
  does after the artifact is edited. This is direct evidence the staleness risk named in the task
  ("staleness when the target is edited after review") is a real, measured failure class, not a
  theoretical worry.

- **"Wait, wasn't that code here before? Detecting Outdated Software Documentation"**
  (arXiv:2307.04291). Built a GitHub Actions tool to automatically detect outdated code-element
  references in docs; found "more than a quarter of the 1000 most popular projects on GitHub
  contained at least one outdated reference." <https://arxiv.org/abs/2307.04291> — Verified via
  WebFetch this run (fetched and summarized the abstract/findings).
  — **Relevance:** the field's own fix for pointer/reference staleness is automated,
  state-comparison detection external to the artifact (a registry checking a hash/commit against
  current state) — not manual pointer upkeep inside the artifact. This is the same shape as
  design (b): the registry can detect its own staleness (target file hash changed since the
  reviewed commit) without asking the target to carry any state at all.

## 4. Argument: aggregate cost of design (a)

Rough count, this run: **31 `CLAUDE.md` files** and **22 `.claude/rules/*.md` files** exist across
the workspace today (`find . -maxdepth 3 -iname CLAUDE.md` / `find . -maxdepth 4 -path
"*/.claude/rules/*.md"`, both excluding `node_modules`). Using context-slim's own token heuristic
("lines × 15" — `context-slim/SKILL.md:51`), a single HTML-comment pointer line
(`<!-- citations: see citation-needed breakdown <slug> -->`, ~12-14 words) costs roughly
**15-20 tokens**.

That looks negligible per line — the real cost is compounding, in two directions:

1. **Per-session recurrence.** Any pointer landed in a file that loads every turn is paid **every
   turn of every session** for that project, for as long as the line exists — not once. This is
   exactly `subagent-economy.md:3`'s "resident *forever* once it lands," applied to a different
   content class (provenance metadata) than the rule's literal subject (Agent returns/Reads).
2. **Cross-file accumulation under the stated use cases.** The prompt names "a project-wide rigor
   pass feeding a ranked `distill_queue`" as a use case. If that pass reviews, say, a project's
   own `CLAUDE.md` plus its half-dozen rules files in one sweep, each getting one pointer, that is
   ~6-8 lines × 15-20 tokens ≈ **100-160 extra tokens on every turn of every session for that
   project, indefinitely** — a self-inflicted, permanent increase to the exact 150k+ resident-cost
   regime the workspace's own investigation
   (`docs/investigations/token-usage-levers-consolidated-2026-06-22.md:36`) found responsible for
   83.2% of billed tokens. Doing this **as a side effect of a tool whose whole purpose is rigor
   auditing** is a bad trade: the tool would be adding to the exact bulk `context-slim` exists to
   strip out of these same files.

Design (b) adds **zero** tokens to any of these 53 files, at any point, no matter how many times
an artifact is reviewed or how large the registry grows — the registry's own size is fully
decoupled from the auto-load cost of the workspace.

## 5. Argument: discoverability without a pointer

This is design (b)'s real weakness and must be answered concretely, not waved away. Without any
in-file signal, a future editor opening a `SKILL.md` or rules file has no organic way to learn a
citation breakdown exists unless they already know to check.

The workspace's own precedent (§2.4, the Plan location convention) shows the fix is **convention
+ tooling discovery**, not embedded markers:

- citation-needed keeps ONE discoverable index at a fixed, documented path (e.g.
  `citation-needed/docs/registry/index.md` or a queryable `citation-needed report <target-path>`
  CLI verb over the SQLite DB), keyed by the target's repo-relative path. This mirrors exactly how
  dev-observatory finds every project's plan without any project embedding a pointer to it
  (`descriptor-contract.md` §4) — the observer checks a small set of conventional
  locations/verbs, not markers scattered through the codebase.
- The *habitual* discovery moment is not "someone happens to notice a pointer while reading the
  file" — it is a **process boundary that already touches the artifact**: `/plan-review`,
  `/plan-wrap`, `/repo-update`, or `context-slim` itself could gain a cheap "check citation-needed
  registry for this artifact path" step (opt-in, analogous to `descriptor-contract.md` §5's
  existing per-skill hooks table), surfacing "N citations exist, M unsupported" at exactly the
  moment someone is already reviewing or editing the file — a stronger signal than a passively-read
  inline comment that most editors will skim past anyway.
- For a human operator (not a pipeline), a `dev-observatory` per-project card or a
  `citation-needed status` CLI command is a cheaper, more current discovery surface than a static
  comment frozen at review time — it can show live counts instead of a slug that says nothing
  about whether the breakdown is fresh.

Net: discoverability is a real problem, but it does not require inline residency to solve —
it requires **one discoverable path/verb**, exactly like the plan-location convention already
proves out in this workspace.

## 6. Argument: staleness when the target is edited after review

Design (a)'s pointer is a claim frozen at review time ("this artifact has a citation breakdown"),
sitting inside a file the workspace's own working rules expect to keep changing ("Small diffs...
Update tests + docs when behavior/architecture changes" — `CLAUDE.md:19`). Every edit to the
target after review is a chance for the pointer to go stale relative to what's actually in the
file now — the exact shape of failure `arXiv:2409.10781` measured as raising bug-introduction
risk 1.5x for code comments, and that `arXiv:2307.04291` found present in >25% of the 1000 most
popular GitHub repos for documentation references generally. Nothing about markdown/CLAUDE.md
pointers makes them immune to the same drift; if anything, `arXiv:2606.09090`'s whole premise is
that AI-agent configuration artifacts suffer this exact rot class and proposes external
consistency-checking (registry-shaped) as the fix, not more embedded markers.

Design (b) sidesteps this cleanly: the registry can record the target's file hash / git blob SHA
at review time and detect staleness itself ("target has changed since review — breakdown may be
outdated") without the target ever needing to carry state. This is strictly better than a frozen
inline comment, which cannot self-detect its own staleness at all.

## 7. Argument: the read-only constraint

The task's own framing is explicit: "Reviews are READ-ONLY toward targets ... the pointer, if
endorsed, is a separate opt-in write step." A default that writes into every reviewed target
quietly turns "read-only reviews" into "read-mostly reviews" — every review becomes a write to
the target unless someone remembers to suppress it. Defaulting to (b) keeps the review's contract
exactly as designed (zero writes to targets, ever, unless a human opts a specific artifact in),
which is also the simpler system invariant to reason about and test.

## 8. Design comparison (summary table)

| Dimension | (a) inline pointer | (b) zero-touch registry |
|---|---|---|
| Resident token cost | ~15-20 tok/line, recurring every turn, forever, per always-loaded target (§4) | 0, always |
| Aggregate cost across N files | Compounds linearly with reviews; directly fights context-slim's purpose (§4) | Fully decoupled from auto-load cost |
| Discoverability | High *if* someone reads the file, but passive/skimmable | Needs one convention/verb (plan-location precedent, §5) — solvable, not free |
| Staleness | Frozen claim, cannot self-detect drift; matches a measured real failure class (§6, arXiv:2409.10781/2307.04291/2606.09090) | Can hash-check target vs. reviewed state; single owner of the fact (§2.1, §6) |
| Matches read-only review contract | No — turns review into a write by default (§7) | Yes, exactly as specified |
| Tier-tree fit (knowledge-placement.md) | Tier-3 content forced into tier-1/2 residency | Correctly placed at tier 3 (§2.1) |

## 9. Recommendation

**Default: design (b), the zero-touch central registry.** Reviews write only to SQLite + the
breakdown doc; the reviewed target is never touched. Solve discoverability with a fixed,
documented registry path/CLI verb (mirroring the workspace's existing Plan-location convention)
and, where it's cheap, an opt-in "check the registry" step added to skills that already touch the
artifact (`plan-review`, `repo-update`, `context-slim`) — not with per-artifact inline markers.

**Design (a) applies only as a narrow, explicit exception**, matching knowledge-placement.md's
inline-stub bar of "trigger condition + safety-critical fact, nothing else"
(`knowledge-placement.md:43-44`): when a specific reviewed choice inside a **high-traffic,
always-loaded, behavior-gating** artifact (a root `CLAUDE.md`, a security/safety rule) is
classified **contradicted** or carries a genuinely safety-critical caveat that every future reader
of that exact line needs to see *before* relying on it — not merely "well-supported" or
"interesting." Even then, the pointer must be operator-endorsed per artifact (the task's own "opt-in
write step"), never citation-needed's autonomous default, and should be revisited/removed once the
underlying rule is fixed or the caveat resolved (treat it as a temporary flag, not a permanent
annotation).

## Verdict

- **Default = zero-touch registry (b)**; inline pointers (a) are never citation-needed's
  autonomous default.
- One-line rationale: reviewed targets are auto-loaded every turn forever, so any bulk citation-needed
  adds to them is a permanent tax, not a one-time one — exactly the resident-cost failure mode
  `subagent-economy.md` and `knowledge-placement.md`'s tier tree exist to prevent, and the
  citation content is tier-3 reference material by that tree's own test.
- (a) is a rare, human-endorsed exception for a contradicted/safety-critical finding on a
  high-traffic behavior-gating file — never the default, and always a separate opt-in write, per
  the task's own read-only-review contract.
- Discoverability without a pointer is solved by convention (fixed registry path/CLI verb), the
  same way this workspace already solves plan-discoverability — not by embedding markers in
  N always-loaded files.
- Staleness is a real, literature-documented failure class for embedded pointers/comments
  (arXiv:2409.10781, arXiv:2307.04291); a registry with a target-hash check can detect its own
  staleness, a frozen inline comment cannot.
