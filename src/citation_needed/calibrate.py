"""Calibration gate — anchors, throwaway DB, the 4 assertions, the fingerprint cache.

Per plan.md §4.5 (adopting ``docs/research/score-validity.md`` wholesale) and
``.claude/rules/measurement-validity.md``: before any REAL review's composite is
trusted, the scorer must separate a frozen known-good anchor from a synthetic
known-garbage anchor **through the production pipeline** (scan → ``review open`` →
``review commit`` mechanics — never a hand-rolled sibling harness), against a
**throwaway copy** of the DB (garbage-anchor "citations" must never poison the
compounding corpus).

The mechanics ship HERE; the LLM judging itself happens in the skill layer (Step 8),
which supplies real commit payloads at calibration time. Tests supply synthetic ones.

The gate (ABORT semantics — D7; thresholds are never loosened because a run misses
them):

- pre-gate: parse-fail rate > 5% across all calibration votes ABORTS **before** the
  score assertions (:class:`CalibrationParseFailure` — a judge/parser combination
  failing 1-in-20 of its own outputs is broken independent of what it scores);
- ``composite(good) >= 65`` and ``composite(garbage) <= 35`` and ``margin >= 40``
  and the SHAPE check (mean evidence-backed vote share across good's choices
  ``>= 0.6``; mean unsupported+contradicted share across garbage's ``>= 0.6``).
  All four are evaluated and ALL failures reported; any failure means no fingerprint
  cache is written and no real review may run.

Shape-gate semantics (D-decision, iteration 2): plan §4.5's ``evidence_backed_
fraction`` is deliberately implemented as the **mean per-choice evidence-backed VOTE
SHARE** — ``mean(count(evidence-backed votes) / k per choice)`` — NOT the equally
readable alternative "fraction of choices whose MAJORITY classification is
evidence-backed". The mean-vote-share reading is continuous (every judge vote moves
it), strictly harder to game (5/5 choices can be majority-evidence-backed at 3-of-7
votes each and still FAIL at mean 3/7 ≈ 0.43 < 0.6 — a weak-agreement profile the
majority-fraction reading would score a perfect 1.0), and consistent with §4.4
storing per-vote shares as the audit trail. A test pins the divergence case as
intended behavior.

Fingerprint cache (``data/calibration-fingerprint.json`` — gitignored with the rest
of ``data/``): recalibration is not a per-session tax. The cache stores five
fingerprints — **A** sha256 over the ``prompts/`` template files (sorted filenames,
names + contents concatenated), **B** the caller-supplied resolved model id, **C**
the corpus fingerprint (``citations`` row count + max id), **D** the schema
``PRAGMA user_version``, **E** sha256 over the two frozen anchor fixtures'
LF-normalized content (an anchor edit after a PASS must invalidate the cache) —
plus the gate results. ``review open`` hard-refuses when any of A-E mismatches the
current values; age > 30 days is an ADVISORY ceiling — a distinct refusal the
operator can override with ``--accept-aged`` (A-E mismatches can never be
overridden). Every accepted-aged use is durably stamped onto the cache
(``accepted_aged_at`` + the age) and surfaced in ``review open``'s JSON, so a later
audit can tell overridden runs from fresh-gate runs. Cache writes are atomic
(tmp sibling + ``os.replace``).

Poisoning guard: calibration NEVER writes the real DB. The throwaway is built with
the sqlite3 BACKUP API through a ``query_only`` source connection — an atomic,
WAL-safe snapshot (a checkpoint or concurrent writer landing mid-copy can never
produce a torn throwaway, unlike a file-bytes copy of ``.db`` + ``-wal``) — and
fingerprints C/D are read from the throwaway **before** any anchor write lands on
it (a consistent snapshot ⇒ identical values). A test asserts the real DB is
byte-identical after a calibration run, including a failing one. Default throwaway
dirs are temp-created AND removed on every exit path (:func:`throwaway_db_session`);
a caller-supplied ``--throwaway-dir`` is caller-owned and survives for inspection.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from citation_needed import db, discover, review
from citation_needed.models import RuleDetails

# ---------------------------------------------------------------------------
# Constants — thresholds are the plan §4.5 contract, never loosened (D7)
# ---------------------------------------------------------------------------

CALIBRATION_FIXTURES_DIR = db.PROJECT_ROOT / "fixtures" / "calibration"
GOOD_ANCHOR_FILENAME = "good-anchor.code-quality.frozen.md"
GARBAGE_ANCHOR_FILENAME = "garbage-anchor.SYNTHETIC.md"
EXPECTED_LABELS_FILENAME = "expected-labels.json"
PROMPTS_DIR = db.PROJECT_ROOT / "prompts"
FINGERPRINT_FILENAME = "calibration-fingerprint.json"

#: Stored artifact paths the anchors register under on the THROWAWAY DB only —
#: stable identifiers independent of where the repo is checked out.
GOOD_ANCHOR_ARTIFACT_PATH = "fixtures/calibration/good-anchor.code-quality.frozen.md"
GARBAGE_ANCHOR_ARTIFACT_PATH = "fixtures/calibration/garbage-anchor.SYNTHETIC.md"

GOOD_COMPOSITE_FLOOR = 65.0
GARBAGE_COMPOSITE_CEILING = 35.0
MARGIN_FLOOR = 40.0
SHAPE_FRACTION_FLOOR = 0.6
PARSE_FAIL_RATE_CEILING = 0.05
STALENESS_CEILING_DAYS = 30.0


class CalibrationError(RuntimeError):
    """A calibration contract violation — the CLI reports it as ``error: ...``."""


class CalibrationParseFailure(CalibrationError):
    """Parse-fail rate exceeded 5% across the calibration votes — ABORT before the
    score assertions (distinct from a gate failure; CLI exit code 2)."""

    def __init__(self, rate: float, total_votes: int) -> None:
        self.rate = rate
        self.total_votes = total_votes
        super().__init__(
            f"parse-fail ABORT: {rate:.1%} of {total_votes} calibration votes failed to "
            f"parse (ceiling {PARSE_FAIL_RATE_CEILING:.0%}) — the judge/parser combination "
            "is broken independent of what it scores; force-scored zeros would make the "
            "gate look like it passed for the wrong reason. No assertions were evaluated, "
            "nothing was cached."
        )


def _utc_now() -> datetime:
    """Injectable clock seam — tests monkeypatch this to freeze time."""
    return datetime.now(UTC)


def _format_ts(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Fingerprints A-D
# ---------------------------------------------------------------------------


def prompts_fingerprint(prompts_dir: Path = PROMPTS_DIR) -> str:
    """Fingerprint **A**: sha256 over the prompt template files, sorted by filename,
    filenames + contents concatenated (a rename alone also invalidates — the skill
    layer loads templates by name). Fails LOUD on an empty/missing prompts dir —
    hashing nothing would silently bless a calibration that covered no prompts
    (measurement-validity: fail loud on fallback config)."""
    files = sorted(prompts_dir.glob("*.md"))
    if not files:
        raise CalibrationError(
            f"no prompt templates found under {prompts_dir.as_posix()} — refusing to "
            "fingerprint an empty prompts dir (fingerprint A would bless nothing)"
        )
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(file.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def corpus_fingerprint(conn: sqlite3.Connection) -> str:
    """Fingerprint **C**: ``<citations row count>:<max citation id>`` — the corpus
    compounds across reviews, and corpus-first lookup can find different hits for the
    same anchor choices as it grows (score-validity.md §6 trigger C)."""
    row = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM citations").fetchone()
    return f"{int(row[0])}:{int(row[1])}"


def schema_version(conn: sqlite3.Connection) -> int:
    """Fingerprint **D**: ``PRAGMA user_version`` (score-validity.md §6 trigger D)."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def anchor_content_sha256(path: Path) -> str:
    """Frozen-anchor content hash — same normalization policy as ``cite scan``
    (``discover._read``: decoded text, CRLF/CR -> LF, BOM stripped), reused rather
    than re-declared so the two hash policies cannot drift (code-quality.md § one
    source of truth). A checkout under a different ``core.autocrlf`` must not read
    as an anchor edit."""
    read = discover._read(path)  # deliberate reuse of the ONE hash policy
    if read is None:
        raise CalibrationError(f"calibration anchor is missing or unreadable: {path.as_posix()}")
    return read[0]


def anchors_fingerprint(fixtures_dir: Path = CALIBRATION_FIXTURES_DIR) -> str:
    """Fingerprint **E**: sha256 over BOTH frozen anchor fixtures' content, via
    :func:`anchor_content_sha256` (LF-normalized — a CRLF checkout must not read as
    an anchor edit, same policy as ``cite scan``'s hash). Editing either anchor
    after a PASS invalidates the cache: the gate's demonstrated good/garbage
    separation was measured against content no longer on disk (the pytest-time
    byte-freeze assertion alone is not a runtime control)."""
    digest = hashlib.sha256()
    for name in (GOOD_ANCHOR_FILENAME, GARBAGE_ANCHOR_FILENAME):
        digest.update(name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(anchor_content_sha256(fixtures_dir / name).encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()


def fingerprint_path(db_path: Path) -> Path:
    """The cache lives NEXT TO the DB it calibrates (``data/calibration-fingerprint
    .json`` for the default DB) — a test DB gets its own sibling cache."""
    return db_path.parent / FINGERPRINT_FILENAME


# ---------------------------------------------------------------------------
# Cache file models (pydantic; extra="forbid" — a malformed cache is loud)
# ---------------------------------------------------------------------------


class _CacheBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GateResults(_CacheBase):
    """The gate numbers a passed calibration ran at — stored for the audit trail."""

    composite_good: float
    composite_garbage: float
    margin: float
    good_evidence_share: float
    garbage_negative_share: float
    parse_fail_rate: float
    passed: bool


class CalibrationFingerprint(_CacheBase):
    """The ``calibration-fingerprint.json`` cache contents (fields A-E + results).

    ``accepted_aged_at`` / ``accepted_aged_age_days`` are the durable audit stamp of
    the LATEST ``--accept-aged`` use against this cache (``None`` until the override
    is ever exercised) — without it, runs opened under a stale-calibration override
    would be indistinguishable from runs under a fresh gate pass.
    """

    version: int = 2  # v2 added fingerprint E + the accepted-aged audit stamp
    prompts_sha256: str  # A — prompt template hash
    model_id: str  # B — resolved model id (caller-supplied string)
    corpus_fingerprint: str  # C — "<citations count>:<max id>"
    schema_user_version: int  # D — PRAGMA user_version
    anchors_sha256: str  # E — frozen anchor fixtures content hash (LF-normalized)
    computed_at: str  # ISO 8601 UTC
    accepted_aged_at: str | None = None  # ISO 8601 UTC of the latest --accept-aged use
    accepted_aged_age_days: float | None = None  # the cache age at that use
    gate: GateResults


def load_fingerprint(path: Path) -> CalibrationFingerprint | None:
    """``None`` when no cache file exists; raises :class:`CalibrationError` on a
    file that exists but cannot be parsed/validated (a corrupt cache must read as
    LOUDLY invalid, never as silently absent-and-ignorable)."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationError(
            f"calibration fingerprint cache at {path.as_posix()} is unreadable: {exc}"
        ) from exc
    try:
        return CalibrationFingerprint.model_validate(data)
    except ValidationError as exc:
        raise CalibrationError(
            f"calibration fingerprint cache at {path.as_posix()} does not match the "
            f"expected shape: {exc}"
        ) from exc


def write_fingerprint(path: Path, fingerprint: CalibrationFingerprint) -> None:
    """ATOMIC cache write: serialize to a tmp sibling, then ``os.replace``.

    A bare ``write_text`` truncates-then-writes, so a concurrent reader (or a second
    ``cite calibrate commit`` racing this one) could observe a torn file. The tmp
    name carries the pid so two racing processes never share a tmp sibling; the
    ``os.replace`` is atomic on both POSIX and Windows, so readers only ever see a
    complete old or complete new cache (last-writer-wins, never torn)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(fingerprint.model_dump(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# check_calibration — the review-open gatekeeper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationCheck:
    """Outcome of a validity check against the current A-D values."""

    valid: bool
    aged: bool  # past the 30-day advisory ceiling (overridable, unlike A-D)
    age_days: float | None
    reasons: list[str] = field(default_factory=list)
    fingerprint: CalibrationFingerprint | None = None
    fingerprint_file: Path | None = None


def _db_file(conn: sqlite3.Connection) -> Path | None:
    for _seq, name, file in conn.execute("PRAGMA database_list").fetchall():
        if name == "main" and file:
            return Path(str(file))
    return None


def check_calibration(
    conn: sqlite3.Connection,
    *,
    model_id: str | None = None,
    prompts_dir: Path = PROMPTS_DIR,
    fixtures_dir: Path = CALIBRATION_FIXTURES_DIR,
    accept_aged: bool = False,
    now: datetime | None = None,
) -> CalibrationCheck:
    """Compare the cached fingerprint against the CURRENT values of A-E.

    ``model_id`` is the resolved model about to be used (``review open`` passes its
    ``reviewer_model``); ``None`` skips the B comparison (the bare ``cite calibrate
    check`` verb has no model at hand — the CLI never calls an LLM). Age past the
    30-day ceiling is ADVISORY: refused with a distinct message unless
    ``accept_aged`` (A-E mismatches are hard refusals with no override).

    When an aged-but-otherwise-valid calibration is ACCEPTED via ``accept_aged``,
    the use is durably stamped onto the cache file (``accepted_aged_at`` + the age
    in days) before returning — the override must leave an audit trail, never pass
    silently. A stamp that cannot be written refuses loudly (the override does not
    proceed unrecorded).
    """
    moment = now if now is not None else _utc_now()
    reasons: list[str] = []
    aged = False
    age_days: float | None = None

    db_file = _db_file(conn)
    if db_file is None:
        return CalibrationCheck(
            valid=False,
            aged=False,
            age_days=None,
            reasons=[
                "database has no file path (in-memory?) — no calibration fingerprint "
                "cache can exist for it"
            ],
        )
    cache_file = fingerprint_path(db_file)
    try:
        cached = load_fingerprint(cache_file)
    except CalibrationError as exc:
        return CalibrationCheck(
            valid=False,
            aged=False,
            age_days=None,
            reasons=[str(exc)],
            fingerprint_file=cache_file,
        )
    if cached is None:
        return CalibrationCheck(
            valid=False,
            aged=False,
            age_days=None,
            reasons=[
                f"no calibration fingerprint cache at {cache_file.as_posix()} — the "
                "anchor gate has never passed for this DB"
            ],
            fingerprint_file=cache_file,
        )

    if not cached.gate.passed:
        reasons.append(
            "cached calibration did not pass its gate — a failed run must never be "
            "cached; re-run `cite calibrate commit`"
        )
    current_a = prompts_fingerprint(prompts_dir)
    if cached.prompts_sha256 != current_a:
        reasons.append(
            f"fingerprint A mismatch: prompt templates changed since calibration "
            f"(cached {cached.prompts_sha256[:12]}…, current {current_a[:12]}…)"
        )
    if model_id is not None and cached.model_id != model_id:
        reasons.append(
            f"fingerprint B mismatch: calibrated for model {cached.model_id!r}, "
            f"current model is {model_id!r}"
        )
    current_c = corpus_fingerprint(conn)
    if cached.corpus_fingerprint != current_c:
        reasons.append(
            f"fingerprint C mismatch: the citations corpus changed since calibration "
            f"(cached {cached.corpus_fingerprint}, current {current_c})"
        )
    current_d = schema_version(conn)
    if cached.schema_user_version != current_d:
        reasons.append(
            f"fingerprint D mismatch: schema user_version changed since calibration "
            f"(cached {cached.schema_user_version}, current {current_d})"
        )
    current_e = anchors_fingerprint(fixtures_dir)
    if cached.anchors_sha256 != current_e:
        reasons.append(
            f"fingerprint E mismatch: the frozen anchor fixtures changed since "
            f"calibration (cached {cached.anchors_sha256[:12]}…, current "
            f"{current_e[:12]}…) — a re-freeze requires re-running the gate"
        )
    try:
        computed_at = datetime.strptime(cached.computed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
    except ValueError:
        reasons.append(f"cached computed_at is not ISO 8601 UTC: {cached.computed_at!r}")
    else:
        age_days = (moment - computed_at).total_seconds() / 86400.0
        if age_days > STALENESS_CEILING_DAYS:
            aged = True
            if not accept_aged:
                reasons.append(
                    f"advisory ceiling: calibration is {age_days:.1f} days old "
                    f"(> {STALENESS_CEILING_DAYS:.0f}) — re-run the gate, or override "
                    "deliberately with --accept-aged"
                )
    valid = not reasons
    if valid and aged and accept_aged and age_days is not None:
        # The override actually did the work — stamp the durable audit record
        # (latest use wins; every overwrite still records that an override happened).
        cached = cached.model_copy(
            update={
                "accepted_aged_at": _format_ts(moment),
                "accepted_aged_age_days": round(age_days, 2),
            }
        )
        try:
            write_fingerprint(cache_file, cached)
        except OSError as exc:
            raise CalibrationError(
                f"--accept-aged use could not be recorded on {cache_file.as_posix()}: "
                f"{exc} — refusing to proceed with an unauditable override"
            ) from exc
    return CalibrationCheck(
        valid=valid,
        aged=aged,
        age_days=age_days,
        reasons=reasons,
        fingerprint=cached,
        fingerprint_file=cache_file,
    )


# ---------------------------------------------------------------------------
# Expected labels (machine-readable; the gate context + Step 9 read this)
# ---------------------------------------------------------------------------


class _ExpectedBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedVerification(_ExpectedBase):
    """The verification record that CLOSED a choice's expected label.

    ``external`` requires a real, resolvable citation (DOI or URL + title + the
    contradicting/supporting finding); ``absence`` requires the documented search
    (exact queries + the zero-relevant-results note). Enforced by the validator —
    a placeholder expected-value is structurally impossible, not just discouraged.
    """

    kind: Literal["external", "absence"]
    doi: str | None = None
    url: str | None = None
    title: str | None = None
    finding: str | None = None
    queries: list[str] = []
    note: str | None = None
    verified: str  # when/how (e.g. "2026-07-21 live Crossref + S2 via resolve.py")

    @model_validator(mode="after")
    def _check_shape(self) -> ExpectedVerification:
        if self.kind == "external":
            if not (self.doi or self.url):
                raise ValueError("an external verification requires a locator (doi or url)")
            if not self.title or not self.finding:
                raise ValueError("an external verification requires title + finding")
        else:
            if not self.queries:
                raise ValueError("a documented absence requires the exact queries run")
            if not self.note:
                raise ValueError("a documented absence requires the zero-relevant-results note")
        return self


class ExpectedChoice(_ExpectedBase):
    choice_key: str
    section: str
    expected_label: review.DimensionLabel
    alternates: list[review.DimensionLabel] = []
    verification: ExpectedVerification


class AnchorExpectation(_ExpectedBase):
    file: str
    sha256: str  # anchor_content_sha256 of the frozen fixture — re-freeze = one edit
    choices: list[ExpectedChoice]


class ExpectedLabels(_ExpectedBase):
    version: int = 1
    frozen_at: str
    good_anchor: AnchorExpectation
    garbage_anchor: AnchorExpectation


def load_expected_labels(fixtures_dir: Path = CALIBRATION_FIXTURES_DIR) -> ExpectedLabels:
    path = fixtures_dir / EXPECTED_LABELS_FILENAME
    if not path.is_file():
        raise CalibrationError(f"expected-labels file is missing: {path.as_posix()}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise CalibrationError(f"expected-labels file is not valid JSON: {exc}") from exc
    try:
        return ExpectedLabels.model_validate(data)
    except ValidationError as exc:
        raise CalibrationError(f"expected-labels file failed validation: {exc}") from exc


# ---------------------------------------------------------------------------
# Throwaway DB (the poisoning guard)
# ---------------------------------------------------------------------------


def make_throwaway_db(real_db_path: Path, throwaway_dir: Path) -> Path:
    """Consistent-snapshot throwaway via the sqlite3 BACKUP API.

    ``src.backup(dest)`` takes a coherent read snapshot of the WHOLE logical
    database — main file plus any un-checkpointed WAL pages — atomically. A
    two-step file-bytes copy (``.db`` then ``-wal``) had a race window: a
    checkpoint landing between the two copies (``cite status`` issues one; a
    concurrent writer's last connection closing triggers one) silently dropped
    committed rows from the throwaway. The backup API has no such window.

    The source connection is opened ``query_only`` — the real DB is never WRITTEN
    by calibration (the byte-identity poisoning test asserts exactly that); the
    caller-visible contract is read-only access, structurally enforced. No real DB
    yet -> a fresh schema.sql init.

    Lifecycle note: callers inside calibrate go through
    :func:`throwaway_db_session`, which owns temp-dir cleanup; direct callers own
    ``throwaway_dir`` themselves.
    """
    throwaway_dir.mkdir(parents=True, exist_ok=True)
    target = throwaway_dir / "calibration-throwaway.db"
    for stale in (target, Path(f"{target}-wal"), Path(f"{target}-shm")):
        stale.unlink(missing_ok=True)
    if real_db_path.is_file():
        src = sqlite3.connect(str(real_db_path))
        try:
            src.execute("PRAGMA query_only = ON")  # structural never-write guarantee
            src.execute("PRAGMA busy_timeout = 5000")
            dest = sqlite3.connect(str(target))
            try:
                src.backup(dest)
            finally:
                dest.close()
        finally:
            src.close()
    else:
        db.init_db(target)
    return target


@contextmanager
def throwaway_db_session(real_db_path: Path, throwaway_dir: Path | None = None) -> Iterator[Path]:
    """The ONE owner of the throwaway lifecycle — yields the throwaway DB path.

    ``throwaway_dir=None`` (the CLI default) creates a private temp dir and ALWAYS
    removes it on exit — pass, gate-fail, and any exception path — so calibration
    never leaves full byte-copies of the compounding corpus accumulating under the
    OS temp directory. A caller-supplied dir is CALLER-OWNED: created if needed,
    never removed (tests and operators inspect it afterwards).
    """
    if throwaway_dir is not None:
        yield make_throwaway_db(real_db_path, throwaway_dir)
        return
    directory = Path(tempfile.mkdtemp(prefix="citation-needed-calibration-"))
    try:
        yield make_throwaway_db(real_db_path, directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        if directory.exists():
            print(
                f"note: throwaway calibration dir could not be fully removed: "
                f"{directory} — it holds a full copy of the corpus DB; remove it "
                "manually",
                file=sys.stderr,
            )


def _register_anchor(conn: sqlite3.Connection, fixture_path: Path, artifact_path: str) -> None:
    """Scan mechanics for one anchor: hash + typed upsert through the production
    ``discover.upsert_artifact`` path (never a hand-rolled INSERT)."""
    artifact = discover.DiscoveredArtifact(
        path=artifact_path,
        artifact_type="rule",
        project="calibration",
        content_hash=anchor_content_sha256(fixture_path),
        details=RuleDetails(),
        abs_path=fixture_path,
    )
    discover.upsert_artifact(conn, artifact)


# ---------------------------------------------------------------------------
# open_calibration — context for the skill layer (calibrate open)
# ---------------------------------------------------------------------------


def open_calibration(
    db_path: Path,
    *,
    reviewer_model: str,
    fixtures_dir: Path = CALIBRATION_FIXTURES_DIR,
    throwaway_dir: Path | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Build the JSON-ready calibration context: a throwaway DB with both anchors
    registered and two OPEN review runs, plus the expected labels.

    Advisory context for the skill layer's judging pass — ``cite calibrate commit``
    is deliberately self-contained/stateless (it builds its OWN fresh throwaway from
    the payloads), so nothing here needs to survive to commit time.
    """
    expected = load_expected_labels(fixtures_dir)
    good_fixture = fixtures_dir / GOOD_ANCHOR_FILENAME
    garbage_fixture = fixtures_dir / GARBAGE_ANCHOR_FILENAME
    ws_root = workspace_root if workspace_root is not None else discover.default_workspace_root()
    with throwaway_db_session(db_path, throwaway_dir) as throwaway:
        conn = db.connect(throwaway)
        try:
            with conn:
                _register_anchor(conn, good_fixture, GOOD_ANCHOR_ARTIFACT_PATH)
                _register_anchor(conn, garbage_fixture, GARBAGE_ANCHOR_ARTIFACT_PATH)
                # calibration_check=False: these ARE the calibration anchor runs on the
                # throwaway — the gate cannot require itself before it has ever passed.
                good_open = review.open_review(
                    conn,
                    GOOD_ANCHOR_ARTIFACT_PATH,
                    reviewer_model=reviewer_model,
                    workspace_root=ws_root,
                    calibration_check=False,
                )
                garbage_open = review.open_review(
                    conn,
                    GARBAGE_ANCHOR_ARTIFACT_PATH,
                    reviewer_model=reviewer_model,
                    workspace_root=ws_root,
                    calibration_check=False,
                )
        finally:
            conn.close()
    return {
        "throwaway_db": throwaway.as_posix(),
        # False = the default temp dir was already removed (commit is stateless and
        # rebuilds its own; everything the skill layer needs is IN this JSON).
        "throwaway_retained": throwaway_dir is not None,
        "fingerprint_file": fingerprint_path(db_path).as_posix(),
        "anchors": {
            "good": {
                "fixture": good_fixture.as_posix(),
                "open": good_open.model_dump(),
            },
            "garbage": {
                "fixture": garbage_fixture.as_posix(),
                "open": garbage_open.model_dump(),
            },
        },
        "expected_labels": expected.model_dump(),
    }


# ---------------------------------------------------------------------------
# run_calibration — the gate itself (calibrate commit)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateAssertion:
    """One of the four gate assertions, evaluated regardless of the others."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class CalibrationResult:
    """Everything one calibration run produced. ``passed`` False = ABORT: the
    fingerprint cache was not written and no real review may run.

    ``throwaway_db`` is the path the run used; when no ``--throwaway-dir`` was
    supplied it points into a temp dir that has ALREADY been removed by
    :func:`throwaway_db_session` (reported for the audit trail, not for reopening).
    """

    passed: bool
    parse_fail_rate: float
    composite_good: float
    composite_garbage: float
    margin: float
    good_evidence_share: float
    garbage_negative_share: float
    assertions: list[GateAssertion]
    throwaway_db: Path
    good_run_id: int
    garbage_run_id: int
    fingerprint_file: Path
    fingerprint_written: bool

    @property
    def failures(self) -> list[str]:
        return [a.detail for a in self.assertions if not a.passed]


def _all_votes(payload: review.CommitPayload) -> list[str]:
    votes: list[str] = []
    for choice in payload.choices:
        votes.extend(choice.votes)
    return votes


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def run_calibration(
    db_path: Path,
    good_payload: review.CommitPayload | dict[str, Any],
    garbage_payload: review.CommitPayload | dict[str, Any],
    *,
    model_id: str,
    fixtures_dir: Path = CALIBRATION_FIXTURES_DIR,
    prompts_dir: Path = PROMPTS_DIR,
    throwaway_dir: Path | None = None,
    workspace_root: Path | None = None,
    memory_root: Path | None = None,
    now: datetime | None = None,
) -> CalibrationResult:
    """Run both anchors through the production scan+open+commit mechanics on a
    throwaway DB, evaluate the gate, and cache the fingerprint on PASS only.

    Raises :class:`CalibrationParseFailure` when the parse-fail pre-gate trips
    (BEFORE any DB mechanics — the score assertions are never evaluated) and
    :class:`CalibrationError` / :class:`review.ReviewError` on mechanics failures.
    A gate failure is NOT an exception: the result carries every failed assertion
    (all four are evaluated) with ``passed=False`` and nothing cached.
    """
    if not model_id or not model_id.strip():
        raise CalibrationError("model_id must be non-empty (calibration fingerprint B)")
    if isinstance(good_payload, dict):
        good_payload = review.CommitPayload.model_validate(good_payload)
    if isinstance(garbage_payload, dict):
        garbage_payload = review.CommitPayload.model_validate(garbage_payload)

    # Pre-gate: parse-fail rate across ALL calibration votes, before any mechanics.
    votes = _all_votes(good_payload) + _all_votes(garbage_payload)
    parse_fails = sum(1 for vote in votes if vote == review.PARSE_FAILED_LABEL)
    parse_fail_rate = parse_fails / len(votes)
    if parse_fail_rate > PARSE_FAIL_RATE_CEILING:
        raise CalibrationParseFailure(parse_fail_rate, len(votes))

    good_fixture = fixtures_dir / GOOD_ANCHOR_FILENAME
    garbage_fixture = fixtures_dir / GARBAGE_ANCHOR_FILENAME
    ws_root = workspace_root if workspace_root is not None else discover.default_workspace_root()

    with throwaway_db_session(db_path, throwaway_dir) as throwaway:
        conn = db.connect(throwaway)
        try:
            # C/D come from the throwaway SNAPSHOT before any anchor write lands on
            # it — a consistent backup of the real DB at copy time, so the cached
            # values match what `review open` will later read from the real DB,
            # without calibration ever writing the real file (the poisoning guard).
            fp_corpus = corpus_fingerprint(conn)
            fp_schema = schema_version(conn)
            with conn:
                _register_anchor(conn, good_fixture, GOOD_ANCHOR_ARTIFACT_PATH)
                _register_anchor(conn, garbage_fixture, GARBAGE_ANCHOR_ARTIFACT_PATH)
                good_open = review.open_review(
                    conn,
                    GOOD_ANCHOR_ARTIFACT_PATH,
                    reviewer_model=model_id,
                    workspace_root=ws_root,
                    calibration_check=False,  # the anchor runs ARE the calibration
                )
                garbage_open = review.open_review(
                    conn,
                    GARBAGE_ANCHOR_ARTIFACT_PATH,
                    reviewer_model=model_id,
                    workspace_root=ws_root,
                    calibration_check=False,
                )
            good_result = review.commit_review(
                conn,
                good_open.run_id,
                good_payload,
                workspace_root=ws_root,
                memory_root=memory_root,
            )
            garbage_result = review.commit_review(
                conn,
                garbage_open.run_id,
                garbage_payload,
                workspace_root=ws_root,
                memory_root=memory_root,
            )
        finally:
            conn.close()

    composite_good = good_result.composite
    composite_garbage = garbage_result.composite
    margin = composite_good - composite_garbage
    # D-decision: mean per-choice VOTE SHARE, not fraction-of-choices-with-an-
    # evidence-backed-majority — see the module docstring (shape-gate semantics).
    good_evidence_share = _mean(
        [choice.tally.evidence_backed_share for choice in good_result.choices]
    )
    garbage_negative_share = _mean(
        [
            choice.tally.unsupported_share + choice.tally.contradicted_share
            for choice in garbage_result.choices
        ]
    )

    assertions = [
        GateAssertion(
            name="good-floor",
            passed=composite_good >= GOOD_COMPOSITE_FLOOR,
            detail=(f"composite(good) = {composite_good:.1f} (floor {GOOD_COMPOSITE_FLOOR:.0f})"),
        ),
        GateAssertion(
            name="garbage-ceiling",
            passed=composite_garbage <= GARBAGE_COMPOSITE_CEILING,
            detail=(
                f"composite(garbage) = {composite_garbage:.1f} "
                f"(ceiling {GARBAGE_COMPOSITE_CEILING:.0f})"
            ),
        ),
        GateAssertion(
            name="margin",
            passed=margin >= MARGIN_FLOOR,
            detail=f"margin = {margin:.1f} (floor {MARGIN_FLOOR:.0f})",
        ),
        GateAssertion(
            name="shape",
            passed=(
                good_evidence_share >= SHAPE_FRACTION_FLOOR
                and garbage_negative_share >= SHAPE_FRACTION_FLOOR
            ),
            detail=(
                f"shape: evidence-backed share(good) = {good_evidence_share:.2f}, "
                f"unsupported+contradicted share(garbage) = {garbage_negative_share:.2f} "
                f"(floors {SHAPE_FRACTION_FLOOR})"
            ),
        ),
    ]
    passed = all(a.passed for a in assertions)

    cache_file = fingerprint_path(db_path)
    fingerprint_written = False
    if passed:
        fingerprint = CalibrationFingerprint(
            prompts_sha256=prompts_fingerprint(prompts_dir),
            model_id=model_id.strip(),
            corpus_fingerprint=fp_corpus,
            schema_user_version=fp_schema,
            anchors_sha256=anchors_fingerprint(fixtures_dir),
            computed_at=_format_ts(now if now is not None else _utc_now()),
            gate=GateResults(
                composite_good=composite_good,
                composite_garbage=composite_garbage,
                margin=margin,
                good_evidence_share=good_evidence_share,
                garbage_negative_share=garbage_negative_share,
                parse_fail_rate=parse_fail_rate,
                passed=True,
            ),
        )
        write_fingerprint(cache_file, fingerprint)  # atomic: tmp sibling + os.replace
        fingerprint_written = True

    return CalibrationResult(
        passed=passed,
        parse_fail_rate=parse_fail_rate,
        composite_good=composite_good,
        composite_garbage=composite_garbage,
        margin=margin,
        good_evidence_share=good_evidence_share,
        garbage_negative_share=garbage_negative_share,
        assertions=assertions,
        throwaway_db=throwaway,
        good_run_id=good_open.run_id,
        garbage_run_id=garbage_open.run_id,
        fingerprint_file=cache_file,
        fingerprint_written=fingerprint_written,
    )


__all__ = [
    "CALIBRATION_FIXTURES_DIR",
    "EXPECTED_LABELS_FILENAME",
    "FINGERPRINT_FILENAME",
    "GARBAGE_ANCHOR_ARTIFACT_PATH",
    "GARBAGE_ANCHOR_FILENAME",
    "GARBAGE_COMPOSITE_CEILING",
    "GOOD_ANCHOR_ARTIFACT_PATH",
    "GOOD_ANCHOR_FILENAME",
    "GOOD_COMPOSITE_FLOOR",
    "MARGIN_FLOOR",
    "PARSE_FAIL_RATE_CEILING",
    "PROMPTS_DIR",
    "SHAPE_FRACTION_FLOOR",
    "STALENESS_CEILING_DAYS",
    "AnchorExpectation",
    "CalibrationCheck",
    "CalibrationError",
    "CalibrationFingerprint",
    "CalibrationParseFailure",
    "CalibrationResult",
    "ExpectedChoice",
    "ExpectedLabels",
    "ExpectedVerification",
    "GateAssertion",
    "GateResults",
    "anchor_content_sha256",
    "anchors_fingerprint",
    "check_calibration",
    "corpus_fingerprint",
    "fingerprint_path",
    "load_expected_labels",
    "load_fingerprint",
    "make_throwaway_db",
    "open_calibration",
    "prompts_fingerprint",
    "run_calibration",
    "schema_version",
    "throwaway_db_session",
    "write_fingerprint",
]
