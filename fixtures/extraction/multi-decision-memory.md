---
name: user_model_preference
metadata:
  node_type: memory
  type: user
---

# Model preference — Opus default, Fable at seed points

Frozen §4.1 acceptance fixture: a COMPOSITE memory carrying THREE independently
falsifiable decisions (modeled on the real user_model_preference memory). Extraction
must yield one choice per decision — see fixtures/extraction/expected.json.

1. **Diversity beats a stronger single model for committee roles.** Opus orchestrating
   plus Sonnet fan-out is quality-optimal for nearly every skill — reviewer and
   iteration diversity beats a stronger single model, so running the top-tier model
   more widely adds cost, not quality.

2. **Escalate to the top-tier model only at single-seed-artifact points.** A session
   moves to Fable 5 only where one seed artifact bounds the whole pipeline's quality
   (greenfield plan authoring, hard root-cause diagnosis, cited-report synthesis) — a
   diversity committee cannot substitute for a stronger reasoner there.

3. **Re-pin the model after auto-updates.** Auto-updates can silently reset the model
   picker to the tier default, so any session found off the pinned model re-pins the
   settings line before continuing.

Why: the three claims carry different evidential standing — the diversity claim is
benchmarkable, the seed-point rule is a cost/quality tradeoff, and the re-pin rule is
an observed-incident guard. This elaboration paragraph attaches to its parents as
provenance, never as a choice of its own (the over-split guard).
