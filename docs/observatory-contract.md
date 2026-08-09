# Citation Needed observatory handoff contract

## Terminal update selector

`cite update-select` is a terminal-only convenience handoff. It reads active
scanned `skill` artifacts and their persisted review state, then prints one exact
user-invocable command:

```text
/citation-review <workspace-relative-skill-path>
```

or:

```text
/citation-distill <workspace-relative-skill-path>
```

It never invokes either skill, calls a model, runs a review, creates a queue row,
or writes a source file. A blank, `q`, `quit`, or `cancel` selection is a successful
no-op. `--artifact-id <id>` selects one active scanned skill directly for a scripted
terminal handoff.

The command choice is mechanical and visible:

| Persisted state | Handoff |
| --- | --- |
| No completed review, no current content hash, or current hash differs from the completed review | `/citation-review <path>` |
| Current content hash matches the latest completed review and that review contains a `needs-improvement` score | `/citation-distill <path>` |
| Current content hash matches and the review has no `needs-improvement` score | `/citation-review <path>` |

Interactive output is capped at 50 displayed candidates; use `--artifact-id` for a
known active skill beyond that display. Paths are validated as workspace-relative
before a handoff is printed. This terminal contract is separate from the bounded
read-only `x-justify` artifact planned for the observatory exporter.

## Read-only producer artifacts

`cite observatory-export` writes a paired, atomic `observatory.v1` producer contract
to its output directory (default: `observatory/`):

| File | Generic Dev Observatory view kind | Contents |
| --- | --- | --- |
| `citation-overview.v1.json` | `summary` | Readiness state, real table counts, reviewed-skill export totals, and bounded recent review activity. |
| `citation-justifications.v1.json` | `explorer` | Reviewed-skill list entries, each with a bounded detail containing frozen review provenance, claims, locator state, documented search result, and verified citation records. |

Both envelopes contain exactly `schema: "observatory.v1"` and a UTC
`generated_at` timestamp. The summary uses only scalar stats plus route-safe recent
rows; the explorer uses route-safe `artifact-<id>` identifiers. A missing or
uninitialized Citation Needed database makes export fail visibly rather than creating
an apparently-empty producer. An initialized empty database produces actual zeroes
and `items: []`.

The producer keeps output bounded: at most 100 reviewed skills, 20 choices per skill,
10 citations per choice, 8,000 characters per detailed text field, and 512 KiB per
artifact. Each omitted tail has an explicit `*_total`/`*_exported` count (including
`recent_activity_total` / `recent_activity_exported` in the summary) and each
shortened text value is an object with `text` and `truncated`; no truncation is
presented as the original whole record. Export writes only its two JSON files and
never writes the citation database, source artifacts, or Dev Observatory registry.
