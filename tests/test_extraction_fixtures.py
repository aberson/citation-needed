"""The plan §4.1 memory-splitting acceptance fixture (Step 4's mechanical half).

Extraction itself is LLM-side (Step 8), so what Step 4 can test mechanically is:

1. the extraction TEMPLATE (prompts/extraction.v1.md) carries the §4.1 splitting rule
   — the independently-falsifiable unit, the different-verdicts test, and the
   over-split guard — so a prompt edit that weakens the rule fails loudly here;
2. the frozen fixture pair (fixtures/extraction/multi-decision-memory.md — a
   composite modeled on user_model_preference with 3 independently-falsifiable
   decisions — and single-decision-memory.md) PARSES as valid memory artifacts
   through discover.py's production frontmatter path (scan_workspace's memory walk);
3. fixtures/extraction/expected.json records the expected choice COUNTS (multi > 1,
   single == 1) in the shape Step 9 consumes.

The LIVE half — running real extraction over each fixture and asserting the produced
choice count matches expected.json — runs in plan Step 9's end-to-end smoke
("calibrate, then one real review"), where an actual LLM extraction pass exists.
This split is deliberate: no LLM call happens in this suite.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from citation_needed import discover

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = PROJECT_ROOT / "fixtures" / "extraction"
EXTRACTION_PROMPT = PROJECT_ROOT / "prompts" / "extraction.v1.md"

MULTI = "multi-decision-memory.md"
SINGLE = "single-decision-memory.md"


def test_extraction_template_contains_the_splitting_rule() -> None:
    """The §4.1 rule text is load-bearing prompt content: the unit definition, the
    different-verdicts test, and the over-split guard must all be present verbatim
    as section markers (a reworded/weakened rule is a test failure, not a drift)."""
    text = EXTRACTION_PROMPT.read_text(encoding="utf-8")
    assert "independently-falsifiable decision" in text
    assert "**Different-verdicts test:**" in text
    assert "**Over-split guard:**" in text
    # The rule's two poles: composite memories split per decision; single-decision
    # memories (the common case) yield exactly one choice.
    assert "one choice per decision" in text
    assert "exactly one" in text


def test_expected_json_records_the_acceptance_counts() -> None:
    data = json.loads((FIXTURES_DIR / "expected.json").read_text(encoding="utf-8"))
    fixtures = data["fixtures"]
    assert set(fixtures) == {MULTI, SINGLE}
    assert fixtures[MULTI]["expected_choice_count"] > 1  # the multi-decision pole
    assert fixtures[SINGLE]["expected_choice_count"] == 1  # the single-decision pole
    # Step 9 is the consumer of the live count check — keep the pointer honest.
    assert "Step 9" in data["consumer"]


def test_fixture_memories_parse_through_discover_frontmatter_path(tmp_path: Path) -> None:
    """Both fixtures ingest as VALID memory artifacts through the production scan
    (scan_workspace's memory walk -> parse_frontmatter -> MemoryDetails): clean
    frontmatter, a recognized memory kind, and the memory: path scheme. A fixture
    that discover.py cannot parse could never reach a real extraction pass."""
    ws = tmp_path / "ws"
    ws.mkdir()
    memory_root = tmp_path / "mem"
    slug = discover.workspace_memory_slug(ws.resolve())
    memory_dir = memory_root / slug / "memory"
    memory_dir.mkdir(parents=True)
    for name in (MULTI, SINGLE):
        shutil.copyfile(FIXTURES_DIR / name, memory_dir / name)

    report = discover.scan_workspace(ws, memory_root=memory_root)
    memories = {a.path: a for a in report.artifacts if a.artifact_type == "memory"}
    assert set(memories) == {f"memory:{slug}/{MULTI}", f"memory:{slug}/{SINGLE}"}
    for artifact in memories.values():
        details = artifact.details
        assert getattr(details, "frontmatter_error", "unset") is None
        assert getattr(details, "node_type", None) == "memory"
        assert artifact.content_hash
    assert getattr(memories[f"memory:{slug}/{MULTI}"].details, "memory_kind", None) == "user"
    assert getattr(memories[f"memory:{slug}/{SINGLE}"].details, "memory_kind", None) == "feedback"
