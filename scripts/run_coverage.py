#!/usr/bin/env python3
"""Run the hermetic safe suite and report honest package coverage.

The safe runner already isolates every test in a subprocess.  This wrapper
asks those children to emit parallel coverage data, combines the data without
placing files in the checkout, and applies both a package-wide floor and
smaller safety-boundary floors.  It never runs a live FL Studio acceptance
script or a physical-MIDI test.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
COVERAGE_ENV = "POSTFADER_COVERAGE_DIR"
GLOBAL_THRESHOLD = 45.0


@dataclass(frozen=True)
class CoverageGroup:
    """One independently reported safety/quality boundary."""

    name: str
    threshold: float
    files: tuple[str, ...]


COVERAGE_GROUPS = (
    CoverageGroup(
        "policy and contracts",
        55.0,
        ("contracts.py", "track_b_contracts.py", "evidence.py"),
    ),
    CoverageGroup(
        "transport and framing",
        45.0,
        ("bridge_client.py", "bridge_stamp.py", "acceptance.py"),
    ),
    CoverageGroup(
        "setup and packaging",
        45.0,
        (
            "setup_wizard.py",
            "client_config.py",
            "bridge_install.py",
            "codex_installer.py",
            "diagnostics.py",
            "host_config.py",
        ),
    ),
    CoverageGroup(
        "fake-FL bridge behavior",
        45.0,
        (
            "readonly_inspector.py",
            "verified_writer.py",
            "workflows.py",
            "performance.py",
            "mixing.py",
            "creative.py",
        ),
    ),
)


def _run(command: list[str], *, env: dict[str, str] | None = None) -> int:
    """Run a command with its output visible and return its exit status."""

    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    return completed.returncode


def _coverage_command(*arguments: str) -> list[str]:
    return [sys.executable, "-m", "coverage", *arguments]


def _relative_file(path: str) -> str:
    """Normalize coverage's absolute/relative filename to a package basename."""

    candidate = Path(path)
    try:
        candidate = candidate.resolve().relative_to(ROOT.resolve())
    except ValueError:
        pass
    parts = candidate.as_posix().split("/")
    if len(parts) >= 2 and parts[-2] == "fl_studio_mcp":
        return parts[-1]
    return candidate.as_posix()


def _group_summary(
    files: dict[str, dict[str, Any]], group: CoverageGroup
) -> tuple[int, int, float]:
    """Return statements, covered statements, and percent for one group."""

    selected = {
        _relative_file(filename): record.get("summary", {})
        for filename, record in files.items()
    }
    statements = 0
    covered = 0
    for filename in group.files:
        summary = selected.get(filename)
        if summary is None:
            continue
        statements += int(summary.get("num_statements", 0))
        covered += int(summary.get("covered_lines", 0))
    percent = 100.0 * covered / statements if statements else 0.0
    return statements, covered, percent


def _write_artifacts(
    artifact_dir: Path | None,
    *,
    json_path: Path,
    report_text: str,
) -> None:
    if artifact_dir is None:
        return
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "coverage.json").write_bytes(json_path.read_bytes())
    (artifact_dir / "coverage.txt").write_text(report_text, encoding="utf-8")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the hermetic safe suite with package and boundary coverage; "
            "live FL Studio paths remain outside CI coverage."
        )
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="Optional directory for coverage.json and coverage.txt.",
    )
    parser.add_argument(
        "--global-threshold",
        type=float,
        default=GLOBAL_THRESHOLD,
        help=f"Package coverage floor (default: {GLOBAL_THRESHOLD:g}).",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    artifact_dir = (
        args.artifact_dir
        if args.artifact_dir is None or args.artifact_dir.is_absolute()
        else ROOT / args.artifact_dir
    )

    with tempfile.TemporaryDirectory(prefix="postfader-coverage-") as raw_dir:
        raw_path = Path(raw_dir)
        environment = os.environ.copy()
        environment[COVERAGE_ENV] = os.fspath(raw_path)
        safe_result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_safe_tests.py")],
            cwd=ROOT,
            env=environment,
            check=False,
        )
        if safe_result.returncode != 0:
            return safe_result.returncode or 1

        data_file = raw_path / ".coverage"
        combine_result = _run(
            _coverage_command(
                "combine", "--data-file", os.fspath(data_file), os.fspath(raw_path)
            )
        )
        if combine_result != 0:
            return combine_result

        json_path = raw_path / "coverage.json"
        json_result = subprocess.run(
            _coverage_command(
                "json",
                "--data-file",
                os.fspath(data_file),
                "--pretty-print",
                "-o",
                os.fspath(json_path),
            ),
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if json_result.returncode != 0:
            print(json_result.stdout, end="")
            print(json_result.stderr, end="", file=sys.stderr)
            return json_result.returncode

        report_result = subprocess.run(
            _coverage_command(
                "report",
                "--data-file",
                os.fspath(data_file),
                "--show-missing",
                "--fail-under",
                str(args.global_threshold),
            ),
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        print(report_result.stdout, end="")
        print(report_result.stderr, end="", file=sys.stderr)
        _write_artifacts(
            artifact_dir,
            json_path=json_path,
            report_text=report_result.stdout + report_result.stderr,
        )
        if report_result.returncode != 0:
            return report_result.returncode

        report = json.loads(json_path.read_text(encoding="utf-8"))
        files = report.get("files", {})
        totals = report.get("totals", {})
        total_percent = float(totals.get("percent_covered", 0.0))
        print(
            f"PACKAGE COVERAGE: {total_percent:.1f}% "
            f"(threshold {args.global_threshold:.1f}%)"
        )
        group_failed = False
        for group in COVERAGE_GROUPS:
            statements, covered, percent = _group_summary(files, group)
            print(
                f"{group.name}: {percent:.1f}% "
                f"({covered}/{statements} statements; threshold {group.threshold:.1f}%)"
            )
            if statements == 0 or percent < group.threshold:
                group_failed = True
        if group_failed:
            print(
                "Coverage boundary threshold failed. These floors describe "
                "hermetic evidence only; live FL Studio qualification remains manual.",
                file=sys.stderr,
            )
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
