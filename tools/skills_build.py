"""Build the Citation Needed skills into this repository's own skill folders.

The skills (citation-review, citation-distill, citation-triage, citation-sweep) live in
``skills/<name>/`` in Skill Mesh's source shape: a provider-neutral ``core.md`` plus thin
``providers/<host>.md`` wrappers and a ``skill-entry.json`` holding the discovery
description. They left the Skill Mesh catalog on 2026-11-16 (operator decision) and are
project-local: they load only in a session opened in this repository, never from the
shared skill folders.

* **write** renders, per host, ``<name>/SKILL.md`` (YAML frontmatter, a generated-file
  header with this tool's own marker, then the wrapper text) and ``<name>/core.md`` into
  the host's project skill folder: Claude ``.claude/skills/``, Codex ``.agents/skills/``.
  It touches only the four citation skill folders, and removes a stale file there only
  when it carries this tool's marker.
* **check** exits 1 when a committed generated file differs from the build, so the
  committed folders cannot drift from ``skills/``.

Usage (from a citation-needed checkout)::

    uv run python tools/skills_build.py write
    uv run python tools/skills_build.py check

Exit codes: 0 done or current; 1 stale, or refused (nothing was written); 2 bad arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SKILLS_SOURCE: Final = REPO_ROOT / "skills"
SKILLS: Final = ("citation-review", "citation-distill", "citation-triage", "citation-sweep")
HOSTS: Final = ("claude", "codex")
HOST_ROOTS: Final = {"claude": Path(".claude") / "skills", "codex": Path(".agents") / "skills"}
MARKER: Final = "CITATION-NEEDED-GENERATED-FILE"
ENTRY_NAME: Final = "skill-entry.json"
SKIP_SUPPORT: Final = frozenset({"core.md", "providers", ENTRY_NAME})
SUMMARY: Final = "Build the Citation Needed skills into this repository's own skill folders."


class Refused(Exception):
    """The operation would be unsafe; nothing was written."""


@dataclass(frozen=True)
class Report:
    written: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()


# --- Build -----------------------------------------------------------------------------


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :].lstrip("\n")
    return text


def _header(source: str, host: str) -> str:
    return (
        "<!-- GENERATED FILE - DO NOT EDIT.\n"
        f"     Marker: {MARKER}\n"
        "     Produced by citation-needed tools/skills_build.py.\n"
        f"     Canonical source: citation-needed/{source}\n"
        f"     Profile: {host}\n"
        "     Edit the canonical source and rebuild; edits here are overwritten. -->\n"
    )


def _read_text(path: Path) -> str:
    """Source text with LF line endings, whatever the checkout's autocrlf setting."""
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _entry(name: str, source: Path) -> Mapping[str, object]:
    data = json.loads(_read_text(source / name / ENTRY_NAME))
    if not isinstance(data, dict) or data.get("name") != name:
        raise Refused(f"{name}: {ENTRY_NAME} does not name {name}")
    return data


def render_skill_md(name: str, host: str, source: Path = SKILLS_SOURCE) -> str:
    entry = _entry(name, source)
    description = entry.get("description")
    if not isinstance(description, str) or not description:
        raise Refused(f"{name}: {ENTRY_NAME} has no description")
    wrapper = _read_text(source / name / "providers" / f"{host}.md")
    body = _strip_frontmatter(wrapper).replace("](../core.md)", "](core.md)")
    body = body.replace("[../core.md]", "[core.md]")
    front = ["---", f"name: {name}", f"description: {json.dumps(description)}"]
    if host == "claude":
        front.append("user-invocable: true")
    front.append("---")
    header = _header(f"skills/{name}/providers/{host}.md", host)
    return "\n".join(front) + "\n" + header + "\n" + body


def build_files(host: str, source: Path = SKILLS_SOURCE) -> dict[str, bytes]:
    """Every file of one host's build, keyed by ``<skill>/<relative path>`` (``/``)."""
    if host not in HOSTS:
        raise Refused(f"unknown host {host}")
    files: dict[str, bytes] = {}
    for name in SKILLS:
        folder = source / name
        files[f"{name}/SKILL.md"] = render_skill_md(name, host, source).encode("utf-8")
        core = _read_text(folder / "core.md")
        files[f"{name}/core.md"] = (_header(f"skills/{name}/core.md", host) + "\n" + core).encode(
            "utf-8"
        )
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(folder)
            if (
                path.is_file()
                and rel.parts[0] not in SKIP_SUPPORT
                and "__pycache__" not in rel.parts
                and path.suffix != ".pyc"
            ):
                files[f"{name}/{rel.as_posix()}"] = path.read_bytes()
    return files


# --- Write / check ---------------------------------------------------------------------


def _normalized(data: bytes) -> bytes:
    """Compare as LF: a CRLF checkout of a committed generated file is still current."""
    return data.replace(b"\r\n", b"\n")


def _existing(root: Path) -> dict[str, Path]:
    """Files now inside the four citation skill folders under ``root``."""
    found: dict[str, Path] = {}
    for name in SKILLS:
        folder = root / name
        if folder.is_dir():
            for path in folder.rglob("*"):
                if path.is_file():
                    found[f"{name}/{path.relative_to(folder).as_posix()}"] = path
    return found


def _is_ours(path: Path) -> bool:
    try:
        return MARKER.encode("utf-8") in path.read_bytes()
    except OSError:
        return False


def compare(repo: Path = REPO_ROOT, source: Path = SKILLS_SOURCE) -> Report:
    """Which generated files are missing, different, or left over (``stale``)."""
    stale: list[str] = []
    for host in HOSTS:
        root = repo / HOST_ROOTS[host]
        files = build_files(host, source)
        existing = _existing(root)
        for rel, data in files.items():
            path = existing.get(rel)
            if path is None or _normalized(path.read_bytes()) != _normalized(data):
                stale.append(f"{host}/{rel}")
        stale.extend(f"{host}/{rel}" for rel in sorted(set(existing) - set(files)))
    return Report(stale=tuple(stale))


def write(repo: Path = REPO_ROOT, source: Path = SKILLS_SOURCE) -> Report:
    plans = {host: build_files(host, source) for host in HOSTS}
    # Check everything before writing anything: never delete or overwrite a hand-made file.
    # Every file write would overwrite or remove is checked; support files copied verbatim
    # carry no marker, so one that already matches the build is accepted as ours.
    problems: list[str] = []
    for host, files in plans.items():
        for rel, path in _existing(repo / HOST_ROOTS[host]).items():
            current = rel in files and _normalized(path.read_bytes()) == _normalized(files[rel])
            if not current and not _is_ours(path):
                problems.append(f"{path} has no {MARKER} marker; edit skills/ instead")
    if problems:
        raise Refused("; ".join(problems))
    written: list[str] = []
    removed: list[str] = []
    for host, files in plans.items():
        root = repo / HOST_ROOTS[host]
        existing = _existing(root)
        for rel, data in files.items():
            target = root.joinpath(*rel.split("/"))
            if target.exists() and _normalized(target.read_bytes()) == _normalized(data):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".cn-tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            written.append(f"{host}/{rel}")
        for rel in sorted(set(existing) - set(files)):
            existing[rel].unlink()
            removed.append(f"{host}/{rel}")
    return Report(written=tuple(written), removed=tuple(removed))


# --- CLI -------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skills_build", description=SUMMARY)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("write", help="render both hosts' skill folders into this repository")
    sub.add_parser("check", help="exit 1 if a committed generated file is out of date")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            report = compare()
            for rel in report.stale:
                print(f"stale\t{rel}")
            print(f"check: {len(report.stale)} stale file(s)")
            return 1 if report.stale else 0
        report = write()
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    for rel in report.written:
        print(f"wrote\t{rel}")
    for rel in report.removed:
        print(f"removed\t{rel}")
    print(f"write: {len(report.written)} written, {len(report.removed)} removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
