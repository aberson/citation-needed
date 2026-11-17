# Merge extraction

#### Plan: citation-needed core
- Vision / goal: verified citation trails for discrete choices in LLM-facing artifacts, read-only toward targets.
- Phase list: 1 schema; 2 discovery; 3 verifier/corpus; 4 reviews; 5 calibration; 6 distill; 7 seed; 8 skills; 9 real review; 10 sweep (DONE); M1 breakdown UAT and M2 triage remain.
- Data contracts: seven SQLite tables, FTS, numbered migrations, semantic choice keys, source hashes, verified citation writer, categorical votes and fixed calibration thresholds, stdin JSON contracts.
- External dependencies: host citation skills; structured citation APIs and optional OpenAlex credential; workspace registry; Toolkit status.
- Kill criteria and rollbacks: failed/stale calibration aborts real review; missing verification inserts nothing; SSRF refusal; corpus is valuable and backed up, never rebuilt as disposable data; no target writes.

#### Plan: justification surfaces
- Vision / goal: readable corpus overview and skill justification list/detail, reusing existing calibrated review skills.
- Phase list: 11 overview DONE; 12 details DONE; 13 selector DONE; 14 exporter code complete but acceptance open; 15 real populated smoke.
- Data contracts: versioned overview/justification DTOs; exact/ambiguous/missing locators; bounded Observatory artifacts, read-only DB export.
- External dependencies: core review/corpus/skills and real reviewed skill; Observatory owns UI/registry; Toolkit status.
- Kill criteria and rollbacks: empty corpus is setup state; no guessed locators, target edits, synthetic-as-real acceptance, duplicate review engine or calibration bypass.


## Spine and subsumption

[PATTERN: same-goal-different-fidelity] Surface queries/rendering reuse core DB/review provenance and existing skills. [PATTERN: implicit-prerequisites] Step 15 needs Step 14 implementation, while Step 14 final acceptance needs Step 15 evidence; clarify that readiness distinction to remove an apparent DONE dependency cycle. Keep all original IDs and both UAT units. No data format or calibration invariant changes.

## Move record

See `moves.json` for pre-move git status, original hashes, archive destinations and affected references. Original plans are preserved byte-for-byte. Historical evidence paths/hashes are not current execution authorization.
