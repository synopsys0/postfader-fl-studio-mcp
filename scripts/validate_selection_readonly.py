#!/usr/bin/env python3
"""Run a bounded, read-only repeatability check for Playlist selection reads.

This diagnostic intentionally preserves FL Studio's raw endpoint values.  It
does not infer whether a selection exists, what unit the values use, or whether
the end point would be inclusive during export.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
VENV_PYTHON = next(
    (
        candidate
        for candidate in (VENV / "Scripts" / "python.exe", VENV / "bin" / "python")
        if candidate.is_file()
    ),
    None,
)
if (
    VENV_PYTHON is not None
    and Path(sys.prefix).resolve() != VENV.resolve()
):
    command = [os.fspath(VENV_PYTHON), *sys.argv]
    if os.name == "nt":
        # CPython's Windows exec emulation can misquote a spaced executable
        # path (notably with the Microsoft Store launcher).  A direct,
        # shell-free child preserves the arguments and inherited stdio.
        raise SystemExit(subprocess.call(command))
    os.execv(command[0], command)
sys.path.insert(0, os.fspath(ROOT))

from fl_studio_mcp.contracts import ProjectSummary  # noqa: E402


def _midi_port_name(value: str) -> str:
    query = value.strip()
    if not query:
        raise argparse.ArgumentTypeError("must not be empty")
    return query


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repeat FL Studio's raw Playlist selection read without changing "
            "the project or interpreting the endpoint values."
        )
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=10,
        choices=range(2, 21),
        metavar="2..20",
        help="Number of bounded sequential observations (default: 10).",
    )
    parser.add_argument(
        "--midi-port",
        type=_midi_port_name,
        help=(
            "exact virtual MIDI endpoint name; required on Windows unless "
            "FL_BRIDGE_MIDI_PORT is already set"
        ),
    )
    return parser.parse_args(argv)


def project_token(summary: ProjectSummary) -> dict[str, int | None]:
    return {
        "dirty_flag": summary.dirty_flag,
        "undo_history_position": summary.undo_history_position,
        "undo_history_count": summary.undo_history_count,
    }


def transport_token(summary: ProjectSummary) -> dict[str, Any]:
    transport = summary.transport
    return {
        "playing": transport.playing,
        "recording": transport.recording,
        "loop_mode": transport.loop_mode,
        "song_position_normalized": transport.song_position_normalized,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    os.environ["FL_BRIDGE_ENABLE_MIDI"] = "1"
    if args.midi_port is not None:
        os.environ["FL_BRIDGE_MIDI_PORT"] = args.midi_port
    from fl_studio_mcp.bridge_client import BridgeError
    from fl_studio_mcp.readonly_inspector import (
        IncompatibleFLStudio,
        ReadOnlyInspector,
    )

    inspector = ReadOnlyInspector()
    try:
        before = inspector.project_summary()
        if before.transport.recording is not False:
            raise ValueError(
                "recording must be confirmed off before the live repeatability check"
            )
        observations = [inspector.selected_range() for _ in range(args.samples)]
        after = inspector.project_summary()
    except (BridgeError, IncompatibleFLStudio, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    sample_values = [
        {
            "observed_at": item.observed_at.isoformat(),
            "raw_start_time": item.raw_start_time,
            "raw_end_time": item.raw_end_time,
            "timebase_ppq": item.timebase_ppq,
            "raw_start_display_hint": item.raw_start_display_hint,
            "raw_end_display_hint": item.raw_end_display_hint,
            "repeated_read_consistent": item.repeated_read_consistent,
            "interpretation_status": item.interpretation_status,
            "selection_state": item.selection_state,
            "selection_presence": item.selection_presence,
            "raw_time_unit": item.raw_time_unit,
            "start_ticks": item.start_ticks,
            "end_ticks": item.end_ticks,
            "duration_ticks": item.duration_ticks,
            "safe_for_rendering": item.safe_for_rendering,
        }
        for item in observations
    ]
    comparable = {
        (
            item.raw_start_time,
            item.raw_end_time,
            item.timebase_ppq,
            item.raw_start_display_hint,
            item.raw_end_display_hint,
        )
        for item in observations
    }
    before_project_token = project_token(before)
    after_project_token = project_token(after)
    before_transport_token = transport_token(before)
    after_transport_token = transport_token(after)
    all_samples_identical = len(comparable) == 1
    project_token_stable = before_project_token == after_project_token
    interpretation_statuses = sorted(
        {item.interpretation_status for item in observations}
    )
    selection_states = sorted({item.selection_state for item in observations})
    selection_presence = sorted(
        {item.selection_presence for item in observations}
    )
    raw_time_units = sorted({item.raw_time_unit for item in observations})
    transport_mode_stable = (
        before_transport_token["playing"] == after_transport_token["playing"]
        and before_transport_token["recording"]
        == after_transport_token["recording"]
        and before_transport_token["loop_mode"] == after_transport_token["loop_mode"]
    )
    stopped_position_stable = None
    if before.transport.playing is False and after.transport.playing is False:
        stopped_position_stable = (
            before.transport.song_position_normalized
            == after.transport.song_position_normalized
        )

    ok = (
        all_samples_identical
        and project_token_stable
        and transport_mode_stable
        and stopped_position_stable is not False
        and after.transport.recording is False
    )
    result = {
        "ok": ok,
        "schema_version": "1.0",
        "sample_count": len(observations),
        "all_samples_identical": all_samples_identical,
        "project_token_stable": project_token_stable,
        "transport_mode_stable": transport_mode_stable,
        "stopped_position_stable": stopped_position_stable,
        "before_project_token": before_project_token,
        "after_project_token": after_project_token,
        "before_transport_token": before_transport_token,
        "after_transport_token": after_transport_token,
        "samples": sample_values,
        "interpretation_statuses": interpretation_statuses,
        "selection_states": selection_states,
        "selection_presence": selection_presence,
        "raw_time_units": raw_time_units,
        "safe_for_rendering": False,
        "warnings": [
            "Bounded repeatability does not widen the exact semantic scope reported by each observation.",
            "The public dirty and undo fields are a coarse token, not an atomic project revision.",
            "Export endpoint inclusivity remains unvalidated.",
        ],
    }
    print(json.dumps(result, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
