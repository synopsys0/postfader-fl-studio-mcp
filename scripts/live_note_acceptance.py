#!/usr/bin/env python3
"""Run one explicitly supervised live-note dispatch, separate from writes."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT))

from fl_studio_mcp.acceptance import (  # noqa: E402
    AcceptanceConfigurationError,
    authoritative_tool_surface,
    tool_payload,
)
from fl_studio_mcp.evidence import (  # noqa: E402
    EvidenceOutputError,
    configure_acceptance_transport,
    reserve_evidence_output,
    structured_failure,
)
from fl_studio_mcp.host_config import HostConfigurationError  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Dispatch one bounded live note. This is not a persistent-write test."
    )
    parser.add_argument("--channel", type=int, required=True)
    parser.add_argument("--note", type=int, default=60)
    parser.add_argument("--velocity", type=int, default=80)
    parser.add_argument("--duration-ms", type=int, default=250)
    parser.add_argument(
        "--midi-port",
        help="Exact virtual MIDI endpoint name. Omit to keep native MIDI disabled.",
    )
    parser.add_argument("--confirm-user-present", action="store_true")
    parser.add_argument("--confirm-disposable-project", action="store_true")
    parser.add_argument("--confirm-live-note-dispatch", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--output", help="Required for live mode; must be a new file.")
    return parser.parse_args(argv)


async def async_main(args):
    surface = await authoritative_tool_surface()
    if "fl_trigger_note" not in surface.ephemeral_tools:
        raise AcceptanceConfigurationError(
            "fl_trigger_note is not currently annotated as a separate ephemeral tool"
        )
    arguments = {
        "channel_index": args.channel,
        "note": args.note,
        "velocity": args.velocity,
        "duration_ms": args.duration_ms,
    }
    if args.plan:
        return {
            "mode": "plan_only",
            "physical_io_performed": False,
            "tool": "fl_trigger_note",
            "arguments": arguments,
            "requested_midi_port": args.midi_port,
        }
    if not (
        args.confirm_user_present
        and args.confirm_disposable_project
        and args.confirm_live_note_dispatch
    ):
        raise AcceptanceConfigurationError(
            "live-note acceptance requires all three explicit confirmation switches"
        )
    # Transport configuration is complete before this import can initialize a
    # bridge client. Plan mode returns above without importing the server here.
    from fl_studio_mcp import mcp_server

    try:
        result = tool_payload(await mcp_server.mcp.call_tool("fl_trigger_note", arguments))
    except Exception as exc:
        return {
            "schema_version": 1,
            "kind": "postfader_live_note_acceptance",
            "overall": "fail",
            "phase": "note_dispatch",
            "contact_started": True,
            "project_saved": False,
            "tool": "fl_trigger_note",
            "attempts": 1,
            "automatic_replay": False,
            "error": str(exc),
        }
    return {
        "schema_version": 1,
        "kind": "postfader_live_note_acceptance",
        "overall": "pass",
        "phase": "complete",
        "contact_started": True,
        "project_saved": False,
        "tool": "fl_trigger_note",
        "receipt": result,
    }


def main(argv=None):
    args = parse_args(argv)
    try:
        destination = reserve_evidence_output(
            args.output, required=not args.plan
        )
    except EvidenceOutputError as exc:
        print(
            json.dumps(
                structured_failure(
                    kind="postfader_live_note_acceptance",
                    phase="output_reservation",
                    error=exc,
                    contact_started=False,
                ),
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        configure_acceptance_transport(args.midi_port, live=not args.plan)
        if destination is not None and not args.plan:
            destination.write(
                {
                    "schema_version": 1,
                    "kind": "postfader_live_note_acceptance",
                    "overall": "started",
                    "phase": "contact_started",
                    "contact_started": True,
                    "project_saved": False,
                }
            )
        try:
            value = asyncio.run(async_main(args))
        except AcceptanceConfigurationError as exc:
            value = structured_failure(
                kind="postfader_live_note_acceptance",
                phase="confirmation_or_tool_validation",
                error=exc,
                contact_started=not args.plan,
            )
        except Exception as exc:
            value = structured_failure(
                kind="postfader_live_note_acceptance",
                phase="unexpected_execution_failure",
                error=exc,
                contact_started=not args.plan,
            )
        if destination is not None:
            destination.write(value)
            print(destination.path)
        else:
            print(json.dumps(value, indent=2, sort_keys=True, default=str))
        return 1 if value.get("overall") == "fail" else 2 if value.get("overall") == "refused" else 0
    except HostConfigurationError as exc:
        value = structured_failure(
            kind="postfader_live_note_acceptance",
            phase="transport_configuration",
            error=exc,
            contact_started=False,
        )
        if destination is not None:
            destination.write(value)
        print(json.dumps(value, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    finally:
        if destination is not None:
            destination.close()


if __name__ == "__main__":
    raise SystemExit(main())
