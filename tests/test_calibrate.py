"""Step-5 calibration gate tests.

Four families, mirroring the plan §4.5 contract:

1. **Gate math** — the 4 assertions at their exact edges (composite(good) exactly 65
   passes / below fails; margin exactly 40; shape fraction exactly 0.6), the parse-fail
   5% boundary, and the RED-ON-GARBAGE SELF-TEST: a deliberately broken scorer (inverted
   anchor labels) MUST fail the gate — this is the gate's own calibration
   (measurement-validity: a bench that can't fail garbage can't pick winners).
2. **Fingerprint cache** — per-component invalidation (A/B/C/D independently), the
   30-day advisory ceiling with an injectable clock, and loud invalidity on a corrupt
   cache.
3. **Review-open hard refusal** — no cache / stale cache refuse loudly through the
   production CLI; a valid cache opens; ``--accept-aged`` overrides ONLY age.
4. **Poisoning guard + CLI integration + fixture integrity** — the real DB file is
   byte-identical after passing AND failing calibration runs; the three CLI verbs; the
   frozen anchors carry their banner, zero UNVERIFIED placeholders, valid
   expected-labels, and byte-frozen hashes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from citation_needed import calibrate, db, review
from citation_needed.cli import main
from conftest import _stdin_bytes, write_valid_calibration_fingerprint

MODEL = "claude-sonnet-5"

# ---------------------------------------------------------------------------
# Synthetic payload builders (the skill supplies real payloads at calibration
# time; tests supply citation-free synthetic ones — no network by construction)
# ---------------------------------------------------------------------------


def _choice(key: str, votes: list[str]) -> dict[str, Any]:
    return {
        "choice_key": key,
        "summary": f"Synthetic calibration choice {key}.",
        "quote": f"Quote for {key}.",
        "span_start_line": 1,
        "span_end_line": 2,
        "category": "context-economy",
        "votes": votes,
        "literature_searched": True,
        "literature_found": False,
        "search_queries": [f"synthetic query for {key}"],
    }


def _payload(choices: list[dict[str, Any]]) -> dict[str, Any]:
    return {"choices": choices}


def good_expected_payload() -> dict[str, Any]:
    """The expected good-anchor profile: 4/5 evidence-backed, 1/5 interesting-novel
    (the deliberate hard-but-fair internal-only item) -> composite 95, shape 0.8."""
    return _payload(
        [_choice(f"good-{i}", ["evidence-backed"] * 3) for i in range(4)]
        + [_choice("good-4", ["interesting-novel"] * 3)]
    )


def garbage_expected_payload() -> dict[str, Any]:
    """The expected garbage-anchor profile: 5/5 contradicted -> composite 0, shape 1.0."""
    return _payload([_choice(f"garbage-{i}", ["contradicted"] * 3) for i in range(5)])


def _run(
    tmp_path: Path,
    good: dict[str, Any],
    garbage: dict[str, Any],
    db_path: Path | None = None,
) -> calibrate.CalibrationResult:
    return calibrate.run_calibration(
        db_path if db_path is not None else tmp_path / "data" / "citation.db",
        good,
        garbage,
        model_id=MODEL,
        throwaway_dir=tmp_path / "throwaway",
        workspace_root=tmp_path,
    )


def _assertion(result: calibrate.CalibrationResult, name: str) -> calibrate.GateAssertion:
    matches = [a for a in result.assertions if a.name == name]
    assert len(matches) == 1
    return matches[0]


# ---------------------------------------------------------------------------
# 1. Gate math
# ---------------------------------------------------------------------------


def test_gate_passes_on_expected_anchor_profiles(tmp_path: Path) -> None:
    result = _run(tmp_path, good_expected_payload(), garbage_expected_payload())
    assert result.passed
    assert result.composite_good == 95.0
    assert result.composite_garbage == 0.0
    assert result.margin == 95.0
    assert result.good_evidence_share == 0.8
    assert result.garbage_negative_share == 1.0
    assert result.parse_fail_rate == 0.0
    assert all(a.passed for a in result.assertions)
    assert result.fingerprint_written
    cached = calibrate.load_fingerprint(result.fingerprint_file)
    assert cached is not None
    assert cached.model_id == MODEL
    assert cached.gate.passed
    assert cached.gate.composite_good == 95.0
    assert cached.gate.margin == 95.0
    assert cached.anchors_sha256 == calibrate.anchors_fingerprint()  # E is cached
    # The atomic writer leaves no tmp sibling behind (tmp + os.replace).
    leftovers = [p for p in result.fingerprint_file.parent.iterdir() if ".tmp-" in p.name]
    assert leftovers == []


def test_red_on_garbage_self_test_inverted_labels_fail_every_assertion(tmp_path: Path) -> None:
    """The gate's own calibration: a broken scorer that rates garbage evidence-backed
    and good contradicted MUST go red on all four assertions and cache nothing."""
    inverted_good = _payload([_choice(f"good-{i}", ["contradicted"] * 3) for i in range(5)])
    inverted_garbage = _payload(
        [_choice(f"garbage-{i}", ["evidence-backed"] * 3) for i in range(5)]
    )
    result = _run(tmp_path, inverted_good, inverted_garbage)
    assert not result.passed
    assert result.composite_good == 0.0
    assert result.composite_garbage == 100.0
    assert result.margin == -100.0
    assert [a.name for a in result.assertions if not a.passed] == [
        "good-floor",
        "garbage-ceiling",
        "margin",
        "shape",
    ]
    assert len(result.failures) == 4
    assert not result.fingerprint_written
    assert not result.fingerprint_file.exists()


def test_composite_good_exactly_65_passes_the_floor(tmp_path: Path) -> None:
    """Majority labels [e,e,i,u,u] -> composite exactly 65.0; votes are mixed so the
    mean evidence-backed share lands exactly on the 0.6 shape floor (>= passes)."""
    good = _payload(
        [
            _choice("g0", ["evidence-backed"] * 3),
            _choice("g1", ["evidence-backed"] * 3),
            _choice("g2", ["interesting-novel", "interesting-novel", "evidence-backed"]),
            _choice("g3", ["unsupported", "unsupported", "evidence-backed"]),
            _choice("g4", ["unsupported", "unsupported", "evidence-backed"]),
        ]
    )
    result = _run(tmp_path, good, garbage_expected_payload())
    assert result.composite_good == 65.0
    assert result.good_evidence_share == pytest.approx(0.6)
    assert result.passed
    assert result.fingerprint_written


def test_composite_good_below_65_fails_the_floor_only(tmp_path: Path) -> None:
    """Majority labels [e,e,i,u,c] -> composite 60.0 (< 65): the good-floor assertion
    fails; the other three still evaluate (all-failures reporting) and pass."""
    good = _payload(
        [
            _choice("g0", ["evidence-backed"] * 3),
            _choice("g1", ["evidence-backed"] * 3),
            _choice("g2", ["interesting-novel", "interesting-novel", "evidence-backed"]),
            _choice("g3", ["unsupported", "unsupported", "evidence-backed"]),
            _choice("g4", ["contradicted", "contradicted", "evidence-backed"]),
        ]
    )
    result = _run(tmp_path, good, garbage_expected_payload())
    assert result.composite_good == 60.0
    assert not result.passed
    assert not _assertion(result, "good-floor").passed
    assert _assertion(result, "garbage-ceiling").passed
    assert _assertion(result, "margin").passed  # 60 - 0 = 60 >= 40
    assert _assertion(result, "shape").passed
    assert not result.fingerprint_written


def _good_payload_at(composite: int) -> dict[str, Any]:
    """10-choice good payloads at exact composites (75 or 70); mixed-but-unambiguous
    votes keep every mean evidence share at >= 0.6 so only the tested assertion moves."""
    if composite == 75:  # majorities: 6x e, 1x i, 3x u -> sum 5.0 -> 75.0
        tail = [_choice("g6", ["interesting-novel", "interesting-novel", "evidence-backed"])] + [
            _choice(f"g{7 + i}", ["unsupported", "unsupported", "evidence-backed"])
            for i in range(3)
        ]
    else:  # composite == 70; majorities: 6x e, 4x u -> sum 4.0 -> 70.0
        tail = [
            _choice(f"g{6 + i}", ["unsupported", "unsupported", "evidence-backed"])
            for i in range(4)
        ]
    return _payload([_choice(f"g{i}", ["evidence-backed"] * 3) for i in range(6)] + tail)


def _garbage_35_payload() -> dict[str, Any]:
    """Majorities [c,c,i,i,u] -> composite exactly 35.0 (the ceiling edge); the
    interesting choices carry one contradicted vote so the negative share stays >= 0.6."""
    return _payload(
        [
            _choice("b0", ["contradicted"] * 3),
            _choice("b1", ["contradicted"] * 3),
            _choice("b2", ["interesting-novel", "interesting-novel", "contradicted"]),
            _choice("b3", ["interesting-novel", "interesting-novel", "contradicted"]),
            _choice("b4", ["unsupported", "unsupported", "contradicted"]),
        ]
    )


def test_margin_exactly_40_and_garbage_exactly_35_pass(tmp_path: Path) -> None:
    result = _run(tmp_path, _good_payload_at(75), _garbage_35_payload())
    assert result.composite_good == 75.0
    assert result.composite_garbage == 35.0
    assert result.margin == 40.0
    assert result.passed
    assert result.fingerprint_written


def test_margin_below_40_fails_the_margin_assertion_only(tmp_path: Path) -> None:
    """good 70 / garbage 35 -> margin 35 < 40: only the margin assertion fails —
    good-floor (70 >= 65) and garbage-ceiling (35 <= 35) both pass, proving the
    relative-margin check catches what the absolute bounds alone would bless."""
    result = _run(tmp_path, _good_payload_at(70), _garbage_35_payload())
    assert result.composite_good == 70.0
    assert result.margin == 35.0
    assert not result.passed
    assert _assertion(result, "good-floor").passed
    assert _assertion(result, "garbage-ceiling").passed
    assert not _assertion(result, "margin").passed
    assert _assertion(result, "shape").passed
    assert not result.fingerprint_written


def test_shape_failure_alone_aborts(tmp_path: Path) -> None:
    """Every good choice's majority is evidence-backed (composite 100) but at 3/7
    votes each, the mean evidence share is ~0.43 < 0.6 — the scalar looks perfect
    and the SHAPE check still refuses (checks the classification's structure)."""
    seven = ["evidence-backed"] * 3 + ["unsupported"] * 2 + ["interesting-novel"] * 2
    good = _payload([_choice(f"g{i}", list(seven)) for i in range(5)])
    result = _run(tmp_path, good, garbage_expected_payload())
    assert result.composite_good == 100.0
    assert result.good_evidence_share == pytest.approx(3 / 7)
    assert not result.passed
    assert [a.name for a in result.assertions if not a.passed] == ["shape"]
    assert not result.fingerprint_written


def test_shape_gate_d_decision_mean_vote_share_diverges_from_majority_fraction(
    tmp_path: Path,
) -> None:
    """D-decision pin (iteration 2): the shape gate reads plan §4.5's
    'evidence_backed_fraction' as the MEAN PER-CHOICE VOTE SHARE, not the
    fraction-of-choices-majority-classified reading. Divergence case: every one of
    the 5 good choices majority-classifies evidence-backed (majority-fraction
    reading = 5/5 = 1.0, which would sail past the 0.6 floor) yet each majority is
    a weak 3-of-7 plurality, so the mean vote share is 3/7 ≈ 0.43 and the gate
    FAILS. This is INTENDED: weak judge agreement must not calibrate the scorer.
    Owner of the rationale: calibrate.py module docstring ('Shape-gate semantics')
    + docs/interpretation-guide.md ('Shape-assertion semantics')."""
    seven = ["evidence-backed"] * 3 + ["unsupported"] * 2 + ["interesting-novel"] * 2
    good = _payload([_choice(f"g{i}", list(seven)) for i in range(5)])
    result = _run(tmp_path, good, garbage_expected_payload())
    # Composite 100 == every choice's majority label is evidence-backed, i.e. the
    # REJECTED majority-fraction reading evaluates to 1.0 here and would PASS.
    assert result.composite_good == 100.0
    # The ADOPTED mean-vote-share reading refuses the same profile.
    assert result.good_evidence_share == pytest.approx(3 / 7)
    assert not result.passed
    assert [a.name for a in result.assertions if not a.passed] == ["shape"]
    assert not result.fingerprint_written


def test_garbage_ceiling_failure_alone_aborts(tmp_path: Path) -> None:
    """Isolation mirror of the good-floor/margin pattern for the garbage ceiling:
    composite(garbage) 40.0 (> 35) while good-floor (95), margin (55), and shape
    (0.8 both sides) all still pass — a ceiling-specific regression (flipped
    comparison, wrong constant) can no longer hide behind the all-four-fail
    self-test's garbage=100 profile."""
    garbage = _payload(
        [_choice(f"b{i}", ["evidence-backed"] * 3) for i in range(2)]
        + [_choice(f"b{2 + i}", ["unsupported"] * 3) for i in range(8)]
    )
    result = _run(tmp_path, good_expected_payload(), garbage)
    assert result.composite_garbage == 40.0  # (2*1 - 8*0.5 + 10) * 50 / 10
    assert result.margin == 55.0
    assert result.garbage_negative_share == pytest.approx(0.8)
    assert not result.passed
    assert _assertion(result, "good-floor").passed
    assert not _assertion(result, "garbage-ceiling").passed
    assert _assertion(result, "margin").passed
    assert _assertion(result, "shape").passed
    assert not result.fingerprint_written


def test_garbage_side_shape_failure_alone_aborts(tmp_path: Path) -> None:
    """The shape assertion's RIGHT operand in isolation: every garbage majority is
    still contradicted (composite 0 — ceiling and margin pass) but each choice's
    negative vote share is a weak 4/7 ≈ 0.57 < 0.6, while good's evidence share
    stays 0.8 — so ONLY the garbage side of the shape AND drags it down. A bug
    flipping that operand (`or` for `and`, or comparing the wrong share) goes red
    here and nowhere else in the suite."""
    seven = ["contradicted"] * 4 + ["interesting-novel"] * 3
    garbage = _payload([_choice(f"b{i}", list(seven)) for i in range(5)])
    result = _run(tmp_path, good_expected_payload(), garbage)
    assert result.composite_garbage == 0.0  # every majority is contradicted
    assert result.good_evidence_share == pytest.approx(0.8)  # left operand passes
    assert result.garbage_negative_share == pytest.approx(4 / 7)  # right operand fails
    assert not result.passed
    assert [a.name for a in result.assertions if not a.passed] == ["shape"]
    assert not result.fingerprint_written


def test_parse_fail_rate_at_exactly_5_percent_proceeds(tmp_path: Path) -> None:
    """20 votes, exactly 1 parse-failed = 5.0% — the ceiling is EXCEEDED-only (> 5%),
    so the run proceeds (and the parse-failed vote force-scores contradicted)."""
    good = _payload([_choice(f"g{i}", ["evidence-backed"] * 3) for i in range(3)])
    garbage = _payload(
        [
            _choice("b0", ["contradicted"] * 3),
            _choice("b1", ["contradicted"] * 3),
            _choice("b2", ["contradicted"] * 4 + ["parse-failed"]),
        ]
    )
    result = _run(tmp_path, good, garbage)
    assert result.parse_fail_rate == pytest.approx(0.05)
    assert result.passed


def test_parse_fail_rate_above_5_percent_aborts_before_assertions(tmp_path: Path) -> None:
    good = _payload([_choice(f"g{i}", ["evidence-backed"] * 3) for i in range(3)])
    garbage = _payload(
        [
            _choice("b0", ["contradicted"] * 3),
            _choice("b1", ["contradicted"] * 3),
            _choice("b2", ["contradicted"] * 3 + ["parse-failed"] * 2),
        ]
    )
    with pytest.raises(calibrate.CalibrationParseFailure) as excinfo:
        _run(tmp_path, good, garbage)
    assert excinfo.value.rate == pytest.approx(0.1)
    assert "ABORT" in str(excinfo.value)
    # Nothing was cached, and no throwaway mechanics ran far enough to matter.
    assert not calibrate.fingerprint_path(tmp_path / "data" / "citation.db").exists()


# ---------------------------------------------------------------------------
# 2. Fingerprint cache — per-component invalidation + the advisory ceiling
# ---------------------------------------------------------------------------


@pytest.fixture()
def cal_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """A fresh initialized DB with a currently-valid fingerprint cache."""
    db_path = tmp_path / "data" / "citation.db"
    assert main(["init-db", "--db", str(db_path)]) == 0
    write_valid_calibration_fingerprint(db_path, model_id=MODEL)
    capsys.readouterr()
    return db_path


def _check(
    db_path: Path,
    *,
    model_id: str | None = MODEL,
    accept_aged: bool = False,
    now: datetime | None = None,
    fixtures_dir: Path | None = None,
) -> calibrate.CalibrationCheck:
    conn = db.connect(db_path)
    try:
        kwargs: dict[str, Any] = {}
        if fixtures_dir is not None:
            kwargs["fixtures_dir"] = fixtures_dir
        return calibrate.check_calibration(
            conn, model_id=model_id, accept_aged=accept_aged, now=now, **kwargs
        )
    finally:
        conn.close()


def _edit_cache(db_path: Path, **overrides: Any) -> None:
    cache_file = calibrate.fingerprint_path(db_path)
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    data.update(overrides)
    cache_file.write_text(json.dumps(data), encoding="utf-8")


def test_untouched_cache_is_valid(cal_db: Path) -> None:
    check = _check(cal_db)
    assert check.valid
    assert not check.aged
    assert check.reasons == []


def test_fingerprint_a_prompts_hash_mismatch_invalidates(cal_db: Path) -> None:
    _edit_cache(cal_db, prompts_sha256="0" * 64)
    check = _check(cal_db)
    assert not check.valid
    assert any("fingerprint A" in reason for reason in check.reasons)


def test_fingerprint_b_model_mismatch_invalidates(cal_db: Path) -> None:
    check = _check(cal_db, model_id="some-other-model")
    assert not check.valid
    assert any("fingerprint B" in reason for reason in check.reasons)
    # B is skipped when no model is at hand (the CLI cannot resolve one itself).
    assert _check(cal_db, model_id=None).valid


def test_fingerprint_c_corpus_growth_invalidates(cal_db: Path) -> None:
    """Insert a citation row — the corpus fingerprint drifts from the cached value
    (score-validity §6 trigger C: corpus growth shifts what corpus-first finds)."""
    conn = db.connect(cal_db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO citations (kind, natural_key, title, url_or_doi, verified_at, "
                "resolution_method) VALUES ('external', 'doi:10.1/x', 't', 'doi:10.1/x', "
                "'2026-07-21T00:00:00Z', 'api_structured')"
            )
    finally:
        conn.close()
    check = _check(cal_db)
    assert not check.valid
    assert any("fingerprint C" in reason for reason in check.reasons)


def test_fingerprint_d_schema_version_mismatch_invalidates(cal_db: Path) -> None:
    _edit_cache(cal_db, schema_user_version=99)
    check = _check(cal_db)
    assert not check.valid
    assert any("fingerprint D" in reason for reason in check.reasons)


def test_failed_gate_in_cache_never_counts_as_valid(cal_db: Path) -> None:
    cache_file = calibrate.fingerprint_path(cal_db)
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    data["gate"]["passed"] = False
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    check = _check(cal_db)
    assert not check.valid
    assert any("did not pass" in reason for reason in check.reasons)


def test_corrupt_cache_is_loudly_invalid_not_silently_absent(cal_db: Path) -> None:
    calibrate.fingerprint_path(cal_db).write_text("{not json", encoding="utf-8")
    check = _check(cal_db)
    assert not check.valid
    assert any("unreadable" in reason for reason in check.reasons)


def test_missing_cache_is_invalid_with_actionable_reason(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "citation.db"
    assert main(["init-db", "--db", str(db_path)]) == 0
    check = _check(db_path)
    assert not check.valid
    assert any("never passed" in reason for reason in check.reasons)


def test_advisory_ceiling_30_days_frozen_clock(cal_db: Path) -> None:
    """Age is checked against an injectable clock: exactly 30 days is still valid;
    a day past refuses with the DISTINCT advisory message; accept_aged overrides
    age (and only age)."""
    written = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    write_valid_calibration_fingerprint(cal_db, model_id=MODEL, computed_at=written)
    at_ceiling = _check(cal_db, now=written + timedelta(days=30))
    assert at_ceiling.valid
    assert not at_ceiling.aged

    past = _check(cal_db, now=written + timedelta(days=31))
    assert not past.valid
    assert past.aged
    assert past.age_days == pytest.approx(31.0)
    assert any("advisory ceiling" in reason for reason in past.reasons)

    overridden = _check(cal_db, accept_aged=True, now=written + timedelta(days=31))
    assert overridden.valid
    assert overridden.aged  # still reported, just accepted


def test_accept_aged_never_overrides_a_component_mismatch(cal_db: Path) -> None:
    written = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    write_valid_calibration_fingerprint(cal_db, model_id=MODEL, computed_at=written)
    _edit_cache(cal_db, prompts_sha256="0" * 64)
    check = _check(cal_db, accept_aged=True, now=written + timedelta(days=31))
    assert not check.valid
    assert any("fingerprint A" in reason for reason in check.reasons)


def test_run_calibration_uses_injectable_clock_for_computed_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(calibrate, "_utc_now", lambda: frozen)
    result = _run(tmp_path, good_expected_payload(), garbage_expected_payload())
    cached = calibrate.load_fingerprint(result.fingerprint_file)
    assert cached is not None
    assert cached.computed_at == "2026-07-01T00:00:00Z"


def test_accept_aged_use_is_durably_stamped_on_the_cache(cal_db: Path) -> None:
    """SEC-1 fix: an accepted-aged use writes accepted_aged_at + the age in days
    onto the fingerprint cache — a later audit can tell overridden runs from
    fresh-gate runs. A fresh (non-aged) check never stamps."""
    written = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    write_valid_calibration_fingerprint(cal_db, model_id=MODEL, computed_at=written)
    check = _check(cal_db, accept_aged=True, now=written + timedelta(days=31))
    assert check.valid
    assert check.aged
    stamped = calibrate.load_fingerprint(calibrate.fingerprint_path(cal_db))
    assert stamped is not None
    assert stamped.accepted_aged_at == "2026-08-21T12:00:00Z"
    assert stamped.accepted_aged_age_days == pytest.approx(31.0)
    # The check result carries the stamped fingerprint too (same audit view).
    assert check.fingerprint is not None
    assert check.fingerprint.accepted_aged_at == "2026-08-21T12:00:00Z"

    # Fresh cache, no override at play -> no stamp is ever written.
    write_valid_calibration_fingerprint(cal_db, model_id=MODEL)
    assert _check(cal_db).valid
    fresh = calibrate.load_fingerprint(calibrate.fingerprint_path(cal_db))
    assert fresh is not None
    assert fresh.accepted_aged_at is None
    assert fresh.accepted_aged_age_days is None


def test_fingerprint_e_anchor_fixture_edit_invalidates(cal_db: Path, tmp_path: Path) -> None:
    """SEC-2 fix: editing a frozen anchor fixture after a PASS invalidates the
    cache at RUNTIME (fingerprint E mismatch) — the pytest-time byte-freeze pin is
    not the only control anymore."""
    edited = tmp_path / "edited-fixtures"
    shutil.copytree(calibrate.CALIBRATION_FIXTURES_DIR, edited)
    target = edited / calibrate.GOOD_ANCHOR_FILENAME
    target.write_bytes(target.read_bytes() + b"x")  # a single-byte edit
    check = _check(cal_db, fixtures_dir=edited)
    assert not check.valid
    assert any("fingerprint E" in reason for reason in check.reasons)


def test_fingerprint_e_is_crlf_robust(cal_db: Path, tmp_path: Path) -> None:
    """A CRLF re-checkout of the anchors must NOT read as an anchor edit: the E
    hash goes through anchor_content_sha256's LF normalization (the same ONE hash
    policy as `cite scan`)."""
    crlf = tmp_path / "crlf-fixtures"
    shutil.copytree(calibrate.CALIBRATION_FIXTURES_DIR, crlf)
    for name in (calibrate.GOOD_ANCHOR_FILENAME, calibrate.GARBAGE_ANCHOR_FILENAME):
        path = crlf / name
        raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        path.write_bytes(raw)
    assert calibrate.anchors_fingerprint(crlf) == calibrate.anchors_fingerprint()
    assert _check(cal_db, fixtures_dir=crlf).valid


# ---------------------------------------------------------------------------
# 3. review-open hard refusal (through the production CLI)
# ---------------------------------------------------------------------------


RULE_PATH = ".claude/rules/subagent-economy.md"


def _open_args(ws: dict[str, Path]) -> list[str]:
    return [
        "review",
        "open",
        RULE_PATH,
        "--reviewer-model",
        MODEL,
        "--db",
        str(ws["db"]),
        "--workspace-root",
        str(ws["root"]),
    ]


def test_review_open_refuses_without_any_calibration(
    ws: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    calibrate.fingerprint_path(ws["db"]).unlink()
    assert main(_open_args(ws)) == 1
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "REFUSED" in out
    assert "cite calibrate commit" in out  # actionable, not just loud


def test_review_open_refuses_on_stale_fingerprint(
    ws: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    cache_file = calibrate.fingerprint_path(ws["db"])
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    data["corpus_fingerprint"] = "999:999"
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    assert main(_open_args(ws)) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert "fingerprint C" in out


def test_review_open_passes_with_valid_calibration(
    ws: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    write_valid_calibration_fingerprint(ws["db"], model_id=MODEL)
    assert main(_open_args(ws)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["run_id"] >= 1


def test_review_open_aged_calibration_refuses_unless_accept_aged(
    ws: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    aged = datetime.now(UTC) - timedelta(days=31)
    write_valid_calibration_fingerprint(ws["db"], model_id=MODEL, computed_at=aged)
    assert main(_open_args(ws)) == 1
    out = capsys.readouterr().out
    assert "advisory ceiling" in out

    assert main([*_open_args(ws), "--accept-aged"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["run_id"] >= 1


def test_review_open_accept_aged_surfaces_in_json_and_stamps_cache(
    ws: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """SEC-1 fix, end-to-end through the production CLI: an --accept-aged open
    reports calibration_aged_accepted=true in its JSON AND leaves the durable
    accepted_aged_at stamp on the fingerprint cache; a fresh-calibration open
    reports false and stamps nothing."""
    aged = datetime.now(UTC) - timedelta(days=31, hours=1)
    write_valid_calibration_fingerprint(ws["db"], model_id=MODEL, computed_at=aged)
    assert main([*_open_args(ws), "--accept-aged"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["calibration_aged_accepted"] is True
    stamped = calibrate.load_fingerprint(calibrate.fingerprint_path(ws["db"]))
    assert stamped is not None
    assert stamped.accepted_aged_at is not None
    assert stamped.accepted_aged_age_days == pytest.approx(31.04, abs=0.1)

    write_valid_calibration_fingerprint(ws["db"], model_id=MODEL)
    assert main(_open_args(ws)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["calibration_aged_accepted"] is False
    fresh = calibrate.load_fingerprint(calibrate.fingerprint_path(ws["db"]))
    assert fresh is not None
    assert fresh.accepted_aged_at is None


def test_no_escape_hatch_reaches_the_cli_surface() -> None:
    """calibration_check=False exists ONLY for calibrate.py's internal anchor runs —
    the CLI must expose no flag that disables the check."""
    with pytest.raises(SystemExit):
        main(["review", "open", RULE_PATH, "--no-calibration-check"])


# ---------------------------------------------------------------------------
# 4. Poisoning guard + CLI integration
# ---------------------------------------------------------------------------


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _calibrate_commit(
    tmp_path: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    good: dict[str, Any],
    garbage: dict[str, Any],
) -> int:
    _stdin_bytes(
        monkeypatch,
        json.dumps({"good": good, "garbage": garbage}).encode("utf-8"),
    )
    return main(
        [
            "calibrate",
            "commit",
            "--model",
            MODEL,
            "--db",
            str(db_path),
            "--throwaway-dir",
            str(tmp_path / "throwaway"),
            "--workspace-root",
            str(tmp_path),
        ]
    )


def test_real_db_byte_identical_after_passing_and_failing_runs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poisoning guard: a calibration run — pass, gate-fail, or parse-fail —
    never opens the real DB file, so its bytes are identical before and after."""
    db_path = tmp_path / "data" / "citation.db"
    assert main(["init-db", "--db", str(db_path)]) == 0
    before = _file_sha256(db_path)

    code = _calibrate_commit(
        tmp_path, db_path, monkeypatch, good_expected_payload(), garbage_expected_payload()
    )
    assert code == 0
    assert _file_sha256(db_path) == before

    inverted_good = _payload([_choice(f"g{i}", ["contradicted"] * 3) for i in range(5)])
    inverted_garbage = _payload([_choice(f"b{i}", ["evidence-backed"] * 3) for i in range(5)])
    code = _calibrate_commit(tmp_path, db_path, monkeypatch, inverted_good, inverted_garbage)
    assert code == 1
    assert _file_sha256(db_path) == before

    parse_fail_garbage = _payload(
        [_choice(f"b{i}", ["contradicted", "parse-failed", "parse-failed"]) for i in range(5)]
    )
    code = _calibrate_commit(
        tmp_path, db_path, monkeypatch, good_expected_payload(), parse_fail_garbage
    )
    assert code == 2
    assert _file_sha256(db_path) == before
    capsys.readouterr()


def test_garbage_citations_land_on_the_throwaway_not_the_real_db(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "data" / "citation.db"
    assert main(["init-db", "--db", str(db_path)]) == 0
    code = _calibrate_commit(
        tmp_path, db_path, monkeypatch, good_expected_payload(), garbage_expected_payload()
    )
    assert code == 0
    capsys.readouterr()
    throwaway = tmp_path / "throwaway" / "calibration-throwaway.db"
    assert throwaway.is_file()
    t_conn = db.connect(throwaway)
    r_conn = db.connect(db_path)
    try:
        # Anchor artifacts + committed runs exist ONLY on the throwaway.
        t_count = int(t_conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
        r_count = int(r_conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
        assert t_count == 2
        assert r_count == 0
        assert int(t_conn.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0]) == 2
        assert int(r_conn.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0]) == 0
    finally:
        t_conn.close()
        r_conn.close()


# ---------------------------------------------------------------------------
# 4b. Throwaway snapshot consistency (WAL race) + lifecycle cleanup
# ---------------------------------------------------------------------------


def test_throwaway_snapshot_includes_committed_rows_still_in_the_wal(tmp_path: Path) -> None:
    """Deterministic version of the iteration-2 reviewer repro (wal_probe3): a
    committed row living only in the -wal sidecar MUST appear in the throwaway.
    The torn state the old two-step file copy could produce — main file copied,
    checkpoint lands, -wal copied — is demonstrated by reading a main-file-only
    copy (row absent); the backup-API snapshot cannot tear."""
    db_path = tmp_path / "data" / "citation.db"
    db.init_db(db_path)
    holder = db.connect(db_path)  # second conn: blocks checkpoint-on-close (wal_probe2)
    try:
        writer = db.connect(db_path)
        try:
            with writer:
                writer.execute(
                    "INSERT INTO citations (kind, natural_key, title, url_or_doi, "
                    "verified_at, resolution_method) VALUES ('external', 'doi:10.1/wal', "
                    "'w', 'doi:10.1/wal', '2026-07-21T00:00:00Z', 'api_structured')"
                )
        finally:
            writer.close()
        # Precondition: the committed row still lives in the WAL sidecar.
        assert Path(f"{db_path}-wal").stat().st_size > 0
        # The old hazard, reproduced: the main file's bytes alone LACK the row.
        torn = tmp_path / "torn-main-file-only.db"
        shutil.copyfile(db_path, torn)
        torn_conn = sqlite3.connect(str(torn))
        try:
            assert int(torn_conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0]) == 0
        finally:
            torn_conn.close()
        # The fixed throwaway sees the whole logical DB, WAL content included.
        throwaway = calibrate.make_throwaway_db(db_path, tmp_path / "throwaway")
        t_conn = db.connect(throwaway)
        try:
            assert int(t_conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0]) == 1
        finally:
            t_conn.close()
    finally:
        holder.close()


def test_throwaway_snapshot_race_writer_and_checkpointer(tmp_path: Path) -> None:
    """Regression for the two-step-copy WAL race (BUGS-1): with a writer committing
    rows and a checkpointer hammering wal_checkpoint(TRUNCATE) concurrently —
    exactly the checkpoint `cite status` issues — every throwaway must contain
    every row committed before that copy started. The old shutil-based copy
    silently dropped committed rows in this window; the backup API is atomic."""
    db_path = tmp_path / "data" / "citation.db"
    db.init_db(db_path)
    committed = [0]
    stop = threading.Event()
    failures: list[str] = []

    def writer() -> None:
        conn = db.connect(db_path)
        try:
            i = 0
            while not stop.is_set():
                i += 1
                try:
                    with conn:
                        conn.execute(
                            "INSERT INTO citations (kind, natural_key, title, url_or_doi, "
                            "verified_at, resolution_method) VALUES ('external', ?, 't', ?, "
                            "'2026-07-21T00:00:00Z', 'api_structured')",
                            (f"doi:10.1/race-{i}", f"doi:10.1/race-{i}"),
                        )
                except sqlite3.OperationalError as exc:  # pragma: no cover - timing
                    failures.append(f"writer: {exc}")
                    return
                committed[0] = i
        finally:
            conn.close()

    def checkpointer() -> None:
        conn = db.connect(db_path)
        try:
            while not stop.is_set():
                # busy (the writer holds the lock) is expected — keep hammering
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    threads = [threading.Thread(target=writer), threading.Thread(target=checkpointer)]
    for thread in threads:
        thread.start()
    try:
        for n in range(6):
            floor = committed[0]
            throwaway = calibrate.make_throwaway_db(db_path, tmp_path / f"copy-{n}")
            conn = sqlite3.connect(str(throwaway))
            try:
                count = int(conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0])
            finally:
                conn.close()
            assert count >= floor, (
                f"throwaway copy {n} dropped committed rows: contains {count}, but "
                f"{floor} were already committed before the copy started"
            )
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=30)
    assert failures == []


@pytest.fixture()
def tracked_mkdtemp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Path]:
    """Record every DEFAULT throwaway temp dir calibrate creates, rooted under this
    test's tmp_path so even a cleanup regression cannot pollute the real OS temp."""
    base = tmp_path / "tmpbase"
    base.mkdir()
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording(*args: Any, **kwargs: Any) -> str:
        kwargs.setdefault("dir", str(base))
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", recording)
    return created


def _run_default_dir(
    tmp_path: Path, good: dict[str, Any], garbage: dict[str, Any]
) -> calibrate.CalibrationResult:
    """run_calibration WITHOUT --throwaway-dir — the real-usage default the
    iteration-1 suite never exercised (BUGS-2)."""
    return calibrate.run_calibration(
        tmp_path / "data" / "citation.db",
        good,
        garbage,
        model_id=MODEL,
        workspace_root=tmp_path,
    )


def test_default_throwaway_dir_removed_after_pass(
    tmp_path: Path, tracked_mkdtemp: list[Path]
) -> None:
    result = _run_default_dir(tmp_path, good_expected_payload(), garbage_expected_payload())
    assert result.passed
    assert len(tracked_mkdtemp) == 1
    assert not tracked_mkdtemp[0].exists()  # the whole temp dir is gone
    # The result still reports the path it used, for the audit trail.
    assert result.throwaway_db == tracked_mkdtemp[0] / "calibration-throwaway.db"


def test_default_throwaway_dir_removed_after_gate_fail(
    tmp_path: Path, tracked_mkdtemp: list[Path]
) -> None:
    inverted_good = _payload([_choice(f"g{i}", ["contradicted"] * 3) for i in range(5)])
    inverted_garbage = _payload([_choice(f"b{i}", ["evidence-backed"] * 3) for i in range(5)])
    result = _run_default_dir(tmp_path, inverted_good, inverted_garbage)
    assert not result.passed
    assert len(tracked_mkdtemp) == 1
    assert not tracked_mkdtemp[0].exists()


def test_default_throwaway_dir_not_created_on_parse_fail_abort(
    tmp_path: Path, tracked_mkdtemp: list[Path]
) -> None:
    """The parse-fail pre-gate aborts BEFORE any throwaway mechanics — no temp dir
    is ever created, so there is nothing to leak."""
    garbage = _payload(
        [_choice(f"b{i}", ["contradicted", "parse-failed", "parse-failed"]) for i in range(5)]
    )
    with pytest.raises(calibrate.CalibrationParseFailure):
        _run_default_dir(tmp_path, good_expected_payload(), garbage)
    assert tracked_mkdtemp == []


def test_default_throwaway_dir_removed_after_injected_midrun_exception(
    tmp_path: Path, tracked_mkdtemp: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-run crash (anchor commit blows up AFTER the throwaway exists) must
    still remove the default temp dir — cleanup is a finally, not a happy path."""

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("injected mid-run failure")

    monkeypatch.setattr(review, "commit_review", boom)
    with pytest.raises(RuntimeError, match="injected mid-run failure"):
        _run_default_dir(tmp_path, good_expected_payload(), garbage_expected_payload())
    assert len(tracked_mkdtemp) == 1
    assert not tracked_mkdtemp[0].exists()


def test_calibrate_open_default_throwaway_removed_and_flagged(
    tmp_path: Path, tracked_mkdtemp: list[Path]
) -> None:
    """`calibrate open` without --throwaway-dir: the temp snapshot is removed when
    the context is built (commit is stateless and rebuilds its own), the JSON says
    so via throwaway_retained=false, and the context is still complete."""
    context = calibrate.open_calibration(
        tmp_path / "data" / "citation.db",
        reviewer_model=MODEL,
        workspace_root=tmp_path,
    )
    assert context["throwaway_retained"] is False
    assert len(tracked_mkdtemp) == 1
    assert not tracked_mkdtemp[0].exists()
    assert context["anchors"]["good"]["open"]["run_id"] == 1
    assert context["anchors"]["garbage"]["open"]["run_id"] == 2
    assert len(context["expected_labels"]["good_anchor"]["choices"]) == 5


def test_cli_calibrate_commit_exit_codes_and_cache_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "data" / "citation.db"
    cache_file = calibrate.fingerprint_path(db_path)

    # Gate-fail: exit 1, every failed assertion reported, nothing cached.
    inverted_good = _payload([_choice(f"g{i}", ["contradicted"] * 3) for i in range(5)])
    inverted_garbage = _payload([_choice(f"b{i}", ["evidence-backed"] * 3) for i in range(5)])
    assert _calibrate_commit(tmp_path, db_path, monkeypatch, inverted_good, inverted_garbage) == 1
    out = capsys.readouterr().out
    assert "ABORT" in out
    assert out.count("[FAIL]") == 4
    assert not cache_file.exists()

    # Pass: exit 0, fingerprint cached with the gate numbers.
    assert (
        _calibrate_commit(
            tmp_path, db_path, monkeypatch, good_expected_payload(), garbage_expected_payload()
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "PASSED" in out
    assert out.count("[PASS]") == 4
    cached = calibrate.load_fingerprint(cache_file)
    assert cached is not None
    assert cached.gate.composite_good == 95.0
    assert cached.gate.composite_garbage == 0.0

    # Parse-fail: exit 2 (distinct from a gate failure), cache untouched.
    parse_fail_good = _payload(
        [_choice(f"g{i}", ["evidence-backed", "parse-failed", "parse-failed"]) for i in range(5)]
    )
    assert (
        _calibrate_commit(
            tmp_path, db_path, monkeypatch, parse_fail_good, garbage_expected_payload()
        )
        == 2
    )
    out = capsys.readouterr().out
    assert "parse-fail ABORT" in out
    assert calibrate.load_fingerprint(cache_file) is not None  # prior PASS survives


def test_cli_calibrate_commit_rejects_malformed_stdin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stdin_bytes(monkeypatch, b'{"only": "one anchor"}')
    assert main(["calibrate", "commit", "--db", str(tmp_path / "citation.db")]) == 1
    assert '"good"' in capsys.readouterr().out


def test_cli_calibrate_open_prints_context_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "data" / "citation.db"
    assert (
        main(
            [
                "calibrate",
                "open",
                "--db",
                str(db_path),
                "--reviewer-model",
                MODEL,
                "--throwaway-dir",
                str(tmp_path / "throwaway"),
                "--workspace-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    context = json.loads(capsys.readouterr().out)
    assert Path(context["throwaway_db"]).is_file()
    assert context["throwaway_retained"] is True  # caller-owned dir survives
    good = context["anchors"]["good"]
    garbage = context["anchors"]["garbage"]
    assert good["fixture"].endswith("good-anchor.code-quality.frozen.md")
    assert garbage["fixture"].endswith("garbage-anchor.SYNTHETIC.md")
    assert good["open"]["run_id"] == 1
    assert garbage["open"]["run_id"] == 2
    assert good["open"]["artifact"]["artifact_type"] == "rule"
    labels = context["expected_labels"]
    assert len(labels["good_anchor"]["choices"]) == 5
    assert len(labels["garbage_anchor"]["choices"]) == 5


def test_cli_calibrate_check_reports_validity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "data" / "citation.db"
    assert main(["init-db", "--db", str(db_path)]) == 0
    capsys.readouterr()
    assert main(["calibrate", "check", "--db", str(db_path)]) == 1
    out = capsys.readouterr().out
    assert "INVALID" in out
    assert "never passed" in out

    write_valid_calibration_fingerprint(db_path, model_id=MODEL)
    assert main(["calibrate", "check", "--db", str(db_path)]) == 0
    assert "VALID" in capsys.readouterr().out

    # --model compares fingerprint B; a mismatch flips the verdict.
    assert main(["calibrate", "check", "--db", str(db_path), "--model", MODEL]) == 0
    capsys.readouterr()
    assert main(["calibrate", "check", "--db", str(db_path), "--model", "other-model"]) == 1
    assert "fingerprint B" in capsys.readouterr().out


def test_cli_calibrate_check_missing_db_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["calibrate", "check", "--db", str(tmp_path / "nope.db")]) == 1
    assert "does not exist" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 5. Fixture integrity — the frozen anchors themselves
# ---------------------------------------------------------------------------


FIXTURES = calibrate.CALIBRATION_FIXTURES_DIR


def test_garbage_anchor_keeps_its_synthetic_banner() -> None:
    text = (FIXTURES / calibrate.GARBAGE_ANCHOR_FILENAME).read_text(encoding="utf-8")
    banner_pos = text.find("SYNTHETIC CALIBRATION FIXTURE")
    assert banner_pos != -1 and banner_pos < 100  # present AND prominent (top of file)
    assert "Do not apply" in text


def test_no_unverified_placeholder_survives_in_any_fixture() -> None:
    """Step 5's done-when: the 3 draft rows score-validity §2b left open were CLOSED
    with real literature checks — grep for UNVERIFIED must return zero hits."""
    for file in sorted(FIXTURES.iterdir()):
        assert "UNVERIFIED" not in file.read_text(encoding="utf-8"), file.name


def test_good_anchor_is_byte_frozen_against_expected_labels_hash() -> None:
    """Intentional re-freezing is ONE explicit edit: update sha256 in
    expected-labels.json. Any other drift of the frozen snapshot fails here."""
    expected = calibrate.load_expected_labels()
    actual = calibrate.anchor_content_sha256(FIXTURES / expected.good_anchor.file)
    assert actual == expected.good_anchor.sha256


def test_garbage_anchor_is_byte_frozen_against_expected_labels_hash() -> None:
    expected = calibrate.load_expected_labels()
    actual = calibrate.anchor_content_sha256(FIXTURES / expected.garbage_anchor.file)
    assert actual == expected.garbage_anchor.sha256


def test_good_anchor_is_the_real_rule_snapshot() -> None:
    text = (FIXTURES / calibrate.GOOD_ANCHOR_FILENAME).read_text(encoding="utf-8")
    assert "FROZEN CALIBRATION SNAPSHOT" in text
    assert ".claude/rules/code-quality.md" in text  # names its source
    assert "# Code-quality discipline" in text  # carries the real rule content
    assert "## Source memories" in text


def test_expected_labels_schema_validates_and_every_row_is_closed() -> None:
    """The machine-readable form the gate + Step 9 use: 5 choices per anchor; every
    garbage choice is evidence-hostile with a REAL verification record — a verified
    contradicting citation (locator + title + finding) or a documented absence
    (queries + note). The pydantic model enforces this; assert the semantics too."""
    expected = calibrate.load_expected_labels()
    assert len(expected.good_anchor.choices) == 5
    assert len(expected.garbage_anchor.choices) == 5
    good_labels = [choice.expected_label for choice in expected.good_anchor.choices]
    assert good_labels.count("evidence-backed") == 4  # the §2a profile
    for choice in expected.garbage_anchor.choices:
        assert choice.expected_label in ("unsupported", "contradicted")
        record = choice.verification
        if record.kind == "external":
            assert record.doi or record.url
            assert record.title and record.finding
        else:
            assert record.queries and record.note


def test_garbage_anchor_closed_rows_cite_the_verified_dois() -> None:
    """The three rows this build step closed live in BOTH the fixture prose and the
    machine-readable record, with the DOIs verified via the project's own resolve
    clients (Crossref round-trip; S2 lookup-by-DOI for the two papers S2 indexes)."""
    text = (FIXTURES / calibrate.GARBAGE_ANCHOR_FILENAME).read_text(encoding="utf-8")
    expected = calibrate.load_expected_labels()
    by_key = {choice.choice_key: choice for choice in expected.garbage_anchor.choices}
    closures = {
        "flat-24h-ttl-all-caches": "10.1109/12.675713",
        "skip-review-under-20-lines": "10.1109/TSE.2005.74",
        "god-object-config-class": "10.1007/s10664-011-9171-y",
    }
    for key, doi in closures.items():
        assert doi in text, f"fixture prose must record the closing DOI for {key}"
        assert by_key[key].verification.doi == doi
        assert by_key[key].expected_label == "contradicted"


def test_prompts_fingerprint_fails_loud_on_empty_dir(tmp_path: Path) -> None:
    empty = tmp_path / "prompts"
    empty.mkdir()
    with pytest.raises(calibrate.CalibrationError, match="no prompt templates"):
        calibrate.prompts_fingerprint(empty)


def test_missing_anchor_fixture_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(calibrate.CalibrationError, match="missing or unreadable"):
        calibrate.anchor_content_sha256(tmp_path / "nope.md")


def test_run_calibration_requires_a_model_id(tmp_path: Path) -> None:
    with pytest.raises(calibrate.CalibrationError, match="model_id"):
        calibrate.run_calibration(
            tmp_path / "citation.db",
            good_expected_payload(),
            garbage_expected_payload(),
            model_id="  ",
            throwaway_dir=tmp_path / "throwaway",
            workspace_root=tmp_path,
        )


def test_tie_in_anchor_payload_rejects_through_review_mechanics(tmp_path: Path) -> None:
    """Production-path assembly: a tie reaching commit is review.py's TieError, not a
    calibration-specific reimplementation of vote math."""
    tie = _payload(
        [_choice("g0", ["evidence-backed", "contradicted", "unsupported", "interesting-novel"])]
    )
    with pytest.raises(review.TieError):
        _run(tmp_path, tie, garbage_expected_payload())


def test_throwaway_db_starts_as_a_consistent_snapshot_of_the_real_db(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "citation.db"
    assert main(["init-db", "--db", str(db_path)]) == 0
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO citations (kind, natural_key, title, url_or_doi, verified_at, "
                "resolution_method) VALUES ('external', 'doi:10.1/seed', 'seed', "
                "'doi:10.1/seed', '2026-07-21T00:00:00Z', 'api_structured')"
            )
    finally:
        conn.close()
    throwaway = calibrate.make_throwaway_db(db_path, tmp_path / "throwaway")
    t_conn = db.connect(throwaway)
    try:
        assert int(t_conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0]) == 1
        # The copy carries the corpus fingerprint the cache will be validated against.
        assert calibrate.corpus_fingerprint(t_conn) == "1:1"
    finally:
        t_conn.close()


def test_make_throwaway_without_real_db_inits_fresh_schema(tmp_path: Path) -> None:
    throwaway = calibrate.make_throwaway_db(tmp_path / "never-created.db", tmp_path / "t")
    conn = db.connect(throwaway)
    try:
        assert calibrate.schema_version(conn) == 2
        assert calibrate.corpus_fingerprint(conn) == "0:0"
    finally:
        conn.close()


def test_check_calibration_on_in_memory_db_is_invalid(cal_db: Path) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        check = calibrate.check_calibration(conn, model_id=MODEL)
    finally:
        conn.close()
    assert not check.valid
    assert any("no file path" in reason for reason in check.reasons)
