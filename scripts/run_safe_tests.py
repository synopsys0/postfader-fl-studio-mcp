#!/usr/bin/env python3
"""Run the explicit safe suite without touching any physical MIDI endpoint.

The allowlist is intentional. ``tests/test_midi_transport.py`` contains real
physical-MIDI traffic and is never discovered or run by this command.
Every included test is self-contained and must pass in a fresh clone without
FL Studio, a MIDI device, user projects, or user audio.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


# The suite mixes two harnesses: unittest files report "Ran N tests" and the
# hand-rolled check files report "N passed, M failed". Both are counted so the
# documented total is derived from the run instead of maintained by hand.
UNITTEST_TOTAL = re.compile(r"^Ran (\d+) tests?\b", re.MULTILINE)
CHECK_TOTAL = re.compile(r"^(\d+) passed, (\d+) failed$", re.MULTILINE)
SAFE_TEST_TIMEOUT_SECONDS = 180
COVERAGE_DIRECTORY_ENV = "POSTFADER_COVERAGE_DIR"


def count_checks(relative: str, output: str) -> int:
    """Return the number of checks a file reported, or -1 if it reported none."""
    unittest_counts = [int(value) for value in UNITTEST_TOTAL.findall(output)]
    check_counts = [int(passed) + int(failed) for passed, failed in CHECK_TOTAL.findall(output)]
    if not unittest_counts and not check_counts:
        return -1
    return sum(unittest_counts) + sum(check_counts)


ROOT = Path(__file__).resolve().parents[1]
SAFE_TESTS = (
    "tests/test_bridge_stamp.py",
    "tests/test_bridge_install.py",
    "tests/test_windows_bootstrap.py",
    "tests/test_setup_wizard.py",
    "tests/test_codex_installer.py",
    "tests/test_diagnostics.py",
    "tests/test_live_acceptance.py",
    "tests/test_bridge.py",
    "tests/test_audio.py",
    "tests/test_advisory.py",
    "tests/test_file_transport.py",
    "tests/test_readonly.py",
    "tests/test_readonly_mcp.py",
    "tests/test_sdk_compatibility.py",
    "tests/test_tick_budget.py",
    "tests/test_midi_framing.py",
    "tests/test_resource_bounds.py",
    "tests/test_plugin_profile.py",
    "tests/test_plugin_report.py",
    "tests/test_bridge_client_recovery.py",
    "tests/test_fixtures.py",
    "tests/test_package_hygiene.py",
    "tests/test_release_bundles.py",
    "tests/test_sbom.py",
    "tests/test_mcpb.py",
    "tests/test_performance.py",
    "tests/test_workflows.py",
    "tests/test_mixing.py",
    "tests/test_creative.py",
    "tests/test_production_runs.py",
)


def safe_child_environment() -> dict[str, str]:
    """Return an isolated child environment that cannot initialize MIDI.

    A developer commonly has MIDI and write opt-ins in the shell used to launch
    an MCP client. The safe runner must not inherit either opt-in and rely on
    every test to undo it before importing runtime modules. Keep the independent
    fail-closed gates explicit in every child instead.
    """

    environment = os.environ.copy()
    environment["FL_BRIDGE_SANDBOXED"] = "1"
    environment["FL_BRIDGE_ENABLE_MIDI"] = "0"
    environment["FL_BRIDGE_ENABLE_WRITES"] = "0"
    environment["FL_BRIDGE_MIDI_PORT"] = "SAFE_TEST_MIDI_DISABLED"
    return environment


def run_safe_test(path: Path) -> subprocess.CompletedProcess[str]:
    """Run one allowlisted file with isolation and a bounded wall clock."""

    environment = safe_child_environment()
    coverage_directory = os.environ.get(COVERAGE_DIRECTORY_ENV)
    if coverage_directory:
        # Each child writes a parallel data file. The coverage wrapper combines
        # them after the suite, so no test process shares mutable coverage state.
        environment["COVERAGE_FILE"] = os.fspath(
            Path(coverage_directory) / ".coverage"
        )
        command = [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--parallel-mode",
            str(path),
        ]
    else:
        command = [sys.executable, "-B", str(path)]

    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=SAFE_TEST_TIMEOUT_SECONDS,
    )


def _timeout_output(error: subprocess.TimeoutExpired) -> str:
    """Return any bounded partial child output as text."""

    pieces: list[str] = []
    for value in (error.stdout, error.stderr):
        if isinstance(value, bytes):
            pieces.append(value.decode(errors="replace"))
        elif isinstance(value, str):
            pieces.append(value)
    return "".join(pieces)


def main() -> int:
    total_checks = 0
    for relative in SAFE_TESTS:
        path = ROOT / relative
        if not path.is_file():
            print(f"SAFE TEST LIST ERROR: missing {relative}", file=sys.stderr)
            return 2
        print(f"\n=== {relative} ===", flush=True)
        try:
            completed = run_safe_test(path)
        except subprocess.TimeoutExpired as error:
            print(_timeout_output(error), end="", flush=True)
            print(
                f"SAFE TEST FAILURE: {relative} exceeded "
                f"{SAFE_TEST_TIMEOUT_SECONDS} seconds",
                file=sys.stderr,
            )
            return 1
        output = completed.stdout + completed.stderr
        print(output, end="", flush=True)
        if completed.returncode != 0:
            print(
                f"SAFE TEST FAILURE: {relative} exited {completed.returncode}",
                file=sys.stderr,
            )
            return completed.returncode or 1
        checks = count_checks(relative, output)
        if checks < 0:
            # A file that exits 0 without reporting a single check is not
            # evidence of anything; fail rather than silently count zero.
            print(
                f"SAFE TEST FAILURE: {relative} reported no checks",
                file=sys.stderr,
            )
            return 1
        total_checks += checks
    print(
        f"\nSAFE TEST SUITE PASSED: {total_checks} checks "
        f"across {len(SAFE_TESTS)} allowlisted files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
