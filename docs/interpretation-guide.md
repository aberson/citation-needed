# Interpretation guide — v1

**Version: `v1`.** Every `scores` row and every committed `review_runs` row stamps
`interpretation_guide_version`, and revisions of anything on this page bump the version —
old rows are **never silently reinterpreted** under new semantics (plan.md D6). The single
code implementation of everything below is `src/citation_needed/review.py`; this page is the
prose of the same math, and the two are kept in sync by tests.

## The four dimension labels and their weights

Each judge call emits exactly ONE label for one choice:

| Label | Weight | Meaning |
|---|---|---|
| `evidence-backed` | **+1.0** | Verified external literature and/or internal provenance directly supports the choice. |
| `interesting-novel` | **+0.5** | No direct backing found, but the choice is a genuinely novel, plausible idea worth its place — potential original-research material. |
| `unsupported` | **−0.5** | Checked, and nothing backs it; the choice rests on assertion alone. |
| `contradicted` | **−1.0** | Verified evidence argues AGAINST the choice. |

## Vote shares (what the four `scores` columns store)

Per choice, **k ≥ 3 independent judge calls** each emit one label. The four
`scores.*_share` columns store the fraction of votes per label:

```
share(label) = count(label) / k
```

The shares are the audit trail of judge agreement — a 3/3 `evidence-backed` choice and a
2/3 one both classify `well-supported`, but the shares record the difference.

## Parse-fail force-scoring

A judge call whose output cannot be parsed into one of the four labels arrives in the
commit payload as the literal label **`parse-failed`** and is **force-scored
`contradicted`, counted in the denominator** — never dropped. Dropping it would silently
shrink k and drag means toward whatever the parseable votes said
(`.claude/rules/measurement-validity.md` § calibrate with anchors: parse-fail→0 silently
drags means). A calibration-run parse-fail rate above 5% ABORTs the run (Step 5's gate).

## Majority vote and the escalation ladder

The majority label decides the choice's dimension label. **Ties escalate k: 3 → 5 → 7.**
Escalation is the skill layer's job at judging time; a tie that reaches
`cite review commit` is an error and rejects the whole payload loudly (nothing is
written). A tie still standing at k=7 is treated as `unsupported` by the skill layer —
judge disagreement at that depth IS evidence the backing isn't decisive — and recorded in
the choice's rationale.

## Derived per-choice classification

| Majority label | Classification |
|---|---|
| `evidence-backed` | **well-supported** |
| `interesting-novel` | **interesting** |
| `unsupported` | **needs-improvement** |
| `contradicted` | **needs-improvement** |

Every needs-improvement choice gets actionable suggestions in the breakdown doc.

## Artifact composite

```
composite = (mean(per-choice majority-label weights) + 1) / 2 × 100
```

The mean runs over the choices scored in the committing run (removed choices don't
count). The rescale maps the weight range [−1, +1] onto 0–100. The composite, its band,
and this guide's version are stored on the committed `review_runs` row; each `scores` row
also carries the same rescale applied to its own single choice.

## Bands

| Composite | Band |
|---|---|
| ≥ 70 | **strong** |
| 40 – 69 | **adequate** |
| 20 – 39 | **weak** |
| < 20 | **unsupported** |

Boundaries compare with `≥` (a composite of exactly 70.0 is `strong`, exactly 40.0 is
`adequate`, exactly 20.0 is `weak`).

Anchoring (plan §4.5): no composite is trustworthy until the calibration gate holds —
`composite(good anchor) ≥ 65`, `composite(garbage anchor) ≤ 35`, margin ≥ 40, plus the
per-dimension shape assertions — through the production path on a throwaway DB.

**Shape-assertion semantics (a deliberate D-decision).** The gate's
`evidence_backed_fraction(good)` / `unsupported+contradicted fraction(garbage)` are the
**mean per-choice vote share**, not the fraction of choices whose majority
classification lands on that side:

```
good_evidence_share    = mean over good's choices of  count(evidence-backed votes) / k
garbage_negative_share = mean over garbage's choices of
                         (count(unsupported) + count(contradicted)) / k
```

Both must be ≥ 0.6. The rejected alternative reading — "fraction of choices
majority-classified evidence-backed" — is equally consistent with the plan's wording but
discards judge-agreement information and is strictly easier to game: a run where every
good choice scrapes a 3-of-7 evidence-backed plurality would score a perfect 1.0 under
the majority-fraction reading while the mean vote share is only ≈ 0.43 — weak agreement
the gate is designed to refuse. Mean vote share is continuous (every vote moves it) and
consistent with the vote-share columns above being the audit trail. A regression test
pins the divergence case (5/5 majority-evidence-backed, mean share 3/7 → FAIL) as
intended behavior.

## "No literature found" is a result

`scores.literature_searched` / `literature_found` / `search_queries` distinguish three
states a naive "citations missing" heuristic conflates: **never checked**
(`searched=0` — a data gap), **checked and empty** (`searched=1, found=0` — the
first-class no-literature-found finding, with the actual query strings recorded so the
null result is auditable), and **checked and contradicted** (`found=1` with a
`contradicted` majority — a different, worse outcome).

## Load weights and the distill rank

`distill_queue` proposals rank by how costly an unsupported choice is *where it lives*,
operationalizing knowledge-placement.md's tier cost ordering:

```
rank = (1 − composite/100) × artifact_load_weight
```

| artifact_type | Load weight (v1) | Why |
|---|---|---|
| `claude_md` | **3.0** | Auto-loads every session — the most expensive tier. |
| `rule` | **3.0** | Auto-loads every session (situational-but-always). |
| `memory` | **1.5** | Index always loaded; body on demand. |
| `skill` | **1.0** | Loads only on trigger. |
| `plan` | **0.75** | Read at planning/build moments only. |

Higher rank = more urgent. An unsupported CLAUDE.md-inline choice outranks an equally
unsupported skill choice for triage attention.

`composite` in the rank formula is the **per-choice composite** stored on the choice's
`scores` row (each queue row is one choice), so within the same tier a `contradicted`
choice (composite 0 → rank = weight) outranks an `unsupported` one (composite 25 →
rank = 0.75 × weight). The single code implementation of the formula and the weight
table is `distill.py` (`LOAD_WEIGHTS` / `queue_rank`); the table above is tested
against it for drift.

### Mechanical proposal defaults (`cite distill generate`)

Only **needs-improvement** choices get a queue row — well-supported and interesting
choices yield NO row (a backlog entry for a fine choice is noise, not signal). The
mechanical default kind comes from the majority label:

| Majority label | Default `proposal_kind` |
|---|---|
| `contradicted` | `rewrite` (evidence argues against the text — replace it) |
| `unsupported` | `trim` (a real search found nothing — the text earns no load cost) |

The row's justification is built from the choice's real record — its linked citation
ids, or its documented absence (`literature_searched=1` + the queries tried). A choice
with **neither** rejects the whole generate run loudly: a justification cannot be
fabricated (`distill_queue.justification` is NOT NULL by design). One row per choice:
re-generating or re-proposing refreshes the open row in place (no duplicates); a
resolved row is never touched. The skill layer can override kind + justification via
`cite distill propose` (contract: `prompts/distill.v1.md`); rank always stays
formula-computed.

**Queue lifecycle across re-reviews (supersession).** Open rows are derived state —
an unresolved row carries no operator decision, so it never outlives its evidence. A
re-review commit purges, inside the same transaction, every open row of the artifact
that the just-committed run no longer justifies: the choice was removed from the
artifact, or its newest classification is anything but `needs-improvement`. Resolved
rows (`accepted`/`rejected`/`applied`) are immutable audit and always survive. As a
display-side guard, `cite queue list` appends `[superseded run]` to any row whose
source run is no longer the artifact's newest committed run — a still-flagged choice
loses the marker the next time `cite distill generate` refreshes its row against the
new run. The single implementation is `distill.supersede_stale_open_rows`, called
from `review.commit_review`.

### Resolution mapping (`cite queue resolve`)

The status enum is `open | accepted | rejected | applied`. The operator decision maps:

| Decision | `status` | Meaning |
|---|---|---|
| `--keep` | `rejected` | Proposal declined; the target text stays. |
| `--cut` | `accepted` | Proposal proceeds; the text should be cut. |
| `--rewrite` | `accepted` | Proposal proceeds; the text should be rewritten. |

`--cut` and `--rewrite` both land on `accepted` — WHICH edit shape is recorded by the
row's `proposal_kind` (run `cite distill propose` first if the kind should change).
Every resolution stores `resolved_by` (`--by`, else env `USERNAME`/`USER`) and
`resolved_at` (pipeline clock). The `applied` transition is **out of the CLI's
scope**: target edits happen outside citation-needed (the engine never edits a
target), so `applied` is reserved for whatever tool performs the edit.

## Versioning policy

- `v1` is defined by this page: the four labels + weights, vote-share storage,
  k ≥ 3 majority with the 3→5→7 escalation ladder, parse-fail force-scoring, the
  classification map, the composite rescale, the 70/40/20 bands, the load-weight
  table, the mechanical proposal defaults, and the resolution mapping above.
- Any change to any of these cutpoints or semantics ships as `v2` (a new page section or
  file revision), and new rows stamp `v2` — existing rows keep `v1` and keep meaning what
  `v1` says. Comparisons across guide versions are done explicitly, never by pretending
  the numbers are on the same scale.
