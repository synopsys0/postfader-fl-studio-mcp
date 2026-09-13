"""One comprehensive, read-only collection pass for creation readiness.

This module owns live observation only.  Readiness evaluation remains a pure
service in :mod:`readiness`, and execution consumes the immutable context
snapshot produced here instead of rescanning the full project between phases.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence, cast

from .. import __version__
from ..contracts import ConnectionInfo, ProjectSummary
from ..creative import PIANO_ROLL
from ..host_config import midi_port_query
from ..performance import TrackBInspector
from ..plugin_atlas import load_bundled_registry
from ..readonly_inspector import ReadOnlyInspector
from ..sound_selection.executor import SOUND_SELECTION, SoundSelectionService
from ..sound_selection.models import (
    SoundInventory,
    SoundSelectionRequest,
    SoundTargetInventory,
    canonical_digest,
    target_identity_key,
)
from ..track_b_contracts import MixerEffectTarget, PatternList
from .context import (
    ContextTargetIdentity,
    CreationRunContextSnapshot,
    PianoRollArmingReceipt,
    build_context_snapshot,
)
from .models import (
    CompletionTarget,
    ConnectionReadiness,
    CreationReadinessInput,
    CreationReadinessReport,
    DrumCoverage,
    EffectCoverageReport,
    EffectCoverageState,
    InstrumentPoolCoverage,
    InstrumentTargetCoverage,
    LoadedProcessingCapability,
    MissingProcessingCapability,
    PatternCoverage,
    PatternIdentity,
    PianoRollReadiness,
    RoleEffectCoverage,
    ScopeReadiness,
)
from .processing import (
    EffectCoverageReport as SemanticEffectCoverageReport,
)
from .processing import (
    LoadedProcessingCapability as SemanticLoadedProcessingCapability,
)
from .processing import (
    ProcessingRequest,
    evaluate_effect_coverage,
    resolve_loaded_capabilities,
)
from .readiness import CreationReadinessService


_PROCESS_NONCE = secrets.token_hex(16)
MCP_PROCESS_IDENTITY = hashlib.sha256(
    f"{os.getpid()}:{_PROCESS_NONCE}".encode("ascii")
).hexdigest()


@dataclass(frozen=True)
class CollectedCreationReadiness:
    """Internal live collection returned to the Production Run registry."""

    readiness_input: CreationReadinessInput
    context_snapshot: CreationRunContextSnapshot
    sound_inventory: SoundInventory | None
    loaded_processing_observations: tuple[dict[str, Any], ...]
    full_inventory_scan_count: int
    target_refresh_count: int
    preset_enumeration_count: int
    connection: ConnectionInfo | None = None
    project: ProjectSummary | None = None
    patterns: PatternList | None = None
    mixer_names: tuple[tuple[int, str], ...] = ()
    channel_names: tuple[tuple[int, str], ...] = ()
    playlist_names: tuple[tuple[int, str], ...] = ()
    mixer_route_destinations: tuple[tuple[int, tuple[int, ...]], ...] = ()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = cast(Any, value).model_dump(mode="json", exclude_none=False)
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _midi_endpoint() -> str | None:
    try:
        return midi_port_query()
    except Exception:
        return None


def interpret_completion_target(text: str) -> CompletionTarget:
    """Map connected-AI completion prose to bounded readiness semantics."""

    folded = text.casefold()
    if any(token in folded for token in ("polished", "mix-ready", "mix ready")):
        return "polished_mix_ready"
    if any(
        token in folded
        for token in ("first-pass", "first pass", "produced draft", "production")
    ):
        return "first_pass_production"
    if "composition" in folded and not any(
        token in folded for token in ("playable", "arrangement", "draft")
    ):
        return "composition_only"
    if any(token in folded for token in ("playable", "draft", "arrangement")):
        return "playable_draft"
    return "custom"


def _operation_name(operation: object) -> str:
    return str(getattr(operation, "operation", ""))


def _plan_sound_request(
    operations: Sequence[object],
) -> SoundSelectionRequest | None:
    candidate = next(
        (
            getattr(operation, "request", None)
            for operation in operations
            if _operation_name(operation) == "plan_sound_palette"
        ),
        None,
    )
    return candidate if isinstance(candidate, SoundSelectionRequest) else None


def _requested_roles(
    sound_request: SoundSelectionRequest | None,
) -> tuple[str, ...]:
    roles = getattr(sound_request, "roles", ()) if sound_request is not None else ()
    return tuple(
        dict.fromkeys(
            str(getattr(role, "role_id"))
            for role in roles
            if getattr(role, "role_id", None)
        )
    )


def _required_drum_roles(
    operations: Sequence[object], sound_request: SoundSelectionRequest | None
) -> tuple[str, ...]:
    values: list[str] = []
    for role in getattr(sound_request, "roles", ()) if sound_request is not None else ():
        role_type = str(getattr(role, "role_type", "")).casefold()
        role_id = str(getattr(role, "role_id", "")).casefold()
        if role_type in {"drum", "drums", "drum_kit", "percussion"} or role_id in {
            "drum",
            "drums",
            "drum_kit",
        }:
            values.extend(getattr(role, "required_drum_roles", ()))
    for operation in operations:
        if _operation_name(operation) in {"inspect_drum_map", "select_drum_kit"}:
            values.extend(getattr(operation, "required_roles", ()))
    for operation in operations:
        if _operation_name(operation) != "generate_drums":
            continue
        values.extend(("kick", "snare", "closed_hat"))
        if getattr(operation, "style", None) == "house":
            values.append("open_hat")
    return tuple(dict.fromkeys(str(item) for item in values if str(item).strip()))


def _piano_roll_readiness(operations: Sequence[object]) -> PianoRollReadiness:
    required = any(
        _operation_name(operation) in {"write_note_sequence", "transform_piano_roll"}
        for operation in operations
    )
    try:
        status = PIANO_ROLL.status()
    except Exception as exc:
        return PianoRollReadiness(
            required=required,
            warnings=(f"Piano Roll setup could not be inspected: {exc}",),
        )
    manual_action = None
    if required and not status.armed_this_session:
        from .models import ReadinessManualAction

        manual_action = ReadinessManualAction(
            action_id="arm-piano-roll-bridge",
            dimension="piano_roll",
            instruction=status.setup_instruction,
            required=True,
            completed=False,
            blocking=True,
        )
    return PianoRollReadiness(
        required=required,
        apply_script_present=status.script_exists,
        armed_this_process=status.armed_this_session,
        # ``confirm`` sets armed only after verifying an authenticated receipt.
        authenticated_arming_receipt=status.armed_this_session,
        arming_receipt_id=status.last_request_id,
        target_selection_supported=status.automatic_trigger_supported,
        persistence_receipt_supported=status.automatic_trigger_supported,
        manual_action=manual_action,
    )


def _connection_readiness(
    connection: ConnectionInfo, *, midi_endpoint: str | None
) -> ConnectionReadiness:
    expected = connection.expected_bridge_source_sha256
    running = connection.bridge_source_sha256
    transport_ready = connection.connected and connection.bridge_transport != "none"
    return ConnectionReadiness(
        connection_info=connection,
        mcp_process_identity=MCP_PROCESS_IDENTITY,
        package_source_revision=f"postfader-{__version__}",
        deployed_bridge_revision=expected,
        running_bridge_revision=running,
        midi_endpoint=midi_endpoint,
        midi_input_available=transport_ready,
        midi_output_available=transport_ready,
        queue_healthy=transport_ready,
        supported_fl_build=connection.compatible,
        scripting_api_version=connection.midi_scripting_api_version,
        runtime_write_mode_control=connection.runtime_write_mode_control,
        current_write_state=(
            "enabled" if connection.verified_writes_enabled else "disabled"
        ),
        require_process_identity=True,
        require_midi=True,
    )


def _instrument_coverage(
    inventory: SoundInventory | None,
    sound_request: SoundSelectionRequest | None,
) -> InstrumentPoolCoverage:
    requested_roles = _requested_roles(sound_request)
    if inventory is None:
        return InstrumentPoolCoverage(
            requested_roles=requested_roles,
            missing_roles=requested_roles,
            warnings=("Loaded generator inventory was unavailable.",),
        )
    targets = tuple(
        InstrumentTargetCoverage(
            target=item.target,
            target_fingerprint=item.target_fingerprint,
            product_id=item.product_id,
            product_name=item.product_name,
            current_preset=item.current_preset,
            requested_roles=tuple(
                role
                for role in requested_roles
                if not item.role_ids
                or role.casefold() in {value.casefold() for value in item.role_ids}
                or role.casefold()
                in {value.casefold() for value in item.atlas_common_roles}
            ),
            atlas_match=item.atlas_product_id is not None,
            preset_navigation_supported=item.preset_navigation_available,
            preset_discovery_supported=bool(item.preset_names or item.preset_count == 0),
            usable_preset_candidate=(
                item.current_preset is not None or bool(item.preset_names)
            ),
            preset_identity_verified=item.preset_identity_stable,
            warnings=item.warnings,
        )
        for item in inventory.loaded_generators
    )
    role_requests = {
        role.role_id: role
        for role in (() if sound_request is None else sound_request.roles)
        if role.required
    }
    required_roles = tuple(
        role for role in requested_roles if role in role_requests
    )

    # One Channel Rack generator target can hold only one exact preset at a
    # time.  Use a deterministic bipartite match so a single wildcard target
    # cannot make an arbitrarily large requested palette appear ready.  Atlas
    # role metadata narrows a target when present; sparse metadata remains a
    # wildcard whose suitability confidence is decided by Sound Selection.
    candidate_indices: dict[str, tuple[int, ...]] = {}
    for role_id in required_roles:
        role_request = role_requests[role_id]
        explicit_targets = set(role_request.target_candidates)
        exact: list[int] = []
        wildcard: list[int] = []
        for index, (coverage, source) in enumerate(
            zip(targets, inventory.loaded_generators, strict=True)
        ):
            if explicit_targets and source.target not in explicit_targets:
                continue
            declared_roles = {
                value.casefold()
                for value in (*source.role_ids, *source.atlas_common_roles)
            }
            if role_id.casefold() in declared_roles:
                exact.append(index)
            elif not declared_roles:
                wildcard.append(index)
            elif not coverage.requested_roles:
                # The inventory coverage model may intentionally leave custom
                # roles broad when Atlas metadata does not establish a hard
                # exclusion.  Keep those targets behind exact matches.
                wildcard.append(index)
        candidate_indices[role_id] = tuple((*exact, *wildcard))

    target_owner: dict[int, str] = {}

    def assign(role_id: str, visited: set[int]) -> bool:
        for target_index in candidate_indices.get(role_id, ()):
            if target_index in visited:
                continue
            visited.add(target_index)
            previous = target_owner.get(target_index)
            if previous is None or assign(previous, visited):
                target_owner[target_index] = role_id
                return True
        return False

    covered = [
        role_id
        for role_id in required_roles
        if assign(role_id, set())
    ]
    missing = tuple(role for role in required_roles if role not in covered)
    loaded_products = tuple(
        dict.fromkeys(item.product_id or item.product_name for item in targets)
    )
    return InstrumentPoolCoverage(
        loaded_generators=targets,
        requested_roles=requested_roles,
        covered_roles=tuple(covered),
        missing_roles=missing,
        loaded_products=loaded_products,
        preset_navigation_supported=any(
            item.preset_navigation_supported for item in targets
        ),
        usable_preset_discovery=any(
            item.preset_discovery_supported for item in targets
        ),
        warnings=inventory.warnings,
    )


def _plan_processing_request(operations: Sequence[object]) -> ProcessingRequest | None:
    candidate = next(
        (
            getattr(operation, "request", None)
            for operation in operations
            if _operation_name(operation) == "plan_processing"
        ),
        None,
    )
    return candidate if isinstance(candidate, ProcessingRequest) else None


def _creation_effect_coverage(
    semantic: SemanticEffectCoverageReport,
) -> EffectCoverageReport:
    """Project semantic coverage into the readiness foundation contract."""

    def readiness_state(value: str) -> EffectCoverageState:
        if value in {"partially_covered", "unresolved_effect"}:
            return "loaded_but_unresolved"
        return cast(EffectCoverageState, value)

    loaded = tuple(
        _creation_loaded_capability(item)
        for item in semantic.loaded_capabilities
    )
    loaded_by_target = {
        (item.track_index, item.slot_index): item for item in loaded
    }

    def missing_capability(item: object) -> MissingProcessingCapability:
        required = bool(getattr(item, "required", False))
        return MissingProcessingCapability(
            role_id=str(getattr(item, "role", "project")),
            category=str(getattr(item, "category")),
            reason=str(getattr(item, "reason")),
            required_for_completion=required,
            classification="blocking" if required else "limitation",
        )

    missing = tuple(missing_capability(item) for item in semantic.missing_capabilities)
    role_rows: list[RoleEffectCoverage] = []
    for item in semantic.roles:
        role_loaded = tuple(
            loaded_by_target[key]
            for effect in item.loaded_effects
            if (
                key := (effect.track_index, effect.slot_index)
            ) in loaded_by_target
        )
        role_missing = tuple(
            missing_capability(value) for value in item.missing_capabilities
        )
        state = readiness_state(item.state)
        role_rows.append(
            RoleEffectCoverage(
                role_id=item.role,
                loaded_effects=role_loaded,
                requested_techniques=item.requested_techniques,
                supported_techniques=item.supported_techniques,
                unresolved_controls=item.unresolved_controls,
                missing_capabilities=role_missing,
                state=state,
                dry_playback_allowed=item.dry_playback_allowed,
                processing_required_for_completion=(
                    item.processing_required_for_completion
                ),
                warning="; ".join(item.limitations) or None,
            )
        )
    project_state = readiness_state(semantic.state)
    return EffectCoverageReport(
        roles=tuple(role_rows),
        requested_categories=semantic.requested_categories,
        missing_capabilities=missing,
        loaded_capabilities=loaded,
        completion_target=semantic.completion_target,
        processing_required_for_completion=(
            semantic.processing_required_for_completion
        ),
        can_produce_dry_draft=semantic.can_produce_dry_draft,
        dry_by_design=semantic.dry_by_design,
        state=project_state,
        processing_state=semantic.processing_state,
        required_processing_missing=semantic.required_processing_missing,
        warnings=semantic.warnings,
    )


def _creation_loaded_capability(
    item: SemanticLoadedProcessingCapability,
) -> LoadedProcessingCapability:
    return LoadedProcessingCapability(
        role_id=item.role_id,
        track_index=item.track_index,
        slot_index=item.slot_index,
        product_id=item.product_id,
        product_name=item.plugin_name,
        target_fingerprint=item.target_fingerprint,
        atlas_match=item.atlas_match,
        atlas_product_id=item.product_id,
        adapter_id=item.adapter_id,
        semantic_controls=item.controls,
        supported_techniques=item.supported_techniques,
        unresolved_controls=item.unresolved_controls,
        controllable=item.control_evidence,
    )


def _drum_coverage(
    inventory: SoundInventory | None,
    required_roles: tuple[str, ...],
    operations: Sequence[object],
    sound_request: SoundSelectionRequest | None,
) -> DrumCoverage:
    if not required_roles:
        return DrumCoverage(required=False)
    loaded = () if inventory is None else inventory.loaded_generators
    candidates: tuple[SoundTargetInventory, ...]
    requested_targets: list[object] = []
    for operation in operations:
        if _operation_name(operation) not in {"inspect_drum_map", "select_drum_kit"}:
            continue
        target = getattr(operation, "target", None)
        if getattr(target, "kind", None) in {"channel_generator", "mixer_effect"}:
            requested_targets.append(target)
    if sound_request is not None:
        for role in sound_request.roles:
            role_kind = str(getattr(role, "role_type", "")).casefold()
            role_id = str(getattr(role, "role_id", "")).casefold()
            if role_kind not in {"drum", "drums", "drum_kit", "percussion"} and role_id not in {
                "drum",
                "drums",
                "drum_kit",
            }:
                continue
            requested_targets.extend(role.target_candidates)
    unique_targets = {
        target_identity_key(cast(Any, target)): target for target in requested_targets
    }
    selected_target = next(iter(unique_targets.values()), None)
    if selected_target is not None:
        selected_loaded = next(
            (item for item in loaded if item.target == selected_target),
            None,
        )
        if selected_loaded is None:
            return DrumCoverage(
                required=True,
                loaded_drum_generator=False,
                target=cast(Any, selected_target),
                required_roles=required_roles,
                missing_roles=required_roles,
                general_midi_fallback_available=True,
                warnings=("The requested drum-generator target is not loaded.",),
            )
        if selected_loaded.pad_map is None:
            return DrumCoverage(
                required=True,
                loaded_drum_generator=True,
                target=selected_loaded.target,
                target_fingerprint=selected_loaded.target_fingerprint,
                product_name=selected_loaded.product_name,
                exact_kit_preset_supported=(
                    selected_loaded.preset_navigation_available
                    and selected_loaded.preset_readback_available
                ),
                required_roles=required_roles,
                missing_roles=required_roles,
                general_midi_fallback_available=True,
                warnings=("The requested drum generator did not report a pad map.",),
            )
        candidates = (selected_loaded,)
    else:
        candidates = tuple(item for item in loaded if item.pad_map is not None)
    if not candidates:
        return DrumCoverage(
            required=True,
            loaded_drum_generator=False,
            required_roles=required_roles,
            missing_roles=required_roles,
            general_midi_fallback_available=True,
        )
    if len(candidates) > 1:
        return DrumCoverage(
            required=True,
            loaded_drum_generator=True,
            pad_map_available=True,
            required_roles=required_roles,
            missing_roles=required_roles,
            general_midi_fallback_available=True,
            warnings=(
                "Multiple loaded drum generators expose pad maps; select one target and reference its typed drum_map output.",
            ),
        )
    selected = next(iter(candidates))
    assert selected.pad_map is not None
    mappings = {item.role.casefold() for item in selected.pad_map.mappings}
    missing = tuple(role for role in required_roles if role.casefold() not in mappings)
    return DrumCoverage(
        required=True,
        loaded_drum_generator=True,
        target=selected.target,
        target_fingerprint=selected.target_fingerprint,
        product_name=selected.product_name,
        exact_kit_preset_supported=(
            selected.preset_navigation_available and selected.preset_readback_available
        ),
        drum_map=selected.pad_map,
        pad_map_available=True,
        required_roles=required_roles,
        mapped_roles=tuple(sorted(mappings)),
        missing_roles=missing,
        general_midi_fallback_available=True,
    )


def _pattern_coverage(
    patterns: PatternList | None,
    operations: Sequence[object],
) -> PatternCoverage:
    intended = tuple(
        dict.fromkeys(
            int(getattr(operation, "pattern_number"))
            for operation in operations
            if _operation_name(operation)
            in {"prepare_pattern", "select_pattern", "write_note_sequence", "transform_piano_roll"}
            and getattr(operation, "pattern_number", None) is not None
        )
    )
    required_empty = tuple(
        int(getattr(operation, "pattern_number"))
        for operation in operations
        if _operation_name(operation) == "prepare_pattern"
    )
    existing: list[PatternIdentity] = []
    empty: list[int] = []
    material: list[int] = []
    if patterns is not None:
        for item in patterns.patterns:
            fingerprint = _digest(
                {
                    "number": item.pattern_number,
                    "name": item.name,
                    "color": item.color,
                    "length": item.length_beats,
                    "default_empty": item.default_empty,
                }
            )
            contains = item.default_empty is False
            existing.append(
                PatternIdentity(
                    pattern_number=item.pattern_number,
                    name=item.name,
                    fingerprint=fingerprint,
                    contains_material=contains,
                )
            )
            if item.default_empty is True:
                empty.append(item.pattern_number)
            elif contains:
                material.append(item.pattern_number)
    missing_sections = tuple(
        str(getattr(operation, "name", f"Pattern {getattr(operation, 'pattern_number', '')}"))
        for operation in operations
        if _operation_name(operation) == "prepare_pattern"
        and int(getattr(operation, "pattern_number")) not in empty
    )
    note_application = any(
        _operation_name(operation) == "write_note_sequence" for operation in operations
    )
    manual_actions = (
        (
            "Place the created pattern clips on their intended Playlist tracks; "
            "PostFader cannot create or move Playlist clips through FL's API."
        ),
    ) if note_application else ()
    return PatternCoverage(
        existing_patterns=tuple(existing),
        empty_pattern_numbers=tuple(empty),
        intended_pattern_numbers=intended,
        required_empty_patterns=required_empty,
        patterns_with_existing_material=tuple(material),
        missing_sections=missing_sections,
        playlist_clip_visibility_supported=False,
        playlist_placement_supported=False,
        manual_playlist_placement_required=note_application,
        expected_manual_playlist_actions=manual_actions,
    )


def _processing_observations(
    track_inspector: TrackBInspector,
    inventory: SoundInventory | None,
) -> tuple[tuple[dict[str, Any], ...], int]:
    if inventory is None:
        return (), 0
    rows: list[dict[str, Any]] = []
    refreshes = 0
    for effect in inventory.loaded_effects:
        if not isinstance(effect.target, MixerEffectTarget):
            continue
        parameters: tuple[dict[str, object], ...] = ()
        try:
            scan = track_inspector.scan_plugin_parameters(
                target=effect.target,
                max_indices=8192,
                max_results=512,
            )
        except Exception:
            pass
        else:
            refreshes += 1
            parameters = tuple(
                {
                    "index": item.index,
                    "reported_name": item.reported_name or None,
                    "display_text": item.display_text,
                }
                for item in scan.parameters
            )
        rows.append(
            {
                "target": effect.target.model_dump(mode="python"),
                "plugin_name": effect.product_name,
                "target_fingerprint": effect.target_fingerprint,
                "product_id": effect.product_id,
                "runtime_parameters": parameters,
            }
        )
    return tuple(rows), refreshes


def _basic_effect_coverage(
    inventory: SoundInventory | None,
    roles: tuple[str, ...],
    completion_target: CompletionTarget,
    processing_observations: tuple[dict[str, Any], ...] = (),
) -> EffectCoverageReport:
    if processing_observations:
        loaded = tuple(
            _creation_loaded_capability(item)
            for item in resolve_loaded_capabilities(
                processing_observations, registry=load_bundled_registry()
            )
        )
    else:
        # Atlas identity and an adapter name do not establish a writable
        # control.  Preserve the loaded effect but keep it unresolved until a
        # runtime parameter observation proves the semantic control.
        loaded = tuple(
            LoadedProcessingCapability(
                track_index=getattr(item.target, "track_index", None),
                slot_index=getattr(item.target, "slot_index", None),
                product_id=item.product_id,
                product_name=item.product_name,
                target_fingerprint=item.target_fingerprint,
                atlas_match=item.atlas_product_id is not None,
                adapter_id=item.control_adapter_id,
                semantic_controls=(),
                supported_techniques=item.atlas_technique_ids,
                unresolved_controls=("runtime control evidence",),
                controllable=False,
            )
            for item in (() if inventory is None else inventory.loaded_effects)
        )
    processing_requested = completion_target in {
        "first_pass_production",
        "mix_ready",
        "polished_mix_ready",
    }
    required = completion_target in {"mix_ready", "polished_mix_ready"}
    if not processing_requested:
        role_rows = tuple(
            RoleEffectCoverage(role_id=role, state="dry_by_design") for role in roles
        )
        return EffectCoverageReport(
            roles=role_rows,
            loaded_capabilities=loaded,
            completion_target=completion_target,
            dry_by_design=True,
        )
    if not loaded:
        missing = MissingProcessingCapability(
            category="requested_first_pass_processing",
            reason="No compatible Mixer effects were observed as loaded.",
            required_for_completion=required,
            classification="blocking" if required else "limitation",
        )
        return EffectCoverageReport(
            roles=tuple(
                RoleEffectCoverage(
                    role_id=role,
                    state="missing_requested_effect",
                    missing_capabilities=(missing,),
                    processing_required_for_completion=required,
                )
                for role in roles
            ),
            missing_capabilities=(missing,),
            loaded_capabilities=(),
            completion_target=completion_target,
            processing_required_for_completion=required,
            can_produce_dry_draft=True,
        )
    unresolved = not any(item.controllable for item in loaded)
    state = "loaded_but_unresolved" if unresolved else "loaded_and_controllable"
    return EffectCoverageReport(
        roles=tuple(
            RoleEffectCoverage(
                role_id=role,
                loaded_effects=loaded,
                state=state,
                processing_required_for_completion=required,
            )
            for role in roles
        ),
        loaded_capabilities=loaded,
        completion_target=completion_target,
        processing_required_for_completion=required,
        can_produce_dry_draft=True,
    )


def refresh_creation_readiness_from_cache(
    collected: CollectedCreationReadiness,
    *,
    operations: Sequence[object],
    completion_target_text: str,
    allowed_mutation_categories: Iterable[str] = (),
    required_mutation_categories: Iterable[str] = (),
    preserved_targets: Sequence[object] = (),
    unavailable_operations: Iterable[str] = (),
) -> tuple[CreationReadinessReport, CollectedCreationReadiness]:
    """Re-evaluate request/plan facts from one previously captured collection.

    Continuations may replace their unexecuted plan or revise the completion
    target.  The original readiness report is therefore not reusable: its
    role, drum, pattern, effect, and scope requirements describe the old
    request.  Rebuild those derived dimensions from the cached inventory and
    observations, without asking FL Studio for another inventory scan.

    The captured context is deliberately carried forward.  It contains the
    process/session proof and append-only receipt references that make a
    continuation safe; request-dependent readiness fields live in the rebuilt
    input instead.
    """

    cached_input = collected.readiness_input
    readiness_connection = cached_input.connection
    if collected.connection is not None:
        readiness_connection = _connection_readiness(
            collected.connection,
            midi_endpoint=(
                cached_input.connection.midi_endpoint
                or collected.context_snapshot.midi_endpoint
            ),
        )
        # Some older process-local registries retained the raw connection and
        # context but only a compact/default readiness input.  Do not invent a
        # missing MIDI observation for that compatibility shape; a current
        # collector always records ``require_midi=True`` explicitly.
        if (
            cached_input.connection.connection_info is None
            and cached_input.connection.mcp_process_identity is None
            and not cached_input.connection.require_midi
        ):
            readiness_connection = readiness_connection.model_copy(
                update={
                    "midi_input_available": None,
                    "midi_output_available": None,
                    "require_midi": False,
                }
            )
    cached_piano = cached_input.piano_roll
    piano_required = any(
        _operation_name(operation)
        in {"write_note_sequence", "transform_piano_roll"}
        for operation in operations
    )
    piano = (
        cached_piano
        if cached_piano.required == piano_required
        else cached_piano.model_copy(update={"required": piano_required})
    )

    sound_request = _plan_sound_request(operations)
    requested_roles = _requested_roles(sound_request)
    required_drum_roles = _required_drum_roles(operations, sound_request)
    completion_target = interpret_completion_target(completion_target_text)
    inventory = collected.sound_inventory
    instrument_pool = _instrument_coverage(inventory, sound_request)
    drums = _drum_coverage(
        inventory,
        required_drum_roles,
        operations,
        sound_request,
    )
    pattern_coverage = _pattern_coverage(collected.patterns, operations)
    processing_observations = collected.loaded_processing_observations
    processing_request = _plan_processing_request(operations)
    if processing_request is None:
        effects = _basic_effect_coverage(
            inventory,
            requested_roles or ("project",),
            completion_target,
            processing_observations,
        )
    else:
        if completion_target != "custom":
            processing_request = processing_request.model_copy(
                update={"completion_target": completion_target}
            )
        effects = _creation_effect_coverage(
            evaluate_effect_coverage(
                processing_request,
                loaded_plugins=processing_observations,
                registry=load_bundled_registry(),
            )
        )

    allowed = tuple(dict.fromkeys(str(item) for item in allowed_mutation_categories))
    allowed_set = set(allowed)
    required = tuple(
        item
        for item in dict.fromkeys(str(value) for value in required_mutation_categories)
        if item in allowed_set
    )
    scope = ScopeReadiness(
        preserved_targets=tuple(preserved_targets),
        allowed_mutation_categories=allowed,
        required_mutation_categories=required,
        unavailable_operations=tuple(dict.fromkeys(unavailable_operations)),
        expected_manual_playlist_actions=pattern_coverage.expected_manual_playlist_actions,
    )

    # Keep process/session identity and append-only receipts from the original
    # collection while keeping derived digest fields aligned with the rebuilt
    # readiness facts.
    context = collected.context_snapshot.model_copy(
        update={
            "drum_map_digest": (
                None if drums.drum_map is None else canonical_digest(drums.drum_map)
            ),
            "effect_coverage_digest": _digest(effects),
        }
    )
    refreshed_input = cached_input.model_copy(
        update={
            "connection": readiness_connection,
            "piano_roll": piano,
            "instrument_pool": instrument_pool,
            "drum_coverage": drums,
            "patterns": pattern_coverage,
            "effects": effects,
            "scope": scope,
            "requested_roles": requested_roles,
            "required_drum_roles": required_drum_roles,
            "completion_target": completion_target,
            "processing_required": effects.processing_required_for_completion,
            "context_snapshot": context,
            "drum_map_digest": (
                None if drums.drum_map is None else canonical_digest(drums.drum_map)
            ),
            "effect_coverage_digest": _digest(effects),
        }
    )
    refreshed = replace(
        collected,
        readiness_input=refreshed_input,
        context_snapshot=context,
    )
    return CreationReadinessService().evaluate(refreshed_input), refreshed


def collect_creation_readiness(
    *,
    operations: Sequence[object],
    completion_target_text: str,
    allowed_mutation_categories: Iterable[str] = (),
    required_mutation_categories: Iterable[str] = (),
    preserved_targets: Sequence[object] = (),
    unavailable_operations: Iterable[str] = (),
    inspector: ReadOnlyInspector | None = None,
    track_inspector: TrackBInspector | None = None,
    sound_service: SoundSelectionService | None = None,
) -> CollectedCreationReadiness:
    """Collect all independently available creation facts without mutation."""

    observed_at = _now()
    main_inspector = inspector or ReadOnlyInspector()
    tracks = track_inspector or TrackBInspector()
    sound = sound_service or SOUND_SELECTION
    connection = main_inspector.connection_info()
    project: ProjectSummary | None = None
    patterns: PatternList | None = None
    sound_inventory: SoundInventory | None = None
    mixer_names: tuple[tuple[int, str], ...] = ()
    channel_names: tuple[tuple[int, str], ...] = ()
    playlist_names: tuple[tuple[int, str], ...] = ()
    mixer_routes: list[tuple[int, tuple[int, ...]]] = []
    metadata_refreshes = 0
    sound_request = _plan_sound_request(operations)
    requested_roles = _requested_roles(sound_request)
    required_drum_roles = _required_drum_roles(operations, sound_request)
    piano = _piano_roll_readiness(operations)
    midi_endpoint = _midi_endpoint()

    if connection.connected and connection.compatible:
        try:
            project = main_inspector.project_summary()
        except Exception:
            project = None
        try:
            patterns = tracks.list_patterns()
        except Exception:
            patterns = None
        try:
            mixer_names = tuple(
                (item.index, item.name)
                for item in main_inspector.list_mixer_tracks(
                    only_used=False, include_peaks=False
                ).tracks
            )
            metadata_refreshes += 1
        except Exception:
            mixer_names = ()
        try:
            channel_names = tuple(
                (item.channel_index, item.name)
                for item in tracks.list_channels().channels
            )
            metadata_refreshes += 1
        except Exception:
            channel_names = ()
        try:
            playlist_names = tuple(
                (item.track_index, item.name)
                for item in tracks.list_playlist_tracks().tracks
            )
            metadata_refreshes += 1
        except Exception:
            playlist_names = ()
        send_sources = tuple(
            dict.fromkeys(
                int(getattr(item, "track_index"))
                for operation in operations
                for item in getattr(operation, "operations", ())
                if getattr(item, "operation", None) == "mixer_send_level"
                and getattr(item, "track_index", None) is not None
            )
        )
        for track_index in send_sources:
            try:
                inspection = main_inspector.inspect_mixer_track(track_index)
            except Exception:
                continue
            mixer_routes.append(
                (
                    track_index,
                    tuple(
                        route.destination_track_index
                        for route in inspection.routes
                    ),
                )
            )
            metadata_refreshes += 1
        try:
            sound_inventory = sound.inventory(
                sound_request,
                include_effects=True,
                preset_start=0,
                preset_limit=64,
                include_current=True,
                include_pad_maps=True,
                include_atlas=True,
                discover_presets=True,
            )
        except Exception:
            sound_inventory = None

    processing_observations, target_refreshes = _processing_observations(
        tracks, sound_inventory
    )
    completion_target = interpret_completion_target(completion_target_text)
    instrument_pool = _instrument_coverage(sound_inventory, sound_request)
    drums = _drum_coverage(
        sound_inventory,
        required_drum_roles,
        operations,
        sound_request,
    )
    pattern_coverage = _pattern_coverage(patterns, operations)
    processing_request = _plan_processing_request(operations)
    if processing_request is None:
        effects = _basic_effect_coverage(
            sound_inventory,
            requested_roles or ("project",),
            completion_target,
            processing_observations,
        )
    else:
        if completion_target != "custom":
            processing_request = processing_request.model_copy(
                update={"completion_target": completion_target}
            )
        effects = _creation_effect_coverage(
            evaluate_effect_coverage(
                processing_request,
                loaded_plugins=processing_observations,
                registry=load_bundled_registry(),
            )
        )
    allowed = tuple(dict.fromkeys(str(item) for item in allowed_mutation_categories))
    required = tuple(
        item
        for item in dict.fromkeys(str(value) for value in required_mutation_categories)
        if item in set(allowed)
    )
    scope = ScopeReadiness(
        preserved_targets=tuple(preserved_targets),
        allowed_mutation_categories=allowed,
        required_mutation_categories=required,
        unavailable_operations=tuple(dict.fromkeys(unavailable_operations)),
        expected_manual_playlist_actions=(
            pattern_coverage.expected_manual_playlist_actions
        ),
    )

    target_fingerprints: list[ContextTargetIdentity] = []
    if sound_inventory is not None:
        for item in (*sound_inventory.loaded_generators, *sound_inventory.loaded_effects):
            if item.target_fingerprint is None:
                continue
            target_fingerprints.append(
                ContextTargetIdentity(
                    target_id=target_identity_key(item.target),
                    kind=item.target.kind,
                    fingerprint=item.target_fingerprint,
                    target=item.target,
                )
            )
    pattern_identities = pattern_coverage.existing_patterns
    palette_digest = (
        None if sound_inventory is None else canonical_digest(sound_inventory)
    )
    preset_digest = (
        None
        if sound_inventory is None
        else _digest(
            tuple(
                (
                    target_identity_key(item.target),
                    item.preset_count,
                    item.preset_names,
                    item.preset_indices,
                    item.current_preset,
                    item.current_preset_index,
                )
                for item in (
                    *sound_inventory.loaded_generators,
                    *sound_inventory.loaded_effects,
                )
            )
        )
    )
    drum_digest = None if drums.drum_map is None else canonical_digest(drums.drum_map)
    effect_digest = _digest(effects)
    arming_receipt = None
    if piano.armed_this_process:
        arming_receipt = PianoRollArmingReceipt(
            receipt_id="piano-" + _digest(
                {
                    "process": MCP_PROCESS_IDENTITY,
                    "receipt": piano.arming_receipt_id,
                }
            )[:24],
            process_identity=MCP_PROCESS_IDENTITY,
            authenticated=piano.authenticated_arming_receipt,
            script_present=piano.apply_script_present,
            captured_at=observed_at,
        )
    context = build_context_snapshot(
        session_fingerprint=connection.session_fingerprint,
        project=project,
        target_fingerprints=tuple(target_fingerprints),
        pattern_identities=pattern_identities,
        palette_inventory_digest=palette_digest,
        preset_inventory_digest=preset_digest,
        drum_map_digest=drum_digest,
        effect_coverage_digest=effect_digest,
        piano_roll_arming_receipt=arming_receipt,
        sound_inventory=sound_inventory,
        mcp_process_identity=MCP_PROCESS_IDENTITY,
        package_source_revision=f"postfader-{__version__}",
        deployed_bridge_revision=connection.expected_bridge_source_sha256,
        running_bridge_revision=connection.bridge_source_sha256,
        midi_endpoint=midi_endpoint,
        captured_at=observed_at,
    )
    readiness_input = CreationReadinessInput(
        observed_at=observed_at,
        connection=_connection_readiness(
            connection, midi_endpoint=midi_endpoint
        ),
        project=project,
        piano_roll=piano,
        instrument_pool=instrument_pool,
        drum_coverage=drums,
        patterns=pattern_coverage,
        effects=effects,
        scope=scope,
        requested_roles=requested_roles,
        required_drum_roles=required_drum_roles,
        completion_target=completion_target,
        processing_required=effects.processing_required_for_completion,
        context_snapshot=context,
        palette_inventory_digest=palette_digest,
        preset_inventory_digest=preset_digest,
        drum_map_digest=drum_digest,
        effect_coverage_digest=effect_digest,
        project_checkpoint_digest=context.project_checkpoint.digest,
    )
    preset_enumerations = 0
    if sound_inventory is not None:
        preset_enumerations = len(sound_inventory.loaded_generators) + len(
            sound_inventory.loaded_effects
        )
        # The initial page is already represented by the per-target count.
        # Add only the extra deterministic discovery pages so timing reflects
        # the real bounded work without double-counting the first page.
        preset_enumerations += sum(
            max(0, len(item.pages_examined) - 1)
            for item in sound_inventory.preset_discovery_coverage
        )
    return CollectedCreationReadiness(
        readiness_input=readiness_input,
        context_snapshot=context,
        sound_inventory=sound_inventory,
        loaded_processing_observations=processing_observations,
        full_inventory_scan_count=1 if sound_inventory is not None else 0,
        target_refresh_count=target_refreshes + metadata_refreshes,
        preset_enumeration_count=preset_enumerations,
        connection=connection,
        project=project,
        patterns=patterns,
        mixer_names=mixer_names,
        channel_names=channel_names,
        playlist_names=playlist_names,
        mixer_route_destinations=tuple(mixer_routes),
    )


__all__ = [
    "CollectedCreationReadiness",
    "MCP_PROCESS_IDENTITY",
    "collect_creation_readiness",
    "interpret_completion_target",
    "refresh_creation_readiness_from_cache",
]
