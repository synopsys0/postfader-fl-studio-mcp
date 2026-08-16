#!/usr/bin/env python3
"""Plan or execute every authoritative public read tool and emit evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT))

from fl_studio_mcp.acceptance import (  # noqa: E402
    AcceptanceConfigurationError,
    IsolatedReadToolSupervisor,
    authoritative_tool_surface,
    read_acceptance_arguments,
    run_read_acceptance,
)
from fl_studio_mcp.evidence import (  # noqa: E402
    EvidenceOutputError,
    configure_acceptance_transport,
    reserve_evidence_output,
    structured_failure,
)
from fl_studio_mcp.host_config import HostConfigurationError  # noqa: E402


def positive_finite_seconds(value):
    try:
        resolved = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(resolved) or resolved <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return resolved


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Exercise every current MCP read tool. --plan performs no FL, MIDI, "
            "or tool calls; a live run records failures rather than inventing support."
        )
    )
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--mixer-track", type=int, default=1)
    parser.add_argument("--plugin-track", type=int, default=1)
    parser.add_argument("--plugin-slot", type=int, default=0)
    parser.add_argument("--pattern", type=int, default=1)
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument(
        "--midi-port",
        help="Exact virtual MIDI endpoint name. Omit to keep native MIDI disabled.",
    )
    parser.add_argument(
        "--per-tool-timeout-seconds",
        type=positive_finite_seconds,
        default=180.0,
        help="Hard parent-owned deadline for each isolated live read (default: 180).",
    )
    parser.add_argument(
        "--overall-timeout-seconds",
        type=positive_finite_seconds,
        default=1200.0,
        help="Hard deadline for the complete live read sequence (default: 1200).",
    )
    parser.add_argument("--output", help="Create this new evidence file; never overwrite.")
    return parser.parse_args(argv)


async def async_main(args, *, checkpoint=None):
    started_at = time.monotonic()
    surface = await authoritative_tool_surface()
    arguments = read_acceptance_arguments(
        mixer_track_index=args.mixer_track,
        plugin_track_index=args.plugin_track,
        plugin_slot_index=args.plugin_slot,
        pattern_number=args.pattern,
        channel_index=args.channel,
        fixture_root=ROOT / "tests" / "fixtures" / "audio",
    )
    if args.plan:
        value = {
            "mode": "plan_only",
            "physical_io_performed": False,
            "authoritative_tools": list(surface.all_tools),
            "read_tools": list(surface.read_tools),
            "workflow_reads_excluded": list(surface.workflow_read_tools),
            "arguments": arguments,
            "requested_midi_port": args.midi_port,
            "per_tool_timeout_seconds": args.per_tool_timeout_seconds,
            "overall_timeout_seconds": args.overall_timeout_seconds,
        }
    else:
        supervisor = IsolatedReadToolSupervisor()
        try:
            value = await run_read_acceptance(
                arguments,
                surface=surface,
                checkpoint=checkpoint,
                bounded_caller=supervisor.invoke,
                per_tool_timeout_seconds=args.per_tool_timeout_seconds,
                overall_timeout_seconds=args.overall_timeout_seconds,
                started_at=started_at,
            )
        finally:
            supervisor.close()
    return value


def _evidence_output_failure(stage, error):
    return {"stage": stage, "reason": str(error)}


def _finish(destination, value, *, contact_started):
    """Persist and close without allowing evidence errors to escape."""

    output_failures = list(value.get("evidence_output_failures", []))
    if destination is not None:
        try:
            destination.write(value)
        except Exception as exc:
            output_failures.append(
                _evidence_output_failure("final_evidence_write", exc)
            )
        try:
            destination.close()
        except Exception as exc:
            output_failures.append(
                _evidence_output_failure("final_evidence_close", exc)
            )
    if output_failures:
        failure = structured_failure(
            kind="postfader_read_acceptance",
            phase="final_evidence_output",
            error="one or more evidence output operations failed",
            contact_started=contact_started,
        )
        failure["evidence_output_failures"] = output_failures
        print(json.dumps(failure, indent=2, sort_keys=True), file=sys.stderr)
        return 1 if contact_started else 2
    if destination is not None:
        print(destination.path)
    else:
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
    if value.get("overall") == "fail":
        return 1
    if value.get("overall") == "refused":
        return 2
    return 0


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
                    kind="postfader_read_acceptance",
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
            try:
                destination.write(
                    {
                        "schema_version": 1,
                        "kind": "postfader_read_acceptance",
                        "overall": "started",
                        "phase": "contact_started",
                        "contact_started": True,
                        "project_saved": False,
                    }
                )
            except Exception as exc:
                value = structured_failure(
                    kind="postfader_read_acceptance",
                    phase="evidence_output_before_contact",
                    error=exc,
                    contact_started=False,
                )
                value["evidence_output_failures"] = [
                    _evidence_output_failure("contact_started_checkpoint", exc)
                ]
                return _finish(destination, value, contact_started=False)
        try:
            value = asyncio.run(
                async_main(
                    args,
                    checkpoint=(
                        destination.write
                        if destination is not None and not args.plan
                        else None
                    ),
                )
            )
        except (AcceptanceConfigurationError, OSError, ValueError) as exc:
            value = structured_failure(
                kind="postfader_read_acceptance",
                phase="read_execution",
                error=exc,
                contact_started=not args.plan,
            )
        except BaseException as exc:
            value = structured_failure(
                kind="postfader_read_acceptance",
                phase="interrupted_execution",
                error="%s: %s" % (type(exc).__name__, exc),
                contact_started=not args.plan,
            )
        return _finish(destination, value, contact_started=not args.plan)
    except HostConfigurationError as exc:
        value = structured_failure(
            kind="postfader_read_acceptance",
            phase="transport_configuration",
            error=exc,
            contact_started=False,
        )
        print(json.dumps(value, indent=2, sort_keys=True), file=sys.stderr)
        return _finish(destination, value, contact_started=False)


if __name__ == "__main__":
    raise SystemExit(main())
