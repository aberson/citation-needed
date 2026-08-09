# Seed corpus provenance and redistribution boundary

`seed_citations.jsonl` is a small bibliographic bootstrap for Citation Needed's local FTS corpus.
It is not a collection of papers, abstracts, supporting quotations, or review outcomes. Each row is
strict JSONL and names its provider, provider record, and the exact API URL used to retrieve the
bibliographic metadata.

## Permitted sources

| Provider | Seed rows | License basis | Stored fields |
| --- | --- | --- | --- |
| Crossref | CheckList, SQuAD, MLSUM | Crossref releases its generated bibliographic metadata under CC0/public-domain terms. | DOI, title, authors, year, venue, search keywords, provider locator. |
| OpenAlex | Lost in the Middle | OpenAlex data is available under CC0. | DOI, title, authors, year, venue, search keywords, provider locator. |

The source and license analysis is preserved in
[`docs/research/public-boundary.md`](../docs/research/public-boundary.md). The Crossref rows were
re-checked through the listed Crossref Works endpoints; the OpenAlex row was re-checked through its
listed Works filter endpoint. The importer stores a compact structured provenance record for each
row through the normal citation writer.

## Explicit exclusions

- No Semantic Scholar-derived field is present. Semantic Scholar remains a live lookup source only;
  its dataset license does not provide the clean redistribution grant needed for this tracked seed.
- No Crossref abstract is present. Crossref's CC0 grant does not cover publisher-provided abstracts.
- No full text, supporting quote, citation classification, private workspace path, or model result
  is present. Those belong to review-time verification and the gitignored local database.

The seed records support corpus-first discovery only. A later review must still obtain and verify
its own evidence before a citation can justify an artifact choice.
