# Remaining-work estimates

Forward remaining effort in hours, including implementation and its checks/review. Operator attention is separate. Elapsed waits, service quotas and hardware availability are not labor. These are model forecasts, not measured history. Held rows remain disabled.

Rows validate as ForecastEstimate. Portable worksheet only: production ForecastConfig is currently restricted to dev-observatory and 100 estimates; no saved forecast was modified.

Plan: [master_plan.md](master_plan.md). SHA-256: `f3753b6ef35ab24762e8399a5a590e18b2dfd6334a56f5b375e8e0ae1a2f33de`.

Rebind after editing the plan; source fingerprints deliberately become stale. The JSON preserves dependencies and evidence locations.

| Step | Worker hours | Attention hours | Confidence | Availability | Basis |
|---|---:|---:|---:|---|---|
| 14 — Observatory artifact export | 0.25–0.75 | 0.1–0.3 | 0.35 | enabled forecast | Already implemented exporter needs real reviewed-skill round-trip and no-DB-write verification; do not estimate reimplementation. |
| 15 — Real populated smoke | 0.4–1.0 | 0.5–1.25 | 0.35 | enabled forecast | Calibrate production path, review one real skill and verify populated status/citations; provider time is unknown. |
| M1 — Operator UAT — read one breakdown against its target | 0.1–0.25 | 0.3–0.75 | 0.35 | enabled forecast | Operator inspects extraction, evidence classes, empty-search record, score interpretation and untouched target. |
| M2 — citation-triage session over the sweep queue | 0.1–0.3 | 0.4–1.0 | 0.35 | enabled forecast | Inspect ranked justifications and persist keep/cut/rewrite choices without target edits. |
