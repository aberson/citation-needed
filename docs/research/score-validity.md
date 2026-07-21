# Score validity — anchored calibration gate design

Status: design only (citation-needed has no code yet — `citation-needed/` contains just this
`docs/research/` scaffold as of 2026-07-21). Nothing in this document has been executed; every
external citation below was opened/verified this session (URL fetched, content confirmed) — see
§8 for the full list. Internal citations are `path:line` against the live workspace.

## 0. Governing rule (read first)

Design target: `c:/Users/abero/dev/.claude/rules/measurement-validity.md`. The load-bearing clause
is **"Calibrate with anchors before comparing candidates"**
(`.claude/rules/measurement-validity.md:23-27`):

> Before trusting a new metric, feed it a frozen known-good and a known-garbage input and assert
> `score(good) > score(garbage)`. A bench that can't fail garbage can't pick winners. For LLM
> judges, additionally: a parse-failure-rate threshold (parse-fail→0 silently drags means toward
> zero), and k≥3 samples or a median (a single-sample judge scored the same build 10 and 5).

Its positive exemplar (`scripts/readiness_bench/`, `measurement-validity.md:27`) asserts a
4-archetype canonical key 4/4 through the *production* classifier before any comparative run — the
pattern this design copies: prove the instrument on known inputs through the real code path, then
and only then trust it on an unknown target.

Two sibling clauses this design also has to satisfy:
- **"Assemble through the production code path"** (`measurement-validity.md:11-15`) — calibration
  must call citation-needed's real extract → cite → classify → score pipeline, never a
  hand-rolled parallel harness.
- **"Fail loud on fallback config"** (`measurement-validity.md:17-21`) — calibration failure is an
  ABORT, never a logged warning.

## Pre-registration (per `measurement-validity.md:29-33`)

> This number — the anchored composite score (0–100, aggregated from each extracted choice's
> evidence-backed / unsupported / contradicted / interesting-novel classification) — computed from
> a target artifact's extracted choices via citation-needed's production review pipeline
> (choice-extraction → DB-corpus-first citation lookup → live-web-search fallback → per-choice
> classification → per-dimension aggregation) will decide whether that artifact's embedded choices
> are trustworthy as-is or should be flagged for revision, feeding the ranked `distill_queue` and
> operator backlog triage; the synthetic garbage-rule fixture (§2) must score in the bottom band and
> the frozen `code-quality.md` good anchor must score in the top band before any real review's
> composite is trusted.

Every blank in that sentence is filled (artifact, production path, decision, garbage-low/gold-high
expectation) — the instrument is spec'd enough to build, per the rule's own readiness test.

## 1. Candidate survey for the good anchor

Surveyed every file in `c:/Users/abero/dev/.claude/rules/` (13 files) for the richest **documented
regression provenance** — a real incident, with a cost, not just a stated convention:

| Rule file | Incidents w/ quantified cost | Verdict |
|---|---|---|
| `code-quality.md` | **4** distinct incidents, each with a number (70 min soak, 4/682 tests, 4 reviewers approved a 5→1 narrowing, "cost one iteration") + a 5th orchestration contract | **Chosen** |
| `measurement-validity.md` | 2-3 incidents (void_furnace bake-off, Windows harness fallback) | Rich, but it's the rule *this gate itself* is built to satisfy — using it as the anchor risks the appearance of grading citation-needed's own governing document against itself. Passed over for that reason, not lack of quality. |
| `security.md` | 2 incidents (toybox #4/#5 injection, void_furnace secrets 2026-05-09 "twice") | Real but thinner: no cost figures, fewer choices to extract (3 total) |
| `worktree-hygiene.md` | 0 named incidents (stub referencing a fuller reference doc) | Directive-only, no provenance to score |
| `plan-and-issue-flow.md`, `descriptor-contract.md`, `knowledge-placement.md`, `working-directory.md`, `shareable-docs.md`, `subagent-economy.md`, `windows-shell.md`, `command-presentation.md`, `python.md` | Mostly conventions/rationale, 0-1 loosely-dated incidents each | Not incident-dense enough |

**Good anchor: `c:/Users/abero/dev/.claude/rules/code-quality.md`.** It is a real, currently-loaded
workspace rule (not authored for this exercise), already checked into the repo, with five
extractable choices each carrying an explicit internal-provenance citation (its own
`## Source memories` block, lines 72-77) — exactly the "internal workspace provenance, secondary"
citation class citation-needed's spec calls for, present and ready to be picked up by the reviewer
without any authoring on my part.

## 2. The two anchors

### 2a. Good anchor — `code-quality.md` (frozen, real)

Freeze the file at its current committed content (`git rev-parse HEAD` at freeze time; re-freeze
only if the file itself is edited — see §6 trigger D). Five choices a production extraction pass
should find:

| # | Choice (path:line) | Internal provenance (secondary citation) | External literature found this session (primary citation) |
|---|---|---|---|
| 1 | Grep all downstream consumers before landing a key/id-shape change (`code-quality.md:9-17`) | Alpha4Gate Phase 4.6 Step 1, 70-min soak (`:15`) | **Verified**: [Change impact analysis](https://en.wikipedia.org/wiki/Change_impact_analysis) (Wikipedia, summarizing Bohner & Arnold 1996) — "identifying the potential consequences of a change... before altering shared code, practitioners use [impact analysis] to... trace dependencies." Direct match: grep-all-consumers *is* dependency-impact-analysis practiced manually. |
| 2 | One source of truth for data-shape constants, assert `is` not `==` (`code-quality.md:19-27`) | Alpha4Gate Phase 4.5, 4 instances / 682 tests (`:27`) | **Verified**: [Don't repeat yourself](https://en.wikipedia.org/wiki/Don%27t_repeat_yourself) (Wikipedia, citing Hunt & Thomas, *The Pragmatic Programmer*) — "Every piece of knowledge must have a single, unambiguous, authoritative representation within a system." Direct match. |
| 3 | Audit wire shape on storage-representation change; treat response-shape test-diffs as suspect (`code-quality.md:29-35`) | Toybox G2, 5→1 steps, 4 reviewers approved (`:35`) | **Verified**: [STING (arXiv 2604.01518)](https://arxiv.org/abs/2604.01518) — empirically found 77% of SWE-bench-Verified instances have "surviving variant patches" that pass existing tests despite being semantically wrong, and re-strengthened tests dropped reported success 4.2-9.0%: *"a substantial share of previously passing patches exploit weaknesses in the benchmark tests rather than faithfully implementing the intended fix."* Same failure shape as the Toybox G2 incident, independently documented. |
| 4 | New components need an integration test through the production caller (`code-quality.md:37-43`) | Toybox step 15 iter 1, silent zero-judge-calls (`:43`) | **Verified**: [Martin Fowler, TestPyramid](https://martinfowler.com/bliki/TestPyramid.html) — high-level/integration tests are needed as "a second line of test defense" precisely because unit tests alone don't exercise cross-module wiring. Direct match (unit-tested-in-isolation-but-never-invoked is exactly the wiring gap the pyramid's higher layers exist to catch). |
| 5 | Build-phase halt contract, 5 legitimate halt conditions (`code-quality.md:45-70`) | BPA Step 10 addition (`:7`); this is an orchestration convention specific to this workspace's `/build-phase` skill | **No literature found this session** — this is a workspace-specific orchestration contract, not a general software-engineering principle; a real reviewer would legitimately record "no external literature — internal-provenance-only" rather than force a citation. This is expected to land as **interesting-novel** or **unsupported-but-internally-grounded**, not evidence-backed, and that's correct, not a defect in the anchor. |

Expected good-anchor profile: **4 of 5 choices evidence-backed** (both citation classes present),
**1 of 5 internal-only** with no external hit (a legitimate, not-fabricated gap). This mix is
deliberate: a good anchor that scores 5/5 evidence-backed would itself be suspicious (real
artifacts always have at least one workspace-only convention) — see the mutation-testing framing in
§4 for why an anchor set should include a "hard-but-fair" item, not only slam-dunks.

### 2b. Garbage anchor — synthetic, to be authored as a frozen fixture

**Not written to disk by this investigation** (this deliverable modifies nothing outside this one
file). The content below is the fixture text a build step should freeze verbatim at, e.g.,
`citation-needed/fixtures/calibration/garbage-anchor.SYNTHETIC.md`, with a `SYNTHETIC — DO NOT USE
AS REAL GUIDANCE` banner kept in the frozen file itself so it can never be mistaken for a real rule
if it leaks into a listing.

```markdown
<!-- SYNTHETIC CALIBRATION FIXTURE — fabricated for citation-needed's score-validity gate.
     Not a real workspace rule. Do not apply. Do not delete the banner when freezing. -->

# Performance & Reliability Shortcuts (synthetic)

Five practices adopted to keep CI green and services fast under load.

## Auto-retry every failing test up to 3 times, treat any pass as fully green
If a test fails, rerun it automatically up to 3 times; if it passes on any attempt, mark the whole
suite green with no further logging or investigation. This keeps CI green during transient network
blips.

## Cache every read-through query with a flat 24-hour TTL, no exceptions
All cached reads use a single 24-hour time-to-live regardless of how often the underlying data
changes, to keep the cache layer simple.

## Skip code review on any diff under 20 lines
Diffs under 20 lines are auto-merged without review to keep velocity high; small diffs are
"obviously safe."

## Log full request/response bodies at INFO level in production for debuggability
Every service logs complete request and response payloads at INFO level in production so on-call
engineers can always see exactly what happened.

## Prefer a single god-object config class over per-module config
Centralize every module's configuration into one large shared config class, rather than scoping
config to the module that owns it, so there is only one place to look.
```

Expected classification (per-choice, using literature checked this session — a real calibration
build should re-verify at fixture-freeze time, not trust this table blindly):

| # | Choice | Expected class | Grounding |
|---|---|---|---|
| 1 | Auto-retry-to-green, no investigation | **Contradicted** | **Verified**: [QA Wolf](https://www.qawolf.com/blog/what-your-system-should-do-with-a-flaky-test) — even a source generally favorable to retries states retries must be "cap[ped]... to avoid masking real instability"; the broader documented consensus (multiple independent sources surfaced this session) is that retry-until-green without root-causing "masks real, intermittent bugs" and creates "a false sense of reliability." The fixture's version — no logging, no investigation, just green — is the unqualified anti-pattern form. |
| 4 | Log full req/response bodies at INFO in prod | **Contradicted** | **Verified**: [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) — lists "HTTP request body... and body" among data that should be isolated from general logs, and PII/tokens/secrets as data that must be "removed, masked, sanitized, hashed, or encrypted," never logged at a routine level. |
| 2 | Flat 24h TTL for all caches | UNVERIFIED this session (**no literature search run** — flag as a real finding, not fabricated) | Plausible anti-pattern (cache-invalidation-strategy-should-match-data-volatility is a well-known engineering maxim) but not checked against a resolvable source this run. A real fixture-freeze pass must search before labeling; do not inherit this row's "expected" value without doing so. |
| 3 | Skip review under 20 lines | UNVERIFIED this session | Same caveat — plausible but unchecked. |
| 5 | God-object config class | UNVERIFIED this session | Same caveat; also genuinely debatable in practice (context-dependent), which is a feature for a calibration set — not everything should be a slam-dunk (§4). |

Two of five choices are already verified-contradicted by real, resolvable literature; the other
three are honestly flagged unverified rather than backfilled with invented sources. **A production
fixture-freeze step must close those three before the fixture is frozen** — either with a real
citation (contradicted/unsupported) or by replacing the choice with one that resolves cleanly. The
gate does not require 5/5 contradicted — it requires the *aggregate* to land far below the
good anchor (see §3).

## 3. Gate assertion

**Scoring model** (proposed; citation-needed has no schema yet, so this doubles as the schema
proposal): each extracted choice gets exactly one of four dimension labels — `evidence-backed`
(+1.0), `interesting-novel` (+0.5), `unsupported` (−0.5), `contradicted` (−1.0) — and the
project's three-way classification (`well-supported` / `needs-improvement` / `interesting`) is a
derived view (`evidence-backed → well-supported`; `unsupported`/`contradicted` →
`needs-improvement`; `interesting-novel → interesting`). **Composite** = mean(dimension weight
across all choices in the artifact), rescaled `(mean + 1) / 2 * 100` to a 0–100 band for the
interpretation guide.

Applying §2's tables: good anchor ≈ (4×1.0 + 1×0.5)/5 = 0.90 → **~95/100**; garbage anchor with
just the 2 verified-contradicted rows scored and the 3 unverified rows scored conservatively as
`unsupported` ≈ (2×−1.0 + 3×−0.5)/5 = −0.70 → **~15/100**. These are *design-time estimates from
literature checked this session*, not a real run — the actual numbers must come from executing the
production pipeline (§4/§5), but they show the anchors are separated by a wide margin under
plausible scoring, which is the property the gate needs.

**Gate assertion** (all four must hold; any failure is a single ABORT, not a partial pass):

1. `composite(good) >= 65` (absolute floor — the good anchor lands in the top band)
2. `composite(garbage) <= 35` (absolute ceiling — the garbage anchor lands in the bottom band)
3. `composite(good) - composite(garbage) >= 40` (relative margin — collapsed-together compositors both landing at, say, 45 and 50 would each individually clear neither absolute bound but a margin-only check could still be gamed by two mediocre scores; require all three)
4. Per-dimension: `evidence_backed_fraction(good) >= 0.6` AND `(unsupported_fraction + contradicted_fraction)(garbage) >= 0.6` — checks the *shape* of the classification, not just the scalar, so a composite that happens to land in-band via an unrelated dimension mix still fails on structure.

**On failure: ABORT.** No real target review may run, no composite may be emitted, and — critically
for this project's compounding-corpus design — **no citations found during the failed calibration
attempt may be persisted to the real DB** (write only to the throwaway calibration DB, §5). Emit a
loud, structured failure record: which of the 4 assertions failed, the per-choice dimension diff
vs. the last-passed calibration (if any), and the current prompt/model/corpus fingerprints (§6) so
a human can tell *what changed* rather than just *that it broke*. This is the "fail loud on
fallback config" clause (`measurement-validity.md:17-21`) applied to the calibration gate itself,
not just to config resolution.

## 4. LLM-judge hygiene

Per `measurement-validity.md:25`, and independently corroborated this session:
[arXiv 2606.13685, "The Coin Flip Judge?"](https://arxiv.org/abs/2606.13685) — measured
single-trial LLM-judge pairwise-preference flip rates averaging **13.6%**, exceeding 20% on 28% of
questions and reaching **56%** on one; its recommendation: *"single-trial LLM judging is often too
noisy for high-stakes evaluation, and... multi-trial aggregation, position randomization, and
explicit uncertainty reporting should be standard practice."* The paper further finds ~11 repeated
trials are needed for a majority vote to recover a 50-trial reference verdict at 95% probability on
average (15 on high-variance items) — i.e., the rule's `k>=3` is a workable floor for a
budget-bound per-choice classifier, not a number that guarantees convergence on a hard case.

Design for citation-needed's classifier:

- **k=3 independent classification calls** per (choice, dimension-label) pair as the floor.
  Aggregate the categorical label by **majority vote**; if 3-way split (no majority), escalate to
  k=5, then k=7 if still split — mirroring the paper's finding that variance-not-fixed-k is what
  should drive sample count.
- **Median**, not mean, for any numeric confidence sub-score a judge call returns (protects against
  one outlier call skewing the aggregate, matching the rule's "or a median" alternative).
- **Parse-failure handling**: a call whose output fails schema validation (missing required field,
  invalid enum value, non-JSON) is a **parse failure**. Per `measurement-validity.md:25` ("parse-
  fail→0 silently drags means toward zero"), a parse failure is **force-scored as `contradicted`
  (the worst dimension, −1.0 / 0-on-100)** and counted in the denominator — never silently dropped
  (dropping it would shrink N and quietly inflate the mean, the exact failure mode the rule calls
  out).
- **Parse-failure-rate threshold**: track `parse_failures / total_calls` across the whole
  calibration run (both anchors, all k-samples). If this exceeds **5%**, ABORT calibration before
  even reaching the score(good) > score(garbage) assertion — a judge/parser combination failing to
  parse its own output more than 1-in-20 times is broken independent of what it scores, and letting
  force-scored zeros silently drag the garbage anchor's mean down would make the gate look like it
  passed for the wrong reason (a broken parser, not real discrimination).

## 5. Production-code-path requirement

Calibration must invoke citation-needed's actual pipeline functions in sequence — extract choices →
DB-corpus-first citation lookup → live-web-search fallback only on corpus miss → classify → score —
never a parallel hand-rolled calibration script that re-implements a lighter version of any stage.
This is `measurement-validity.md:11-15` applied directly, and it matters doubly here because the
DB-corpus-first *ordering itself* is part of what calibration must validate: a citation-lookup bug
that fuzzy-matches the wrong DB rows could make the garbage anchor look artificially well-cited if
the search step is reimplemented instead of imported.

**Throwaway DB, not the real corpus.** Citation-needed's own design compounds every vetted citation
into a persistent SQLite DB across reviews (task brief, "every vetted citation persists to SQLite
so the corpus compounds"). Calibration must run against a **copy-on-write throwaway** — e.g., copy
the current production DB file to a temp path (or open the real DB read-only and layer writes onto
an in-memory overlay) — using the *same schema module* production imports (not a hand-copied
`CREATE TABLE`, per `code-quality.md:19-27`'s one-source-of-truth clause applied to the calibration
harness itself). Two reasons this is non-negotiable, not just tidy:

1. The garbage anchor's fabricated claims must never land in the real, compounding corpus — a
   synthetic "auto-retry-to-green is fine" citation persisting into the DB that later real reviews
   draw on would poison every subsequent review's DB-corpus-first lookup.
2. The good anchor's real citations found *during calibration* are legitimate and could be
   pre-seeded into the real corpus as a one-time bootstrap — but that must be a deliberate,
   reviewed promotion step, not an automatic side-effect of every calibration run (which would
   otherwise re-insert duplicate rows each time the gate re-runs).

## 6. Re-calibration triggers

Recalibration is **not** a per-session tax — cache the last-passed calibration's verdict plus three
fingerprints in the calibration DB, and only re-run the full k≥3-sample anchor scoring when one of
them changes:

| Trigger | Fingerprint compared | Why |
|---|---|---|
| **A. Prompt/rubric change** | Hash of the extraction + citation + classification prompt template file(s) | Any wording change to what the classifier is asked to do invalidates prior calibration by definition |
| **B. Pinned model change** | The *resolved* model id string (e.g. the dated snapshot id, not the `opus` alias) | CLAUDE.md itself notes auto-updates can silently reset the model pin — the exact silent-drift case this must catch; compare the resolved id, not the config label |
| **C. DB-corpus growth** | Row count + max citation id (or a content hash) of the citation table | The corpus compounds across reviews by design — DB-corpus-first lookup can find different (more, or differently-ranked) hits for the *same* anchor choices as the corpus grows, shifting anchor scores with no prompt or model change at all |
| **D. Schema/taxonomy migration** | Schema version of the citation DB / dimension taxonomy | Structural change; always recalibrate |

Non-load-bearing but recommended: a **staleness ceiling** (e.g. 30 days) even with all fingerprints
unchanged, as a cheap backstop against unmodeled drift (e.g. a cited web page going stale or
disappearing, changing what a live re-run would find for the same anchor choice). Unlike A-D this
is advisory, not a hard block.

## 7. Open design questions for the planner

- The three-way (`well-supported`/`needs-improvement`/`interesting`) ↔ four-way
  (`evidence-backed`/`unsupported`/`contradicted`/`interesting-novel`) mapping in §3 is this
  investigation's proposal, not an existing citation-needed schema (none exists yet) — confirm or
  revise before implementation.
- The three UNVERIFIED garbage-anchor rows (§2b, choices 2/3/5) need a real literature check (or
  swap) before the fixture is frozen — do not freeze with placeholder "expected" values.
- Composite weights (+1.0/+0.5/−0.5/−1.0) and gate thresholds (65/35/40-point margin/0.6 fraction)
  are this design's proposal, chosen to give comfortable separation given the estimates in §3 —
  they are tunable once a real calibration run produces actual numbers, but should not be loosened
  reactively just because a first real run misses them (that would defeat the gate's purpose).

## 8. Verified external citations (opened this session)

- [Don't repeat yourself](https://en.wikipedia.org/wiki/Don%27t_repeat_yourself) — Wikipedia
- [Change impact analysis](https://en.wikipedia.org/wiki/Change_impact_analysis) — Wikipedia
- [Are Benchmark Tests Strong Enough? (STING)](https://arxiv.org/abs/2604.01518) — arXiv 2604.01518
- [TestPyramid](https://martinfowler.com/bliki/TestPyramid.html) — Martin Fowler
- [The Coin Flip Judge?](https://arxiv.org/abs/2606.13685) — arXiv 2606.13685
- [What your system should do with a flaky test](https://www.qawolf.com/blog/what-your-system-should-do-with-a-flaky-test) — QA Wolf
- [Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) — OWASP

## 9. Verdict (compressed)

Good anchor: `.claude/rules/code-quality.md` (frozen, real, 4/5 choices already literature-backed
this session). Garbage anchor: synthetic 5-choice fixture in §2b (2/5 already verified-contradicted;
3/5 flagged UNVERIFIED, not fabricated — must be closed before freeze). Gate: composite(good)≥65
AND composite(garbage)≤35 AND margin≥40 AND per-dimension-shape check, ABORT (no DB writes, no
target review) on any failure. Judge: k≥3 majority/median, parse-fail force-scored 0 and counted,
>5% parse-fail-rate also ABORTs. Recalibrate on prompt/model/corpus/schema fingerprint change
(cached otherwise, not per-session); production pipeline + throwaway-DB copy only, never a sibling
harness or the real compounding corpus.
