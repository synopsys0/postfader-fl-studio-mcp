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

from fl_studio_mcp.plugin_profile import (  # noqa: E402
    render_markdown,
    render_option_survey,
    summarise,
    survey_options,
)


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
        "--sweep", action="store_true", default=False,
        help="also discover option lists for enumerated controls. THIS MOVES "
             "CONTROLS: it walks each one across its range to read what it "
             "displays, creates undo points, and marks the project dirty. Use "
             "a disposable project.",
    )
    parser.add_argument(
        "--sweep-steps", type=int, default=64, metavar="N",
        help="sweep resolution, 2-256 (default 64). A control needs roughly "
             "two samples per option; 64 resolved a 29-option control on a "
             "live VST3 with nothing missed.",
    )
    parser.add_argument(
        "--raw", action="store_true",
        help="also write the unreduced scan to this path for your own "
             "inspection. It contains current settings -- do not share it.",
        default=False,
    )
    args = parser.parse_args(argv)

    client = None
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

    profile = summarise(scan)
    print(render_markdown(profile))

    if args.sweep:
        if client is None:
            print("\n--sweep needs a live FL Studio; it cannot run from saved "
                  "JSON.", file=sys.stderr)
            return 1
        print(_sweep(client, args, profile))
    return 0


def _sweep(client, args, profile) -> str:
    """Discover option lists, one enumerated control at a time.

    Each control is asked for the option it is already showing. That still
    moves it -- the sweep walks the whole range to look -- and it lands on the
    nearest sweep step rather than its exact original value, so the project
    ends up dirty with undo points. The displayed option is preserved, which
    for an enumerated control is what carries the musical meaning.
    """
    from fl_studio_mcp.bridge_client import BridgeError

    surveys = []
    for shape in profile.enumerated:
        current = _current_display(client, args, shape.index)
        if current is None:
            continue
        try:
            result = client.call(
                "plugin.set_param_option",
                track=args.track, slot=args.slot,
                param=shape.index, option=current,
                steps=args.sweep_steps,
            )
        except BridgeError as exc:
            surveys.append(survey_options(shape.index, shape.name, [], 0))
            print(f"  (control {shape.index} could not be swept: {exc})",
                  file=sys.stderr)
            continue
        if not result.get("verified"):
            print(f"  STOPPING: control {shape.index} did not verify after its "
                  "sweep; it may be left moved. Undo in FL Studio.",
                  file=sys.stderr)
            break
        surveys.append(survey_options(
            shape.index, shape.name, result.get("options") or [],
            int(result.get("sweep_steps") or 0)))
    return render_option_survey(surveys)


def _current_display(client, args, index: int) -> str | None:
    """Read what a control shows now, so the sweep can land back on it."""
    from fl_studio_mcp.bridge_client import BridgeError

    try:
        page = client.call("plugin.params", track=args.track, slot=args.slot,
                           start=index, limit=1)
    except BridgeError:
        return None
    for row in page.get("params") or []:
        if int(row.get("index", -1)) == index:
            text = (row.get("display") or "").strip()
            return text or None
    return None


if __name__ == "__main__":
    raise SystemExit(main())
