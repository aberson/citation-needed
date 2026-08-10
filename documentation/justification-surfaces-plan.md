# Citation Justification Surfaces

**Status:** APPROVED (2026-08-07)
**Depends on:** canonical `plan.md` Steps 7-10 (corpus, four skills, real review, small sweep)

## 1. What This Feature Does

Turns the populated citation corpus into readable status and skill-justification views: choose a
skill, inspect its extracted claims and citations, and optionally launch the existing review/distill
workflow to update evidence.

Proposal: [Utility Projects UAT proposal](../../docs/utility-project-surfaces-proposal.html)

## 2. Existing Context

`db.py` owns SQLite access, `review.py` commits frozen review runs, `breakdown.py` renders detailed
documents, and `cli.py` owns mechanical verbs. The canonical plan already assigns LLM judgment to
four `/citation-*` skills; this feature must reuse them. same-page detects status contradictions and
does not own research justification.

## 3. Scope

**In:** useful populated status, recent reviews, skill list/detail JSON, citation freshness,
`x-justify` observatory view, and a terminal update selector. **Out:** browser editing, target-file
writes, bypassing calibration, fabricating citations, or adding a second review engine.

## 4. Impact Analysis

| File | Change Type | Reason | Verified |
|---|---|---|---|
| `src/citation_needed/db.py` | reuse | connection/bootstrap/migration boundary | module docstring confirms this scope |
| `src/citation_needed/read_queries.py` | create | overview/justification SQL and locator relocation | avoids adding domain queries to db.py |
| `src/citation_needed/review.py` | reuse | review provenance/freshness | current review mechanics |
| `src/citation_needed/breakdown.py` | reuse/extend | detail rendering | current breakdown owner |
| `src/citation_needed/cli.py` | extend | overview/justify/update-select | only CLI dispatcher |
| `src/citation_needed/models.py` | extend | versioned read DTOs | current typed boundary |
| `tests/` | extend | query, JSON, empty/populated behavior | existing module coverage |

## 5. New Components

- `cite overview --json`: schema version, row counts, latest reviews, stale targets, open distill
  queue, and explicit setup/readiness state.
- `cite justify list --type skill --json` and `cite justify show <artifact-id> --json`: claims,
  locators, classifications, citations, search-empty findings, and review provenance.
- `cite update-select`: terminal selector that prints the exact `/citation-review` or
  `/citation-distill` invocation for the selected artifact; it does not invoke Claude or implement
  judgment itself.
- Locator relocation matches the stored quote/content hash against current text: one exact match
  yields `current`, multiple matches yield `ambiguous`, no match yields `missing`, and an unchanged
  artifact hash may reuse the stored span. No guessed line is displayed.

## 6. Design Decisions

The requested `x-justify` label is an observatory label; the CLI uses the clearer `justify`.
`x-update` is optional and remains a terminal/skill action because writes require calibration and
judgment. `init-db` and migration remain setup instructions in README, never recurring buttons.
Empty corpus is a setup state, not a successful-looking dashboard. citation-needed does not import,
invoke, or require same-page; assigning justification here preserves both projects' independent
contracts rather than creating a convenience integration between them.

## 7. Build Steps

### Step 11: Overview and readiness JSON
- **Status:** done (2026-08-09; focused and full verification passed)
- **Problem:** Replace opaque table counts with a versioned overview that distinguishes uninitialized,
  initialized-empty, ready, stale, and review-in-progress states and includes recent activity.
- **Type:** code
- **Issue:** #17
- **Flags:** --reviewers code --isolation worktree
- **Files:** `src/citation_needed/read_queries.py`, `src/citation_needed/models.py`,
  `src/citation_needed/cli.py`, `tests/test_read_queries.py`, `tests/test_cli.py`
- **Produces:** overview query/model/CLI and tests
- **Done when:** empty and populated fixtures render different, truthful states
- **Depends on:** 7

### Step 12: Skill justification list/detail
- **Status:** done (2026-08-09; focused and full verification passed)
- **Problem:** Add deterministic list/show queries for reviewed skills, claims, exact locators,
  classifications, citation records, documented search absence, review provenance, and
  exact/ambiguous/missing current-locator status.
- **Type:** code
- **Issue:** #18
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `src/citation_needed/read_queries.py`, `src/citation_needed/breakdown.py`,
  `src/citation_needed/models.py`, `src/citation_needed/cli.py`,
  `tests/test_read_queries.py`, `tests/test_breakdown.py`
- **Produces:** justify queries/CLI, JSON contract, tests
- **Done when:** every list ID resolves to detail and every citation maps to a verified DB row
- **Depends on:** 9, 11

### Step 13: Update selector over existing skills
- **Status:** done (2026-08-09; focused and full verification passed)
- **Problem:** Add a terminal selector that picks an artifact then prints the exact
  `/citation-review` or `/citation-distill` command; do not invoke Claude or duplicate prompts,
  calibration, or writes.
- **Type:** code
- **Issue:** #19
- **Flags:** --reviewers code
- **Files:** `src/citation_needed/update_select.py`, `src/citation_needed/cli.py`,
  `tests/test_update_select.py`, `docs/observatory-contract.md`
- **Produces:** `cite update-select`, skill handoff contract
- **Done when:** cancel writes nothing and each selection emits the correct canonical skill command
- **Depends on:** 8, 12

### Step 14: Observatory artifact export
- **Status:** CODE COMPLETE — **NOT DONE** (code merged to master as `3e3cf12` on 2026-08-10,
  full suite green at 387 passed / 2 deselected; exporter, fixture contracts, and all automated
  verification pass). The exit criterion is unmet: every automated test runs against synthetic or
  empty fixtures, and `Done when` requires a **real** reviewed `skill` artifact to round-trip.
  No `/citation-review` run has ever produced one. Verified 2026-08-10 against the production
  surface: `cite justify list --type skill --json` returns `{"artifact_type": "skill",
  "items": [], "schema_version": 1}`. The 13 breakdowns in `breakdowns/` are all artifact type
  `rule`; none are type `skill`. Clearing this requires Step 15's real populated smoke — do not
  mark DONE until then.
- **Problem:** Write one bounded, versioned overview + justification list/detail artifact and
  contract fixtures. dev-observatory alone owns labels, registry integration, and setup-button removal.
- **Type:** code
- **Issue:** #20 (OPEN — stays open until the exit criterion below is met)
- **Flags:** --reviewers code
- **Files:** `src/citation_needed/observatory_export.py`, `src/citation_needed/cli.py`,
  `tests/test_observatory_export.py`, `docs/observatory-contract.md`
- **Produces:** artifact exporter and fixtures
- **Done when:** a real reviewed skill round-trips and export performs no DB writes
- **Depends on:** 12, 13

### Step 15: Real populated smoke
- **Problem:** Run calibration, review one real skill, then verify overview and justification output.
- **Type:** operator
- **Issue:** #
- **Produces:** operator evidence only
- **Done when:** recent activity is non-empty and every displayed citation is fetch/API/internal-read verified
- **Depends on:** 14

## 8. Risks and Open Questions

| Item | Risk | Mitigation |
|---|---|---|
| Canonical Steps 7-10 incomplete | surfaces remain empty | hard dependency and readiness state |
| `x-update` bypasses review controls | uncalibrated writes | hand off to existing skills only |
| Line locators drift | stale-looking proof | show review hash/freshness and current locator |

## 9. Testing Strategy

Use uninitialized, empty, populated, stale, removed-choice, no-literature, and mixed citation-class
fixtures. The real smoke must exercise the production review path before any UI acceptance.

## Appendix: Decision Inventory

| ID | P/D | Choice | Status |
|---|---|---|---|
| P2 | P | Add a two-level skill justification view and optional update workflow | accepted |
| D2 | D | Emit bounded JSON artifacts; dev-observatory owns HTML and registry wiring | accepted |
| D4 | D | Keep updates behind existing calibrated citation skills | accepted |
| D5 | D | Keep same-page and citation-needed independent; justification belongs solely here | changed 2026-08-07 |
