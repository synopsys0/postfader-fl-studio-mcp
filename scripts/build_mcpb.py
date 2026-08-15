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


def _resolve_cli() -> tuple[str, ...]:
    """Return a shell-free command prefix for the pinned MCPB package.

    Node's Windows installers normally provide ``npx.cmd`` while bundled
    developer runtimes commonly provide only ``pnpm.cmd``.  Both runners can
    execute the same exact package pin without a global installation.  Keep
    the resolved executable as argv[0] so paths containing spaces remain safe.
    """

    for name in ("npx", "npx.cmd"):
        executable = shutil.which(name)
        if executable is not None:
            return (
                executable,
                "--yes",
                f"--package={MCPB_NPM_PACKAGE}",
                "mcpb",
            )
    for name in ("pnpm", "pnpm.cmd"):
        executable = shutil.which(name)
        if executable is not None:
            return (executable, "dlx", MCPB_NPM_PACKAGE)
    raise RuntimeError(
        "npx or pnpm is required to validate and build the MCPB archive"
    )


def _run_cli(command_prefix: tuple[str, ...], *arguments: str) -> None:
    command = [*command_prefix, *arguments]
    print("+ " + subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def _preflight() -> tuple[str, tuple[str, ...]]:
    command_prefix = _resolve_cli()
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
    return project_version, command_prefix


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
        version, command_prefix = _preflight()
        _run_cli(command_prefix, "validate", "manifest.json")
        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        bundle = output_dir / f"postfader-fl-studio-mcp-{version}.mcpb"
        if bundle.exists():
            bundle.unlink()
        _run_cli(command_prefix, "pack", ".", str(bundle))
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
