---
template: extraction
version: v1
# The sha256 of this file is calibration fingerprint A (Step 5): any edit here
# invalidates the cached calibration and forces a re-run of the anchor gate.
---

# Choice extraction — prompt template v1

You are extracting the **discrete design choices** embedded in ONE LLM-facing artifact.
A choice is a decision someone made that could have gone another way — a rule, a
threshold, a convention, a structural commitment — that could receive its own verdict
from evidence. You extract; you do NOT judge support here (classification is a separate
pass, `prompts/classification.v1.md`).

## Inputs you receive

1. The artifact's full text (pointer artifacts already resolved to the choice-bearing
   file by `cite scan`; when your span comes from a different file than the artifact's
   own, record it in `source_path`).
2. The artifact's `artifact_type`.
3. The prior `(choice_key, summary, status)` pairs from `cite review open` (empty on a
   first review).

## Choice units by artifact type (plan §12.B)

| artifact_type | One choice = | Notes |
|---|---|---|
| `skill` | one `##`/`###` instructional section | An `evals/` sidecar's rubric choices extract with the owning skill (record the sidecar file in `source_path`). `.claude/commands/*.md` files are this type. |
| `rule` | one named `##` sub-rule | The rule + incident + source-memory triad IS pre-existing internal provenance — quote it. |
| `claude_md` | one `##` section | Highest load weight (auto-loads every session). Inlined non-artifact `@`-import content extracts here with `source_path` set; imports that are themselves scanned artifacts are SKIPPED (their choices belong to their own review). |
| `plan` | one `### Step N:` / `## Phase` block | Pointer-only plans were resolved by scan; extract from the linked targets with `source_path`. |
| `memory` | one **independently-falsifiable decision** | See the splitting rule below. |

## The memory per-decision splitting rule (plan §4.1)

- The unit is an **independently-falsifiable decision**: a claim that could receive its
  own verdict and be reversed on its own.
- **Different-verdicts test:** two claims are separate choices ONLY if they could
  plausibly receive different verdicts (one evidence-backed while the other is
  contradicted). If every plausible verdict moves them together, they are ONE choice.
- Single-decision memories — the common case — yield **exactly one** choice. A large
  composite memory yields one choice per decision (e.g. a model-preference memory splits
  into its diversity-beats-stronger-model claim, its seed-point escalation rule, and its
  re-pin-after-autoupdate convention: different evidential standing, separate scores).
- **Over-split guard:** narrative, incident evidence, and **Why:** / **How to apply:**
  elaboration attach to their parent choice as span/provenance — NEVER as choices of
  their own. When in doubt, attach rather than split.

## choice_key: REUSE before minting

You are given the prior `(choice_key, summary)` pairs for this artifact:

- **REUSE a prior key verbatim** when the underlying decision is the same, **even if the
  text was reworded** — identity must survive rewording (this is the D4 contract; a
  reworded re-review must produce zero duplicate rows).
- Reuse applies to `removed` keys too: a decision that reappears revives its old key.
- **Mint a new key** only for a genuinely new decision: a kebab slug
  (`^[a-z0-9]+(-[a-z0-9]+)*$`), specific enough to survive a rewrite
  (`subagent-terse-verdict-file-detail`, not `rule-1`).
- A prior key you did NOT re-observe: simply omit it — the CLI marks it `removed`.

## Output contract

Emit the `choices` array skeleton of `docs/contracts/review-commit.schema.json` — for
each choice: `choice_key`, `summary` (one sentence, present tense), `quote` (the literal
extracted span text, verbatim), `span_start_line` / `span_end_line` (1-based, locator
only), `category` (one of the 11 categories in `prompts/classification.v1.md`),
`source_path` (only when the span is not from the artifact's own file), and your
proposed `search_queries`. The classification pass fills `votes`; the citation pass
fills `citations` / `literature_searched` / `literature_found`. The final assembled
payload MUST validate against `docs/contracts/review-commit.schema.json`.

Emit JSON only — no prose around it.
