"""Review-run lifecycle + the ONE implementation of the plan §4.4 scoring math.

Two halves, mirroring the two CLI verbs (plan.md §4.1 items 4-5):

- :func:`open_review` creates a ``review_runs`` row with FROZEN provenance (the
  artifact's stored content hash, best-effort ``git -C <workspace> rev-parse HEAD``,
  reviewer model, ``PRAGMA user_version``) and returns the prior ``(choice_key,
  summary)`` pairs the skill layer feeds back into extraction for key REUSE
  (schema-draft.md §3 — identity survives rewording).
- :func:`commit_review` takes the parsed stdin payload (validated against
  ``docs/contracts/review-commit.schema.json`` via the pydantic mirrors below),
  upserts choices by ``(artifact_id, choice_key)`` (matching key -> UPDATE the
  existing row; absent key -> ``status='removed'``, never deleted), writes one
  ``scores`` row per choice from the k-sample VOTES, links/inserts citations ONLY
  through :func:`verify.insert_citation` (the sole-writer contract), and stamps the
  artifact composite + band + guide version on the run row (migration 0002). All DB
  writes happen in ONE transaction; any failure rolls back everything.

Scoring math lives here and ONLY here (code-quality.md § one source of truth):
label weights, vote shares (a ``parse-failed`` vote is force-scored ``contradicted``
and counted in the denominator, never dropped), majority -> derived classification,
the composite rescale, and the bands. A tie reaching commit is an ERROR — the skill
layer must already have escalated k (3 -> 5 -> 7) per §4.4, so the payload is
rejected loudly and nothing is written. ``docs/interpretation-guide.md`` is the
versioned prose of the same semantics; the two are kept in sync by tests.

Anti-fabrication at the commit seam: a ``web_fetch_verified`` entry NEVER carries
fetched page text (the contract has no such field; ``extra="forbid"`` rejects one) —
commit re-fetches the URL itself through :func:`verify.fetch_url` and re-verifies the
quote server-side. An ``api_structured`` entry likewise NEVER carries the API echo
(the contract has no ``api_echo`` field) — commit performs the structured-API lookup
ITSELF from the payload's locator (DOI -> Crossref; arXiv/S2 paper URL -> Semantic
Scholar lookup-by-id), captures the API's OWN response as the stored echo, and
requires the payload's claimed title to match the retrieved title (normalized;
mismatch rejects the whole payload). Internal citations are read-verified against the
actual workspace/memory file inside ``insert_citation``, path-CONFINED to their root.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from citation_needed import resolve, verify

# ---------------------------------------------------------------------------
# §4.4 scoring constants — the single source of truth (interpretation guide v1)
# ---------------------------------------------------------------------------

#: The four dimension labels a judge call may emit (one label per call).
DimensionLabel = Literal["evidence-backed", "interesting-novel", "unsupported", "contradicted"]

#: A judge call whose output could not be parsed arrives as this literal label and is
#: force-scored ``contradicted``, counted in the denominator — never dropped (§4.4).
PARSE_FAILED_LABEL = "parse-failed"

VoteLabel = Literal[
    "evidence-backed", "interesting-novel", "unsupported", "contradicted", "parse-failed"
]

#: Per-label composite weights (§4.4). Every value is a multiple of 0.5 — exactly
#: representable in binary floating point, which :func:`composite_from_labels` exploits
#: so band-edge composites (exactly 70.0 / 40.0 / 20.0) come out exact, not 69.99...
LABEL_WEIGHTS: dict[str, float] = {
    "evidence-backed": 1.0,
    "interesting-novel": 0.5,
    "unsupported": -0.5,
    "contradicted": -1.0,
}

#: Derived per-choice classification from the majority label (§4.4).
CLASSIFICATION_BY_LABEL: dict[str, str] = {
    "evidence-backed": "well-supported",
    "interesting-novel": "interesting",
    "unsupported": "needs-improvement",
    "contradicted": "needs-improvement",
}

#: Version stamp stored on every scores row and committed run; revisions of the
#: cutpoints bump this so old rows are never silently reinterpreted (D6).
INTERPRETATION_GUIDE_VERSION = "v1"

#: Minimum judge calls per choice (§4.4: k>=3; ties escalate to 5, then 7).
MIN_VOTES = 3

_CHOICE_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: arXiv abs/pdf URL -> the arXiv id (version suffix kept — S2 accepts it).
_ARXIV_URL_RE = re.compile(
    r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/(?P<id>[^/?#]+?)(?:\.pdf)?/?$",
    re.IGNORECASE,
)
#: semanticscholar.org paper URL -> the 40-hex S2 paperId.
_S2_PAPER_URL_RE = re.compile(
    r"^https?://(?:www\.)?semanticscholar\.org/paper/(?:[^?#]*?)(?P<id>[0-9a-f]{40})"
    r"(?:[/?#].*)?$",
    re.IGNORECASE,
)


class ReviewError(RuntimeError):
    """A review-lifecycle contract violation — the CLI reports it as ``error: ...``."""


class TieError(ReviewError):
    """A majority-vote tie reached commit. The skill layer must escalate k (3 -> 5 ->
    7) and re-vote BEFORE committing; a tie here means it did not, so the whole
    payload is rejected and nothing is written."""


# ---------------------------------------------------------------------------
# Scoring math (the ONE implementation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoteTally:
    """Vote shares + majority outcome for one choice's k judge calls."""

    evidence_backed_share: float
    interesting_novel_share: float
    unsupported_share: float
    contradicted_share: float
    majority_label: str  # one of the four DimensionLabel values
    classification: str  # derived: well-supported | needs-improvement | interesting


def tally_votes(votes: Sequence[str]) -> VoteTally:
    """Shares + majority label from k raw vote labels (k >= 3).

    ``parse-failed`` votes are force-scored ``contradicted`` and counted in the
    denominator. A tie for the top count raises :class:`TieError` — escalation is the
    skill layer's job, before commit.
    """
    if len(votes) < MIN_VOTES:
        raise ReviewError(f"k >= {MIN_VOTES} judge votes required, got {len(votes)} (§4.4)")
    counts: dict[str, int] = {label: 0 for label in LABEL_WEIGHTS}
    for vote in votes:
        if vote == PARSE_FAILED_LABEL:
            counts["contradicted"] += 1  # force-scored, kept in the denominator
        elif vote in counts:
            counts[vote] += 1
        else:
            raise ReviewError(
                f"unknown vote label {vote!r} — expected one of "
                f"{sorted(LABEL_WEIGHTS)} or {PARSE_FAILED_LABEL!r}"
            )
    top = max(counts.values())
    leaders = sorted(label for label, count in counts.items() if count == top)
    if len(leaders) > 1:
        raise TieError(
            f"majority-vote tie between {leaders} at {top}/{len(votes)} votes — a tie "
            "must be escalated (k=5, then 7) by the skill layer BEFORE commit; "
            "rejecting the payload"
        )
    majority = leaders[0]
    k = len(votes)
    return VoteTally(
        evidence_backed_share=counts["evidence-backed"] / k,
        interesting_novel_share=counts["interesting-novel"] / k,
        unsupported_share=counts["unsupported"] / k,
        contradicted_share=counts["contradicted"] / k,
        majority_label=majority,
        classification=CLASSIFICATION_BY_LABEL[majority],
    )


def composite_from_labels(labels: Sequence[str]) -> float:
    """Artifact composite: mean of per-choice majority-label weights, rescaled
    ``(mean + 1) / 2 * 100`` to 0..100 (§4.4).

    Computed as ``(sum_of_weights + n) * 50 / n``: weight sums are exact binary
    floats (all weights are multiples of 0.5), so band-edge values land exactly on
    70.0 / 40.0 / 20.0 instead of 69.999... — the bands compare with ``>=``.
    """
    if not labels:
        raise ReviewError("composite requires at least one scored choice")
    total = 0.0
    for label in labels:
        if label not in LABEL_WEIGHTS:
            raise ReviewError(f"unknown dimension label {label!r}")
        total += LABEL_WEIGHTS[label]
    count = len(labels)
    return (total + count) * 50.0 / count


def band_of(composite: float) -> str:
    """§4.4 bands: >=70 strong / 40-69 adequate / 20-39 weak / <20 unsupported."""
    if composite >= 70.0:
        return "strong"
    if composite >= 40.0:
        return "adequate"
    if composite >= 20.0:
        return "weak"
    return "unsupported"


# ---------------------------------------------------------------------------
# Contract models — the pydantic mirrors of docs/contracts/*.schema.json.
# No-new-dependency sync route (Step 4 item 5): every payload/output is validated
# through these models, and tests/test_review.py asserts each schema $defs entry's
# properties + required lists match the model's fields exactly, so the .json files
# and the code cannot drift apart.
# ---------------------------------------------------------------------------


class _ContractBase(BaseModel):
    """``extra="forbid"`` mirrors ``"additionalProperties": false`` in the schemas —
    notably, a caller-supplied ``fetched_text`` on a citation entry is REJECTED."""

    model_config = ConfigDict(extra="forbid")


class CitationEntry(_ContractBase):
    """One citation reference for one choice — an existing-corpus link OR a new
    verified resolution record (never an unverified claim)."""

    relevance_note: str
    support_direction: Literal["supports", "contradicts", "tangential"]
    # -- existing-citation link (exactly one of the two link forms):
    citation_id: int | None = None
    natural_key: str | None = None  # requires kind (identity is UNIQUE(kind, natural_key))
    # -- new-record fields:
    kind: Literal["external", "internal"] | None = None
    resolution_method: Literal["api_structured", "web_fetch_verified", "internal-read"] | None = (
        None
    )
    title: str | None = None
    authors: str | None = None
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    url: str | None = None
    workspace_path: str | None = None
    quote: str | None = None  # supporting quote (web_fetch_verified / internal-read)
    # NOTE deliberately NO api_echo field: the echo is captured SERVER-SIDE by
    # commit_review's own structured-API lookup — a caller-supplied echo is rejected
    # outright by extra="forbid" (the same shape as the absent fetched_text field).
    source_line_ref: str | None = None  # internal only: 'path:line' locator
    keywords: str | None = None  # FTS5 corpus terms; defaults to the choice category
    notes: str | None = None

    def is_new_record(self) -> bool:
        """True when this entry inserts a new citation (vs linking an existing one)."""
        return self.citation_id is None and self.natural_key is None

    @model_validator(mode="after")
    def _check_shape(self) -> CitationEntry:
        if not self.relevance_note.strip():
            raise ValueError("relevance_note must be non-empty (never a bare join)")
        if not self.is_new_record():
            if self.citation_id is not None and self.natural_key is not None:
                raise ValueError("link via citation_id OR kind+natural_key, not both")
            if self.natural_key is not None and self.kind is None:
                raise ValueError(
                    "a natural_key link requires kind — citation identity is "
                    "UNIQUE(kind, natural_key)"
                )
            if self.resolution_method is not None:
                raise ValueError("an existing-citation link must not carry a new resolution record")
            return self
        if self.resolution_method is None:
            raise ValueError(
                "a new citation requires resolution_method — or link an existing one "
                "via citation_id / kind+natural_key"
            )
        if not self.title or not self.title.strip():
            raise ValueError("a new citation requires the retrieved title")
        if self.resolution_method == "api_structured":
            if self.kind != "external":
                raise ValueError("api_structured citations must be kind='external'")
            if not self.doi and not self.url:
                raise ValueError(
                    "api_structured requires a locator (doi or url) — commit performs "
                    "the structured-API lookup itself and captures the echo server-side"
                )
        elif self.resolution_method == "web_fetch_verified":
            if self.kind != "external":
                raise ValueError("web_fetch_verified citations must be kind='external'")
            if not self.url:
                raise ValueError(
                    "web_fetch_verified requires url — commit re-fetches it server-side "
                    "(caller-supplied page text is never accepted)"
                )
            if not self.quote:
                raise ValueError("web_fetch_verified requires the supporting quote")
        else:  # internal-read
            if self.kind != "internal":
                raise ValueError("internal-read citations must be kind='internal'")
            if not self.workspace_path:
                raise ValueError("internal-read requires workspace_path")
            if not self.quote:
                raise ValueError("internal-read requires the quoted span")
        return self


class ChoiceEntry(_ContractBase):
    """One extracted choice with its k judge votes and citation references."""

    choice_key: str
    summary: str
    quote: str  # literal extracted span text (stored as choices.quote_or_span)
    span_start_line: int = Field(ge=1)
    span_end_line: int = Field(ge=1)
    category: str  # one of the 11 taxonomy categories (prompts/classification.v1.md)
    votes: list[VoteLabel] = Field(min_length=MIN_VOTES)
    source_path: str | None = None  # file the span came from, when not the artifact's own
    citations: list[CitationEntry] = Field(default_factory=list)
    literature_searched: bool
    literature_found: bool
    search_queries: list[str] = Field(default_factory=list)
    rationale: str | None = None
    suggestions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_shape(self) -> ChoiceEntry:
        if not _CHOICE_KEY_RE.match(self.choice_key):
            raise ValueError(f"choice_key must be a kebab slug ([a-z0-9-]): {self.choice_key!r}")
        if not self.summary.strip():
            raise ValueError("summary must be non-empty")
        if not self.quote.strip():
            raise ValueError("quote must be non-empty (the literal extracted span)")
        if self.span_end_line < self.span_start_line:
            raise ValueError("span_end_line must be >= span_start_line")
        if self.literature_found and not self.literature_searched:
            raise ValueError("literature_found requires literature_searched")
        if self.literature_searched and not self.search_queries:
            raise ValueError(
                "literature_searched requires the search_queries actually tried — "
                "a null result must be auditable, not a black box"
            )
        return self


class CommitPayload(_ContractBase):
    """The ``cite review commit`` stdin payload (review-commit.schema.json)."""

    choices: list[ChoiceEntry] = Field(min_length=1)
    run_id: int | None = None  # optional when --run is passed; must agree when both

    @model_validator(mode="after")
    def _check_unique_keys(self) -> CommitPayload:
        keys = [choice.choice_key for choice in self.choices]
        dupes = sorted({key for key in keys if keys.count(key) > 1})
        if dupes:
            raise ValueError(f"duplicate choice_key(s) in payload: {', '.join(dupes)}")
        return self


class OpenArtifact(_ContractBase):
    """Artifact snapshot inside the ``review open`` output."""

    id: int
    path: str
    artifact_type: str
    project: str
    content_hash: str
    git_sha: str | None
    tool_schema_version: int


class PriorChoice(_ContractBase):
    """One prior (choice_key, summary) pair — the key-REUSE input for re-extraction."""

    choice_key: str
    summary: str
    status: str


class OpenOutput(_ContractBase):
    """The ``cite review open`` stdout JSON (review-open.schema.json).

    ``calibration_aged_accepted`` is True when this run was opened under the
    ``--accept-aged`` override of an over-30-day calibration — surfaced here (and
    stamped onto the fingerprint cache by ``calibrate.check_calibration``) so a
    downstream audit can tell overridden runs from fresh-gate runs.
    """

    run_id: int
    reviewer_model: str
    started_at: str
    calibration_aged_accepted: bool = False
    artifact: OpenArtifact
    prior_choices: list[PriorChoice]


# ---------------------------------------------------------------------------
# open_review
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_head_sha(workspace_root: Path) -> str | None:
    """Best-effort ``git -C <workspace_root> rev-parse HEAD`` — None when the
    directory is not a git repo, git is absent, or the call fails/times out."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def open_review(
    conn: sqlite3.Connection,
    artifact_path: str,
    *,
    reviewer_model: str,
    workspace_root: Path,
    calibration_check: bool = True,
    accept_aged: bool = False,
) -> OpenOutput:
    """Create a review_runs row with frozen provenance; return it JSON-ready.

    The frozen hash comes from the artifact's STORED ``current_content_hash`` (set by
    ``cite scan``), the git sha from a best-effort ``rev-parse HEAD`` (nullable), and
    ``tool_schema_version`` from ``PRAGMA user_version``. Prior choice_key/summary
    pairs for the artifact ride along so the skill layer can instruct key REUSE.
    Transactions belong to the caller (``with conn:``); this function only executes.

    CALIBRATION GATE (plan §4.5 / D7): after the target resolves but before any row
    is created, the cached calibration fingerprint is checked against the current
    prompt hash / model id / corpus / schema values — no valid calibration, no real
    review. ``accept_aged`` overrides ONLY the 30-day advisory ceiling, never an A-D
    mismatch. ``calibration_check=False`` is the escape hatch that exists SOLELY for
    ``calibrate.py``'s own anchor runs on the throwaway DB: the gate cannot require
    a passed calibration before any calibration has ever run. ``cite review open``
    always passes the check enabled.
    """
    if not reviewer_model or not reviewer_model.strip():
        raise ReviewError("reviewer_model must be non-empty (frozen run provenance)")
    path = artifact_path.replace("\\", "/").strip()
    row = conn.execute(
        "SELECT id, path, artifact_type, project, current_content_hash "
        "FROM artifacts WHERE path = ?",
        (path,),
    ).fetchone()
    if row is None:
        raise ReviewError(f"artifact not registered: {path} — run `cite scan` first")
    if row[4] is None:
        raise ReviewError(f"artifact has no stored content hash: {path} — re-run `cite scan`")
    aged_accepted = False
    if calibration_check:
        # Late import: calibrate drives open/commit for its anchor runs, so a
        # top-level import here would be circular.
        from citation_needed import calibrate

        check = calibrate.check_calibration(
            conn, model_id=reviewer_model.strip(), accept_aged=accept_aged
        )
        if not check.valid:
            reasons = "; ".join(check.reasons)
            raise ReviewError(
                "review open REFUSED — no valid calibration for this DB "
                f"({reasons}). The anchor gate must pass before any real review "
                "(measurement-validity: calibrate with anchors before comparing "
                "candidates). Run the calibration flow (`cite calibrate commit`, "
                "driven by /citation-review --calibrate), then re-open."
            )
        # valid AND aged is only reachable via accept_aged — the override did the
        # work; check_calibration already stamped the durable cache-side record.
        aged_accepted = check.aged
    artifact_id = int(row[0])
    tool_schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    git_sha = git_head_sha(workspace_root)
    started_at = _utc_now()
    cursor = conn.execute(
        "INSERT INTO review_runs (artifact_id, started_at, artifact_content_hash_at_review, "
        "artifact_git_sha_at_review, reviewer_model, tool_schema_version) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            artifact_id,
            started_at,
            str(row[4]),
            git_sha,
            reviewer_model.strip(),
            tool_schema_version,
        ),
    )
    assert cursor.lastrowid is not None
    prior = conn.execute(
        "SELECT choice_key, summary, status FROM choices WHERE artifact_id = ? ORDER BY choice_key",
        (artifact_id,),
    ).fetchall()
    return OpenOutput(
        run_id=int(cursor.lastrowid),
        reviewer_model=reviewer_model.strip(),
        started_at=started_at,
        calibration_aged_accepted=aged_accepted,
        artifact=OpenArtifact(
            id=artifact_id,
            path=str(row[1]),
            artifact_type=str(row[2]),
            project=str(row[3]),
            content_hash=str(row[4]),
            git_sha=git_sha,
            tool_schema_version=tool_schema_version,
        ),
        prior_choices=[
            PriorChoice(choice_key=str(p[0]), summary=str(p[1]), status=str(p[2])) for p in prior
        ],
    )


# ---------------------------------------------------------------------------
# commit_review
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommittedCitation:
    """One linked citation, denormalized for rendering (breakdown.py)."""

    citation_id: int
    kind: str
    title: str | None
    natural_key: str
    locator: str  # url_or_doi (external) or workspace_path (internal)
    resolution_method: str
    support_direction: str
    relevance_note: str
    source_line_ref: str | None


@dataclass(frozen=True)
class CommittedChoice:
    """One committed choice with its tally and citations, ready to render."""

    choice_id: int
    choice_key: str
    reused_key: bool  # True when the key matched an existing row (D4 reuse path)
    summary: str
    quote: str
    span_start_line: int
    span_end_line: int
    source_path: str | None
    category: str
    votes: list[str]
    tally: VoteTally
    citations: list[CommittedCitation]
    literature_searched: bool
    literature_found: bool
    search_queries: list[str]
    rationale: str | None
    suggestions: list[str]


@dataclass(frozen=True)
class CommitResult:
    """Everything ``cite review commit`` persisted, for the breakdown renderer."""

    run_id: int
    artifact_id: int
    artifact_path: str
    artifact_type: str
    project: str
    reviewer_model: str
    started_at: str
    finished_at: str
    artifact_content_hash_at_review: str
    artifact_git_sha_at_review: str | None
    tool_schema_version: int
    interpretation_guide_version: str
    composite: float
    composite_band: str
    choices: list[CommittedChoice]
    removed_keys: list[str]

    def classification_counts(self) -> dict[str, int]:
        counts = {"well-supported": 0, "needs-improvement": 0, "interesting": 0}
        for choice in self.choices:
            counts[choice.tally.classification] += 1
        return counts


def _content_hash(quote: str) -> str:
    """sha256 of the literal extracted span — the byte-identical reuse fast path."""
    return hashlib.sha256(quote.encode("utf-8")).hexdigest()


def _upsert_choice(
    conn: sqlite3.Connection,
    artifact_id: int,
    run_id: int,
    choice: ChoiceEntry,
    existing: dict[str, tuple[int, str]],
) -> tuple[int, bool]:
    """Reuse-or-insert by (artifact_id, choice_key); returns (choice_id, reused)."""
    content_hash = _content_hash(choice.quote)
    if choice.choice_key in existing:
        choice_id = existing[choice.choice_key][0]
        conn.execute(
            "UPDATE choices SET summary = ?, quote_or_span = ?, span_start_line = ?, "
            "span_end_line = ?, source_path = ?, content_hash_at_extraction = ?, "
            "status = 'active', superseded_at = NULL, last_confirmed_review_run_id = ? "
            "WHERE id = ?",
            (
                choice.summary,
                choice.quote,
                choice.span_start_line,
                choice.span_end_line,
                choice.source_path,
                content_hash,
                run_id,
                choice_id,
            ),
        )
        return choice_id, True
    cursor = conn.execute(
        "INSERT INTO choices (artifact_id, choice_key, summary, quote_or_span, "
        "span_start_line, span_end_line, source_path, content_hash_at_extraction, "
        "status, first_extracted_review_run_id, last_confirmed_review_run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (
            artifact_id,
            choice.choice_key,
            choice.summary,
            choice.quote,
            choice.span_start_line,
            choice.span_end_line,
            choice.source_path,
            content_hash,
            run_id,
            run_id,
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid), False


def _normalized_title(title: str) -> str:
    """Title-match normalization for api_structured: whitespace-collapse + casefold."""
    return " ".join(title.split()).casefold()


def _s2_paper_id_from_url(url: str) -> str | None:
    """Derive the S2 lookup id from an api_structured locator URL, or ``None``.

    Accepted forms: an arXiv abs/pdf URL (-> ``ARXIV:<id>``) or a
    semanticscholar.org paper URL (-> the 40-hex paperId).
    """
    arxiv = _ARXIV_URL_RE.match(url.strip())
    if arxiv is not None:
        return f"ARXIV:{arxiv.group('id')}"
    s2 = _S2_PAPER_URL_RE.match(url.strip())
    if s2 is not None:
        return s2.group("id")
    return None


def _api_structured_echo(entry: CitationEntry) -> dict[str, Any]:
    """SERVER-SIDE structured-API verification for one NEW api_structured entry.

    The payload supplies only the locator + the claimed title — never the echo
    (:class:`CitationEntry` has no ``api_echo`` field; ``extra="forbid"`` rejects one).
    This function performs the lookup ITSELF (DOI -> :func:`resolve.lookup_crossref_doi`;
    arXiv / S2 paper URL -> :func:`resolve.lookup_semantic_scholar_id`), captures the
    API's OWN JSON response as the echo to store (plan §4.2: "captured at insert time
    from the actual fetch/API response"), and requires the claimed title to match the
    retrieved title after :func:`_normalized_title`. ANY failure — unresolvable
    locator, unreachable API, missing retrieved title, title mismatch — raises
    :class:`verify.VerificationFailed`: the whole payload rejects, nothing is written.
    Transport is injectable for offline tests via the ``resolve`` functions' ``client``
    parameter / monkeypatching the ``resolve`` module attributes.
    """
    assert entry.title is not None  # guaranteed by the model validator
    locator = entry.doi or entry.url
    assert locator is not None  # guaranteed by the model validator
    retrieved: str | None
    try:
        if entry.doi:
            echo = resolve.lookup_crossref_doi(entry.doi)
            titles = echo.get("title")
            retrieved = (
                str(titles[0])
                if isinstance(titles, list) and titles and isinstance(titles[0], str)
                else None
            )
        else:
            assert entry.url is not None
            paper_id = _s2_paper_id_from_url(entry.url)
            if paper_id is None:
                raise verify.VerificationFailed(
                    f"api_structured locator {entry.url!r} is not resolvable server-side — "
                    "supply a DOI, an arxiv.org/abs URL, or a semanticscholar.org paper "
                    "URL (or use web_fetch_verified for grey literature)"
                )
            echo = resolve.lookup_semantic_scholar_id(paper_id)
            raw_title = echo.get("title")
            retrieved = raw_title if isinstance(raw_title, str) else None
    except resolve.ResolutionError as exc:
        raise verify.VerificationFailed(
            f"api_structured lookup failed for {locator!r}: {exc} — refusing to insert "
            "an unverified citation (the API echo is captured server-side, never trusted "
            "from the payload)"
        ) from exc
    if retrieved is None or not retrieved.strip():
        raise verify.VerificationFailed(
            f"api_structured response for {locator!r} carries no retrieved title — "
            "refusing to insert an unverified citation"
        )
    if _normalized_title(retrieved) != _normalized_title(entry.title):
        raise verify.VerificationFailed(
            f"api_structured title mismatch for {locator!r}: the payload claims "
            f"{entry.title!r} but the API returned {retrieved!r} — refusing to insert "
            "a fabricated (or mis-attributed) citation"
        )
    return echo


def _resolve_citation_id(
    conn: sqlite3.Connection,
    entry: CitationEntry,
    category: str,
    run_git_sha: str | None,
    workspace_root: Path,
    memory_root: Path | None,
    prefetched: dict[int, verify.FetchResult],
    api_echoes: dict[int, dict[str, Any]],
) -> int:
    """Existing link -> lookup; new record -> verify.insert_citation (sole writer)."""
    if not entry.is_new_record():
        if entry.citation_id is not None:
            row = conn.execute(
                "SELECT id FROM citations WHERE id = ?", (entry.citation_id,)
            ).fetchone()
            if row is None:
                raise ReviewError(f"citation id {entry.citation_id} not found in the corpus")
            return int(row[0])
        assert entry.kind is not None and entry.natural_key is not None
        row = conn.execute(
            "SELECT id FROM citations WHERE kind = ? AND natural_key = ?",
            (entry.kind, entry.natural_key),
        ).fetchone()
        if row is None:
            raise ReviewError(
                f"no existing {entry.kind} citation with natural_key "
                f"{entry.natural_key!r} — supply a full resolution record instead"
            )
        return int(row[0])
    assert entry.resolution_method is not None and entry.title is not None
    keywords = entry.keywords or category
    if entry.resolution_method == "api_structured":
        # The echo was captured by OUR structured-API lookup in the pre-transaction
        # phase (_api_structured_echo) — a caller-supplied echo never exists here.
        return verify.insert_citation(
            conn,
            kind="external",
            resolution_method="api_structured",
            title=entry.title,
            doi=entry.doi,
            url=entry.url,
            api_echo=api_echoes[id(entry)],
            authors=entry.authors,
            year=entry.year,
            venue=entry.venue,
            keywords=keywords,
            notes=entry.notes,
        )
    if entry.resolution_method == "web_fetch_verified":
        # The FetchResult was produced by OUR verify.fetch_url call in the pre-fetch
        # phase — caller-supplied page text never exists in this pipeline.
        return verify.insert_citation(
            conn,
            kind="external",
            resolution_method="web_fetch_verified",
            title=entry.title,
            doi=entry.doi,
            url=entry.url,
            supporting_quote=entry.quote,
            fetch_result=prefetched[id(entry)],
            authors=entry.authors,
            year=entry.year,
            venue=entry.venue,
            keywords=keywords,
            notes=entry.notes,
        )
    return verify.insert_citation(
        conn,
        kind="internal",
        resolution_method="internal-read",
        title=entry.title,
        workspace_path=entry.workspace_path,
        workspace_root=workspace_root,
        memory_root=memory_root,
        supporting_quote=entry.quote,
        keywords=keywords,
        notes=entry.notes,
        source_git_sha=run_git_sha,
        source_line_ref=entry.source_line_ref,
    )


def _commit_citation(
    conn: sqlite3.Connection,
    entry: CitationEntry,
    choice_id: int,
    category: str,
    run_id: int,
    run_git_sha: str | None,
    workspace_root: Path,
    memory_root: Path | None,
    prefetched: dict[int, verify.FetchResult],
    api_echoes: dict[int, dict[str, Any]],
) -> CommittedCitation:
    citation_id = _resolve_citation_id(
        conn, entry, category, run_git_sha, workspace_root, memory_root, prefetched, api_echoes
    )
    conn.execute(
        "INSERT INTO choice_citations (choice_id, citation_id, relevance_note, "
        "support_direction, first_linked_review_run_id, last_confirmed_review_run_id) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(choice_id, citation_id) DO UPDATE SET "
        "relevance_note = excluded.relevance_note, "
        "support_direction = excluded.support_direction, "
        "last_confirmed_review_run_id = excluded.last_confirmed_review_run_id",
        (choice_id, citation_id, entry.relevance_note, entry.support_direction, run_id, run_id),
    )
    row = conn.execute(
        "SELECT kind, title, natural_key, url_or_doi, workspace_path, resolution_method, "
        "source_line_ref FROM citations WHERE id = ?",
        (citation_id,),
    ).fetchone()
    assert row is not None  # inserted or looked up in this same transaction
    locator = row[3] if row[3] is not None else row[4] if row[4] is not None else row[2]
    return CommittedCitation(
        citation_id=citation_id,
        kind=str(row[0]),
        title=str(row[1]) if row[1] is not None else None,
        natural_key=str(row[2]),
        locator=str(locator),
        resolution_method=str(row[5]),
        support_direction=entry.support_direction,
        relevance_note=entry.relevance_note,
        source_line_ref=str(row[6]) if row[6] is not None else None,
    )


def commit_review(
    conn: sqlite3.Connection,
    run_id: int,
    payload: CommitPayload | dict[str, Any],
    *,
    workspace_root: Path,
    memory_root: Path | None = None,
) -> CommitResult:
    """Persist one parsed commit payload — choices, scores, citations, composite.

    Validation, vote tallying, every ``api_structured`` server-side lookup
    (:func:`_api_structured_echo`), and every ``web_fetch_verified`` re-fetch happen
    BEFORE the transaction opens; all DB writes then run in ONE transaction
    (``with conn:``) so any failure — tie, unknown citation link, quote mismatch or
    path escape inside ``insert_citation`` — rolls back everything and the run stays
    uncommitted. ``memory_root`` confines ``memory:``-scheme internal-read citations
    (defaults to the real per-project memory root inside ``verify``).
    """
    if isinstance(payload, dict):
        payload = CommitPayload.model_validate(payload)
    if payload.run_id is not None and payload.run_id != run_id:
        raise ReviewError(f"payload run_id {payload.run_id} does not match run {run_id}")
    run = conn.execute(
        "SELECT r.artifact_id, r.started_at, r.artifact_content_hash_at_review, "
        "r.artifact_git_sha_at_review, r.reviewer_model, r.tool_schema_version, "
        "r.finished_at, r.composite, a.path, a.artifact_type, a.project "
        "FROM review_runs r JOIN artifacts a ON a.id = r.artifact_id WHERE r.id = ?",
        (run_id,),
    ).fetchone()
    if run is None:
        raise ReviewError(f"review run {run_id} does not exist — run `cite review open` first")
    if run[6] is not None or run[7] is not None:
        raise ReviewError(f"review run {run_id} is already committed — open a new run to re-review")
    artifact_id = int(run[0])
    run_git_sha = str(run[3]) if run[3] is not None else None

    # Score everything before any write — a tie or bad label rejects the whole payload.
    tallies = [tally_votes(choice.votes) for choice in payload.choices]
    composite = composite_from_labels([tally.majority_label for tally in tallies])
    composite_band = band_of(composite)

    # Verify every NEW external citation OURSELVES before opening the transaction:
    # the contract carries neither fetched text nor an API echo, and none would be
    # trusted if it did. api_structured -> our own structured-API lookup (echo captured
    # server-side, claimed title must match); web_fetch_verified -> our own SSRF-guarded
    # fetch (quote re-verified inside insert_citation).
    prefetched: dict[int, verify.FetchResult] = {}
    api_echoes: dict[int, dict[str, Any]] = {}
    for choice in payload.choices:
        for entry in choice.citations:
            if not entry.is_new_record():
                continue
            if entry.resolution_method == "api_structured":
                api_echoes[id(entry)] = _api_structured_echo(entry)
            elif entry.resolution_method == "web_fetch_verified":
                assert entry.url is not None  # guaranteed by the model validator
                prefetched[id(entry)] = verify.fetch_url(entry.url)

    finished_at = _utc_now()
    payload_keys = {choice.choice_key for choice in payload.choices}
    committed: list[CommittedChoice] = []
    removed_keys: list[str] = []
    with conn:  # ONE transaction — any failure below rolls back every write
        existing_rows = conn.execute(
            "SELECT id, choice_key, status FROM choices WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchall()
        existing = {str(r[1]): (int(r[0]), str(r[2])) for r in existing_rows}
        for choice, tally in zip(payload.choices, tallies, strict=True):
            choice_id, reused = _upsert_choice(conn, artifact_id, run_id, choice, existing)
            citations = [
                _commit_citation(
                    conn,
                    entry,
                    choice_id,
                    choice.category,
                    run_id,
                    run_git_sha,
                    workspace_root,
                    memory_root,
                    prefetched,
                    api_echoes,
                )
                for entry in choice.citations
            ]
            choice_composite = composite_from_labels([tally.majority_label])
            conn.execute(
                "INSERT INTO scores (review_run_id, choice_id, evidence_backed_share, "
                "interesting_novel_share, unsupported_share, contradicted_share, "
                "classification, composite, composite_band, interpretation_guide_version, "
                "rationale, literature_searched, literature_found, search_queries) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    choice_id,
                    tally.evidence_backed_share,
                    tally.interesting_novel_share,
                    tally.unsupported_share,
                    tally.contradicted_share,
                    tally.classification,
                    choice_composite,
                    band_of(choice_composite),
                    INTERPRETATION_GUIDE_VERSION,
                    choice.rationale,
                    int(choice.literature_searched),
                    int(choice.literature_found),
                    json.dumps(choice.search_queries),
                ),
            )
            committed.append(
                CommittedChoice(
                    choice_id=choice_id,
                    choice_key=choice.choice_key,
                    reused_key=reused,
                    summary=choice.summary,
                    quote=choice.quote,
                    span_start_line=choice.span_start_line,
                    span_end_line=choice.span_end_line,
                    source_path=choice.source_path,
                    category=choice.category,
                    votes=list(choice.votes),
                    tally=tally,
                    citations=citations,
                    literature_searched=choice.literature_searched,
                    literature_found=choice.literature_found,
                    search_queries=list(choice.search_queries),
                    rationale=choice.rationale,
                    suggestions=list(choice.suggestions),
                )
            )
        # Keys present in the DB but ABSENT from this payload: removed, never deleted —
        # their citations remain corpus assets (schema-draft.md §3).
        for key, (choice_id, status) in sorted(existing.items()):
            if key not in payload_keys and status != "removed":
                conn.execute(
                    "UPDATE choices SET status = 'removed', superseded_at = ? WHERE id = ?",
                    (finished_at, choice_id),
                )
                removed_keys.append(key)
        conn.execute(
            "UPDATE review_runs SET finished_at = ?, composite = ?, composite_band = ?, "
            "interpretation_guide_version = ? WHERE id = ?",
            (finished_at, composite, composite_band, INTERPRETATION_GUIDE_VERSION, run_id),
        )
        conn.execute(
            "UPDATE artifacts SET last_reviewed_at = ?, current_git_sha = ? WHERE id = ?",
            (finished_at, run_git_sha, artifact_id),
        )
    return CommitResult(
        run_id=run_id,
        artifact_id=artifact_id,
        artifact_path=str(run[8]),
        artifact_type=str(run[9]),
        project=str(run[10]),
        reviewer_model=str(run[4]),
        started_at=str(run[1]),
        finished_at=finished_at,
        artifact_content_hash_at_review=str(run[2]),
        artifact_git_sha_at_review=run_git_sha,
        tool_schema_version=int(run[5]),
        interpretation_guide_version=INTERPRETATION_GUIDE_VERSION,
        composite=composite,
        composite_band=composite_band,
        choices=committed,
        removed_keys=removed_keys,
    )


__all__ = [
    "CLASSIFICATION_BY_LABEL",
    "INTERPRETATION_GUIDE_VERSION",
    "LABEL_WEIGHTS",
    "MIN_VOTES",
    "PARSE_FAILED_LABEL",
    "ChoiceEntry",
    "CitationEntry",
    "CommitPayload",
    "CommitResult",
    "CommittedChoice",
    "CommittedCitation",
    "OpenArtifact",
    "OpenOutput",
    "PriorChoice",
    "ReviewError",
    "TieError",
    "VoteTally",
    "band_of",
    "commit_review",
    "composite_from_labels",
    "git_head_sha",
    "open_review",
    "tally_votes",
]
