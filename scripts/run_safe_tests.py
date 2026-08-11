#!/usr/bin/env python3
"""Run the explicit safe suite without touching the physical IAC bus.

The allowlist is intentional. ``tests/test_midi_transport.py`` contains real
physical-MIDI traffic and is never discovered or run by this command.
Every included test is self-contained and must pass in a fresh clone without
FL Studio, a MIDI device, user projects, or user audio.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


# The suite mixes two harnesses: unittest files report "Ran N tests" and the
# hand-rolled check files report "N passed, M failed". Both are counted so the
# documented total is derived from the run instead of maintained by hand.
UNITTEST_TOTAL = re.compile(r"^Ran (\d+) tests?\b", re.MULTILINE)
CHECK_TOTAL = re.compile(r"^(\d+) passed, (\d+) failed$", re.MULTILINE)


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
    "tests/test_bridge.py",
    "tests/test_audio.py",
    "tests/test_advisory.py",
    "tests/test_file_transport.py",
    "tests/test_readonly.py",
    "tests/test_readonly_mcp.py",
    "tests/test_tick_budget.py",
    "tests/test_midi_framing.py",
    "tests/test_bridge_client_recovery.py",
    "tests/test_fixtures.py",
    "tests/test_package_hygiene.py",
)


def main() -> int:
    total_checks = 0
    for relative in SAFE_TESTS:
        path = ROOT / relative
        if not path.is_file():
            print(f"SAFE TEST LIST ERROR: missing {relative}", file=sys.stderr)
            return 2
        print(f"\n=== {relative} ===", flush=True)
        completed = subprocess.run(
            [sys.executable, "-B", str(path)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
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
