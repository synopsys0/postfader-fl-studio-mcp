#!/usr/bin/env python3
"""Profile one loaded plug-in and print a row for the compatibility table.

    ./scripts/plugin_report.py --track 3 --slot 1

Read-only. It calls `plugin.scan_params`, which is on the bridge's read-only
allowlist and changes nothing, so it is safe against a project you are working
in. It does not need write mode and cannot enable it.

The printed report describes the plug-in and never its settings: no value, no
display string, no preset. That is deliberate -- a control reading `Key = A`
tells the world what key your song is in. Read what it prints before sharing
it, but it is built so there should be nothing to redact.

Discovering an enumerated control's options is a separate matter: the only way
to learn them is to move the control and read what it says, which mutates the
project. This tool never does that, and reports such controls as "enumerated,
options undiscovered".
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FL_BRIDGE_ENABLE_MIDI", "1")

from fl_studio_mcp.plugin_profile import render_markdown, summarise  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="plugin_report.py",
        description="Summarise a loaded plug-in's parameter map for sharing.",
    )
    parser.add_argument("--track", type=int, help="mixer track index")
    parser.add_argument("--slot", type=int, help="effect slot index, 0-9")
    parser.add_argument(
        "--max-indices", type=int, default=None,
        help="stop after examining this many indices (default: the bridge's own bound)",
    )
    parser.add_argument(
        "--from-json", metavar="PATH", default=None,
        help="summarise a saved scan instead of querying FL Studio; useful for "
             "reviewing what would be shared before sharing it",
    )
    parser.add_argument(
        "--raw", action="store_true",
        help="also write the unreduced scan to this path for your own "
             "inspection. It contains current settings -- do not share it.",
        default=False,
    )
    args = parser.parse_args(argv)

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as handle:
            scan = json.load(handle)
    else:
        if args.track is None or args.slot is None:
            parser.error("--track and --slot are required unless --from-json is used")
        try:
            from fl_studio_mcp.bridge_client import BridgeError, get_client
        except ImportError as exc:  # pragma: no cover - install problem
            print(f"error: cannot import the connector: {exc}", file=sys.stderr)
            return 1
        client = get_client()
        arguments = {"track": args.track, "slot": args.slot}
        if args.max_indices is not None:
            arguments["max_indices"] = args.max_indices
        try:
            scan = client.call("plugin.scan_params", **arguments)
        except BridgeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.raw:
        print(json.dumps(scan, indent=2), file=sys.stderr)
        print("--- the JSON above went to stderr and contains live settings; "
              "the report below is what is safe to share ---", file=sys.stderr)

    print(render_markdown(summarise(scan)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
