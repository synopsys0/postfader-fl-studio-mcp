"""Read-only delivery and manual Playlist handoff services.

FL Studio does not expose a supported Playlist clip writer. These helpers
therefore produce precise, truthful handoff records and export instructions;
they never click the UI, save the project, or claim that placement occurred.
Delivery manifests contain bounded metadata only, never audio bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from ..host_config import fl_studio_user_data_dir
from .models import (
    CreationEvaluationReport,
    DeliveryManifest,
    ExportHandoff,
    FrozenMap,
    ManualHandoff,
    PlaylistHandoff,
    PlaylistHandoffManifest,
    PlaylistPlacement,
    ReviewAssetKind,
    ReviewAudioAsset,
    ReviewGeneratedOutput,
    ReviewRoleAssignment,
    ReviewSection,
    RevisionComparison,
    _canonical_digest,
)
from .persistence import sanitize_review_payload


DELIVERY_PATH_ENV = "POSTFADER_CREATION_DELIVERY_PATH"
DEFAULT_DELIVERY_DIRECTORY = "deliveries"
DELIVERY_DIRECTORY_NAME = DEFAULT_DELIVERY_DIRECTORY
_DELIVERY_WRITE_LOCK = threading.RLock()

_STEM_REASON_MAP: dict[str, tuple[str, ...]] = {
    "masking": ("instrumental_stem", "vocal_stem"),
    "low_end": ("bass_stem", "drum_stem"),
    "sound_selection": ("role_stem",),
    "composition": ("role_stem",),
}
_VALID_STEM_KINDS = frozenset(
    {
        "instrumental_stem",
        "vocal_stem",
        "drum_stem",
        "bass_stem",
        "chord_stem",
        "lead_stem",
        "role_stem",
    }
)


def _dedupe(values: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if value is not None and str(value)))


def _placement(value: PlaylistPlacement | Mapping[str, Any]) -> PlaylistPlacement:
    if isinstance(value, PlaylistPlacement):
        return value
    data = dict(value)
    if "intended_playlist_track_number" not in data and "playlist_track" not in data and "playlist_track_number" in data:
        data["intended_playlist_track_number"] = data.pop("playlist_track_number")
    return PlaylistPlacement.model_validate(data, strict=False)


def _placements(values: Iterable[PlaylistPlacement | Mapping[str, Any]]) -> tuple[PlaylistPlacement, ...]:
    return tuple(_placement(value) for value in values)


def _placement_identity(value: PlaylistPlacement) -> tuple[Any, ...]:
    return (
        value.handoff_item_id,
        value.source_operation_id,
        value.pattern_id,
        value.pattern_number,
        value.pattern_name,
        value.section_id,
        value.intended_playlist_track_number,
        value.layer_order,
        value.replacement_vs_addition,
    )


def _placement_changed(left: PlaylistPlacement, right: PlaylistPlacement) -> bool:
    # Status/proof fields are not placement identity. A user can confirm the
    # same row later without causing a new revision delta.
    excluded = {"status", "completed_state", "user_confirmed_state", "handoff_item_id"}
    return left.model_dump(mode="json", exclude=excluded) != right.model_dump(mode="json", exclude=excluded)


def playlist_handoff_delta(
    placements: Sequence[PlaylistPlacement | Mapping[str, Any]],
    previous: Sequence[PlaylistPlacement | Mapping[str, Any]] = (),
) -> tuple[PlaylistPlacement, ...]:
    """Return only new or changed placement rows relative to ``previous``."""

    current = _placements(placements)
    prior = _placements(previous)
    by_identity = {_placement_identity(item): item for item in prior}
    return tuple(
        item
        for item in current
        if (by_identity.get(_placement_identity(item)) is None or _placement_changed(item, by_identity[_placement_identity(item)]))
    )


def create_playlist_handoff(
    placements: Sequence[PlaylistPlacement | Mapping[str, Any]] = (),
    *,
    previous: Sequence[PlaylistPlacement | Mapping[str, Any]] = (),
    handoff_id: str = "playlist-handoff",
    delta_only: bool = True,
    user_confirmed: bool = False,
    warnings: Sequence[str] = (),
) -> PlaylistHandoff:
    """Build an exact manual Playlist handoff and optional revision delta."""

    current = _placements(placements)
    delta = playlist_handoff_delta(current, previous) if delta_only else current
    status = "none_required" if not current else "user_confirmed_complete" if user_confirmed else "not_verifiable"
    if user_confirmed:
        current = tuple(
            item.model_copy(
                update={
                    "status": "confirmed",
                    "completed_state": "completed",
                    "user_confirmed_state": "confirmed",
                }
            )
            for item in current
        )
        delta = playlist_handoff_delta(current, previous) if delta_only else current
    all_warnings = list(warnings)
    if current and not user_confirmed:
        all_warnings.append("FL Studio does not expose Playlist clip placement readback; confirm these rows manually.")
    return PlaylistHandoff(
        handoff_id=handoff_id,
        placements=current,
        delta_from_source=delta,
        status=status,
        warnings=_dedupe(all_warnings),
    )


build_playlist_handoff = create_playlist_handoff
create_playlist_handoff_manifest = create_playlist_handoff


def create_playlist_handoff_delta(
    placements: Sequence[PlaylistPlacement | Mapping[str, Any]],
    previous: Sequence[PlaylistPlacement | Mapping[str, Any]] = (),
    *,
    handoff_id: str = "playlist-handoff",
    user_confirmed: bool = False,
    warnings: Sequence[str] = (),
) -> PlaylistHandoff:
    """Build the changed-row-only form used after a revision pass."""

    return create_playlist_handoff(
        placements,
        previous=previous,
        handoff_id=handoff_id,
        delta_only=True,
        user_confirmed=user_confirmed,
        warnings=warnings,
    )


def confirm_playlist_handoff(handoff: PlaylistHandoff | PlaylistHandoffManifest) -> PlaylistHandoff:
    """Record user confirmation without asserting FL Studio readback."""

    resolved = _playlist_handoff(handoff)
    if resolved is None:
        raise ValueError("a Playlist handoff is required")
    handoff = resolved
    placements = tuple(
        item.model_copy(
            update={
                "status": "confirmed",
                "completed_state": "completed",
                "user_confirmed_state": "confirmed",
            }
        )
        for item in handoff.placements
    )
    return handoff.model_copy(
        update={
            "placements": placements,
            "delta_from_source": handoff.delta_from_source,
            "status": "user_confirmed_complete" if placements else "none_required",
        }
    )


def _finding_category(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        category = value.get("category")
    else:
        category = getattr(value, "category", None)
    return None if category is None else str(category)


def _normalize_stem(value: str) -> ReviewAssetKind:
    aliases = {
        "vocal": "vocal_stem",
        "vocals": "vocal_stem",
        "instrumental": "instrumental_stem",
        "bass": "bass_stem",
        "drums": "drum_stem",
        "drum": "drum_stem",
        "role": "role_stem",
        "chords": "chord_stem",
        "lead": "lead_stem",
    }
    normalized = aliases.get(value.casefold(), value)
    if normalized not in _VALID_STEM_KINDS:
        raise ValueError(f"unsupported requested review stem: {value!r}")
    return cast(ReviewAssetKind, normalized)


def create_export_handoff(
    *,
    handoff_id: str = "export-handoff",
    recommended_filename: str = "PF_Review_01_After.wav",
    requested_stems: Sequence[str] = (),
    finding_categories: Sequence[str] = (),
    findings: Sequence[Any] = (),
    exact_start_bar: int | None = None,
    exact_end_bar: int | None = None,
    exact_start_seconds: float | None = None,
    exact_end_seconds: float | None = None,
    expected_location: str | None = None,
    bounded_discovery_root: str | None = None,
    next_action: str | None = None,
) -> ExportHandoff:
    """Create a deterministic full-mix export request and minimal stem set.

    Stems are requested only when a finding needs attribution evidence. Mix
    loudness, dynamics, tonal, and stereo findings continue to use the full
    mix alone.
    """

    categories = _dedupe([*finding_categories, *(_finding_category(item) for item in findings)])
    stems: list[ReviewAssetKind] = [_normalize_stem(str(item)) for item in requested_stems]
    reasons: dict[str, list[str]] = {}
    for category in categories:
        for raw_stem in _STEM_REASON_MAP.get(category, ()):
            stem = _normalize_stem(raw_stem)
            if stem not in stems:
                stems.append(stem)
            reasons.setdefault(stem, []).append(category)
    reason_map = {key: _dedupe(value) for key, value in reasons.items()}
    action = next_action or (
        f"Export the full mix as {recommended_filename} with normalization off, matching the before bounce settings."
    )
    return ExportHandoff(
        handoff_id=handoff_id,
        requested_stems=tuple(stems),
        exact_start_bar=exact_start_bar,
        exact_end_bar=exact_end_bar,
        exact_start_seconds=exact_start_seconds,
        exact_end_seconds=exact_end_seconds,
        recommended_filename=recommended_filename,
        before_after_naming_convention="PF_Review_01_Before.wav / PF_Review_01_After.wav",
        include_tails=False,
        normalization_off=True,
        matching_settings_required=True,
        expected_location=expected_location,
        bounded_discovery_root=bounded_discovery_root,
        stem_reasons=FrozenMap(reason_map),
        next_action=action,
    )


export_handoff_for_findings = create_export_handoff
build_export_handoff = create_export_handoff


def _asset_values(values: Sequence[ReviewAudioAsset | Mapping[str, Any]]) -> tuple[ReviewAudioAsset, ...]:
    output: list[ReviewAudioAsset] = []
    for value in values:
        output.append(value if isinstance(value, ReviewAudioAsset) else ReviewAudioAsset.model_validate(value, strict=False))
    return tuple(output)


def _model_payload(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        return None
    payload = model_dump(mode="json", exclude_none=False)
    return payload if isinstance(payload, Mapping) else None


def _frozen_mapping(value: Any) -> FrozenMap:
    payload = _model_payload(value)
    return FrozenMap(dict(payload or {}))


def _sequence_value(value: Any) -> tuple[Any, ...]:
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return ()
    if isinstance(value, Sequence):
        return tuple(value)
    return ()


def _generated_output(value: Any, index: int) -> ReviewGeneratedOutput | None:
    if isinstance(value, ReviewGeneratedOutput):
        return value
    payload = _model_payload(value)
    if payload is None and isinstance(value, str):
        payload = {"output_id": value, "output_kind": "handoff"}
    if payload is None:
        return None
    output_id = payload.get("output_id", payload.get("operation_id"))
    output_kind = payload.get("output_kind", payload.get("output"))
    if not isinstance(output_id, str) or not output_id.strip():
        output_id = f"output-{index + 1}"
    if output_kind == "composition_adaptation":
        # This source-run spelling predates the review output union.  Keep the
        # record useful as a generated pattern without widening that union.
        output_kind = "pattern"
    if output_kind not in {
        "note_sequence",
        "sound_palette",
        "palette_assignment",
        "processing_plan",
        "pattern",
        "handoff",
    }:
        output_kind = "handoff"
    output_kind_value = cast(
        Literal[
            "note_sequence",
            "sound_palette",
            "palette_assignment",
            "processing_plan",
            "pattern",
            "handoff",
        ],
        output_kind,
    )
    metadata = payload.get("metadata", payload.get("value", {}))
    metadata_map = _model_payload(metadata)
    digest = payload.get("digest")
    if not isinstance(digest, str) and metadata_map is not None:
        for digest_key in (
            "digest",
            "note_digest_sha256",
            "preset_identity_digest",
            "plan_digest",
            "request_digest",
        ):
            candidate = metadata_map.get(digest_key)
            if isinstance(candidate, str):
                digest = candidate
                break
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        digest = None
    try:
        return ReviewGeneratedOutput(
            output_id=output_id,
            output_kind=output_kind_value,
            role_id=payload.get("role_id") if isinstance(payload.get("role_id"), str) else None,
            section_id=payload.get("section_id") if isinstance(payload.get("section_id"), str) else None,
            digest=digest,
            metadata=_frozen_mapping(metadata_map),
        )
    except ValueError:
        return None


def _generated_output_values(values: Any) -> tuple[ReviewGeneratedOutput, ...]:
    by_id: dict[str, ReviewGeneratedOutput] = {}
    by_role_kind: dict[tuple[str, str], str] = {}
    for index, value in enumerate(_sequence_value(values)):
        item = _generated_output(value, index)
        if item is None:
            continue
        if item.role_id is not None:
            identity = (item.output_kind, item.role_id.casefold())
            previous_id = by_role_kind.get(identity)
            if previous_id is not None and previous_id != item.output_id:
                by_id.pop(previous_id, None)
            by_role_kind[identity] = item.output_id
        # A later revision is the accepted state for the same output ID.
        by_id[item.output_id] = item
    return tuple(by_id.values())


def _role_assignment(value: Any) -> ReviewRoleAssignment | FrozenMap | None:
    if isinstance(value, ReviewRoleAssignment):
        return value
    payload = _model_payload(value)
    if payload is None:
        return None
    if payload.get("output_kind", payload.get("output")) == "palette_assignment":
        metadata = _model_payload(payload.get("metadata", payload.get("value")))
        if metadata is not None:
            merged = dict(metadata)
            for key in ("role_id", "section_id", "assignment_id"):
                if payload.get(key) is not None:
                    merged.setdefault(key, payload[key])
            payload = merged
    target = _model_payload(payload.get("target"))
    target_id = payload.get("target_id")
    target_name = payload.get("target_name")
    if target is not None:
        if target_id is None:
            target_id = target.get("index", target.get("target_id"))
        if target_name is None:
            target_name = target.get("name", target.get("target_name"))
    role_id = payload.get("role_id", payload.get("role"))
    if not isinstance(role_id, str) or not role_id.strip():
        return None
    section_scope = payload.get("section_scope", payload.get("sections", ()))
    if isinstance(section_scope, str):
        section_scope = (section_scope,)
    if not isinstance(section_scope, (list, tuple)):
        section_scope = ()
    known = {
        "assignment_id": payload.get("assignment_id")
        if isinstance(payload.get("assignment_id"), str)
        else None,
        "role_id": role_id,
        "target_id": str(target_id) if target_id is not None else None,
        "target_name": target_name if isinstance(target_name, str) else None,
        "product_id": payload.get("product_id")
        if isinstance(payload.get("product_id"), str)
        else None,
        "product_name": payload.get("product_name")
        if isinstance(payload.get("product_name"), str)
        else None,
        "preset_name": payload.get("preset_name", payload.get("selected_preset"))
        if isinstance(payload.get("preset_name", payload.get("selected_preset")), str)
        else None,
        "section_scope": tuple(str(item) for item in section_scope),
        "assignment_digest": payload.get("assignment_digest")
        if isinstance(payload.get("assignment_digest"), str)
        else None,
    }
    try:
        return ReviewRoleAssignment(
            **known,
            metadata=_frozen_mapping(payload.get("metadata", payload)),
        )
    except (TypeError, ValueError):
        # A source palette record can be a valid, bounded mapping even when it
        # predates the lightweight role-assignment shape.  Preserve it as an
        # immutable receipt rather than dropping the accepted role entirely.
        try:
            return FrozenMap(dict(payload))
        except (TypeError, ValueError):
            return None


def _role_assignment_values(values: Any) -> tuple[ReviewRoleAssignment | FrozenMap, ...]:
    output: dict[object, ReviewRoleAssignment | FrozenMap] = {}
    for value in _sequence_value(values):
        item = _role_assignment(value)
        if item is None:
            continue
        identity: object = (
            item.role_id.casefold()
            if isinstance(item, ReviewRoleAssignment)
            else str(item.get("role_id", "")).casefold()
            or item.get("assignment_id")
            or _canonical_digest(item.to_dict())
        )
        output[identity] = item
    return tuple(output.values())


def _section_values(values: Sequence[ReviewSection | Mapping[str, Any]]) -> tuple[ReviewSection, ...]:
    output: list[ReviewSection] = []
    for value in values:
        output.append(value if isinstance(value, ReviewSection) else ReviewSection.model_validate(value, strict=False))
    return tuple(output)


def _manual_values(values: Sequence[ManualHandoff | Mapping[str, Any]]) -> tuple[ManualHandoff, ...]:
    output: list[ManualHandoff] = []
    for value in values:
        output.append(value if isinstance(value, ManualHandoff) else ManualHandoff.model_validate(value, strict=False))
    return tuple(output)


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _playlist_handoff(value: PlaylistHandoff | PlaylistHandoffManifest | Mapping[str, Any] | None) -> PlaylistHandoff | None:
    if value is None or isinstance(value, PlaylistHandoff):
        return value
    payload = value.model_dump(mode="python") if isinstance(value, PlaylistHandoffManifest) else value
    return PlaylistHandoff.model_validate(payload, strict=False)


def _export_handoff(value: ExportHandoff | Mapping[str, Any] | None) -> ExportHandoff | None:
    if value is None or isinstance(value, ExportHandoff):
        return value
    return ExportHandoff.model_validate(value, strict=False)


def _evaluation_values(values: Any) -> tuple[CreationEvaluationReport, ...]:
    output: list[CreationEvaluationReport] = []
    seen: set[str] = set()
    for value in _sequence_value(values):
        if isinstance(value, CreationEvaluationReport):
            item = value
        else:
            payload = _model_payload(value)
            if payload is None:
                continue
            try:
                item = CreationEvaluationReport.model_validate(payload, strict=False)
            except (TypeError, ValueError):
                continue
        if item.evaluation_id in seen:
            continue
        seen.add(item.evaluation_id)
        output.append(item)
    return tuple(output)


def _comparison_values(values: Any) -> tuple[RevisionComparison, ...]:
    output: list[RevisionComparison] = []
    seen: set[str] = set()
    for value in _sequence_value(values):
        if isinstance(value, RevisionComparison):
            item = value
        else:
            payload = _model_payload(value)
            if payload is None:
                continue
            try:
                item = RevisionComparison.model_validate(payload, strict=False)
            except (TypeError, ValueError):
                continue
        if item.comparison_id in seen:
            continue
        seen.add(item.comparison_id)
        output.append(item)
    return tuple(output)


def _run_details(value: Any) -> FrozenMap | None:
    """Retain bounded run facts while excluding model/provider instructions."""

    payload = _model_payload(value)
    if payload is None:
        return None
    # Delivery details are a receipt, not a second copy of the source request.
    # Select only operationally useful fields so accidental prompt/transcript
    # fields from an integration cannot enter the exported manifest.
    allowed = {
        "run_id",
        "source_run_id",
        "revision_pass_id",
        "continuation_run_id",
        "status",
        "summary",
        "final_summary",
        "source_state_digest",
        "project_state_digest",
        "session_fingerprint",
        "plan_id",
        "plan_digest",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
        "iteration",
        "current_operation_index",
        "total_operations",
        "completed_operations",
        "verified_receipts",
        "unknown_outcomes",
        "after_bounce_state",
        "timing",
        "timing_report",
        "warnings",
        "blockers",
    }
    selected = {str(key): value for key, value in payload.items() if key in allowed}
    return FrozenMap(selected)


def create_delivery_manifest(
    *,
    source_run_id: str | None = None,
    review_session_id: str | None = None,
    final_revision_pass_id: str | None = None,
    final_run_id: str | None = None,
    original_brief: str | None = None,
    completion_target: str | None = None,
    source_run_status: str = "completed",
    source_state_digest: str | None = None,
    source_run_details: Any = None,
    final_run_status: str | None = None,
    final_run_details: Any = None,
    final_revision_pass: Any = None,
    creation_outcome: Any = None,
    technical_outcome: Any = None,
    arrangement_outcome: Any = None,
    processing_outcome: Any = None,
    audible_quality_outcome: Any = None,
    accepted_palette: Mapping[str, Any] | None = None,
    accepted_generated_outputs: Sequence[Any] = (),
    accepted_role_assignments: Sequence[Any] = (),
    accepted_sections: Sequence[ReviewSection | Mapping[str, Any]] = (),
    pattern_placements: Sequence[PlaylistPlacement | Mapping[str, Any]] = (),
    playlist_handoff: PlaylistHandoff | PlaylistHandoffManifest | Mapping[str, Any] | None = None,
    export_handoff: ExportHandoff | Mapping[str, Any] | None = None,
    review_assets: Sequence[ReviewAudioAsset | Mapping[str, Any]] = (),
    evaluations: Sequence[CreationEvaluationReport | Mapping[str, Any]] = (),
    comparisons: Sequence[RevisionComparison | Mapping[str, Any]] = (),
    remaining_manual_actions: Sequence[ManualHandoff | Mapping[str, Any]] = (),
    unresolved_limitations: Sequence[str] = (),
    final_user_approval: str | bool = "not_requested",
    next_action: str | None = None,
    delivery_id: str = "delivery-manifest",
    review_session: Any = None,
) -> DeliveryManifest:
    """Build a read-only final delivery manifest from review artifacts."""

    snapshot = None
    session_warnings: tuple[str, ...] = ()
    session_blockers: tuple[str, ...] = ()
    if review_session is not None:
        review_session_id = review_session_id or _read(review_session, "review_session_id")
        source_run_id = source_run_id or _read(review_session, "source_run_id")
        snapshot = _read(review_session, "source_snapshot") or _read(review_session, "source_run")
        if final_revision_pass is None:
            # The latest pass is the final checkpoint by construction.  Keep
            # this derivation here as well as in the MCP wrapper so direct
            # service callers retain the same restart-safe manifest.
            passes = _sequence_value(_read(review_session, "revision_passes", ()))
            if passes:
                final_revision_pass = passes[-1]
        if original_brief is None:
            original_brief = _read(snapshot, "original_brief")
        if original_brief is None:
            original_brief = _read(_read(review_session, "request"), "brief")
        completion_target = completion_target or _read(snapshot, "completion_target")
        source_run_status = str(
            _read(snapshot, "source_run_status", source_run_status) or source_run_status
        )
        source_state_digest = source_state_digest or _read(snapshot, "source_state_digest")
        source_run_details = source_run_details or _run_details(snapshot)
        session_warnings = tuple(
            str(item)
            for item in _sequence_value(_read(review_session, "warnings", ()))
            if str(item)
        )
        session_blockers = tuple(
            str(item)
            for item in _sequence_value(_read(review_session, "blockers", ()))
            if str(item)
        )
        if creation_outcome is None:
            creation_outcome = _read(review_session, "source_creation_outcome")
            if creation_outcome is None:
                creation_outcome = _read(snapshot, "creation_outcome")
        if not accepted_palette:
            accepted_palette = _read(review_session, "source_sound_palette", {})
            if not accepted_palette:
                accepted_palette = _read(snapshot, "sound_palette", {})
        if not accepted_generated_outputs:
            source_note_sequences = _sequence_value(
                _read(review_session, "source_note_sequences", ())
            )
            if not source_note_sequences:
                source_note_sequences = _sequence_value(
                    _read(snapshot, "generated_note_sequences", ())
                )
            source_processing_receipts = _sequence_value(
                _read(review_session, "source_processing_receipts", ())
            )
            if not source_processing_receipts:
                source_processing_receipts = _sequence_value(
                    _read(snapshot, "processing_receipts", ())
                )
            accepted_generated_outputs = (
                *source_note_sequences,
                *source_processing_receipts,
            )
        if not accepted_role_assignments:
            palette_assignments = _read(accepted_palette, "assignments", ())
            accepted_role_assignments = _sequence_value(palette_assignments)
        if not accepted_sections:
            source_sections = _sequence_value(
                _read(review_session, "source_sections", ())
            )
            if not source_sections:
                source_sections = _sequence_value(
                    _read(snapshot, "sections", ())
                )
            accepted_sections = _sequence_value(
                source_sections
            )
        if not pattern_placements:
            source_pattern_plan = _sequence_value(
                _read(review_session, "source_pattern_plan", ())
            )
            if not source_pattern_plan:
                source_pattern_plan = _sequence_value(
                    _read(snapshot, "pattern_plan", ())
                )
            pattern_placements = _sequence_value(
                source_pattern_plan
            )
        if not review_assets:
            review_assets = _sequence_value(_read(review_session, "assets", ()))
        if not remaining_manual_actions:
            source_manual_handoffs = _sequence_value(
                _read(review_session, "source_manual_handoffs", ())
            )
            if not source_manual_handoffs:
                source_manual_handoffs = _sequence_value(
                    _read(snapshot, "manual_handoffs", ())
                )
            remaining_manual_actions = _sequence_value(
                source_manual_handoffs
            )
        if not evaluations:
            evaluations = _sequence_value(_read(review_session, "evaluations", ()))
        if not comparisons:
            comparisons = _sequence_value(_read(review_session, "comparisons", ()))
    if final_revision_pass is not None:
        final_revision_pass_id = final_revision_pass_id or _read(final_revision_pass, "revision_pass_id")
        source_run_id = source_run_id or _read(final_revision_pass, "source_run_id")
        final_run_id = final_run_id or _read(
            final_revision_pass, "continuation_run_id"
        )
        final_run_status = final_run_status or _read(
            final_revision_pass, "status", "unknown"
        )
        final_run_details = final_run_details or _run_details(final_revision_pass)
        technical_outcome = technical_outcome if technical_outcome is not None else _read(final_revision_pass, "technical_outcome")
        arrangement_outcome = arrangement_outcome if arrangement_outcome is not None else _read(final_revision_pass, "arrangement_outcome")
        processing_outcome = processing_outcome if processing_outcome is not None else _read(final_revision_pass, "processing_outcome")
        audible_quality_outcome = audible_quality_outcome if audible_quality_outcome is not None else _read(final_revision_pass, "audible_quality_outcome")
        accepted_generated_outputs = (
            *accepted_generated_outputs,
            *_sequence_value(_read(final_revision_pass, "generated_outputs", ())),
        )
        accepted_role_assignments = (
            *accepted_role_assignments,
            *tuple(
                value
                for value in _sequence_value(
                    _read(final_revision_pass, "generated_outputs", ())
                )
                if _read(value, "output_kind", _read(value, "output"))
                == "palette_assignment"
            ),
        )
        if not remaining_manual_actions:
            remaining_manual_actions = _read(final_revision_pass, "manual_handoffs", ())
    latest_evaluation = _sequence_value(evaluations)
    if latest_evaluation:
        report = latest_evaluation[-1]
        technical_outcome = (
            technical_outcome
            if technical_outcome is not None
            else _read(report, "technical_audio_state")
        )
        arrangement_outcome = (
            arrangement_outcome
            if arrangement_outcome is not None
            else _read(report, "arrangement_proxy_state")
        )
        processing_outcome = (
            processing_outcome
            if processing_outcome is not None
            else _read(report, "processing_review_state")
        )
        audible_quality_outcome = (
            audible_quality_outcome
            if audible_quality_outcome is not None
            else _read(report, "audible_quality_state")
        )
    if not source_run_id or not review_session_id:
        raise ValueError("source_run_id and review_session_id are required for a delivery manifest")
    if isinstance(final_user_approval, bool):
        final_user_approval = "approved" if final_user_approval else "not_requested"
    if not unresolved_limitations:
        unresolved_limitations = (*session_blockers, *session_warnings)
    resolved_export_handoff = _export_handoff(export_handoff)
    palette_payload = _model_payload(accepted_palette)
    manifest = DeliveryManifest(
        delivery_id=delivery_id,
        source_run_id=source_run_id,
        review_session_id=review_session_id,
        original_brief=original_brief,
        completion_target=completion_target,
        source_run_status=source_run_status,  # type: ignore[arg-type]
        source_state_digest=source_state_digest,
        source_run_details=source_run_details,
        final_revision_pass_id=final_revision_pass_id,
        final_run_id=final_run_id,
        final_run_status=final_run_status,  # type: ignore[arg-type]
        final_run_details=final_run_details,
        final_revision_pass=final_revision_pass,
        creation_outcome=creation_outcome,
        technical_outcome=technical_outcome,
        arrangement_outcome=arrangement_outcome,
        processing_outcome=processing_outcome,
        audible_quality_outcome=audible_quality_outcome,
        accepted_palette=FrozenMap(dict(palette_payload or {})),
        accepted_generated_outputs=_generated_output_values(accepted_generated_outputs),
        accepted_role_assignments=_role_assignment_values(accepted_role_assignments),
        accepted_sections=_section_values(accepted_sections),
        pattern_placements=_placements(pattern_placements),
        playlist_handoff=_playlist_handoff(playlist_handoff),
        export_handoff=resolved_export_handoff,
        review_assets=_asset_values(review_assets),
        evaluations=_evaluation_values(evaluations),
        comparisons=_comparison_values(comparisons),
        remaining_manual_actions=_manual_values(remaining_manual_actions),
        unresolved_limitations=_dedupe(unresolved_limitations),
        final_user_approval=final_user_approval,  # type: ignore[arg-type]
        next_action=next_action or (resolved_export_handoff.next_action if resolved_export_handoff else "Review the manifest and complete any remaining manual handoff actions."),
    )
    safe_payload = sanitize_review_payload(manifest, persist_asset_paths=True)
    if not isinstance(safe_payload, Mapping):
        raise ValueError("delivery manifest could not be converted to safe local metadata")
    return DeliveryManifest.model_validate(safe_payload, strict=False)


build_delivery_manifest = create_delivery_manifest
delivery_manifest = create_delivery_manifest


def resolve_delivery_directory(
    directory: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    user_data_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the fixed local delivery directory without creating it."""

    environment = os.environ if environ is None else environ
    raw = os.fspath(directory).strip() if directory is not None else environment.get(DELIVERY_PATH_ENV, "").strip()
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError(f"{DELIVERY_PATH_ENV} must be an absolute directory")
        return path.resolve()
    root = Path(user_data_dir) if user_data_dir is not None else fl_studio_user_data_dir()
    return (root / "Settings" / "PostFader" / DEFAULT_DELIVERY_DIRECTORY).resolve()


def _markdown(manifest: DeliveryManifest) -> str:
    lines = [
        "# PostFader Delivery Manifest",
        "",
        f"- Delivery ID: `{manifest.delivery_id}`",
        f"- Manifest digest: `{manifest.digest}`",
        f"- Review Session: `{manifest.review_session_id}`",
        f"- Source Production Run: `{manifest.source_run_id}`",
        f"- Source run status: `{manifest.source_run_status}`",
        f"- Final revision pass: `{manifest.final_revision_pass_id or 'none'}`",
        f"- Final run: `{manifest.final_run_id or 'none'}` ({manifest.final_run_status or 'not recorded'})",
        f"- Final user approval: `{manifest.final_user_approval}`",
        "",
        "## Original brief",
        "",
        manifest.original_brief or "Not recorded.",
        "",
        f"Completion target: {manifest.completion_target or 'not recorded'}",
        "",
        "## Outcome dimensions",
        "",
        f"- Technical execution: {manifest.technical_outcome or 'not recorded'}",
        f"- Arrangement and delivery: {manifest.arrangement_outcome or 'not recorded'}",
        f"- Processing: {manifest.processing_outcome or 'not recorded'}",
        f"- Audible quality: {manifest.audible_quality_outcome or 'requires user judgment'}",
        "",
        "## Playlist handoff",
        "",
    ]
    if manifest.playlist_handoff is None or not manifest.playlist_handoff.placements:
        lines.append("No Playlist handoff is required.")
    else:
        lines.append(f"Status: `{manifest.playlist_handoff.status}` (placement is not verified by FL Studio API).")
        lines.append("")
        lines.append("| Pattern | Section | Track | Bars | Action |")
        lines.append("| --- | --- | ---: | --- | --- |")
        for item in manifest.playlist_handoff.placements:
            pattern = item.pattern_name or item.pattern_id or str(item.pattern_number or "pattern")
            action = item.replacement_vs_addition
            lines.append(f"| {pattern} | {item.section_id or ''} | {item.intended_playlist_track_number or ''} | {item.start_bar}–{item.end_bar} | {action} |")
    lines.extend(["", "## Export handoff", ""])
    if manifest.export_handoff is None:
        lines.append("No export handoff was recorded.")
    else:
        export = manifest.export_handoff
        lines.extend(
            [
                f"- Full mix: `{export.recommended_filename}`",
                f"- Before/after naming: `{export.before_after_naming_convention}`",
                f"- Requested stems: {', '.join(export.requested_stems) if export.requested_stems else 'none'}",
                f"- Normalization off: `{export.normalization_off}`; matching settings required: `{export.matching_settings_required}`",
                f"- Next action: {export.next_action}",
            ]
        )
    lines.extend(["", "## Accepted generated outputs", ""])
    if manifest.accepted_generated_outputs:
        for output in manifest.accepted_generated_outputs:
            role = f"; role `{output.role_id}`" if output.role_id else ""
            section = f"; section `{output.section_id}`" if output.section_id else ""
            lines.append(f"- `{output.output_id}` ({output.output_kind}{role}{section})")
    else:
        lines.append("None recorded.")
    lines.extend(["", "## Accepted role assignments", ""])
    if manifest.accepted_role_assignments:
        for assignment in manifest.accepted_role_assignments:
            if isinstance(assignment, ReviewRoleAssignment):
                detail = assignment.product_name or assignment.preset_name or assignment.target_name or "assignment"
                lines.append(f"- `{assignment.role_id}`: {detail}")
            else:
                lines.append(f"- `{assignment.get('role_id', 'role')}`")
    else:
        lines.append("None recorded.")
    lines.extend(["", "## Evaluation reports", ""])
    if manifest.evaluations:
        for report in manifest.evaluations:
            lines.append(f"- `{report.evaluation_id}`: `{report.status}` ({report.analyzer_version})")
    else:
        lines.append("None recorded.")
    lines.extend(["", "## Revision comparisons", ""])
    if manifest.comparisons:
        for comparison in manifest.comparisons:
            lines.append(f"- `{comparison.comparison_id}`: `{comparison.technical_conclusion}`; approval `{comparison.user_approval_state}`")
    else:
        lines.append("None recorded.")
    lines.extend(["", "## Remaining limitations", ""])
    if manifest.unresolved_limitations:
        lines.extend(f"- {item}" for item in manifest.unresolved_limitations)
    else:
        lines.append("None recorded.")
    lines.extend(["", "## Next action", "", manifest.next_action, ""])
    return "\n".join(lines)


render_delivery_manifest = _markdown


@dataclass(frozen=True)
class DeliveryManifestWriteResult:
    """Paths and digest returned after a create-only manifest write."""

    digest: str
    manifest_digest: str
    json_path: Path | None = None
    markdown_path: Path | None = None
    json_sha256: str | None = None
    markdown_sha256: str | None = None
    json_created: bool = False
    markdown_created: bool = False

    @property
    def path(self) -> Path | None:
        return self.json_path or self.markdown_path


def _write_create_only(path: Path, content: str, *, digest: str) -> bool:
    encoded = content.encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            existing = path.read_text(encoding="utf-8")
            if existing != content:
                raise FileExistsError(f"delivery manifest already exists at {path}")
        except UnicodeDecodeError as exc:
            raise FileExistsError(f"delivery manifest already exists at {path}") from exc
        return False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return True


def _preflight_create_only(path: Path, content: str) -> None:
    """Reject a conflicting existing target before any companion file is written."""

    if not path.exists():
        return
    try:
        existing = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FileExistsError(f"delivery manifest already exists at {path}") from exc
    if existing != content:
        raise FileExistsError(f"delivery manifest already exists at {path}")


def write_delivery_manifest(
    manifest: DeliveryManifest,
    directory: str | os.PathLike[str] | None = None,
    *,
    write_json: bool = True,
    write_markdown: bool = True,
    environ: Mapping[str, str] | None = None,
    user_data_dir: str | os.PathLike[str] | None = None,
    overwrite: bool = False,
) -> DeliveryManifestWriteResult:
    """Persist JSON/Markdown manifests create-only under PostFader delivery."""

    if not isinstance(manifest, DeliveryManifest):
        raise TypeError("write_delivery_manifest expects a canonical DeliveryManifest")
    if overwrite:
        raise ValueError("delivery manifests are create-only; overwrite is not supported")
    if not write_json and not write_markdown:
        raise ValueError("at least one manifest format must be requested")
    safe_payload = sanitize_review_payload(manifest, persist_asset_paths=True)
    if not isinstance(safe_payload, Mapping):
        raise ValueError("delivery manifest could not be converted to safe local metadata")
    safe_manifest = DeliveryManifest.model_validate(safe_payload, strict=False)
    root = resolve_delivery_directory(directory, environ=environ, user_data_dir=user_data_dir)
    json_path: Path | None = None
    markdown_path: Path | None = None
    stem = safe_manifest.delivery_id
    targets: list[tuple[Path, str]] = []
    if write_json:
        json_path = root / f"{stem}.json"
        payload = json.dumps(safe_manifest.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=True) + "\n"
        targets.append((json_path, payload))
    if write_markdown:
        markdown_path = root / f"{stem}.md"
        targets.append((markdown_path, _markdown(safe_manifest)))
    created: list[Path] = []
    with _DELIVERY_WRITE_LOCK:
        root.mkdir(parents=True, exist_ok=True)
        for path, content in targets:
            _preflight_create_only(path, content)
        try:
            for path, content in targets:
                if _write_create_only(path, content, digest=safe_manifest.digest):
                    created.append(path)
        except Exception:
            # A companion-format failure must not leave a partial new export.
            # Identical files that predated this call are never removed.
            for created_path in reversed(created):
                try:
                    created_path.unlink()
                except OSError:
                    pass
            raise
    hashes = {
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in targets
    }
    return DeliveryManifestWriteResult(
        digest=safe_manifest.digest,
        manifest_digest=safe_manifest.digest,
        json_path=json_path,
        markdown_path=markdown_path,
        json_sha256=None if json_path is None else hashes[json_path],
        markdown_sha256=None if markdown_path is None else hashes[markdown_path],
        json_created=json_path in created if json_path is not None else False,
        markdown_created=(
            markdown_path in created if markdown_path is not None else False
        ),
    )


persist_delivery_manifest = write_delivery_manifest
export_delivery_manifest = write_delivery_manifest


__all__ = [
    "DEFAULT_DELIVERY_DIRECTORY",
    "DELIVERY_DIRECTORY_NAME",
    "DELIVERY_PATH_ENV",
    "DeliveryManifestWriteResult",
    "build_delivery_manifest",
    "build_export_handoff",
    "build_playlist_handoff",
    "confirm_playlist_handoff",
    "create_delivery_manifest",
    "create_export_handoff",
    "create_playlist_handoff",
    "create_playlist_handoff_delta",
    "create_playlist_handoff_manifest",
    "delivery_manifest",
    "export_delivery_manifest",
    "export_handoff_for_findings",
    "persist_delivery_manifest",
    "playlist_handoff_delta",
    "resolve_delivery_directory",
    "render_delivery_manifest",
    "write_delivery_manifest",
]
