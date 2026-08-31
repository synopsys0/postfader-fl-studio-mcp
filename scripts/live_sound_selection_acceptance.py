#!/usr/bin/env python3
"""Run or plan the bounded Sound Selection live-acceptance fixture.

The fixture is intentionally maintainer-facing.  It reads a blank disposable
project, proves the preset and pad-map observations that Sound Selection uses,
and (only with explicit confirmations) applies the selected palette.  A small
read-only Production Run is included so that typed output references and a
continuation preserving the acceptance anchor are exercised in the same MCP
process.

``--plan`` is a pure local preview: it does not import the MCP server, inspect
FL Studio, or open a live transport.  It creates no evidence by default; an
explicit ``--output`` remains create-only and must be outside the repository.
Live evidence should be written outside the public repository.
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


SESSION_PATTERN = re.compile(r"^[0-9a-f]{32}$")
TARGET_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
ACCEPTANCE_KIND = "postfader_sound_selection_acceptance"
ACCEPTANCE_ROLE = "acceptance_target"
REFERENCE_OPERATION = "acceptance_palette_reference"
APPLY_OPERATION = "acceptance_palette_apply"
VARIATION_OPERATION = "acceptance_anchor_continuation"


class AcceptanceStepError(AcceptanceConfigurationError):
    """A bounded workflow step failed and dependent steps must stop."""

    def __init__(self, tool: str, message: str, *, unknown_outcome: bool = False):
        super().__init__(message)
        self.tool = tool
        self.unknown_outcome = unknown_outcome


def positive_finite_seconds(value: str) -> float:
    try:
        resolved = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(resolved) or resolved <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return resolved


def bounded_preset_limit(value: str) -> int:
    try:
        resolved = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= resolved <= 256:
        raise argparse.ArgumentTypeError("must be between 1 and 256")
    return resolved


def bounded_navigation_steps(value: str) -> int:
    try:
        resolved = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 0 <= resolved <= 256:
        raise argparse.ArgumentTypeError("must be between 0 and 256")
    return resolved


def bounded_settle_ticks(value: str) -> int:
    try:
        resolved = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= resolved <= 8:
        raise argparse.ArgumentTypeError("must be between 1 and 8")
    return resolved


def bounded_preset_index(value: str) -> int:
    try:
        resolved = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 0 <= resolved <= 999_999:
        raise argparse.ArgumentTypeError("must be between 0 and 999999")
    return resolved


def exact_preset_name(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must contain non-empty text")
    if len(value) > 256:
        raise argparse.ArgumentTypeError("must be at most 256 characters")
    return value


def nonnegative_int(value: str) -> int:
    try:
        resolved = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if resolved < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return resolved


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture bounded blank-project Sound Selection evidence; --plan "
            "performs no FL, MIDI, or MCP calls."
        )
    )
    parser.add_argument(
        "--brief",
        default="Choose a cohesive sound palette for live acceptance.",
        help="Structured creative direction passed to Sound Selection.",
    )
    parser.add_argument("--channel", type=nonnegative_int, default=0)
    parser.add_argument(
        "--pad-map-channel",
        type=nonnegative_int,
        help="Global generator channel used for the pad-map proof; defaults to --channel.",
    )
    parser.add_argument("--only-used", action="store_true")
    parser.add_argument("--include-effects", action="store_true")
    parser.add_argument("--preset-limit", type=bounded_preset_limit, default=64)
    parser.add_argument(
        "--preset-name",
        type=exact_preset_name,
        help="Exact preset name to select; it must occur in the bounded inventory page.",
    )
    parser.add_argument(
        "--preset-index",
        type=bounded_preset_index,
        help="Exact preset index to select; it must occur in the bounded inventory page.",
    )
    parser.add_argument(
        "--max-navigation-steps",
        type=bounded_navigation_steps,
        default=64,
        help="Maximum next/previous steps for exact preset selection (default: 64).",
    )
    parser.add_argument(
        "--settle-tick-limit",
        type=bounded_settle_ticks,
        default=1,
        help="Later idle ticks allowed for preset settling (default: 1).",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument(
        "--midi-port",
        help="Exact virtual MIDI endpoint name. Omit to keep native MIDI disabled.",
    )
    parser.add_argument("--confirm-user-present", action="store_true")
    parser.add_argument("--confirm-disposable-project", action="store_true")
    parser.add_argument("--confirm-safe-to-edit", action="store_true")
    parser.add_argument(
        "--per-tool-timeout-seconds",
        type=positive_finite_seconds,
        default=180.0,
        help="Hard deadline for each live Sound Selection MCP call (default: 180).",
    )
    parser.add_argument(
        "--overall-timeout-seconds",
        type=positive_finite_seconds,
        default=600.0,
        help="Hard deadline for the complete workflow (default: 600).",
    )
    parser.add_argument(
        "--output",
        help="Create this new evidence file outside the public repository; never overwrite.",
    )
    return parser.parse_args(argv)


def _target(channel: int) -> dict[str, Any]:
    return {"kind": "channel_generator", "channel_index": channel}


def _request(args: argparse.Namespace, *, brief: str | None = None) -> dict[str, Any]:
    """Return one strict, deterministic request used by all acceptance phases."""

    return {
        "brief": args.brief if brief is None else brief,
        "source_strategy": "mixed",
        "allow_effect_presets": args.include_effects,
        "persist_history": False,
        "preserve_existing_roles": True,
        "roles": [
            {
                "role_id": ACCEPTANCE_ROLE,
                "display_name": "Live acceptance anchor",
                "role_type": "instrument",
                "lock_existing": True,
                "continuity_priority": 0.90,
                "target_candidates": [_target(args.channel)],
            }
        ],
    }


def _continuation_request(args: argparse.Namespace) -> dict[str, Any]:
    return _request(
        args,
        brief=(
            f"{args.brief} Develop a second-drop variation while preserving the "
            f"{ACCEPTANCE_ROLE} anchor."
        ),
    )


def _production_request(args: argparse.Namespace) -> dict[str, Any]:
    """Build the read-only task envelope for typed Production Run references."""

    return {
        "brief": (
            f"{args.brief} Verify a Sound Selection Production Run reference "
            "and a continuation that preserves its anchor."
        ),
        "scope": {
            "kind": "whole_project",
            "description": "Sound Selection acceptance reference flow; no project save.",
        },
        "preserve": {},
        "allowed_changes": ["sound_selection"],
        "creative_direction": {},
        "completion_target": "A typed palette output and a second-drop anchor-preserving continuation.",
        "interaction_policy": "execute_once",
        "max_operations": 4,
        "max_iterations": 4,
        "authorized_to_modify": args.apply,
    }


def _production_plan(args: argparse.Namespace) -> dict[str, Any]:
    operations: list[dict[str, Any]] = [
        {
            "operation_id": REFERENCE_OPERATION,
            "operation": "plan_sound_palette",
            "request": _request(args),
        }
    ]
    if args.apply:
        operations.append(
            {
                "operation_id": APPLY_OPERATION,
                "operation": "apply_sound_palette",
                "after": [REFERENCE_OPERATION],
                "palette": {
                    "reference": "operation_output",
                    "operation_id": REFERENCE_OPERATION,
                    "output": "sound_palette",
                },
                "role_ids": [],
            }
        )
    return {
        "plan_id": "live-sound-selection-reference",
        "operations": operations,
    }


def _production_continuation(args: argparse.Namespace) -> dict[str, Any]:
    dependency = APPLY_OPERATION if args.apply else REFERENCE_OPERATION
    return {
        "mode": "append",
        "operations": [
            {
                "operation_id": VARIATION_OPERATION,
                "operation": "create_sound_palette_variation",
                "after": [dependency],
                "palette": {
                    "reference": "operation_output",
                    "operation_id": REFERENCE_OPERATION,
                    "output": "sound_palette",
                },
                "request": _continuation_request(args),
                "section": "second_drop",
                "replace_roles": [],
            }
        ],
    }


def _plan_arguments(args: argparse.Namespace) -> dict[str, Any]:
    request = _request(args)
    return {
        "inventory": {
            "request": request,
            "only_used": args.only_used,
            "include_effects": args.include_effects,
            "preset_start": 0,
            "preset_limit": args.preset_limit,
            "include_current": True,
            "include_empty_names": False,
            "include_pad_maps": True,
            "include_atlas": True,
        },
        "preset_inventory": {
            "target": _target(args.channel),
            "start": 0,
            "limit": args.preset_limit,
            "include_current": True,
            "include_empty_names": False,
        },
        "current_preset_before": {"target": _target(args.channel)},
        "pad_map": {
            "target": _target(
                args.channel if args.pad_map_channel is None else args.pad_map_channel
            )
        },
        "plan": {"request": request},
        "exact_selection": {
            "target": _target(args.channel),
            "preset_name": args.preset_name,
            "preset_index": args.preset_index,
            "max_navigation_steps": args.max_navigation_steps,
            "settle_tick_limit": args.settle_tick_limit,
        },
        "apply": {
            "authorized_to_modify": True,
            "role_ids": [],
            "max_navigation_steps": args.max_navigation_steps,
            "settle_tick_limit": args.settle_tick_limit,
            "persist_history": False,
        },
        "production_request": _production_request(args),
        "production_plan": _production_plan(args),
        "production_continuation": _production_continuation(args),
    }


def _normalise_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=False)
    return value


def _dict(value: Any) -> dict[str, Any] | None:
    value = _normalise_json(value)
    return value if isinstance(value, dict) else None


def _nested_dict(value: Any, key: str) -> dict[str, Any] | None:
    body = _dict(value)
    if body is None:
        return None
    nested = body.get(key)
    return nested if isinstance(nested, dict) else None


def _session(value: Any) -> str | None:
    body = _dict(value)
    if body is None:
        return None
    candidate = body.get("session_fingerprint")
    if isinstance(candidate, str) and SESSION_PATTERN.fullmatch(candidate):
        return candidate
    return None


def _target_fingerprint(value: Any) -> str | None:
    body = _dict(value)
    nested = _nested_dict(value, "plugin")
    for source in (body, nested):
        if source is None:
            continue
        candidate = source.get("target_fingerprint")
        if isinstance(candidate, str) and TARGET_FINGERPRINT_PATTERN.fullmatch(candidate):
            return candidate
    return None


def _current_identity(value: Any) -> tuple[str | None, int | None, str | None]:
    body = _dict(value)
    if body is None:
        return None, None, None
    current = body.get("current")
    source = current if isinstance(current, dict) else body
    name = source.get("name", source.get("current_preset_name"))
    index = source.get("index", source.get("current_preset_index"))
    status = source.get("identity_status", source.get("current_preset_status"))
    if not isinstance(name, str) and name is not None:
        name = None
    if isinstance(index, bool) or not isinstance(index, int):
        index = None
    if not isinstance(status, str):
        status = None
    return name, index, status


def _preset_rows(value: Any) -> list[dict[str, Any]]:
    body = _dict(value)
    if body is None or not isinstance(body.get("presets"), list):
        return []
    rows: list[dict[str, Any]] = []
    for row in body["presets"]:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        index = row.get("index")
        if not isinstance(name, str) or not name.strip():
            continue
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            continue
        rows.append({"name": name, "index": index})
    return rows


def _generator_inventory_evidence(
    value: Any, target: dict[str, Any]
) -> dict[str, Any]:
    """Summarise one exact generator target's bounded preset page."""

    body = _dict(value) or {}
    raw_rows = body.get("loaded_generators")
    rows = raw_rows if isinstance(raw_rows, list) else []
    matching = [
        row
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("target"), dict)
        and row["target"].get("kind") == target.get("kind")
        and row["target"].get("channel_index") == target.get("channel_index")
    ]
    selected = matching[0] if matching else {}
    raw_names = selected.get("preset_names")
    raw_indices = selected.get("preset_indices")
    names = raw_names if isinstance(raw_names, list) else []
    indices = raw_indices if isinstance(raw_indices, list) else []
    names_ok = isinstance(raw_names, list) and all(
        isinstance(item, str) and bool(item.strip()) for item in names
    )
    indices_ok = isinstance(raw_indices, list) and all(
        isinstance(item, int) and not isinstance(item, bool) and item >= 0
        for item in indices
    )
    identity_page = (
        bool(matching)
        and names_ok
        and indices_ok
        and bool(names)
        and len(names) == len(indices)
    )
    return {
        "target": target,
        "generator_count": len(rows),
        "matching_target": bool(matching),
        "preset_name_count": len(names),
        "preset_index_count": len(indices),
        "identity_page": identity_page,
    }


def _choose_preset(
    page: Any,
    before: Any,
    *,
    requested_name: str | None,
    requested_index: int | None,
) -> dict[str, Any]:
    """Resolve one exact identity from the bounded page, never from a guess."""

    rows = _preset_rows(page)
    if requested_name is None and requested_index is None:
        if not rows:
            raise AcceptanceConfigurationError(
                "preset inventory contained no non-empty exact preset rows"
            )
        current_name, current_index, _status = _current_identity(before)
        different = [
            row
            for row in rows
            if row["name"] != current_name or row["index"] != current_index
        ]
        return dict((different or rows)[0])

    matches = [
        row
        for row in rows
        if (requested_name is None or row["name"] == requested_name)
        and (requested_index is None or row["index"] == requested_index)
    ]
    if not matches:
        raise AcceptanceConfigurationError(
            "the requested exact preset identity was not present in the bounded inventory page"
        )
    if requested_name is not None and requested_index is None:
        same_name = [row for row in rows if row["name"] == requested_name]
        if len(same_name) != 1:
            raise AcceptanceConfigurationError(
                "duplicate preset names require --preset-index for exact selection"
            )
    return dict(matches[0])


def _identity_matches(value: Any, expected: dict[str, Any]) -> bool:
    name, index, status = _current_identity(value)
    if expected.get("name") is not None and name != expected["name"]:
        return False
    if expected.get("index") is not None and index != expected["index"]:
        return False
    if status is not None and status not in {"stable", "ambiguous"}:
        return False
    return True


def _extract_run_id(value: Any) -> str:
    body = _dict(value)
    run_id = None if body is None else body.get("run_id")
    if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise AcceptanceConfigurationError(
            "Production Run did not return a valid process-local run identifier"
        )
    return run_id


def _outputs(value: Any) -> list[dict[str, Any]]:
    body = _dict(value)
    state = None if body is None else body.get("state")
    if not isinstance(state, dict):
        state = body
    rows = [] if state is None else state.get("generated_outputs")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _palette_assignments(value: Any) -> list[dict[str, Any]]:
    body = _dict(value)
    if body is None:
        return []
    assignments = body.get("assignments")
    if not isinstance(assignments, list):
        return []
    return [item for item in assignments if isinstance(item, dict)]


def _anchor_snapshot(outputs: list[dict[str, Any]]) -> dict[str, tuple[Any, ...]]:
    snapshot: dict[str, tuple[Any, ...]] = {}
    for row in outputs:
        if row.get("output") != "palette_assignment":
            continue
        role_id = row.get("role_id")
        value = row.get("value")
        if not isinstance(role_id, str) or not isinstance(value, dict):
            continue
        snapshot[role_id.casefold()] = (
            value.get("target"),
            value.get("selected_preset"),
            value.get("selected_preset_index"),
            value.get("preset_identity_digest"),
        )
    return snapshot


def _variation_anchor_preserved(
    outputs: list[dict[str, Any]], before: dict[str, tuple[Any, ...]]
) -> tuple[bool, dict[str, Any]]:
    for row in outputs:
        if row.get("output") != "section_variation":
            continue
        value = row.get("value")
        if not isinstance(value, dict):
            continue
        unchanged = {
            str(item).casefold()
            for item in value.get("unchanged_role_ids", [])
            if isinstance(item, str)
        }
        assignments = _palette_assignments(value)
        changed = {
            str(item.get("role_id")).casefold()
            for item in assignments
            if isinstance(item.get("role_id"), str)
        }
        anchor = ACCEPTANCE_ROLE.casefold()
        detail = {
            "unchanged_role_ids": sorted(unchanged),
            "changed_role_ids": sorted(changed),
            "preserve_anchor_roles": value.get("preserve_anchor_roles"),
        }
        if anchor not in before:
            return False, {**detail, "reason": "base run did not emit the anchor assignment"}
        return (
            value.get("preserve_anchor_roles") is True
            and anchor in unchanged
            and anchor not in changed,
            detail,
        )
    return False, {"reason": "continuation did not emit a section variation output"}


def _digest(value: Any) -> str:
    encoded = json.dumps(_normalise_json(value), sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _call(name: str, arguments: dict[str, Any], timeout: float) -> Any:
    from fl_studio_mcp import mcp_server

    return tool_payload(
        await asyncio.wait_for(
            mcp_server.mcp.call_tool(name, arguments), timeout=timeout
        )
    )


async def async_main(
    args: argparse.Namespace,
    *,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Execute the fixture with a single bounded call path and no replay."""

    arguments = _plan_arguments(args)
    if args.plan:
        if args.apply:
            raise AcceptanceConfigurationError("--apply cannot be combined with --plan")
        return {
            "schema_version": 1,
            "kind": ACCEPTANCE_KIND,
            "overall": "pass",
            "phase": "plan_only",
            "contact_started": False,
            "project_saved": False,
            "physical_io_performed": False,
            "automatic_replay": False,
            "automatic_replay_attempted": False,
            "checks": {
                "no_project_save": {
                    "status": "passed",
                    "project_saved": False,
                    "save_tool_called": False,
                }
            },
            "authoritative_tools": [
                "sound_selection_inventory",
                "plugins_list_presets",
                "plugins_get_current_preset",
                "plugins_inspect_pad_map",
                "sound_selection_plan",
                "fl_select_plugin_preset",
                "sound_selection_apply",
                "postfader_validate_run",
                "postfader_execute_run",
                "postfader_get_run",
                "postfader_continue_run",
            ],
            "arguments": arguments,
            "requested_midi_port": args.midi_port,
        }

    if args.apply and not (
        args.confirm_user_present
        and args.confirm_disposable_project
        and args.confirm_safe_to_edit
    ):
        raise AcceptanceConfigurationError(
            "Sound Selection application requires --confirm-user-present, "
            "--confirm-disposable-project, and --confirm-safe-to-edit"
        )

    surface = await authoritative_tool_surface()
    required = {
        "sound_selection_inventory",
        "plugins_list_presets",
        "plugins_get_current_preset",
        "plugins_inspect_pad_map",
        "sound_selection_plan",
        "postfader_validate_run",
        "postfader_execute_run",
        "postfader_get_run",
        "postfader_continue_run",
    }
    if args.apply:
        required.update({"fl_select_plugin_preset", "sound_selection_apply"})
    missing = sorted(required - set(surface.all_tools))
    if missing:
        raise AcceptanceConfigurationError(
            "Sound Selection live-acceptance MCP surface is missing: %s" % missing
        )

    started = asyncio.get_running_loop().time()
    steps: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": ACCEPTANCE_KIND,
        "overall": "pass",
        "phase": "contact_started",
        "contact_started": True,
        "project_saved": False,
        "physical_io_performed": True,
        "automatic_replay": False,
        "automatic_replay_attempted": False,
        "steps": steps,
        "checks": {
            "no_project_save": {
                "status": "passed",
                "project_saved": False,
                "save_tool_called": False,
            }
        },
        "arguments": arguments,
        "requested_midi_port": args.midi_port,
    }

    def elapsed() -> float:
        return round(asyncio.get_running_loop().time() - started, 6)

    def save_checkpoint(entry: dict[str, Any]) -> None:
        if checkpoint is None:
            return
        checkpoint(
            {
                "schema_version": 1,
                "kind": ACCEPTANCE_KIND,
                "overall": report["overall"],
                "phase": report["phase"],
                "contact_started": True,
                "project_saved": False,
                "automatic_replay": False,
                "automatic_replay_attempted": False,
                "last_checkpoint": entry,
            }
        )

    async def invoke(
        name: str,
        call_arguments: dict[str, Any],
        *,
        mutating: bool = False,
    ) -> Any:
        remaining = args.overall_timeout_seconds - elapsed()
        if remaining <= 0:
            raise AcceptanceStepError(
                name,
                "overall Sound Selection acceptance deadline expired",
                unknown_outcome=mutating,
            )
        step: dict[str, Any] = {
            "tool": name,
            "arguments": dict(call_arguments),
            "status": "in_flight",
            "monotonic_elapsed_seconds": elapsed(),
            "mutating": mutating,
            "automatic_replay_attempted": False,
        }
        steps.append(step)
        report["phase"] = "tool_in_flight"
        save_checkpoint(step)
        try:
            result = await _call(
                name,
                call_arguments,
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
        inventory = await invoke("sound_selection_inventory", arguments["inventory"])
        report["inventory"] = inventory
        generator_evidence = _generator_inventory_evidence(
            inventory, _target(args.channel)
        )
        report["checks"]["generator_preset_inventory"] = {
            "status": "passed"
            if generator_evidence["identity_page"]
            else "unverified",
            "tool": "sound_selection_inventory",
            **generator_evidence,
            "session_fingerprint_present": _session(inventory) is not None,
        }
        if not generator_evidence["identity_page"]:
            report.update(
                {"overall": "fail", "phase": "generator_inventory_unverified"}
            )
            return report

        preset_page = await invoke("plugins_list_presets", arguments["preset_inventory"])
        report["preset_inventory"] = preset_page

        before = await invoke(
            "plugins_get_current_preset", arguments["current_preset_before"]
        )
        report["preset_before"] = before
        selected: dict[str, Any] | None = None
        if args.apply:
            selected = _choose_preset(
                preset_page,
                before,
                requested_name=args.preset_name,
                requested_index=args.preset_index,
            )
            report["exact_preset"] = selected
            report["checks"]["exact_preset_selection"] = {
                "status": "prepared",
                "requested": selected,
                "target": _target(args.channel),
            }
        else:
            report["checks"]["exact_preset_selection"] = {
                "status": "not_requested",
                "reason": "--apply is required before a live preset mutation.",
            }

        pad_map = await invoke("plugins_inspect_pad_map", arguments["pad_map"])
        report["pad_map"] = pad_map
        pad_body = _dict(pad_map) or {}
        pad_compatible = (
            isinstance(pad_body.get("pad_count"), int)
            and not isinstance(pad_body.get("pad_count"), bool)
            and isinstance(pad_body.get("pads"), list)
            and isinstance(pad_body.get("complete"), bool)
        )
        report["checks"]["compatible_pad_map"] = {
            "status": "passed" if pad_compatible else "unverified",
            "tool": "plugins_inspect_pad_map",
            "target": arguments["pad_map"]["target"],
            "pad_count": pad_body.get("pad_count"),
            "complete": pad_body.get("complete"),
            "compatible": pad_compatible,
        }
        if not pad_compatible:
            report.update({"overall": "fail", "phase": "pad_map_unverified"})
            return report

        plan = await invoke("sound_selection_plan", arguments["plan"])
        report["plan"] = plan
        plan_body = _dict(plan) or {}
        report["checks"]["palette_planning"] = {
            "status": "blocked" if plan_body.get("blockers") else "passed",
            "tool": "sound_selection_plan",
            "palette_id": plan_body.get("palette_id"),
            "assignment_count": len(plan_body.get("assignments", []))
            if isinstance(plan_body.get("assignments"), list)
            else 0,
            "blockers": plan_body.get("blockers", []),
        }
        if plan_body.get("blockers"):
            report.update({"overall": "fail", "phase": "plan_blocked"})
            return report

        if args.apply:
            if selected is None:
                raise AcceptanceConfigurationError(
                    "exact preset selection was not prepared"
                )
            session = _session(inventory)
            if session is None:
                raise AcceptanceConfigurationError(
                    "Sound Selection inventory did not expose a valid session fingerprint; "
                    "preset selection and palette application were not attempted"
                )
            page_session = _session(preset_page)
            before_session = _session(before)
            if page_session is not None and page_session != session:
                raise AcceptanceConfigurationError(
                    "preset inventory session fingerprint changed before selection"
                )
            if before_session is not None and before_session != session:
                raise AcceptanceConfigurationError(
                    "current preset session fingerprint changed before selection"
                )
            page_target_fp = _target_fingerprint(preset_page)
            before_target_fp = _target_fingerprint(before)
            if (
                page_target_fp is not None
                and before_target_fp is not None
                and page_target_fp != before_target_fp
            ):
                raise AcceptanceConfigurationError(
                    "preset inventory and current-preset target fingerprints disagree"
                )
            target_fp = page_target_fp or before_target_fp
            if target_fp is None:
                raise AcceptanceConfigurationError(
                    "preset observations did not expose a target fingerprint; "
                    "preset selection was not attempted"
                )
            expected_name, expected_index, expected_status = _current_identity(before)
            expected_current = None
            if expected_name is not None or expected_index is not None:
                expected_current = {"name": expected_name, "index": expected_index}
            selection_arguments = dict(arguments["exact_selection"])
            selection_arguments.update(
                {
                    "preset_name": selected["name"],
                    "preset_index": selected["index"],
                    "session_fingerprint": session,
                    "target_fingerprint": target_fp,
                }
            )
            if expected_current is not None and expected_status not in {
                "ambiguous",
                "unsupported",
                "unresolved",
            }:
                selection_arguments["expected_current"] = expected_current
            selection = await invoke(
                "fl_select_plugin_preset",
                selection_arguments,
                mutating=True,
            )
            report["preset_selection"] = selection
            selection_body = _dict(selection) or {}
            selection_session = _session(selection)
            selection_target_fp = _target_fingerprint(selection)
            selection_verified = (
                selection_body.get("verified") is True
                and selection_body.get("outcome", "verified") == "verified"
                and _identity_matches(selection_body.get("after"), selected)
                and (selection_session is None or selection_session == session)
                and (selection_target_fp is None or selection_target_fp == target_fp)
            )
            report["checks"]["exact_preset_selection"] = {
                "status": "passed" if selection_verified else "unverified",
                "requested": selected,
                "target": _target(args.channel),
                "outcome": selection_body.get("outcome"),
                "verification_summary": selection_body.get("verification_summary"),
                "automatic_replay_attempted": False,
            }
            if not selection_verified:
                report.update(
                    {
                        "overall": "fail",
                        "phase": "unknown_outcome"
                        if selection_body.get("outcome") == "unknown"
                        else "preset_selection_unverified",
                    }
                )
                report["automatic_replay_reason"] = (
                    "Preset mutation was not replayed after an unknown or unverified outcome."
                )
                return report

            after = await invoke(
                "plugins_get_current_preset", arguments["current_preset_before"]
            )
            report["preset_after"] = after
            after_session = _session(after)
            after_target_fp = _target_fingerprint(after)
            readback_verified = (
                _identity_matches(after, selected)
                and (after_session is None or after_session == session)
                and (after_target_fp is None or after_target_fp == target_fp)
            )
            report["checks"]["later_tick_preset_readback"] = {
                "status": "passed" if readback_verified else "unverified",
                "tool": "plugins_get_current_preset",
                "requested": selected,
                "readback": {
                    "name": _current_identity(after)[0],
                    "index": _current_identity(after)[1],
                    "status": _current_identity(after)[2],
                },
            }
            if not readback_verified:
                report.update(
                    {"overall": "fail", "phase": "preset_readback_unverified"}
                )
                return report

            apply_arguments = dict(arguments["apply"])
            apply_arguments.update({"palette": plan, "session_fingerprint": session})
            applied = await invoke(
                "sound_selection_apply", apply_arguments, mutating=True
            )
            report["apply"] = applied
            applied_body = _dict(applied) or {}
            applied_session = _session(applied)
            apply_verified = (
                applied_body.get("status") == "applied"
                and not applied_body.get("blockers")
                and (applied_session is None or applied_session == session)
            )
            report["checks"]["palette_application"] = {
                "status": "passed" if apply_verified else "unverified",
                "tool": "sound_selection_apply",
                "status_from_tool": applied_body.get("status"),
                "verified_count": applied_body.get("verified_count"),
                "automatic_replay_attempted": False,
            }
            if not apply_verified:
                report.update({"overall": "fail", "phase": "application_unverified"})
                report["automatic_replay_reason"] = (
                    "Palette application stopped after its first unverified/unknown result; no replay was attempted."
                )
                return report
        else:
            report["checks"]["palette_application"] = {
                "status": "not_requested",
                "reason": "--apply is required before changing the disposable project.",
            }

        production_request = arguments["production_request"]
        production_plan = arguments["production_plan"]
        validation = await invoke(
            "postfader_validate_run",
            {"request": production_request, "plan": production_plan},
        )
        report["production_validation"] = validation
        validation_body = _dict(validation) or {}
        if validation_body.get("valid") is False or validation_body.get("blockers"):
            report.update(
                {"overall": "fail", "phase": "production_run_validation_blocked"}
            )
            return report

        run_result = await invoke(
            "postfader_execute_run",
            {"request": production_request, "plan": production_plan},
            mutating=args.apply,
        )
        report["production_run"] = run_result
        run_body = _dict(run_result) or {}
        if run_body.get("status") not in {None, "completed"}:
            report.update({"overall": "fail", "phase": "production_run_blocked"})
            return report
        run_id = _extract_run_id(run_result)
        run_state = await invoke("postfader_get_run", {"run_id": run_id})
        report["production_run_initial"] = run_state
        initial_outputs = _outputs(run_state)
        anchor_before = _anchor_snapshot(initial_outputs)
        report["checks"]["production_run_reference"] = {
            "status": "passed"
            if anchor_before
            and any(
                row.get("operation_id") == REFERENCE_OPERATION
                and row.get("output") == "sound_palette"
                for row in initial_outputs
            )
            else "unverified",
            "run_id": run_id,
            "reference_operation": REFERENCE_OPERATION,
            "typed_sound_palette_output": any(
                row.get("operation_id") == REFERENCE_OPERATION
                and row.get("output") == "sound_palette"
                for row in initial_outputs
            ),
            "anchor_roles": sorted(anchor_before),
        }
        if args.apply:
            apply_receipt = next(
                (
                    row
                    for row in initial_outputs
                    if row.get("operation_id") == APPLY_OPERATION
                    and row.get("output") == "sound_palette"
                ),
                None,
            )
            apply_value = None if apply_receipt is None else apply_receipt.get("value")
            report["checks"]["production_run_palette_application"] = {
                "status": (
                    "passed"
                    if isinstance(apply_value, dict)
                    and apply_value.get("status") == "applied"
                    else "unverified"
                ),
                "operation_id": APPLY_OPERATION,
                "typed_output": apply_receipt is not None,
            }
            if report["checks"]["production_run_palette_application"]["status"] != "passed":
                report.update(
                    {"overall": "fail", "phase": "production_application_unverified"}
                )
                return report
        if report["checks"]["production_run_reference"]["status"] != "passed":
            report.update(
                {"overall": "fail", "phase": "production_reference_unverified"}
            )
            return report

        continued = await invoke(
            "postfader_continue_run",
            {"run_id": run_id, "delta": arguments["production_continuation"]},
        )
        report["production_continuation"] = continued
        continued_body = _dict(continued) or {}
        if continued_body.get("status") not in {None, "completed"}:
            report.update(
                {"overall": "fail", "phase": "production_continuation_blocked"}
            )
            return report
        final_state = await invoke("postfader_get_run", {"run_id": run_id})
        report["production_run_final"] = final_state
        anchor_preserved, continuation_detail = _variation_anchor_preserved(
            _outputs(final_state), anchor_before
        )
        report["checks"]["continuation_preserves_anchor"] = {
            "status": "passed" if anchor_preserved else "unverified",
            "detail": continuation_detail,
            "automatic_replay_attempted": False,
        }
        if not anchor_preserved:
            report.update(
                {"overall": "fail", "phase": "anchor_preservation_unverified"}
            )
            return report
        report["phase"] = "complete"
        return report
    except AcceptanceStepError as exc:
        report.update(
            {
                "overall": "fail",
                "phase": "unknown_outcome"
                if exc.unknown_outcome
                else "workflow_execution",
                "error": str(exc),
                "failed_tool": exc.tool,
                "automatic_replay": False,
                "automatic_replay_attempted": False,
            }
        )
        return report


def _private_output(path: str | None) -> str | None:
    """Reject evidence destinations inside this public checkout."""

    if path is None:
        return None
    resolved = Path(path).expanduser().resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        return os.fspath(resolved)
    raise EvidenceOutputError(
        "live acceptance evidence must be stored outside the public repository"
    )


def _finish(destination: Any, value: dict[str, Any], *, contact_started: bool) -> int:
    output_failures: list[dict[str, Any]] = []
    if destination is not None:
        try:
            destination.write(value)
        except Exception as exc:
            output_failures.append(
                {"stage": "final_evidence_write", "reason": str(exc)}
            )
        try:
            destination.close()
        except Exception as exc:
            output_failures.append(
                {"stage": "final_evidence_close", "reason": str(exc)}
            )
    if output_failures:
        failure = structured_failure(
            kind=ACCEPTANCE_KIND,
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and args.plan:
        value = structured_failure(
            kind=ACCEPTANCE_KIND,
            phase="argument_validation",
            error="--apply cannot be combined with --plan",
            contact_started=False,
        )
        return _finish(None, value, contact_started=False)
    try:
        output_path = _private_output(args.output)
        destination = reserve_evidence_output(output_path, required=not args.plan)
    except EvidenceOutputError as exc:
        value = structured_failure(
            kind=ACCEPTANCE_KIND,
            phase="output_reservation",
            error=exc,
            contact_started=False,
        )
        print(json.dumps(value, indent=2, sort_keys=True), file=sys.stderr)
        return 2

    try:
        configure_acceptance_transport(args.midi_port, live=not args.plan)
        if destination is not None and not args.plan:
            try:
                destination.write(
                    {
                        "schema_version": 1,
                        "kind": ACCEPTANCE_KIND,
                        "overall": "started",
                        "phase": "contact_started",
                        "contact_started": True,
                        "project_saved": False,
                        "automatic_replay": False,
                        "automatic_replay_attempted": False,
                    }
                )
            except Exception as exc:
                value = structured_failure(
                    kind=ACCEPTANCE_KIND,
                    phase="evidence_output_before_contact",
                    error=exc,
                    contact_started=False,
                )
                return _finish(destination, value, contact_started=False)
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
    except HostConfigurationError as exc:
        value = structured_failure(
            kind=ACCEPTANCE_KIND,
            phase="transport_configuration",
            error=exc,
            contact_started=False,
        )
    except (AcceptanceConfigurationError, OSError, TimeoutError, ValueError) as exc:
        value = structured_failure(
            kind=ACCEPTANCE_KIND,
            phase="workflow_execution",
            error=exc,
            contact_started=not args.plan,
        )
    except BaseException as exc:
        value = structured_failure(
            kind=ACCEPTANCE_KIND,
            phase="interrupted_execution",
            error="%s: %s" % (type(exc).__name__, exc),
            contact_started=not args.plan,
        )
    return _finish(destination, value, contact_started=not args.plan)


if __name__ == "__main__":
    raise SystemExit(main())
