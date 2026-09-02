#!/usr/bin/env python3
"""Inspect a built MCPB archive for required files and privacy leaks."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
ATLAS_PACKAGE_ROOT = ROOT / "fl_studio_mcp" / "plugin_atlas"
ATLAS_DATA_ROOT = ROOT / "fl_studio_mcp" / "plugin_atlas_data"
ATLAS_REQUIRED = {
    path.relative_to(ROOT).as_posix()
    for path in ATLAS_PACKAGE_ROOT.rglob("*.py")
}
ATLAS_REQUIRED.update(
    path.relative_to(ROOT).as_posix()
    for path in ATLAS_DATA_ROOT.rglob("*.json")
)
ATLAS_REQUIRED.add("fl_studio_mcp/plugin_atlas_data/__init__.py")
ATLAS_REQUIRED.add("fl_studio_mcp/plugin_atlas_mcp.py")
SOUND_SELECTION_PACKAGE_ROOT = ROOT / "fl_studio_mcp" / "sound_selection"
SOUND_SELECTION_REQUIRED = {
    path.relative_to(ROOT).as_posix()
    for path in SOUND_SELECTION_PACKAGE_ROOT.rglob("*.py")
}
SOUND_SELECTION_REQUIRED.update(
    path.relative_to(ROOT).as_posix()
    for path in SOUND_SELECTION_PACKAGE_ROOT.rglob("*.json")
)
CREATION_PIPELINE_ROOT = ROOT / "fl_studio_mcp" / "creation_pipeline"
CREATION_PIPELINE_REQUIRED = {
    path.relative_to(ROOT).as_posix()
    for path in CREATION_PIPELINE_ROOT.rglob("*.py")
}
CREATION_REVIEW_ROOT = ROOT / "fl_studio_mcp" / "creation_review"
CREATION_REVIEW_REQUIRED = {
    path.relative_to(ROOT).as_posix()
    for path in CREATION_REVIEW_ROOT.rglob("*.py")
}
REQUIRED = {
    "manifest.json",
    "mcpb_entry.py",
    "pyproject.toml",
    "fl_studio_mcp/mcp_server.py",
    "fl_studio_mcp/_bridge/device_UniversalBridge.py",
} | ATLAS_REQUIRED | SOUND_SELECTION_REQUIRED | CREATION_PIPELINE_REQUIRED | CREATION_REVIEW_REQUIRED
FORBIDDEN_PARTS = {
    ".git",
    ".github",
    ".private",
    ".venv",
    "__pycache__",
    "tests",
    "scripts",
    "dist",
    "mcpb-dist",
}
FORBIDDEN_NAMES = {".mcp.json", ".env", ".DS_Store"}
FORBIDDEN_SUFFIXES = {
    ".aif",
    ".aiff",
    ".flac",
    ".flp",
    ".key",
    ".m4a",
    ".mid",
    ".midi",
    ".mp3",
    ".ogg",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".wav",
    ".wave",
}


def inspect_bundle(bundle: Path) -> list[str]:
    failures: list[str] = []
    if not bundle.is_file():
        return [f"bundle does not exist: {bundle}"]
    if not zipfile.is_zipfile(bundle):
        return [f"bundle is not a ZIP-compatible MCPB archive: {bundle}"]

    with zipfile.ZipFile(bundle) as archive:
        files = {name for name in archive.namelist() if not name.endswith("/")}
        missing = sorted(REQUIRED - files)
        if missing:
            failures.append("missing required files: " + ", ".join(missing))

        leaked: list[str] = []
        for name in sorted(files):
            path = PurePosixPath(name)
            lowered_parts = {part.lower() for part in path.parts}
            if lowered_parts & FORBIDDEN_PARTS:
                leaked.append(name)
                continue
            lowered_name = path.name.lower()
            if (
                lowered_name in FORBIDDEN_NAMES
                or lowered_name.startswith(".env.")
                or path.suffix.lower() in FORBIDDEN_SUFFIXES
            ):
                leaked.append(name)
        if leaked:
            failures.append("private/generated files leaked: " + ", ".join(leaked))

        if "manifest.json" in files:
            bundled_manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            source_manifest = json.loads(
                (ROOT / "manifest.json").read_text(encoding="utf-8")
            )
            if bundled_manifest != source_manifest:
                failures.append("bundled manifest.json differs from the source manifest")

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args(argv)
    failures = inspect_bundle(args.bundle)
    if failures:
        print("MCPB bundle check failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"MCPB bundle OK: {args.bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
