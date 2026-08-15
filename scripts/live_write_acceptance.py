#!/usr/bin/env python3
"""Execute a reviewed all-surface write scenario with exact restoration."""

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
    run_write_acceptance,
    validate_live_write_marker,
    validate_write_scenario,
    validate_write_scenario_plan,
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
        description=(
            "Apply each authoritative persistent write once, verify it, restore "
            "captured before state, and independently verify restoration."
        )
    )
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument(
        "--midi-port",
        help="Exact virtual MIDI endpoint name. Omit to keep native MIDI disabled.",
    )
    parser.add_argument("--confirm-user-present", action="store_true")
    parser.add_argument("--confirm-disposable-project", action="store_true")
    parser.add_argument("--confirm-safe-to-edit", action="store_true")
    parser.add_argument(
        "--acknowledge-master-tool",
        action="append",
        default=[],
        metavar="TOOL",
        help="Individually acknowledge one scenario operation that targets Master.",
    )
    parser.add_argument("--output", help="Create this new evidence file; never overwrite.")
    return parser.parse_args(argv)


async def async_main(args, *, checkpoint=None):
    scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    if not args.plan:
        validate_live_write_marker(scenario)
    surface = await authoritative_tool_surface()
    validate_write_scenario(surface, scenario)
    if args.plan:
        prepared = validate_write_scenario_plan(
            surface,
            scenario,
            acknowledged_master_tools=args.acknowledge_master_tool,
        )
        value = {
            "mode": "plan_only",
            "physical_io_performed": False,
            "persistent_write_tools": list(surface.persistent_write_tools),
            "ephemeral_tools_excluded": list(surface.ephemeral_tools),
            "scenario": scenario,
            "fully_resolved_operation_count": len(prepared),
            "requested_midi_port": args.midi_port,
        }
    else:
        value = await run_write_acceptance(
            scenario,
            confirm_user_present=args.confirm_user_present,
            confirm_disposable_project=args.confirm_disposable_project,
            confirm_safe_to_edit=args.confirm_safe_to_edit,
            acknowledged_master_tools=args.acknowledge_master_tool,
            surface=surface,
            checkpoint=checkpoint,
        )
    return value


def _evidence_output_failure(stage, error):
    return {"stage": stage, "reason": str(error)}


def _finish(destination, value, *, contact_started):
    """Persist the final result and close without allowing output errors to escape."""

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
            kind="postfader_write_acceptance",
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
                    kind="postfader_write_acceptance",
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
        if not args.plan:
            scenario_preview = json.loads(
                Path(args.scenario).read_text(encoding="utf-8")
            )
            validate_live_write_marker(scenario_preview)
    except (AcceptanceConfigurationError, OSError, ValueError) as exc:
        value = structured_failure(
            kind="postfader_write_acceptance",
            phase="scenario_eligibility",
            error=exc,
            contact_started=False,
        )
        print(json.dumps(value, indent=2, sort_keys=True), file=sys.stderr)
        return _finish(destination, value, contact_started=False)
    try:
        configure_acceptance_transport(args.midi_port, live=not args.plan)
    except HostConfigurationError as exc:
        value = structured_failure(
            kind="postfader_write_acceptance",
            phase="transport_configuration",
            error=exc,
            contact_started=False,
        )
        print(json.dumps(value, indent=2, sort_keys=True), file=sys.stderr)
        return _finish(destination, value, contact_started=False)

    if destination is not None and not args.plan:
        try:
            destination.write(
                {
                    "schema_version": 1,
                    "kind": "postfader_write_acceptance",
                    "overall": "started",
                    "phase": "contact_started",
                    "contact_started": True,
                    "project_saved": False,
                }
            )
        except Exception as exc:
            value = structured_failure(
                kind="postfader_write_acceptance",
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
            kind="postfader_write_acceptance",
            phase="scenario_or_preflight",
            error=exc,
            contact_started=not args.plan,
        )
    except Exception as exc:
        value = structured_failure(
            kind="postfader_write_acceptance",
            phase="unexpected_execution_failure",
            error=exc,
            contact_started=not args.plan,
        )
    return _finish(destination, value, contact_started=not args.plan)


if __name__ == "__main__":
    raise SystemExit(main())
