#!/usr/bin/env python3
"""Validate and build the Claude Desktop extension with a pinned MCPB CLI."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from check_mcpb_bundle import inspect_bundle
from sync_mcpb_manifest import discover_tools


ROOT = Path(__file__).resolve().parents[1]
# Exact npm version, deliberately centralized so CI and release cannot drift.
MCPB_NPM_PACKAGE = "@anthropic-ai/mcpb@2.1.2"


def _run_cli(*arguments: str) -> None:
    command = [
        "npx",
        "--yes",
        f"--package={MCPB_NPM_PACKAGE}",
        "mcpb",
        *arguments,
    ]
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def _preflight() -> str:
    if shutil.which("npx") is None:
        raise RuntimeError("npx is required to validate and build the MCPB archive")
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = project["project"]["version"]
    if manifest.get("version") != project_version:
        raise RuntimeError(
            "manifest.json version does not match pyproject.toml: "
            f"{manifest.get('version')!r} != {project_version!r}"
        )
    if manifest.get("tools") != discover_tools():
        raise RuntimeError(
            "manifest.json tools are stale; run "
            "python3 scripts/sync_mcpb_manifest.py"
        )
    return project_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "mcpb-dist",
        help="directory that receives the .mcpb archive",
    )
    args = parser.parse_args(argv)

    try:
        version = _preflight()
        _run_cli("validate", "manifest.json")
        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        bundle = output_dir / f"postfader-fl-studio-mcp-{version}.mcpb"
        if bundle.exists():
            bundle.unlink()
        _run_cli("pack", ".", str(bundle))
        failures = inspect_bundle(bundle)
        if failures:
            raise RuntimeError("; ".join(failures))
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"MCPB build failed: {exc}", file=sys.stderr)
        return 1

    print(f"MCPB bundle OK: {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
