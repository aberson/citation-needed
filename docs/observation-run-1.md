# Observation run 1 — root rules sweep

**Date:** 2026-08-09
**Scope:** all 13 root `.claude/rules/*.md` artifacts
**Reviewer model:** Claude Code `2.1.212`

## Scope and outcome

The sweep completed current review runs #2–#14, one per rule artifact. Every reviewed
target was left byte-unchanged. The current runs contain 14 scored choices: 11
`interesting`, one `needs-improvement`, and two `well-supported` choices. The two
well-supported choices are the subagent-economy review, which reused the existing
verified internal and external citations from the preceding real smoke review.

The one needs-improvement choice is
`.claude/rules/command-presentation.md :: separate-dry-run-block`. Its three isolated
classifier calls returned `unsupported`: corpus queries for `dry run`, `copy paste
command`, and `code block` had no hits, while two live web-search queries returned only
tangential copy/paste/documentation guidance. The review records that absence rather
than claiming the tangential sources prove the policy.

`cite distill generate` ran for every sweep run. It created exactly one open queue row:

| Choice | Composite | Mechanical proposal | Rank | Evidence state |
| --- | ---: | --- | ---: | --- |
| `separate-dry-run-block` | 25.0 | `trim` | 2.25 | Documented no-direct-literature result |

The rank is the configured rule load weight applied to the 25.0 unsupported composite;
the queue is non-empty, ordered, and ready for operator triage. No target edit was made
as part of this observation step.

## Timing and live-call record

| Measure | Observed value |
| --- | --- |
| Database review window, runs #2–#14 | 66 seconds (`15:03:45Z` through `15:04:51Z`) |
| Open/commit batch timer | 11.32 seconds for the 12 newly opened/committed runs; the first opened run was reused after a safe schema refusal |
| Claude CLI classifier calls | 13 calls, all returned; 0 transport failures and 0 `parse-failed` labels |
| Final isolated classifier calls used for new scores | 6 calls: three for the cross-rule majority and three for the dry-run-block absence decision |
| Context-bearing exploratory classifier calls excluded from score assembly | 7 calls; they treated rule text as its own evidence and were retained only as a classifier-boundary finding |
| Web-search calls | 2 successful searches; 0 directly usable citations for the dry-run-block claim |
| Citation resolver/server-side fetch calls newly made in this sweep | 0; the sweep reused the existing verified corpus rather than inventing external evidence |

The 13 Claude CLI process durations summed to 489.7 seconds. The database timing and
process-duration figures are reported separately rather than pretending their sum is one
end-to-end wall-clock timer.

## Corpus and clustering evidence

One corpus-first query was run for each artifact decision. The cumulative curve was:

| Query positions | Cumulative hits | Cumulative hit rate |
| --- | ---: | ---: |
| 1–9 | 0 | 0.00% |
| 10 (`subagent verdict`) | 1 | 10.00% |
| 11–13 | 1 | 7.69% |

The hit is the existing internal token-usage investigation attached to the
subagent-economy review. A manual decision-identity pass plus a token-overlap backstop
(Jaccard threshold 0.50) found zero near-duplicate pairs among the 13 chosen decisions:
0 duplicate decisions removed / 13 candidate decisions = 0.00% cluster reduction.

## Failures and follow-up

There were no unhandled exceptions. One first commit attempt was rejected by the CLI's
schema validator before any write because PowerShell serialized empty arrays incorrectly;
the corrected UTF-8 JSON payload committed the same opened run. This was a safe validation
failure, not a successful review or hidden retry.

The important observation is classifier-boundary quality: when an exploratory classifier
could read a rule file, it over-credited the rule text as evidence. The final classifications
were therefore performed with evidence-isolated prompts, and the source text was used only
to supply literal review spans. Any future prompt/skill change should preserve that boundary;
this observation does not authorize editing the reviewed rules in this step. The follow-up is
[issue #15](https://github.com/aberson/citation-needed/issues/15).

## Commands exercised

```powershell
uv run --project citation-needed cite scan --project coding-root --workspace-root C:\Users\abero\dev
uv run cite calibrate check --model "claude-code-2.1.212"
uv run cite corpus-search <one bounded query per rule>
uv run cite review open <workspace-relative-rule-path>
uv run cite review commit --run <run-id>
uv run cite distill generate --run <run-id>
uv run cite queue list --project coding-root
```
