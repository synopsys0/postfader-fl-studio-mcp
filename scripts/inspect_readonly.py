#!/usr/bin/env python3
"""Run the Phase 1 inspector directly and print strict JSON to stdout.

This command has no write operation and uses the same fail-closed allowlist as
the read-only MCP server.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV = os.path.join(ROOT, ".venv")
VENV_PYTHON = os.path.join(VENV, "bin", "python")
if (
    os.path.isfile(VENV_PYTHON)
    and os.path.realpath(sys.prefix) != os.path.realpath(VENV)
):
    os.execv(VENV_PYTHON, [VENV_PYTHON, *sys.argv])
os.environ.setdefault("FL_BRIDGE_ENABLE_MIDI", "1")
sys.path.insert(0, ROOT)

from fl_studio_mcp.bridge_client import BridgeError  # noqa: E402
from fl_studio_mcp.readonly_inspector import (  # noqa: E402
    IncompatibleFLStudio,
    ReadOnlyInspector,
)


def parse_args() -> argparse.Namespace:
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
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
