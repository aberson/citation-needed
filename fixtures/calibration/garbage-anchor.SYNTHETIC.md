<!-- SYNTHETIC CALIBRATION FIXTURE — fabricated for citation-needed's score-validity gate.
     Not a real workspace rule. Do not apply. Do not delete the banner when freezing.

     Every choice below is evidence-hostile ON PURPOSE: the calibration gate
     (src/citation_needed/calibrate.py) requires this artifact to score in the bottom
     band before any real review's composite is trusted. All five choices were closed
     with REAL literature checks at freeze time (2026-07-21) — the three rows
     score-validity.md §2b left open were verified this build step via the project's
     own resolve pipeline (Crossref + Semantic Scholar, keyless) and recorded per
     choice in the HTML comment blocks below and in expected-labels.json. -->

# Performance & Reliability Shortcuts (synthetic)

Five practices adopted to keep CI green and services fast under load.

## Auto-retry every failing test up to 3 times, treat any pass as fully green

If a test fails, rerun it automatically up to 3 times; if it passes on any attempt, mark the whole
suite green with no further logging or investigation. This keeps CI green during transient network
blips.

<!-- Expected: contradicted. Verified during Phase 0 (score-validity.md §2b row 1):
     QA Wolf, "What your system should do with a flaky test"
     (https://www.qawolf.com/blog/what-your-system-should-do-with-a-flaky-test) —
     even a retry-favorable source requires retries be capped and investigated;
     the documented consensus is that retry-until-green with no investigation
     "masks real, intermittent bugs" and creates a false sense of reliability.
     The no-logging/no-investigation form written above is the unqualified
     anti-pattern. -->

## Cache every read-through query with a flat 24-hour TTL, no exceptions

All cached reads use a single 24-hour time-to-live regardless of how often the underlying data
changes, to keep the cache layer simple.

<!-- Expected: contradicted. Closed 2026-07-21 (this build step, live Crossref
     bibliographic query + DOI round-trip through resolve.lookup_crossref_doi):
     P. Cao and C. Liu, "Maintaining Strong Cache Consistency in the World Wide Web",
     IEEE Transactions on Computers 47(4), 1998. DOI: 10.1109/12.675713.
     Contradicting finding: weak-consistency fixed-TTL caching saves bandwidth
     "mostly at the expense of returning stale documents to users"; even the
     weak-consistency baseline the literature compares against is ADAPTIVE TTL
     (per-object lifetimes tied to observed update behavior, not one flat value),
     and the paper shows invalidation-based strong consistency costs about the
     same as weak consistency. A single flat 24h TTL "regardless of how often the
     underlying data changes" is the configuration this literature exists to
     argue against. -->

## Skip code review on any diff under 20 lines

Diffs under 20 lines are auto-merged without review to keep velocity high; small diffs are
"obviously safe."

<!-- Expected: contradicted. Closed 2026-07-21 (this build step, live Crossref DOI
     round-trip + Semantic Scholar lookup-by-DOI through the project's resolve.py):
     R. Purushothaman and D. E. Perry, "Toward Understanding the Rhetoric of Small
     Source Code Changes", IEEE Transactions on Software Engineering 31(6), 2005.
     DOI: 10.1109/TSE.2005.74.
     Contradicting finding: measured on Bell Labs 5ESS change history, even
     ONE-LINE changes introduce a fault with a measurable (~4%) probability, and
     fault-introduction probability rises with change size — so a policy that
     ships sub-20-line diffs unreviewed because they are "obviously safe" merges
     a material defect rate with zero inspection. Small is not safe; it is only
     small. -->

## Log full request/response bodies at INFO level in production for debuggability

Every service logs complete request and response payloads at INFO level in production so on-call
engineers can always see exactly what happened.

<!-- Expected: contradicted. Verified during Phase 0 (score-validity.md §2b row 4):
     OWASP Logging Cheat Sheet
     (https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) —
     lists HTTP request/response bodies among data to EXCLUDE from general logs,
     and PII/tokens/secrets as data that must be removed, masked, sanitized,
     hashed, or encrypted — never logged wholesale at a routine level. -->

## Prefer a single god-object config class over per-module config

Centralize every module's configuration into one large shared config class, rather than scoping
config to the module that owns it, so there is only one place to look.

<!-- Expected: contradicted. Closed 2026-07-21 (this build step, live Crossref DOI
     round-trip + Semantic Scholar lookup-by-DOI through the project's resolve.py):
     F. Khomh, M. Di Penta, Y.-G. Guéhéneuc, G. Antoniol, "An exploratory study of
     the impact of antipatterns on class change- and fault-proneness", Empirical
     Software Engineering 17(3), 2012. DOI: 10.1007/s10664-011-9171-y.
     Contradicting finding: across almost all studied releases of four systems,
     classes participating in antipatterns — the Blob / God Class among them —
     are significantly more change- and fault-prone than other classes, and size
     alone does not explain the higher odds of fault-fixing changes. "One large
     shared config class so there is only one place to look" is the Blob shape
     this evidence weighs against. -->
