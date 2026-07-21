# Integration surface + new-project obligations — citation-needed

Read-only investigation for the citation-needed pre-planning phase. Everything below was verified
directly this run (filesystem inspection + `Get-Item`/`Get-ChildItem` junction checks + Grep/Read of
the cited rule files) — no external literature applies to this sub-investigation, since it is about
existing workspace conventions, not a design choice that needs research backing. Where I state a
fact about a file, it is anchored `path:line`.

## TL;DR (read this first)

1. citation-needed's **skill definitions** (the 4 SKILL.md files below) must live physically under
   `dev/.claude/skills/<name>/` (the coding-root skills tree) — **not** under
   `dev/citation-needed/.claude/skills/` — because that's the tree the one real junction exposes as
   `~/.claude/skills/`, which is what makes a skill invocable from *any* project's window regardless
   of cwd.
2. citation-needed's **implementation code** (Python/uv package, CLI, SQLite DB) lives in the nested
   repo `dev/citation-needed/`, which will need its **own** `.git`. The skills are thin wrappers that
   shell out to it — exactly the `observatory-doctor` → `dev-observatory` pattern already in
   production.
3. Because the skills live in one repo (coding-root) and the engine lives in another (the nested
   citation-needed repo), a build/build-phase pass touching *both* halves must commit into **two
   separate repos** — this is the sharpest new hazard this design introduces.
4. Breakdown docs (the per-review write output) should be **central**, inside citation-needed's own
   repo (`citation-needed/breakdowns/<project-slug>/<artifact-slug>.md`) — never written into a
   target project's own tree. This is the only shape that is uniformly safe for owned *and*
   not-owned (third-party) targets, and keeps the "reviews are read-only toward targets" contract
   literal: citation-needed never writes anywhere but its own repo.

---

## (a) Skill invocation from other projects — the junction

### What's actually there (verified this run)

```
PS> Get-ChildItem -Path "C:\Users\abero\.claude" -Force | Select Name, Attributes, LinkType, Target
skills          Directory, ReparsePoint  Junction  {C:\Users\abero\dev\.claude\skills}
skills$skill    Directory, ReparsePoint  Junction  {C:\Users\abero\dev\.claude\skills$skill}
```

- **The real, live junction:** `C:\Users\abero\.claude\skills` → `C:\Users\abero\dev\.claude\skills`.
  This is what `CLAUDE.md:16` means by *"Global skills load via one `mklink /J` junction — not
  `additionalDirectories`."* There is exactly **one physical copy** of every global SKILL.md, sitting
  in the coding-root repo at `dev/.claude/skills/<name>/SKILL.md`; the harness's home-relative
  `~/.claude/skills/` load path resolves to that same tree via the junction, which is why `/build-phase`,
  `/plan-init`, `/user-pm`, etc. are invocable from inside *any* nested project's window (Alpha4Gate,
  switchboard, toybox, …) without each project carrying its own copy.
- **A second, broken entry:** `C:\Users\abero\.claude\skills$skill` → target
  `C:\Users\abero\dev\.claude\skills$skill`, and that target directory **does not exist**
  (`ls "c:/Users/abero/dev/.claude/skills$skill"` → `No such file or directory`). This is a stray/
  dead junction from some past mis-typed `mklink` invocation (a `$` literal in a Windows path, most
  likely produced by an unescaped PowerShell variable expansion gone wrong). It is inert — nothing
  resolves through it — but it is a landmine if anyone later tries to debug "why isn't my skill
  loading" by pattern-matching on `skills*`; flag it for eventual cleanup, do not touch it here (this
  investigation is read-only and out of scope for fixing it).
- Confirmed by the workspace's own prior investigation:
  `docs/investigations/skill-deep-dives/skill-xref/12-junction-vs-directory-resolution.md:9-11,25` —
  same mechanism, same rationale (the junction replaced a failed `additionalDirectories` attempt,
  incident `feedback_skills_load_from_junction.md`, 2026-05-11).
- The rule text: `CLAUDE.md:16` — *"Skills/rules live in `<project>/.claude/skills|rules/`, never
  `~/.claude/skills/`. Global skills load via one `mklink /J` junction — not `additionalDirectories`.
  Broken skill? Check junction first."*

### What this means for citation-needed specifically

Note the apparent tension in `CLAUDE.md:16`: it says skills live in `<project>/.claude/skills/`, but
the junction only maps **one** tree (`dev/.claude/skills/`) to `~/.claude/skills/`. Reconciling this
against the verified live production pattern (`observatory-doctor`, below) resolves the tension:

- `<project>/.claude/skills/` is for skills that only need to fire **when cwd is already inside that
  project** (project-local, not globally invocable) — this is not citation-needed's case, since the
  four use cases (review a target, distill a target, project-wide sweep, backlog triage) must be
  callable from *any* project's window, targeting a different project each time.
- The **cross-cutting, invoke-from-anywhere** pattern already exists in production:
  `observatory-doctor` — its SKILL.md lives at `dev/.claude/skills/observatory-doctor/SKILL.md` (i.e.
  physically in the coding-root skills tree, junction-exposed globally) and is described in its own
  header as *"a thin wrapper... all logic lives in the `observatory doctor` CLI subcommand
  (`dev-observatory/src/dev_observatory/doctor.py`)"*
  (`.claude/skills/observatory-doctor/SKILL.md:14-19`). It shells out with:
  ```
  uv run --project dev-observatory observatory doctor
  ```
  (`.claude/skills/observatory-doctor/SKILL.md:41`). `dev-observatory/` itself is explicitly *"in
  this coding-root repo... (not a nested repo)"* per `CLAUDE.md`'s Control-plane section — but the
  `uv run --project <path>` mechanism only needs a `pyproject.toml` at that relative path; it does
  not care whether that path is a nested `.git` repo or a plain coding-root subdirectory. **The same
  wrapper shape works identically whether citation-needed's engine sits in a nested repo or not.**

**Concrete obligation:** author the four citation-needed skills (named below, §b) as thin SKILL.md
wrappers physically at `dev/.claude/skills/citation-review/SKILL.md`,
`dev/.claude/skills/citation-distill/SKILL.md`, `dev/.claude/skills/citation-sweep/SKILL.md`,
`dev/.claude/skills/citation-triage/SKILL.md` — each shelling out to
`uv run --project citation-needed <cli-verb> <args>` (mirroring
`.claude/skills/observatory-doctor/SKILL.md:41`'s exact idiom) against the CLI package that lives in
the nested repo `dev/citation-needed/`. **Do not** put any SKILL.md under
`dev/citation-needed/.claude/skills/` — a skill placed there is invisible outside a window whose cwd
is already inside `dev/citation-needed/`, defeating the "invocable everywhere" requirement.

---

## (b) Skill naming — group-task convention + collision check

Convention source: `C:\Users\abero\.claude\projects\c--Users-abero-dev\memory\feedback_skill_naming_group_task.md:3,9,11` —
*"New skills and slash commands should be named `<group>-<task>`... The user thinks 'the {group}
needs to {task}' when invoking... Match the existing pattern (build-step, plan-init, repo-sync,
session-wrap, user-orient, etc.) — do not introduce a new naming style."*

### Collision check

`Glob ".claude/skills/*" (path: c:/Users/abero/dev)` plus `ls -d */` inside `dev/.claude/skills/`
enumerated all 50 current skill directories (`_shared`, `build-phase`, `build-queue`, `build-step`,
`claude-oauth-auth`, `context-slim`, `goblin-do`, `goblin-suggest`, `judge-motion`, `judge-ui`,
`lesson-harvest`, `memory-distill`, `observatory-doctor`, `plan-expedite`, `plan-feature`,
`plan-init`, `plan-merge`, `plan-redline`, `plan-review`, `plan-trim`, `plan-wrap`, `repo-init`,
`repo-sync`, `repo-update`, `research-prospect`, `review-deep`, `review-gauntlet`, `review-proof`,
`review-uat`, `session-wrap`, `skill-eval-setup`, `skill-evolve`, `skill-iterate`, `task-handoff`,
`test-prune`, `tier-escalate`, `tier-offload`, `user-afterparty`, `user-brainstorm`, `user-debug`,
`user-draft`, `user-gateway`, `user-lavishify`, `user-learn`, `user-orient`, `user-pm`,
`user-project`, `user-shakedown`, `user-uat`, `user-walkthrough`, `user-wrap`). **No `citation-*`
name exists** — zero collision risk for the `citation` group prefix.

### Proposed names

| Use case | Proposed name | Mental model | Precedent for the task word |
|---|---|---|---|
| Single-target review (extract choices, cite, classify, score) | **`citation-review`** | "the citation [system] needs to *review* [a target]" | `plan-review` uses `review` as the task suffix on a different group |
| Distill/trim proposal for ONE target, citation-justified | **`citation-distill`** | "the citation [system] needs to *distill* [a target]" | `memory-distill` is `distill` as task on a different group (`memory`) — same task word, different group is an established, non-colliding pattern in this repo |
| Project-wide rigor pass feeding a ranked `distill_queue` | **`citation-sweep`** | "the citation [system] needs to *sweep* [the whole project]" | echoes `user-afterparty`'s own self-description, *"run the sweep"* — no existing `-sweep` skill, so this coins a new but idiomatic task word rather than colliding with one |
| Operator backlog triage of the `distill_queue` | **`citation-triage`** | "the citation [system] needs to *triage* [the backlog]" | mirrors `goblin-suggest --uat`'s stated job, *"Triage the project's operator-asks"* — same task word as an existing mode, not an existing full skill name, so still collision-free |

All four pass the collision check cleanly (verified above) and all follow the `<group>-<task>`
shape without introducing a new naming style, per the memory's explicit instruction.

---

## (c) New-project obligations — `dev/citation-needed/` as an owned nested repo

Per the task framing, citation-needed is a **new owned nested repo** (its own `.git`), the same
shape as Alpha4Gate/toybox/x-marks-the-spot — **not** the "no separate repo, tracked inside
coding-root" shape that switchboard/songs use (`.claude/rules/working-directory.md:20-24`).
Verified: `dev/citation-needed/` currently exists with only a `docs/` subfolder (no `.git` yet, no
code yet) — genuinely greenfield.

### Checklist (source: `.claude/rules/descriptor-contract.md` + `.claude/rules/working-directory.md`)

1. **Register in the observatory registry** (`descriptor-contract.md:25-39`). Until registered,
   dev-observatory treats it as `owned=false` (write-blocked) by the fail-safe default
   (`descriptor-contract.md:29`). Command (verified live pattern from
   `.claude/observatory/registry.toml:1-256`, which currently has **no `citation-needed` entry**):
   ```
   uv run --project dev-observatory observatory register citation-needed --owned --path citation-needed
   ```
   In practice this step is a **hook**, not manual work: `descriptor-contract.md:72` states
   `/plan-init` performs this registration automatically for a newly-created owned project — so
   running `/plan-init` to seed citation-needed's own plan.md discharges this obligation for free.

2. **Keep citation-needed's own `CLAUDE.md` scrapable** (`descriptor-contract.md:9-23`): a
   `## Commands` / `## Key commands` / `## Stack` heading with each command as a backticked span or
   fenced-code line whose first token is a known runner (`uv`, in this case). Live precedent verified
   this run: `dev-observatory/CLAUDE.md:16-24` (`## Stack` table) and `:30-38` (`## Key commands`
   fenced block, first token `uv` throughout) — copy that shape. citation-needed's commands will look
   like `uv run citation-needed review <target>`, `uv run citation-needed sweep`, etc.

3. **Ports: none needed.** citation-needed is a CLI + SQLite tool with no server component described
   in its brief, so `descriptor-contract.md §3` (port declaration for the collision linter) is
   inapplicable — do not fabricate a port. If a future "browse the corpus" server mode is added, add
   its port then, in the scrapable shapes `descriptor-contract.md:19-20` lists.

4. **Canonical plan location** (`descriptor-contract.md:48-66`, `CLAUDE.md:17`): keep ONE entry plan
   at `dev/citation-needed/plan.md` (root, preferred) or `documentation/*-plan.md` — the observer
   checks root-first, `plan.md` before `master_plan.md`
   (`descriptor-contract.md:56-58`). Keep inline `### Step N:` blocks with `**Status:** DONE` markers
   (or `## Phase` headings) **in that entry plan itself** — a pure pointer/index plan yields a goal
   but no built/total ratio (`descriptor-contract.md:59-63`, `CLAUDE.md:17`). Using `/plan-init` to
   author it keeps this scrapable by construction (`descriptor-contract.md:64-65`).

5. **Nested-repo git implications** (`working-directory.md:26-37`): once `dev/citation-needed/` has
   its own `.git`, every step that touches *citation-needed's own repo* (its `repo-init`,
   `repo-sync`, `build-phase`, `build-step`, `repo-update`, its own UAT) must be **anchored** to that
   repo — cwd inside it, or an explicit `/user-project citation-needed` pin
   (`working-directory.md:52-54`) — never run from `dev/` root, or the commit silently lands in
   coding-root instead (`working-directory.md:41`, the wrong-directory guard at
   `working-directory.md:60-88` is the advisory backstop, not a substitute for anchoring correctly).

6. **The two-repo split this design introduces (new, citation-needed-specific hazard, not covered by
   an existing rule verbatim):** because §(a) puts the four SKILL.md files in coding-root
   (`dev/.claude/skills/citation-*/`) while the CLI/DB/package lives in the nested
   `dev/citation-needed/` repo, a single feature that touches *both* halves (e.g. "add a `--json` flag
   to citation-sweep") produces **two separate diffs in two separate repos** — one commit in `dev/`
   (the SKILL.md wrapper edit) and one commit in `dev/citation-needed/` (the CLI flag + its tests).
   `/build-step`/`/build-phase` runs for such a change must not assume a single `git add` sweeps both;
   apply `working-directory.md`'s anchoring rule **twice**, once per repo, in the same build step.

---

## (d) Where breakdown docs live — central vs per-project

**Recommendation: central**, inside citation-needed's own repo —
`citation-needed/breakdowns/<project-slug>/<artifact-slug>.md` (one file per reviewed artifact,
namespaced by the target project's registry slug).

### Weighing it against per-project (`<project>/docs/citations/<slug>.md`)

| Consideration | Central (`citation-needed/breakdowns/…`) | Per-project (`<project>/docs/citations/…`) |
|---|---|---|
| **"Reviews are read-only toward targets"** | Literal: citation-needed **never** commits into any reviewed repo, owned or not. One write path, one repo, no exceptions. | Requires writing into a repo citation-needed does not own — this is itself a write *toward* the target, even if it never edits the target's *existing* files. For a third-party (`owned=false`) target this directly conflicts with the observatory's own hard rule, *"Zero files are ever written into a non-owned tree"* (`descriptor-contract.md:39`) — citation-needed's stated workspace root explicitly includes not-owned entries (e.g. `tinstar`, `agora`, `career-ops` — all `owned = false` in `.claude/observatory/registry.toml:158-170,252-256`), so per-project storage needs a conditional carve-out for those, which central storage never needs. |
| **Nested-repo git mechanics** | citation-needed writes to its own tree; a normal same-repo commit. Zero cross-repo `git -C <target>` calls, zero risk of landing a commit in the wrong repo (the exact class of hazard `working-directory.md` exists to prevent). | Every write is a cross-repo operation into a *different* nested `.git` (or a coding-root-tracked project like switchboard/dev-observatory, or a third-party clone) — reintroduces the wrong-directory-guard surface (`working-directory.md:60-88`) on citation-needed's own write path, for every single review. |
| **Operator discoverability for the multi-project use cases** | `citation-sweep` (project-wide rigor pass) and `citation-triage` (backlog triage) both span *multiple* target projects in one pass — a single `citation-needed/breakdowns/` tree is the natural, already-unified home for a cross-project ranked `distill_queue`. | Fragments a single sweep's output across N separate repos; the ranked queue itself would still need a central index somewhere, duplicating effort. |
| **Public-repo question** | citation-needed is a plausible-OSS tool in the same family as measure-twice/x-marks-the-spot/on-brand (all tagged PUBLIC in the workspace memory). If it goes public, there is exactly **one** privacy boundary to reason about: does `breakdowns/` ship in the public repo or sit behind citation-needed's own `.gitignore`? | Every reviewed project's own repo would need to correctly `.gitignore` a folder it didn't ask for, to avoid an owned-private project accidentally leaking review content if *that* project is ever made public — N boundaries instead of one, and a target project's own gitignore hygiene is outside citation-needed's control. |
| **Downside of central** | citation-needed's own repo grows with review output over time (a normal, expected cost for a corpus-building tool — the whole point is that "the corpus compounds across reviews"); breakdowns are one `cd` away rather than sitting next to the artifact they're about. | — |

Net: central storage is the only shape that keeps the read-only-toward-targets contract literal for
*every* target shape this workspace has (owned nested repo, owned coding-root-tracked project,
not-owned third-party clone) without a per-target-type conditional in citation-needed's own write
logic, and it is the natural home for the two use cases that are multi-project by definition
(`citation-sweep`, `citation-triage`). Use `<project>/docs/citations/` only if a *specific* future
requirement demands the breakdown live physically next to the artifact (e.g. for a project's own
CI to read it) — treat that as a deliberate, documented exception, not the default.

---

## Sources

- Filesystem/junction facts: `Get-ChildItem -Path "C:\Users\abero\.claude" -Force` and
  `Get-Item 'C:\Users\abero\.claude\skills$skill'` (this run); `ls` verification that
  `C:/Users/abero/dev/.claude/skills$skill` does not exist (this run).
- `c:/Users/abero/dev/CLAUDE.md:16,17,34`
- `c:/Users/abero/dev/.claude/rules/descriptor-contract.md` (full file read this run; §1 lines 9-23,
  §2 lines 25-39, §3 lines 41-46, §4 lines 48-66, §5 lines 68-77)
- `c:/Users/abero/dev/.claude/rules/working-directory.md` (full file read this run; layers table
  lines 9-14, third-case note lines 20-24, anchoring rule lines 26-37, tradeoffs lines 39-48, guard
  lines 60-88)
- `c:/Users/abero/dev/.claude/skills/observatory-doctor/SKILL.md:3,14-19,41` (live thin-wrapper
  precedent)
- `c:/Users/abero/dev/dev-observatory/CLAUDE.md:16-24,30-38` (live scrapable `## Stack` /
  `## Key commands` precedent)
- `c:/Users/abero/dev/.claude/observatory/registry.toml` (full file read this run — confirms no
  `citation-needed` entry yet; confirms `owned = false` entries exist for `TripoSR`, `tinstar`,
  `career-ops`, `agora`)
- `C:\Users\abero\.claude\projects\c--Users-abero-dev\memory\feedback_skill_naming_group_task.md`
  (full file read this run)
- `c:/Users/abero/dev/docs/investigations/skill-deep-dives/skill-xref/12-junction-vs-directory-resolution.md:9-11,25`
  (corroborating prior investigation of the same junction mechanism)
- `Glob ".claude/skills/*" (c:/Users/abero/dev)` and `ls -d */` inside `dev/.claude/skills/` (this
  run — full 50-name enumeration, no `citation-*` collision)
- No external (web) literature was sought or applies — this investigation is entirely about
  verified, internal workspace filesystem/convention facts, not a claim needing external research
  backing.
