---
name: feedback_tee_from_start_for_evidence
metadata:
  node_type: memory
  type: feedback
---

# Tee from start for evidence capture

Frozen §4.1 acceptance fixture: a SINGLE-decision memory (the common case). Extraction
must yield exactly ONE choice — see fixtures/extraction/expected.json.

Long-running commands whose output will be needed as evidence must be piped through
`2>&1 | tee <log>` FROM LAUNCH — output that was never captured cannot be
reconstructed after the fact.

Incident: a soak run's failure signature was lost because the launch command wrote
only to the console; the re-run cost a full day. This incident narrative is evidence
attached to the one decision above — per the over-split guard it is NOT a separate
choice, because no plausible verdict could move it independently of its parent.
