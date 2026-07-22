<!-- FROZEN CALIBRATION SNAPSHOT -- do not edit; this is NOT the live rule file.
     Source: c:/Users/abero/dev/.claude/rules/code-quality.md (the real workspace rule)
     Frozen: 2026-07-21 (citation-needed plan.md Step 5)
     Why frozen: calibration anchors must never drift with the live file -- the gate
     scores a KNOWN input, so a silent drift would invalidate every cached calibration.
     Re-freezing is a deliberate act: copy the live file again AND update the sha256
     recorded in expected-labels.json (one explicit edit), per score-validity.md par.6
     trigger D. -->

---
description: Implementation-time discipline for producer-consumer drift and silent-wiring failures that unit tests with mocks routinely miss.
---

# Code-quality discipline

Four implementation-time rules learned from real regressions across Alpha4Gate and toybox. All four were missed by extensive test suites (600+ tests in two cases) and surfaced via real-client smoke or operator UAT. Common cause: tests with mocks can't see producer-consumer drift. Plus one orchestration-time contract — the build-phase halt allowlist — added in BPA Step 10; it documents which halts are legitimate during `/build-phase` runs rather than implementation-time discipline.

## Grep all downstream consumers when changing a key/id shape

When a fix changes the shape of a primary key, cache key, id format, filename format, or any value referenced from multiple call sites: grep every consumer of the old shape before landing. One grep beats a follow-up phase after a missed caller.

Attach the grep results to the issue or PR with one row per call site and a verdict (`OK | needs fix | already handled`). Add an integration test that exercises the FULL producer → consumer round trip — the bug lives in the relationship, not in either endpoint.

Alpha4Gate Phase 4.6 Step 1: changed `SC2Env._game_id` shape but missed `evaluator._get_game_result(base_id)`. Soak-4 spent 70 minutes with 12 eval games flagged "crashed" before DB forensics found the missed caller. Cost: a whole Phase 4.7 plan-review-repo-sync-build-phase cycle.

This is the implementation-time counterpart to [`plan-and-issue-flow.md`](plan-and-issue-flow.md) § Read producers before drafting plan content.

## One source of truth for data-shape constants

Dimensions, action counts, schema column lists, magic widths — any constant defining data shape must have ONE source of truth. Duplicate definitions always drift, and unit tests that mock either side don't catch it.

- Define once in a leaf module both producer and consumer import.
- Regression tests must assert `is`, not just `==`, so future re-duplication fails CI.
- When fixing one drift instance, audit for siblings — they almost always exist.

Alpha4Gate Phase 4.5: 4 instances in one debugging session, all four invisible to 682 unit tests, all four caught by a 4-minute smoke test.

## Audit wire shape when storage representation changes

When a PR changes how data is persisted (lazy insert, normalization, pagination, new constraint, materialized view → on-demand), audit every API/WS response that reads from that storage. Particularly load-bearing for endpoints whose response the frontend renders directly without intermediate code (suggestion cards, dashboards, summaries).

Treat test diffs that adjust response-shape assertions as suspect — the dev who shipped the storage change typically updates failing tests to match the new behavior and codifies a regression that only a real client catches.

Toybox G2 silently narrowed a propose response from 5 steps to 1. The G2 dev agent updated 6 integration tests to assert `len(steps) == 1`. Four reviewers approved the codifying test diff. Caught only when the operator hit the parent UI.

## New components require an integration test through the production caller

When a `/build-step` adds a module that must be invoked from existing production code, the developer prompt must require an integration test that exercises the production entry point and asserts the new component is reached end-to-end. Unit tests of the new module alone leave silent-wiring failures invisible.

Skip this requirement only for pure utilities with no callers in the same step (e.g. a helper for future code) or schema-only changes.

Toybox step 15 iter 1: dev agent built `schedule_judge_sample` and unit-tested it directly, never invoked it from `_do_propose`. Production effect would have been zero judge calls forever (silent failure mode). Caught by reviewers, cost one iteration.

## Build-phase halt contract

The 5 conditions under which `/build-phase` is permitted to halt mid-run. Anything else is a defect — surface it as a finding, not a halt.

1. **Conditional-step predicate errored or returned non-binary.** A `Type: conditional` step's `Condition:` shell expression exited with a code that build-phase cannot interpret as run-or-skip. Includes: command-not-found (≥126), syntax error, signal-terminated (≥128), or a predicate that intentionally outputs but never exits. The step's defect is in the predicate itself, not the autonomy contract.

2. **Quality-gate hard fail.** typecheck error count > 0, test count regressed below baseline, lint produced a blocker-class finding. The step's developer-agent claims to have implemented but the project's truth-check disagrees. Surfacing this halts the orchestrator before merge.

3. **Stop-and-audit triggered.** `/build-step`'s stop-and-audit rule (third instance of the same bug-shape in this session). Whack-a-mole is wasting time; STOP iterating and audit the codebase for siblings before continuing.

4. **Wait-type step reached.** A step explicitly declared `Type: wait` is long-running observation work (a soak test, a benchmark run). The orchestrator halts intentionally so wall-clock waiting doesn't consume context window. Resume in a fresh session via `--resume <next-step>` after the wait completes.

5. **Worktree merge conflict.** A surgical-edit conflict from earlier steps overlapping the current step's files. Requires human resolution before continuing.

**Defect-of-input class** (Step 0 pre-flight Blockers — surfaced before any step execution, so technically not "mid-run"):
- Plan has `Type: conditional` step without `**Condition:**` field (caught upstream by `/plan-review` §23 / `/plan-wrap` §12)
- Plan has `Type: operator` step with code-shaped `Produces:` but plan-review/plan-wrap autofix didn't run (caught at Step 0 sub-bullet 7's runtime safety-net)

Any other mid-run halt is a defect to be caught upstream. Examples of defects (NOT in the allowlist):
- Mid-run `(y/n)` confirmation prompts (covered by [`plan-and-issue-flow.md` § "Autonomous-by-default skills"](plan-and-issue-flow.md))
- "Should I continue?" gates
- Operator-step halts for code-Produces steps (Step 3 of BPA plan auto-splits these)
- Pure-observation operator-step halts mid-phase (Step 4 of BPA plan defers these to phase-end Manual UAT bundle)
- repo-sync apply-changes confirmation (Step 6 of BPA plan strips this)

This contract is enforced by /build-phase's prose and by the autofix modes in /plan-review and /plan-wrap. Operators running /plan-expedite before /build-phase get the autofix benefits automatically.

## Source memories

- `feedback_grep_all_downstream_when_fixing_key_shape`
- `feedback_duplicate_shape_constants`
- `feedback_audit_wire_shape_on_storage_change`
- `feedback_buildstep_require_integration_test`
