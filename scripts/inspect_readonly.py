#!/usr/bin/env python3
"""Run the read-only inspector directly and print strict JSON to stdout.

This command has no write operation and uses the same fail-closed allowlist as
the read-only MCP server.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

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

def _midi_port_name(value: str) -> str:
    query = value.strip()
    if not query:
        raise argparse.ArgumentTypeError("must not be empty")
    return query


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect a running FL Studio 2026 session without changing it."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--capabilities",
        action="store_true",
        help="Print the validated/unvalidated capability matrix instead of project state.",
    )
    mode.add_argument(
        "--selected-range",
        action="store_true",
        help=(
            "Print repeated raw Playlist endpoints and PPQ context; state, presence, "
            "units, normalized ticks, and rendering remain unvalidated."
        ),
    )
    filtering = parser.add_mutually_exclusive_group()
    filtering.add_argument(
        "--only-used",
        action="store_true",
        help="Apply the conservative used-track heuristic (full scan is default).",
    )
    filtering.add_argument(
        "--all-tracks",
        action="store_true",
        help="Explicitly request the authoritative full scan (already the default).",
    )
    parser.add_argument(
        "--parameter-limit",
        type=int,
        default=16,
        choices=range(1, 65),
        metavar="1..64",
        help="Parameter indices previewed per loaded plug-in (default: 16).",
    )
    parser.add_argument(
        "--max-plugins",
        type=int,
        default=16,
        choices=range(1, 65),
        metavar="1..64",
        help="Maximum loaded plug-ins to preview (default: 16).",
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
        if args.capabilities:
            result = inspector.capabilities()
        elif args.selected_range:
            result = inspector.selected_range()
        else:
            result = inspector.capture(
                only_used=args.only_used and not args.all_tracks,
                parameter_limit=args.parameter_limit,
                max_plugins=args.max_plugins,
            )
    except (BridgeError, IncompatibleFLStudio, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    print(result.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
