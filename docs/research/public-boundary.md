# citation-needed — Open-source boundary + license

Research pass for the citation-needed planning effort. Read-only investigation; workspace was
not modified except for this file. Workspace root: `c:/Users/abero/dev`. Date: 2026-07-21.

## TL;DR / Verdict

- **Public repo:** code, skill(s), DB **schema** (migrations/DDL), docs, and the calibration
  garbage-anchor fixture (synthetic, safe by construction). **Gitignored from commit 0:** the live
  SQLite DB (`data/*.db`) and any breakdown/report doc that names a private artifact path — mirror
  `x-marks-the-spot`'s `data/` pattern (rebuildable, never tracked).
- **Seed corpus:** a Crossref- and/or OpenAlex-sourced bibliographic seed corpus (DOI, title,
  authors, venue, year) is **shippable under CC0** — both confirmed CC0/public-domain for metadata.
  **Do not** bulk-ship Semantic Scholar API/dataset fields — their Dataset License Agreement
  restricts use to non-commercial research/education and forbids redistribution/sublicensing,
  which conflicts with an MIT public repo's permissive grant.
- **License:** **MIT**, matching the one existing OSS precedent in this workspace
  (`claude-skills/LICENSE`, "Copyright (c) 2026 Abraham Robison"). The other three public repos
  checked (measure-twice, x-marks-the-spot, aberson.github.io) ship **no LICENSE file** — GitHub
  reports `licenseInfo: null` for all three, i.e. all-rights-reserved despite being public. MIT is
  the only in-workspace precedent to follow, and matches redistributing a CC0 seed corpus cleanly.
- **Calibration fixture:** ship it. It is synthetic by design (no real workspace content, no
  private paths) — confirmed by direct inspection of an existing example.
- **Scrubbing lesson applied:** treat the DB (and anything derived from it) the way the workspace
  already treats sensitive-data flips — exclusion from day one, not history-rewrite after the
  fact. `git filter-repo` + force-push is necessary-but-not-sufficient once something is tracked
  (dangling commits stay reachable by SHA; issue/comment edits retain pre-edit text) — the fix is
  to never track it.

---

## (a) Public/private boundary

### What ships in the public repo

| Category | Ships? | Rationale |
|---|---|---|
| Source code (`src/`, CLI, review engine) | Yes | No private content by construction. |
| DB **schema** (migrations, DDL, `CREATE TABLE` statements) | Yes | Structure, not data. |
| Skill(s) that drive a review (`SKILL.md` + scripts) | Yes | Mirrors the existing `claude-skills` mirror convention. |
| Docs (README, this research doc, design docs) | Yes | Write with the same hand-scrub discipline as `claude-skills`'s per-file merge (see below). |
| Calibration garbage-anchor fixture (synthetic bad rule file) | Yes | See §(d). |
| A CC0-sourced external-citation seed corpus (bibliographic only) | Yes, conditionally | See §(b). |
| The operator's live SQLite DB (`data/*.db` or similar) | **No — gitignored from creation** | Accumulates reviews of PRIVATE workspace artifacts + internal-provenance citations pointing at private paths (the exact risk the task names). |
| Breakdown docs / review output that names a private artifact path | **No — gitignored, or hand-scrub before commit** | A review of `c:/Users/abero/dev/<private-project>/...` embeds the private path and possibly excerpted private content in its "choices extracted" section. |
| Any per-review JSON/markdown export written by a review run | **No, by default** | Same reasoning — a review's output is workspace-specific unless explicitly hand-scrubbed for a public example. |

### Design pattern: mirror `x-marks-the-spot`'s rebuildable-derived-data split

`c:/Users/abero/dev/x-marks-the-spot/.gitignore`:
```
# Derived artifacts — the DB and site are always rebuildable from seeds/
data/
site/
```
This is the closest structural precedent in the workspace: a tracked `seeds/` (small, safe,
versioned inputs) feeds a gitignored `data/` (the live SQLite DB + generated site), and the DB is
"always rebuildable" from the tracked seed. citation-needed should adopt the identical shape:

- `seed/` or `corpus/` (tracked) — the CC0 external-citation seed corpus (bibliographic metadata
  + relevance notes, no private content) + DB schema/migrations.
- `data/` (gitignored from commit 0) — the live SQLite DB that accumulates review output,
  vetted citations pointing at private internal paths, etc. Rebuildable: schema + seed corpus
  re-applied gives a fresh, empty-of-private-history DB.

Also note the already-established sibling precedent: `c:/Users/abero/dev/claude-skills/.gitignore`
line 7 already has `*.db` as a gitignore pattern (that mirror repo doesn't currently ship a DB, but
the pattern is pre-declared) — one more workspace data point that `*.db` under a public repo is
the established gitignore convention, not a new invention for this project.

### Scrubbing-lesson application (why exclude-from-day-0, not scrub-later)

Two workspace memories directly govern this:

- `C:/Users/abero/.claude/projects/c--Users-abero-dev/memory/feedback_github_history_rewrite_insufficient.md`
  — verified on toybox 2026-06-30: a `git filter-repo` history rewrite + force-push is
  **necessary but not sufficient** to scrub sensitive data before a public flip. Two concrete
  leak paths survive a rewrite: (1) pre-rewrite commits stay reachable by full/short SHA via
  `gh api repos/O/R/commits/<oldsha>` until GitHub's own (non-user-triggerable) GC runs, and any
  doc/README that published an old SHA becomes a live link to the pre-scrub tree; (2) issue/PR
  comment edit-history retains pre-edit text via the "edited" UI and the GraphQL
  `userContentEdits` field. Neither is fixed by a code-history rewrite. The only clean guarantee
  the memory records is delete + recreate the repo (or delete-issues + GitHub Support GC request).
  **Applied to citation-needed:** don't rely on "we'll scrub the DB before open-sourcing later" —
  design the repo so the DB and private-path-bearing docs are gitignored **from the first commit**,
  so there is never a pre-scrub tree to leak.
- `C:/Users/abero/.claude/projects/c--Users-abero-dev/memory/project_claude_skills_public_mirror.md`
  — the workspace's live precedent for a hand-scrubbed public subset (`claude-skills/`, remote
  `github.com/aberson/claude-skills`). Two applicable conventions: (1) it **strips every `evals/`
  dir** specifically because eval fixtures tend to carry workspace-specific scenarios/absolute
  paths — i.e. even test/fixture content needs a scrub pass, not just "code vs data"; (2)
  refreshing shipped content is "a per-file MERGE, not a copy" with a stated EXCLUDE bar: *"a
  skill's CORE FUNCTION needs an unshipped private substrate."* **Applied to citation-needed:**
  the review *engine* and *schema* are the core function and carry no private substrate — they
  ship. A review's *output* (breakdown docs, populated DB) is exactly the unshipped-private-
  substrate case and should follow the same EXCLUDE-bar logic: never auto-committed, gitignored
  by default, hand-scrubbed only if the operator explicitly wants to publish one example review
  as an illustrative doc (with the private path/content redacted first, mirroring the mirror's
  "genericize workspace-project names" convention).

### Recommended `.gitignore` seed for citation-needed (day 0)

```
data/                  # live SQLite DB + per-run output — always rebuildable from seed/ + schema
*.db
*.db-journal
*.db-wal
breakdowns/            # per-review breakdown docs (private-path-bearing) unless hand-scrubbed
.venv/
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.ruff_cache/
```
(Names are placeholders — align to whatever citation-needed's actual plan.md ultimately calls the
DB/output directories; the principle is what matters: DB + per-review output default-excluded,
schema + code + seed corpus default-included.)

---

## (b) Is a seed corpus of vetted EXTERNAL citations shippable?

Checked redistribution terms for the three bibliographic-metadata sources most relevant to a
citation toolkit, by fetching each provider's own license/terms page (not summarizing from memory).

### Crossref — CC0 for metadata, WITH an abstracts carve-out

- Source: [Crossref — REST API metadata license information](https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-metadata-license-information/)
  and [Crossref — Metadata Retrieval](https://www.crossref.org/documentation/retrieve-metadata/)
  (fetched directly).
- Finding: Crossref's own bibliographic metadata (titles, authors, DOIs, references, and other
  "Crossref-generated data") is released as **public domain / CC0** and is freely reusable and
  redistributable via the REST API or bulk snapshots.
- **Exception:** **abstracts** are excluded from the CC0 grant — they remain under the original
  publisher/author copyright; Crossref only has permission to *display* them, not to grant
  redistribution rights on their behalf.
- **Applied:** a citation-needed seed corpus sourced from Crossref should include DOI/title/
  authors/venue/year (safe, CC0) but must **not** bulk-include abstract text pulled from Crossref
  unless a separate abstract-specific license check is done per source.

### OpenAlex — CC0, no abstract carve-out stated

- Source: [openalex-docs/license.md](https://github.com/ourresearch/openalex-docs/blob/main/license.md)
  (fetched directly, primary source).
- Finding: *"OpenAlex data is made available under the CC0 license. That means it's in the public
  domain, and free to use in any way you like."* This applies to the dataset as OpenAlex
  redistributes it, including bibliographic metadata (titles, authors, and OpenAlex's inverted-
  index abstract representation). Attribution is "appreciated... but not necessary" — not a legal
  requirement.
- One documented exception: the legacy **MAG Format snapshot** carries ODC-BY (inherited from
  Microsoft Academic Graph's original license) — not relevant unless citation-needed specifically
  pulls that legacy format.
- **Applied:** OpenAlex is the cleaner of the two general bibliographic sources for a shippable
  seed corpus — CC0 covers the metadata fields citation-needed would actually store (title,
  authors, venue, year, DOI, and even OpenAlex's own abstract field) without Crossref's abstract
  carve-out.

### Semantic Scholar — NOT a clean CC0/ODC-BY story; primary source is more restrictive than general web claims

- Source: [Semantic Scholar Dataset License Agreement](https://api.semanticscholar.org/license/)
  (fetched directly — this contradicts secondary summaries that describe S2 data as blanket
  ODC-BY).
- The actual agreement text found: rights granted are "solely for use by Authorized Users... 
  limited to training and evaluating machine learning models and transforming data sets for
  **legitimate, non-commercial, research and/or educational purposes**." It further requires (1)
  any public use of the Data to link back to semanticscholar.org with a `utm_source=api` param,
  (2) inclusion of the Semantic Scholar name/logo on public displays of the data, and (3) scientific
  credit to non-publishing-party contributors. It explicitly **prohibits** commercial use without
  an expanded license and prohibits sale/lease/sublicense/transfer, and forbids sharing with data
  brokers or ad networks.
- **This is a real conflict for citation-needed:** the project is "a new open-source project" —
  presumably under a permissive OSI license (MIT, see §(c)) that grants commercial use and
  redistribution to anyone. Bulk-shipping Semantic Scholar-sourced metadata rows in a seed corpus
  under an MIT-licensed public repo would contradict the Dataset License Agreement's non-
  commercial-only and no-sublicense terms.
- **Applied:** do **not** include Semantic Scholar bulk API/dataset output in the shippable seed
  corpus. Semantic Scholar can still be used **live**, at review time, as a search/gap-fill source
  the way the task spec describes ("live web search for gaps") — an individual, on-demand lookup
  of a paper's existence/DOI is a materially different use than bulk-redistributing their dataset
  fields in a public git repo. If citation-needed later wants Semantic Scholar rows to persist to
  the *local* (private, gitignored) SQLite DB for the operator's own compounding corpus, that's a
  separate, lower-risk case than shipping the same rows in the public repo's tracked seed corpus —
  but even that should re-derive the safe fields (DOI, title) rather than storing verbatim
  Semantic-Scholar-proprietary payload, given the "no sublicense/transfer" language.

### Net recommendation for (b)

A **shippable seed corpus is valuable and legally clean if sourced from Crossref (metadata fields
only, no abstracts) and/or OpenAlex (CC0, including abstracts)**. Semantic Scholar should be
treated as a live-lookup-only source, excluded from anything committed to the public repo's
tracked seed corpus.

---

## (c) License choice

### Workspace precedent search

Checked for `LICENSE` files at the root of every public repo named in the task, plus the
already-known `claude-skills` public mirror:

| Repo | LICENSE file? | `gh repo view --json licenseInfo` | Visibility |
|---|---|---|---|
| `c:/Users/abero/dev/measure-twice` | No | `licenseInfo: null` | PUBLIC |
| `c:/Users/abero/dev/x-marks-the-spot` | No | `licenseInfo: null` | PUBLIC |
| `c:/Users/abero/dev/aberson.github.io` | No | `licenseInfo: null` | PUBLIC |
| `c:/Users/abero/dev/claude-skills` | **Yes** — `claude-skills/LICENSE` | `{"key":"mit","name":"MIT License"}` | PUBLIC |

(Root-level check only — a naive recursive glob for `LICENSE*` in each of the three no-license
repos surfaces only vendored third-party `LICENSE` files under `.venv/`/`node_modules/`, which are
those dependencies' own licenses, not the project's own; confirmed via direct `ls` at repo root
that no project-level LICENSE exists in any of the three.)

`c:/Users/abero/dev/claude-skills/LICENSE` (read in full):
```
MIT License

Copyright (c) 2026 Abraham Robison
...
```

**Finding:** three of the four public repos checked are technically all-rights-reserved (public
source, no redistribution grant) despite being open on GitHub — likely an oversight rather than a
deliberate choice, since none discuss licensing in their plan docs or READMEs (`grep -i licen`
across each repo's `README.md`/`pyproject.toml`/`package.json` returned nothing). The **one**
actual license decision on record in this workspace is `claude-skills`'s MIT choice.

### Recommendation: MIT

- Matches the sole in-workspace OSS-license precedent (`claude-skills`), keeping license posture
  consistent across the operator's public repos rather than introducing a second license family.
- MIT is compatible with re-shipping CC0-sourced content (§(b)) — CC0 has no restrictions to
  conflict with, and MIT's permissive commercial/redistribution grant doesn't conflict with
  Crossref's or OpenAlex's public-domain dedication.
- MIT is the de facto standard for small permissively-licensed dev tools/CLIs of citation-needed's
  shape (thin Python+uv+SQLite toolkit), lowering friction for anyone who wants to fork/embed it.
- Suggested file: `citation-needed/LICENSE`, MIT, "Copyright (c) 2026 Abraham Robison" (same
  copyright line as `claude-skills/LICENSE`, for consistency).
- Note the DB/corpus data is **not** code — the code LICENSE (MIT) governs the toolkit; the shipped
  seed corpus should separately and explicitly document its own CC0 provenance per source (a
  `seed/PROVENANCE.md` or similar naming exactly which fields came from which CC0-licensed
  provider), so a downstream consumer of the seed corpus doesn't have to guess whether MIT or CC0
  governs the data rows.

---

## (d) Is the calibration garbage-anchor fixture safe to ship publicly?

**Yes — explicitly confirmed by direct inspection, not just by design intent.**

Read `c:/Users/abero/dev/.claude/skills/plan-wrap/evals/golden/bad_completion_gate.md` in full as
a concrete example of the fixture shape this task describes (a synthetic "bad" calibration
document used to gate an eval/judge). Its content is:

- A fabricated schema name (`WidgetEntry`), a fabricated field ("cached value shape... not
  defined"), and a fabricated scenario ("LRU cache keyed by widget_id") — none of which correspond
  to any real workspace project, private path, or real data.
- No absolute paths, no real project names, no private content of any kind.
- Structurally, it's exactly the shape `measurement-validity.md`'s "calibrate with anchors before
  comparing candidates" rule calls for
  (`c:/Users/abero/dev/.claude/rules/measurement-validity.md`, § Calibrate with anchors before
  comparing candidates): *"feed it a frozen known-good and a known-garbage input and assert
  `score(good) > score(garbage)`."* citation-needed's garbage-anchor fixture (a synthetic bad rule
  file with obviously-unsupported or contradicted "choices") is the citation-needed-specific
  instance of this exact same discipline.

One nuance worth flagging: the `claude-skills` public mirror's **general convention** is to strip
`evals/` directories wholesale when publishing a skill
(`project_claude_skills_public_mirror.md`, lines 14-16: *"strips every `evals/` dir — so eval-only
leaks (toybox scenarios, absolute paths in `evals/golden/*.md`) never reach the public repo
automatically"*). That convention exists because **some** golden fixtures in this workspace *do*
reference real project scenarios/paths (the memory calls out "toybox scenarios" specifically) —
it is a blanket strip for safety, not a claim that every fixture is unsafe. citation-needed's
garbage-anchor fixture is a different case: it is **purpose-built synthetic content with no
external reference to scrub**, and per `measurement-validity.md` it is core, load-bearing
documentation of how the tool calibrates itself — a downstream user/contributor needs to see it to
trust the tool's scoring. **Recommendation:** ship it (and any other purely-synthetic calibration
fixtures) in the public repo; apply the blanket-strip convention only if/when citation-needed later
accumulates real-workspace-derived golden fixtures (which would need the same case-by-case scrub
`claude-skills` applies, not a blanket ship).

---

## Sources

**Workspace (read this run):**
- `C:/Users/abero/.claude/projects/c--Users-abero-dev/memory/feedback_github_history_rewrite_insufficient.md`
- `C:/Users/abero/.claude/projects/c--Users-abero-dev/memory/project_claude_skills_public_mirror.md`
- `c:/Users/abero/dev/claude-skills/LICENSE`
- `c:/Users/abero/dev/claude-skills/.gitignore`
- `c:/Users/abero/dev/x-marks-the-spot/.gitignore`
- `c:/Users/abero/dev/measure-twice/.gitignore`
- `c:/Users/abero/dev/measure-twice/`, `x-marks-the-spot/`, `aberson.github.io/` root listings (no LICENSE present)
- `c:/Users/abero/dev/docs/skill-plans/claude-skills-publish-plan.md:16,49`
- `c:/Users/abero/dev/.claude/skills/plan-wrap/evals/golden/bad_completion_gate.md`
- `c:/Users/abero/dev/.claude/rules/measurement-validity.md` (§ Calibrate with anchors before comparing candidates)
- `gh repo view aberson/{measure-twice,x-marks-the-spot,aberson.github.io,claude-skills} --json licenseInfo,visibility` (live GitHub API check, this run)

**External (fetched this run):**
- [Crossref — REST API metadata license information](https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-metadata-license-information/)
- [Crossref — Metadata Retrieval](https://www.crossref.org/documentation/retrieve-metadata/)
- [OpenAlex — license.md (ourresearch/openalex-docs)](https://github.com/ourresearch/openalex-docs/blob/main/license.md)
- [Semantic Scholar — Dataset License Agreement](https://api.semanticscholar.org/license/)
