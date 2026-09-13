#!/usr/bin/env python3
"""Run or plan the maintainer live creation acceptance workflow.

This harness is intentionally conservative.  It expects a blank, disposable
FL Studio project whose generators, empty pattern, and Piano Roll bridge are
already prepared by the maintainer.  ``--plan`` builds the complete typed
request without importing the MCP server or contacting FL Studio.  A live run
uses one ``postfader_execute_run`` call and one process-local run context for
either an armed-ready composition fixture or an armed-ready production
fixture.

Live evidence is create-only and must be written outside this checkout.  The
report records observed receipts and timing, but it never claims that a
musical or audible target was achieved.  Audible quality remains
``not_evaluated`` until a user confirms it or supplies a bounce through the
separate audio-analysis tools.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT))

ACCEPTANCE_KIND = "postfader_creation_acceptance"
SCENARIOS = ("composition", "production")
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
ROLE_IDS = ("main_chords", "main_lead", "primary_bass", "sub_bass", "drums")
CHORDS = ("I", "V", "vi", "IV") * 8


class AcceptanceConfigurationError(RuntimeError):
    """The requested acceptance fixture is incomplete or unsafe."""


class EvidenceOutputError(RuntimeError):
    """The create-only evidence destination is invalid or unavailable."""


class AcceptanceStepError(AcceptanceConfigurationError):
    """A bounded live step failed; no automatic retry is allowed."""

    def __init__(self, tool: str, message: str, *, unknown_outcome: bool = False):
        super().__init__(message)
        self.tool = tool
        self.unknown_outcome = unknown_outcome


def positive_finite_seconds(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return result


def bounded_bars(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= result <= 64:
        raise argparse.ArgumentTypeError("must be between 1 and 64")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or run a bounded armed-ready FL Studio creation acceptance "
            "fixture; --plan performs no MCP, MIDI, or FL calls."
        )
    )
    parser.add_argument(
        "--scenario",
        choices=SCENARIOS,
        default="composition",
        help="armed-ready fixture to exercise (default: composition)",
    )
    parser.add_argument(
        "--bars",
        type=bounded_bars,
        default=32,
        help="modest draft length in bars (default: 32)",
    )
    parser.add_argument("--seed", type=int, default=7331)
    parser.add_argument(
        "--plan",
        action="store_true",
        help="print the typed plan only; do not import or contact the MCP server",
    )
    parser.add_argument(
        "--midi-port",
        help="exact configured virtual MIDI endpoint for a live run",
    )
    parser.add_argument("--confirm-user-present", action="store_true")
    parser.add_argument("--confirm-disposable-project", action="store_true")
    parser.add_argument("--confirm-safe-to-edit", action="store_true")
    parser.add_argument(
        "--per-tool-timeout-seconds",
        type=positive_finite_seconds,
        default=180.0,
        help="hard deadline for each live MCP call (default: 180)",
    )
    parser.add_argument(
        "--overall-timeout-seconds",
        type=positive_finite_seconds,
        default=600.0,
        help="hard deadline for the complete live run (default: 600)",
    )
    parser.add_argument(
        "--output",
        help="new evidence path outside this repository; never overwritten",
    )
    return parser.parse_args(argv)


def _role(role_id: str, role_type: str, *, register: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "role_id": role_id,
        "display_name": role_id.replace("_", " ").title(),
        "role_type": role_type,
        "required": True,
        "anchor_after_selection": role_id in {"main_chords", "main_lead", "drums"},
        "preserve_across_sections": True,
        "allow_section_variation": True,
        "continuity_priority": 0.75,
        "novelty_priority": 0.25,
    }
    if register is not None:
        value["register"] = register
    if role_id == "drums":
        value["required_drum_roles"] = ["kick", "snare", "closed_hat"]
    return value


def _sound_request(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "brief": (
            f"Create a coherent, restrained {args.bars}-bar acceptance draft from the "
            "already loaded instrument pool. Keep role identity stable and "
            "adapt note choices to observed sound metadata where available."
        ),
        "creative_direction": "clear four-on-the-floor-to-breakbeat draft; no audio claim",
        "source_strategy": "instrument_pool",
        "allow_effect_presets": False,
        "allow_drum_kit_change": True,
        "persist_history": False,
        "preserve_existing_roles": True,
        "seed": args.seed,
        "roles": [
            _role("main_chords", "chords", register="mid"),
            _role("main_lead", "lead", register="mid_high"),
            _role("primary_bass", "primary_bass", register="low_mid"),
            _role("sub_bass", "sub_bass", register="low"),
            _role("drums", "drums"),
        ],
    }


def _palette_ref(operation_id: str, role_id: str) -> dict[str, Any]:
    return {
        "reference": "operation_output",
        "operation_id": operation_id,
        "output": "palette_assignment",
        "role_id": role_id,
    }


def _note_ref(operation_id: str) -> dict[str, Any]:
    return {
        "reference": "operation_output",
        "operation_id": operation_id,
        "output": "note_sequence",
    }


def _plan(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one complete closed plan for the selected fixture."""

    palette_id = "acceptance_palette_plan"
    apply_id = "acceptance_palette_apply"
    map_id = "acceptance_drum_map"
    chord_id = "acceptance_chords"
    melody_id = "acceptance_melody"
    lead_id = "acceptance_lead_adapted"
    bass_id = "acceptance_primary_bass"
    sub_id = "acceptance_sub_bass"
    drum_id = "acceptance_drums"
    pattern_id = "acceptance_empty_pattern"
    writes = {
        "main_chords": ("write_chords", chord_id),
        "main_lead": ("write_lead", lead_id),
        "primary_bass": ("write_primary_bass", bass_id),
        "sub_bass": ("write_sub_bass", sub_id),
        "drums": ("write_drums", drum_id),
    }
    progression = (CHORDS * ((args.bars + len(CHORDS) - 1) // len(CHORDS)))[: args.bars]
    operations: list[dict[str, Any]] = [
        {
            "operation_id": palette_id,
            "operation": "plan_sound_palette",
            "request": _sound_request(args),
        },
        {
            "operation_id": apply_id,
            "operation": "apply_sound_palette",
            "after": [palette_id],
            "palette": {
                "reference": "operation_output",
                "operation_id": palette_id,
                "output": "sound_palette",
            },
            "role_ids": list(ROLE_IDS),
        },
        {
            "operation_id": map_id,
            "operation": "inspect_drum_map",
            "after": [apply_id],
            "target": _palette_ref(apply_id, "drums"),
            "required_roles": ["kick", "snare", "closed_hat"],
        },
        {
            "operation_id": chord_id,
            "operation": "generate_chord_progression",
            "after": [apply_id],
            "progression": progression,
            "root": "C",
            "collection": "major",
            "beats_per_chord": 4.0,
            "octave": 4,
            "voicing": "close",
            "velocity": 0.72,
            "tempo_bpm": 128.0,
        },
        {
            "operation_id": melody_id,
            "operation": "generate_melody",
            "after": [apply_id],
            "root": "C",
            "collection": "major",
            "bars": args.bars,
            "beats_per_bar": 4,
            "density": 0.52,
            "register_low": 60,
            "register_high": 96,
            "contour": "wave",
            "seed": args.seed + 1,
            "tempo_bpm": 128.0,
        },
        {
            "operation_id": lead_id,
            "operation": "adapt_note_sequence",
            "after": [melody_id, apply_id],
            "sequence": _note_ref(melody_id),
            "palette_assignment": _palette_ref(apply_id, "main_lead"),
            "characteristics": {
                "role_id": "main_lead",
                "coverage": "unknown",
                "preset_identity_verified": False,
                "characteristics": (
                    {
                        "name": "attack_speed",
                        "value": "fast",
                        "confidence": "low",
                        "provenance": "server_derived_default",
                    },
                    {
                        "name": "articulation",
                        "value": "plucked",
                        "confidence": "low",
                        "provenance": "server_derived_default",
                    },
                    {
                        "name": "monophonic",
                        "value": True,
                        "confidence": "low",
                        "provenance": "server_derived_default",
                    },
                ),
                "warnings": (
                    "Acceptance fixture uses conservative defaults when exact preset metadata is unavailable.",
                ),
            },
            "role_kind": "lead",
            "connected_ai_register": [60, 96],
            "connected_ai_polyphony": 1,
        },
        {
            "operation_id": bass_id,
            "operation": "generate_bassline",
            "after": [apply_id],
            "progression": progression,
            "root": "C",
            "collection": "major",
            "beats_per_chord": 4.0,
            "octave": 2,
            "style": "roots",
            "seed": args.seed + 2,
            "tempo_bpm": 128.0,
        },
        {
            "operation_id": sub_id,
            "operation": "generate_bassline",
            "after": [apply_id],
            "progression": progression,
            "root": "C",
            "collection": "major",
            "beats_per_chord": 4.0,
            "octave": 1,
            "style": "roots",
            "seed": args.seed + 3,
            "tempo_bpm": 128.0,
        },
        {
            "operation_id": drum_id,
            "operation": "generate_drums",
            "after": [map_id],
            "style": "dnb",
            "bars": args.bars,
            "beats_per_bar": 4,
            "seed": args.seed + 4,
            "swing": 0.0,
            "tempo_bpm": 128.0,
            "drum_map": {
                "reference": "operation_output",
                "operation_id": map_id,
                "output": "drum_map",
            },
        },
    ]
    # Keep the operation order aligned with the closed phase order: the
    # pattern is prepared only after palette inspection and composition have
    # produced the sequences that will be written into it.
    operations.append(
        {
            "operation_id": pattern_id,
            "operation": "prepare_pattern",
            "after": [apply_id],
            "pattern_number": 1,
            "name": f"PostFader acceptance {args.bars}-bar draft",
            "length_beats": args.bars * 4,
        }
    )
    for role_id, (operation_id, sequence_id) in writes.items():
        operations.append(
            {
                "operation_id": operation_id,
                "operation": "write_note_sequence",
                "after": [pattern_id, sequence_id, apply_id],
                "sequence": _note_ref(sequence_id),
                "channel_index": _palette_ref(apply_id, role_id),
                "pattern_number": 1,
                "mode": "append",
            }
        )

    if args.scenario == "production":
        processing_id = "acceptance_processing_plan"
        operations.extend(
            [
                {
                    "operation_id": processing_id,
                    "operation": "plan_processing",
                    "after": [apply_id, pattern_id],
                    "request": {
                        "request_id": "acceptance-production-processing",
                        "completion_target": "restrained_first_pass",
                        "roles": [
                            {
                                "role": "main_lead",
                                "processing_required": False,
                                "goals": [
                                    {
                                        "goal_id": "lead_depth",
                                        "role": "main_lead",
                                        "goal": "add_depth",
                                        "controls": [
                                            {
                                                "control_role": "decay",
                                                "display_value": 1.8,
                                                "display_unit": "seconds",
                                            }
                                        ],
                                        "strength": 0.35,
                                        "required": False,
                                        "rationale": "Restrained acceptance-space tail; audible quality is not evaluated.",
                                    }
                                ],
                            }
                        ],
                        "allow_master": False,
                    },
                },
                {
                    "operation_id": "acceptance_processing_apply",
                    "operation": "apply_processing_plan",
                    "after": [processing_id],
                    "plan": {
                        "reference": "operation_output",
                        "operation_id": processing_id,
                        "output": "processing_plan",
                    },
                },
            ]
        )

    request: dict[str, Any] = {
        "brief": (
            "Create one bounded acceptance draft in the open disposable FL Studio project. "
            "Choose loaded sounds, adapt composition to their evidence, write one pattern, "
            + (
                "and apply restrained loaded-effect processing."
                if args.scenario == "production"
                else "and leave processing explicitly dry by design."
            )
        ),
        "scope": {
            "kind": "whole_project",
            "description": "Maintainer live acceptance on a blank disposable project; no save or render.",
        },
        "preserve": {},
        "allowed_changes": [
            "sound_selection",
            "composition",
            "notes",
            "pattern_metadata",
        ]
        + (["plugin_parameters"] if args.scenario == "production" else []),
        "creative_direction": {
            "mood": ["focused", "restrained"],
            "production_notes": "Acceptance evidence only; do not infer audible success.",
        },
        "completion_target": (
            "restrained first-pass production"
            if args.scenario == "production"
            else "playable draft"
        ),
        "interaction_policy": "execute_once",
        "max_operations": len(operations) + 2,
        "max_iterations": 4,
        "authorized_to_modify": True,
    }
    return request, {
        "plan_id": f"live-creation-{args.scenario}-{args.bars}-bar",
        "operations": operations,
    }


def _normalise(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=False)
    return value


def _dict(value: Any) -> dict[str, Any]:
    body = _normalise(value)
    return body if isinstance(body, dict) else {}


def _digest(value: Any) -> str:
    encoded = json.dumps(_normalise(value), sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _extract_run_id(value: Any) -> str:
    run_id = _dict(value).get("run_id")
    if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise AcceptanceConfigurationError("Production Run did not return a valid run_id")
    return run_id


def _state(value: Any) -> dict[str, Any]:
    body = _dict(value)
    state = body.get("state")
    return state if isinstance(state, dict) else body


def _check(status: str, **detail: Any) -> dict[str, Any]:
    return {"status": status, **detail}


async def _call(name: str, arguments: dict[str, Any], timeout: float) -> Any:
    from fl_studio_mcp import mcp_server
    from fl_studio_mcp.acceptance import tool_payload

    result = await asyncio.wait_for(
        mcp_server.mcp.call_tool(name, arguments), timeout=timeout
    )
    return tool_payload(result)


async def async_main(
    args: argparse.Namespace,
    *,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    request, plan = _plan(args)
    if args.plan:
        return {
            "schema_version": 1,
            "kind": ACCEPTANCE_KIND,
            "scenario": args.scenario,
            "overall": "pass",
            "phase": "plan_only",
            "contact_started": False,
            "physical_io_performed": False,
            "project_saved": False,
            "automatic_replay_attempted": False,
            "target_assessment": "not_claimed",
            "acceptance_targets": {
                "under_five_minutes_from_armed_ready": "not_claimed",
                "under_ten_minutes_with_one_manual_action": "not_claimed",
                "one_task_scoped_authorization": "not_claimed",
                "zero_surprise_setup_blockers": "not_claimed",
                "manual_playlist_handoffs_at_most_one": "not_claimed",
            },
            "request": request,
            "plan": plan,
            "expected_checks": [
                "one readiness preflight in the run state",
                "one write-mode enable and one verified shutdown when mutations are available",
                "palette assignment before sound-aware composition",
                "armed Piano Roll note application receipts",
                "phase timings and one consolidated creation outcome",
                "one bounded setup inventory scan with no repeated setup blockers",
                (
                    "effect coverage, semantic processing plan, displayed-value readback, and restrained processing"
                    if args.scenario == "production"
                    else "dry processing status and audible_quality=not_evaluated"
                ),
            ],
            "required_tools": [
                "postfader_creation_readiness",
                "postfader_execute_run",
                "postfader_get_run",
            ],
            "requested_midi_port": args.midi_port,
        }

    from fl_studio_mcp.acceptance import authoritative_tool_surface

    confirmations = (
        args.confirm_user_present,
        args.confirm_disposable_project,
        args.confirm_safe_to_edit,
    )
    if not all(confirmations):
        raise AcceptanceConfigurationError(
            "live creation acceptance requires --confirm-user-present, "
            "--confirm-disposable-project, and --confirm-safe-to-edit"
        )

    surface = await authoritative_tool_surface()
    required = {"postfader_creation_readiness", "postfader_execute_run", "postfader_get_run"}
    missing = sorted(required - set(surface.all_tools))
    if missing:
        raise AcceptanceConfigurationError(
            "creation acceptance MCP surface is missing: %s" % missing
        )

    loop = asyncio.get_running_loop()
    started = loop.time()
    steps: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": ACCEPTANCE_KIND,
        "scenario": args.scenario,
        "overall": "pass",
        "phase": "contact_started",
        "contact_started": True,
        "physical_io_performed": True,
        "project_saved": False,
        "automatic_replay_attempted": False,
        "target_assessment": "not_claimed",
        "steps": steps,
        "checks": {},
        "request": request,
        "plan": plan,
        "requested_midi_port": args.midi_port,
    }

    def elapsed() -> float:
        return round(loop.time() - started, 6)

    def save_checkpoint(entry: dict[str, Any]) -> None:
        if checkpoint is None:
            return
        checkpoint(
            {
                "schema_version": 1,
                "kind": ACCEPTANCE_KIND,
                "scenario": args.scenario,
                "overall": report["overall"],
                "phase": report["phase"],
                "contact_started": True,
                "project_saved": False,
                "automatic_replay_attempted": False,
                "last_checkpoint": entry,
            }
        )

    async def invoke(name: str, arguments: dict[str, Any], *, mutating: bool = False) -> Any:
        remaining = args.overall_timeout_seconds - elapsed()
        if remaining <= 0:
            raise AcceptanceStepError(
                name,
                "overall creation acceptance deadline expired",
                unknown_outcome=mutating,
            )
        step: dict[str, Any] = {
            "tool": name,
            "status": "in_flight",
            "mutating": mutating,
            "arguments_sha256": _digest(arguments),
            "automatic_replay_attempted": False,
            "monotonic_elapsed_seconds": elapsed(),
        }
        steps.append(step)
        report["phase"] = "tool_in_flight"
        save_checkpoint(step)
        try:
            result = await _call(
                name,
                arguments,
                min(args.per_tool_timeout_seconds, remaining),
            )
        except Exception as exc:
            step.update(
                {
                    "status": "failed",
                    "error": str(exc),
                    "outcome": "unknown" if mutating else "not_reached",
                    "monotonic_elapsed_seconds": elapsed(),
                }
            )
            report["phase"] = "unknown_outcome" if mutating else "tool_failed"
            save_checkpoint(step)
            raise AcceptanceStepError(
                name,
                f"{name} failed: {exc}",
                unknown_outcome=mutating,
            ) from exc
        step.update(
            {
                "status": "passed",
                "response_sha256": _digest(result),
                "monotonic_elapsed_seconds": elapsed(),
            }
        )
        report["phase"] = "workflow_execution"
        save_checkpoint(step)
        return result

    try:
        run_result = await invoke(
            "postfader_execute_run",
            {"request": request, "plan": plan},
            mutating=True,
        )
        report["run_result"] = run_result
        run_id = _extract_run_id(run_result)
        lookup = await invoke("postfader_get_run", {"run_id": run_id})
        report["run_state"] = lookup
        state = _state(lookup)
        status = _dict(run_result).get("status", state.get("status"))
        report["observed_elapsed_seconds"] = elapsed()

        readiness_count = state.get("readiness_preflight_count")
        report["checks"]["one_readiness_preflight"] = _check(
            "passed" if readiness_count == 1 else "failed",
            observed=readiness_count,
        )
        readiness = state.get("readiness_report", _dict(run_result).get("readiness_report"))
        readiness = readiness if isinstance(readiness, dict) else {}
        dimensions = readiness.get("dimensions", [])
        dimension_names = [
            item.get("name")
            for item in dimensions
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        required_dimensions = {
            "connection_bridge",
            "piano_roll",
            "instrument_pool",
            "drum_coverage",
            "patterns_arrangement",
            "mixer_effects",
            "scope_manual_work",
        }
        report["checks"]["readiness_dimensions"] = _check(
            "passed" if required_dimensions.issubset(dimension_names) else "failed",
            observed=dimension_names,
            expected=sorted(required_dimensions),
            overall_state=readiness.get("overall_state"),
            blocker_count=len(readiness.get("blockers", []))
            if isinstance(readiness.get("blockers"), list)
            else None,
        )
        enable_count = state.get("write_mode_enable_count", _dict(run_result).get("write_mode_enable_count"))
        disable_count = state.get("write_mode_disable_count", _dict(run_result).get("write_mode_disable_count"))
        shutdown = state.get("write_mode_shutdown_verified", _dict(run_result).get("write_mode_shutdown_verified"))
        report["checks"]["single_write_transition"] = _check(
            "passed"
            if enable_count == 1 and disable_count == 1 and shutdown is True
            else "failed",
            enable_count=enable_count,
            disable_count=disable_count,
            shutdown_verified=shutdown,
            authorization="one task-scoped Production Run authorization; target not claimed",
        )
        phase_plan = state.get("phase_plan", _dict(run_result).get("phase_plan"))
        phase_rows = phase_plan.get("phases", []) if isinstance(phase_plan, dict) else []
        phase_names = [row.get("phase") for row in phase_rows if isinstance(row, dict)]
        expected_phases = [
            "preflight",
            "palette",
            "composition",
            "note_application",
            "processing",
            "finalization",
        ]
        report["checks"]["phased_execution"] = _check(
            "passed" if phase_names == expected_phases else "failed",
            observed=phase_names,
            expected=expected_phases,
        )

        timing = state.get("timing_report", _dict(run_result).get("timing_report"))
        timing_rows = timing.get("phase_timings", []) if isinstance(timing, dict) else []
        timing_names = [row.get("phase") for row in timing_rows if isinstance(row, dict)]
        expected_timing_phases = [
            phase for phase in expected_phases
            if args.scenario == "production" or phase != "processing"
        ]
        report["checks"]["phase_timings"] = _check(
            "passed" if set(expected_timing_phases).issubset(timing_names) else "failed",
            observed=timing_names,
            expected=expected_timing_phases,
            timing=timing,
            target_assessment="not_claimed",
        )
        timing_summary = timing.get("operation_summary", {}) if isinstance(timing, dict) else {}
        full_scans = timing_summary.get("full_inventory_scan_count")
        report["checks"]["bounded_setup_scan"] = _check(
            "passed" if full_scans == 1 else "failed",
            observed_full_inventory_scan_count=full_scans,
            expected_full_inventory_scan_count=1,
            repeated_setup_blockers="not_claimed",
        )

        receipts = state.get("receipts", [])
        receipt_names = {
            operation
            for row in receipts
            if isinstance(row, dict)
            and isinstance((operation := row.get("operation")), str)
        }
        expected_receipts = {
            "plan_sound_palette",
            "apply_sound_palette",
            "inspect_drum_map",
            "adapt_note_sequence",
            "write_note_sequence",
        }
        if args.scenario == "production":
            expected_receipts.update({"plan_processing", "apply_processing_plan"})
        missing_receipts = sorted(expected_receipts - receipt_names)
        report["checks"]["creation_receipts"] = _check(
            "passed" if not missing_receipts else "failed",
            missing=missing_receipts,
            observed=sorted(receipt_names),
            sound_aware_adaptation="adapt_note_sequence" in receipt_names,
        )

        outcome = state.get("creation_outcome", _dict(run_result).get("creation_outcome"))
        outcome = outcome if isinstance(outcome, dict) else {}
        audible = outcome.get("audible_quality", {})
        audible_status = audible.get("status") if isinstance(audible, dict) else None
        report["checks"]["audible_quality_boundary"] = _check(
            "passed" if audible_status == "not_evaluated" else "failed",
            observed=audible_status,
            target_assessment="not_claimed",
        )
        required_outcome_dimensions = {
            "technical_execution",
            "arrangement_delivery",
            "processing",
            "audible_quality",
            "manual_handoff",
        }
        report["checks"]["outcome_dimensions"] = _check(
            "passed"
            if required_outcome_dimensions.issubset(outcome)
            else "failed",
            observed=sorted(outcome),
            expected=sorted(required_outcome_dimensions),
            target_assessment="not_claimed",
        )
        processing = outcome.get("processing", {})
        processing_status = processing.get("status") if isinstance(processing, dict) else None
        if args.scenario == "production":
            allowed_processing = {
                "restrained_first_pass",
                "partially_processed",
                "dry_missing_effects",
                "dry_by_design",
            }
            processing_ok = processing_status in allowed_processing
        else:
            processing_ok = processing_status in {
                "not_requested",
                "dry_by_design",
                "dry_missing_effects",
            }
        report["checks"]["processing_boundary"] = _check(
            "passed" if processing_ok else "failed",
            observed=processing_status,
            expected=(
                "restrained or honest dry/partial status"
                if args.scenario == "production"
                else "not_requested or honest dry status"
            ),
            target_assessment="not_claimed",
        )
        handoff = outcome.get("manual_handoff", {})
        handoff_actions = (
            handoff.get("actions", []) if isinstance(handoff, dict) else []
        )
        handoff_count = len(handoff_actions) if isinstance(handoff_actions, list) else None
        report["checks"]["playlist_handoff_bound"] = _check(
            "passed" if handoff_count is not None and handoff_count <= 1 else "failed",
            observed_action_count=handoff_count,
            expected_at_most=1,
            target_assessment="not_claimed",
        )
        if args.scenario == "production":
            processing_receipt_value = next(
                (
                    row.get("result")
                    for row in receipts
                    if isinstance(row, dict)
                    and row.get("operation") == "apply_processing_plan"
                    and isinstance(row.get("result"), dict)
                ),
                {},
            )
            processing_receipt = (
                processing_receipt_value
                if isinstance(processing_receipt_value, dict)
                else {}
            )
            action_results = processing_receipt.get("results", [])
            verified_actions = [
                row
                for row in action_results
                if isinstance(row, dict) and row.get("status") == "verified"
            ]
            verified_writes = [
                row.get("receipt")
                for row in verified_actions
                if isinstance(row.get("receipt"), dict)
            ]
            setter_names = {
                row.get("bridge_command")
                for row in verified_writes
                if isinstance(row, dict)
            }
            readback_bases = {
                row.get("verification_basis")
                for row in verified_writes
                if isinstance(row, dict)
            }
            acceptable_setters = {
                "plugin.set_param_display",
                "plugin.set_param_option",
            }
            report["checks"]["semantic_processing_evidence"] = _check(
                "passed"
                if verified_writes
                and bool(setter_names & acceptable_setters)
                and readback_bases == {"readback_on_a_later_fl_idle_tick"}
                else "failed",
                processing_plan_receipt=bool(
                    any(
                        isinstance(row, dict)
                        and row.get("operation") == "plan_processing"
                        for row in receipts
                    )
                ),
                verified_action_count=len(verified_actions),
                setter_names=sorted(name for name in setter_names if isinstance(name, str)),
                readback_bases=sorted(
                    name for name in readback_bases if isinstance(name, str)
                ),
                expected_setters=sorted(acceptable_setters),
                expected_readback="readback_on_a_later_fl_idle_tick",
                target_assessment="not_claimed",
            )
        report["creation_outcome"] = outcome
        report["target_assessment"] = "not_claimed"
        report["target_observations"] = {
            "elapsed_seconds": report["observed_elapsed_seconds"],
            "under_five_minutes_observed": report["observed_elapsed_seconds"] < 300.0,
            "under_ten_minutes_observed": report["observed_elapsed_seconds"] < 600.0,
            "manual_playlist_handoff_count": handoff_count,
            "target_assessment": "not_claimed",
        }
        report["acceptance_targets"] = {
            "under_five_minutes_from_armed_ready": "observed_but_not_claimed",
            "under_ten_minutes_with_one_manual_action": "observed_but_not_claimed",
            "one_task_scoped_authorization": "observed_but_not_claimed",
            "zero_surprise_setup_blockers": "observed_but_not_claimed",
            "manual_playlist_handoffs_at_most_one": "observed_but_not_claimed",
        }
        failed = [
            name
            for name, value in report["checks"].items()
            if isinstance(value, dict) and value.get("status") == "failed"
        ]
        if status not in {None, "completed"} or failed:
            report.update(
                {
                    "overall": "fail",
                    "phase": "acceptance_checks_failed",
                    "failed_checks": failed,
                }
            )
        else:
            report["phase"] = "complete"
        return report
    except AcceptanceStepError as exc:
        report.update(
            {
                "overall": "fail",
                "phase": "unknown_outcome" if exc.unknown_outcome else "workflow_execution",
                "error": str(exc),
                "failed_tool": exc.tool,
                "automatic_replay_attempted": False,
                "target_assessment": "not_claimed",
            }
        )
        return report


def _private_output(path: str | None) -> str | None:
    if path is None:
        return None
    resolved = Path(path).expanduser().resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        return os.fspath(resolved)
    raise EvidenceOutputError(
        "live creation acceptance evidence must be stored outside the public repository"
    )


def _failure(*, phase: str, error: BaseException | str, contact_started: bool) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": ACCEPTANCE_KIND,
        "overall": "fail" if contact_started else "refused",
        "phase": phase,
        "contact_started": contact_started,
        "project_saved": False,
        "target_assessment": "not_claimed",
        "error": str(error),
    }


def _finish(destination: Any, value: dict[str, Any], *, contact_started: bool) -> int:
    failures: list[dict[str, Any]] = []
    if destination is not None:
        try:
            destination.write(value)
        except Exception as exc:
            failures.append({"stage": "final_evidence_write", "reason": str(exc)})
        try:
            destination.close()
        except Exception as exc:
            failures.append({"stage": "final_evidence_close", "reason": str(exc)})
    if failures:
        failure = _failure(
            phase="final_evidence_output",
            error="one or more evidence output operations failed",
            contact_started=contact_started,
        )
        failure["evidence_output_failures"] = failures
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Keep the local plan path dependency-light: maintainers can review the
    # generated request on a clean checkout without importing MCP/jsonschema
    # or opening a transport.
    if args.plan:
        if args.output is not None:
            try:
                _private_output(args.output)
            except EvidenceOutputError as exc:
                value = _failure(
                    phase="output_reservation", error=exc, contact_started=False
                )
                print(json.dumps(value, indent=2, sort_keys=True), file=sys.stderr)
                return 2
        try:
            value = asyncio.run(async_main(args))
        except Exception as exc:
            value = _failure(phase="plan_generation", error=exc, contact_started=False)
            print(json.dumps(value, indent=2, sort_keys=True), file=sys.stderr)
            return 2
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
        return 0

    try:
        from fl_studio_mcp.evidence import (
            configure_acceptance_transport,
            reserve_evidence_output,
        )
    except Exception as exc:
        value = _failure(phase="runtime_dependencies", error=exc, contact_started=False)
        print(json.dumps(value, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    try:
        output_path = _private_output(args.output)
        destination = reserve_evidence_output(output_path, required=True)
    except EvidenceOutputError as exc:
        value = _failure(phase="output_reservation", error=exc, contact_started=False)
        print(json.dumps(value, indent=2, sort_keys=True), file=sys.stderr)
        return 2

    contact_started = False
    try:
        configure_acceptance_transport(args.midi_port, live=not args.plan)
        if destination is not None and not args.plan:
            destination.write(
                {
                    "schema_version": 1,
                    "kind": ACCEPTANCE_KIND,
                    "scenario": args.scenario,
                    "overall": "started",
                    "phase": "contact_started",
                    "contact_started": True,
                    "project_saved": False,
                    "automatic_replay_attempted": False,
                    "target_assessment": "not_claimed",
                }
            )
            contact_started = True
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
        contact_started = contact_started or bool(value.get("contact_started"))
    except (AcceptanceConfigurationError, OSError, TimeoutError, ValueError) as exc:
        value = _failure(
            phase="workflow_execution",
            error=exc,
            contact_started=contact_started or not args.plan,
        )
    except BaseException as exc:
        value = _failure(
            phase="interrupted_execution",
            error=f"{type(exc).__name__}: {exc}",
            contact_started=contact_started or not args.plan,
        )
    return _finish(destination, value, contact_started=contact_started or not args.plan)


if __name__ == "__main__":
    raise SystemExit(main())
