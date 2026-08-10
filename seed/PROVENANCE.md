# Seed corpus provenance — `seed/seed_citations.jsonl`

This directory holds the tracked, redistributable seed corpus: one JSON object per line,
sorted by `natural_key` (normalized DOI) so the file diffs cleanly. `cite seed import`
loads it into `data/citation.db` through the anti-fabrication gate
(`verify.insert_citation`, the sole writer to the `citations` table), idempotently
(dedup on `natural_key`; a second import reports zero new rows).

## How the rows were derived (and the import trust boundary)

- **Selection source:** `docs/research/choice-taxonomy-literature.md` (this repo) — the
  Phase-0 investigation. ONLY entries carrying its `verified` label (title/authors/venue
  independently confirmed against the paper's own page during Phase 0) were candidates.
  Entries labeled `UNVERIFIED-CONTENT`, `VERIFIED-EXISTENCE only`, or `No literature
  found` are not seed material.
- **Live re-derivation at seed-BUILD time (2026-07-22):** every shipped row's
  bibliographic record was re-derived live through the production Crossref clients in
  `src/citation_needed/resolve.py` (`lookup_crossref_doi` for DOI'd entries,
  `search_crossref` bibliographic title search otherwise — the same code path production
  resolution uses, never a hand-rolled sibling). A candidate was included only when
  Crossref returned the work's own registered record with an exact normalized-title
  match and the expected author present. Requests carried Crossref's polite-pool
  `mailto` parameter (`CITATION_NEEDED_CROSSREF_MAILTO` override, else `resolve.py`'s
  documented placeholder) and were paced by the live-header throttle.
- **Stored fields:** `title`, `year`, `venue` (Crossref `container-title`), `authors`,
  and DOI come verbatim from Crossref's API response captured at build time. `category`
  is this project's own taxonomy label (from the selection source above, not from any
  API). `retrieved_at` is the UTC timestamp of the build pass's live API calls.
- **Trust boundary:** `cite seed import` is OFFLINE. It validates the file's shape
  (whole-file reject on any invalid row) and then trusts the tracked, reviewed-committed
  contents — the `api_structured` echo stored on each imported citation is the seed
  row's own recorded fields. Verification happened at seed-build time, not import time;
  an already-present row is skipped untouched (no `verified_at` refresh, because the
  import re-verified nothing).

## Sources and license basis

| Source | Rows this build | License basis |
|---|---|---|
| **Crossref REST API** (bibliographic metadata) | 2 | Crossref releases its bibliographic metadata (titles, authors, DOIs, venues, dates) as **public domain / CC0** — freely reusable and redistributable. **Abstracts are excluded from that grant** (they remain under publisher/author copyright), so this corpus stores **no abstracts** — there is no abstract field in the row schema at all. Polite-pool `mailto` used on every request. Source: Crossref "REST API metadata license information" documentation, fetched directly during Phase 0 (`docs/research/public-boundary.md` §b). |
| **OpenAlex** | 0 | **CC0** ("in the public domain, and free to use in any way you like"; attribution appreciated, not required). OpenAlex is the sanctioned second seed source, but its API has required a (free) key since 2026-02-13 and `CITATION_NEEDED_OPENALEX_KEY` was UNSET at seed-build time, so **zero rows this build** used it. Any future row derived from it will carry `source_api: "openalex"`. Source: `openalex-docs/license.md`, fetched directly during Phase 0 (`public-boundary.md` §b). |
| **Semantic Scholar** | 0 — **excluded by policy** | S2's Dataset License Agreement grants rights for non-commercial research/educational use only and **prohibits sublicense/redistribution** — incompatible with shipping rows in an MIT-licensed public repo. S2 is therefore **live-lookup only** in this project (plan D8): zero S2-derived rows ship in the seed, and the importer structurally rejects any row with an S2 `source_api` (`seed.ALLOWED_SOURCE_APIS`). Source: the S2 Dataset License Agreement, fetched directly during Phase 0 (`public-boundary.md` §b). |
| **`docs/research/choice-taxonomy-literature.md`** (selection source) | n/a | This repo's own Phase-0 investigation doc (MIT-repo content). It selected the candidates and supplies each row's `category` label; no bibliographic field is taken from it — those were re-derived live as described above. |

## Relationship to the code license (plan D10)

The repository's `LICENSE` (MIT) governs the **code**. The seed corpus **data rows** are
not code: they ship under the per-source terms documented in this file — all sources are
CC0/public-domain bibliographic metadata, so a downstream consumer may reuse the rows
without the MIT attribution requirement. This file is the single place those data terms
live, so nobody has to guess whether MIT or CC0 governs a data row.

## Per-row exclusions (candidates that could NOT be re-derived this build)

All six exclusions share one mechanism: the work has no Crossref-registered record of its
own (arXiv DOIs are DataCite-registered and invisible to Crossref — `lookup_crossref_doi`
returned HTTP 404 for each; bibliographic title search found no exact-title+author match),
and the OpenAlex fallback was unavailable keyless (`CITATION_NEEDED_OPENALEX_KEY` unset at
seed-build time). Excluded rather than shipping a row the build could not re-verify.

| Candidate (taxonomy category) | Reason excluded |
|---|---|
| Schulhoff et al., *The Prompt Report: A Systematic Survey of Prompt Engineering Techniques* (prompt-phrasing-framing-tactics) | arXiv-only (DataCite DOI `10.48550/arXiv.2406.06608`, Crossref 404); no Crossref-registered published-venue record found by title search. |
| Wang et al., *Self-Consistency Improves Chain of Thought Reasoning in Language Models* (fanout-vs-solo-reviewer-diversity) | ICLR 2023 publishes via OpenReview, which does not register Crossref DOIs; arXiv DOI `10.48550/arXiv.2203.11171` is DataCite (Crossref 404); no match by title search. |
| Gao et al., *Retrieval-Augmented Generation for Large Language Models: A Survey* (memory-schemas-retrieval-design) | arXiv-only (DataCite DOI `10.48550/arXiv.2312.10997`, Crossref 404); no Crossref record by title search. |
| Xu et al., *Benchmark Data Contamination of Large Language Models: A Survey* (measurement-validity-benchmark-calibration) | arXiv-only (DataCite DOI `10.48550/arXiv.2406.04244`, Crossref 404); no Crossref record by title search. |
| Liu et al., *Prompt Injection attack against LLM-integrated Applications* (security-prompt-injection) | arXiv-only (DataCite DOI `10.48550/arXiv.2306.05499`, Crossref 404); no Crossref record by title search. |
| Carroll, *The Nurnberg Funnel: Designing Minimalist Instruction for Practical Computer Skill*, MIT Press 1990 (documentation-minimalism-knowledge-placement) | The 1990 book itself is not Crossref-registered. Crossref's exact-title hits are OTHER works: a third-party book review (`10.1016/0020-7373(92)90046-n`, authored by Benyon) and chapters of the 1998 sequel *Minimalism Beyond the Nurnberg Funnel* (`10.7551/mitpress/4616.003.*`) — shipping any of them would cite the wrong work. |

**Recovering the exclusions:** all six are expected to be resolvable through OpenAlex
(CC0, seed-safe). Set `CITATION_NEEDED_OPENALEX_KEY`, re-run the seed-build re-derivation
through the production `resolve.py` clients, and append the recovered rows (keeping the
file sorted by `natural_key`); their `source_api` will be `"openalex"`.

## Included rows this build (2)

| `natural_key` | Work | Derivation evidence |
|---|---|---|
| `10.1162/tacl_a_00638` | Liu et al., *Lost in the Middle: How Language Models Use Long Contexts*, TACL 2024 | The arXiv DataCite DOI (`10.48550/arXiv.2307.03172`) is invisible to Crossref (404); the published TACL venue record resolved via `lookup_crossref_doi("10.1162/tacl_a_00638")` with exact title + all 7 authors. |
| `10.18653/v1/2025.ijcnlp-long.18` | Shi et al., *Judging the Judges: A Systematic Study of Position Bias in LLM-as-a-Judge*, AACL-IJCNLP 2025 | arXiv DataCite DOI (`10.48550/arXiv.2406.07791`) Crossref 404; the ACL Anthology proceedings record resolved via `search_crossref` title search with exact title + all 6 authors. |
