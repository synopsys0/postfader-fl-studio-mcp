"""Process-local Review Session lifecycle and bounded state updates.

The registry owns review state only.  It accepts already typed source-run
snapshots and user-supplied asset metadata, then returns a new immutable
``ReviewSession`` for every update.  FL Studio execution and audio analysis
remain in their respective services; this module never performs a project
mutation.
"""

from __future__ import annotations

import hashlib
import math
import os
import secrets
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ValidationError

from .models import (
    MAX_REVIEW_ASSETS,
    MAX_REVIEW_ASSET_BYTES,
    MAX_REVIEW_AUDIO_SECONDS,
    MAX_REVIEW_EVALUATIONS,
    MAX_REVIEW_FEEDBACK,
    MAX_REVIEW_MANIFESTS,
    MAX_REVIEW_OPERATIONS,
    MAX_REVIEW_PASSES,
    MAX_REVIEW_SECTIONS,
    MAX_REVIEW_SESSIONS,
    CreationEvaluationReport,
    CreationFeedback,
    DeliveryManifest,
    FrozenMap,
    ManualHandoff,
    PlaylistPlacement,
    ReviewAssetSet,
    ReviewAudioAsset,
    ReviewGeneratedOutput,
    ReviewSection,
    ReviewSectionMap,
    ReviewSession,
    ReviewSessionRequest,
    ReviewSessionStatus,
    ReviewSourceSnapshot,
    RevisionComparison,
    RevisionPass,
    RevisionPlan,
    TempoChange,
    _canonical_digest,
)
from .persistence import LocalReviewSessionStore
from .sections import (
    _find_source_sections,
    bar_duration_seconds,
    bars_to_seconds,
)
from .sections import (
    _mapping as _section_mapping,
)
from .sections import (
    _normalise_tempo_map as _normalise_source_tempo_map,
)
from .sections import (
    _source_transport as _section_source_transport,
)


SUPPORTED_AUDIO_SUFFIXES = frozenset({".wav", ".wave", ".aif", ".aiff", ".flac", ".ogg", ".oga", ".mp3"})
MAX_AUDIO_READ_CHUNK = 1024 * 1024


class ReviewSessionError(RuntimeError):
    """Base class for process-local Review Session errors."""


class UnknownReviewSessionError(ReviewSessionError):
    """The requested Review Session is not in memory or local persistence."""


class UnknownSourceRunError(ReviewSessionError):
    """The requested source run was missing or was not completed."""


class InvalidReviewSessionTransition(ReviewSessionError):
    """A lifecycle transition would skip or revisit an unsupported state."""


class ReviewSessionLimitError(ReviewSessionError):
    """A bounded session collection cannot accept another record."""


class ReviewAudioAssetError(ValueError):
    """A caller-selected audio file failed explicit validation."""


SourceLookup = Callable[[str], object | None]


_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"awaiting_assets", "evaluating", "accepted", "rejected", "blocked", "stopped"}),
    "awaiting_assets": frozenset({"evaluating", "accepted", "rejected", "blocked", "stopped"}),
    "evaluating": frozenset({"evaluated", "blocked", "stopped"}),
    "evaluated": frozenset(
        {"revision_planned", "revising", "comparing", "accepted", "rejected", "completed", "blocked", "stopped"}
    ),
    "revision_planned": frozenset({"revising", "blocked", "stopped"}),
    "revising": frozenset({"awaiting_rebounce", "evaluated", "blocked", "stopped"}),
    "awaiting_rebounce": frozenset({"evaluating", "comparing", "accepted", "rejected", "completed", "blocked", "stopped"}),
    "comparing": frozenset({"evaluated", "revision_planned", "accepted", "rejected", "completed", "blocked", "stopped"}),
    "accepted": frozenset({"completed", "stopped"}),
    "rejected": frozenset({"revision_planned", "revising", "completed", "stopped"}),
    "completed": frozenset(),
    "blocked": frozenset({"awaiting_assets", "evaluating", "revision_planned", "stopped"}),
    "stopped": frozenset(),
}


def _validate_status_transition(current: str, target: str) -> None:
    """Validate one lifecycle edge shared by every registry update path."""

    if target not in _ALLOWED_TRANSITIONS:
        raise InvalidReviewSessionTransition(
            f"unknown Review Session status: {target}"
        )
    if current not in _ALLOWED_TRANSITIONS:
        raise InvalidReviewSessionTransition(
            f"unknown current Review Session status: {current}"
        )
    if target != current and target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidReviewSessionTransition(
            f"cannot transition Review Session from {current!r} to {target!r}"
        )


def _initial_feedback_state(
    feedback: tuple[CreationFeedback, ...],
) -> tuple[ReviewSessionStatus, str | None]:
    """Derive truthful opening state only from explicit producer decisions."""

    status: ReviewSessionStatus = "created"
    next_action: str | None = None
    for item in feedback:
        if item.source != "user_explicit":
            # A metric or connected-AI interpretation can inform a review,
            # but it must never grant artistic approval while opening it.
            continue
        if item.overall_verdict in {
            "approved",
            "accepted",
            "user_approved",
        } or item.approval_level in {"final", "approved"}:
            status = "accepted"
            next_action = "Review Session accepted; no automatic revision is pending."
        elif item.overall_verdict in {"rejected", "user_rejected"} or item.approval_level == "rejected":
            status = "rejected"
            next_action = "Provide an explicit revision request or finalize the session."
        elif item.overall_verdict in {"needs_revision", "user_confirmed_draft"} or item.approval_level == "draft":
            # A revision request or draft confirmation is not itself a plan
            # and therefore leaves the new session in its created state.
            next_action = (
                "Create one bounded revision plan from the explicit producer feedback."
                if item.overall_verdict == "needs_revision"
                else "The draft is producer-confirmed; await an explicit approval or revision request."
            )
    return status, next_action


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


def _read_value(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _source_state(source: object) -> object:
    state = _read_value(source, "state")
    return state if state is not None else source


def _source_status(source: object) -> str | None:
    state = _source_state(source)
    status = _read_value(state, "status")
    if isinstance(status, str):
        return status
    return None


def _source_model_dump(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _preserve_from_source(value: object) -> object:
    if value is None:
        return {}
    fields: dict[str, object] = {
        "tempo": bool(_read_value(value, "tempo", False)),
        "note_content": bool(_read_value(value, "note_content", False)),
        "arrangement": bool(_read_value(value, "arrangement", False)),
        "mixer_state": bool(_read_value(value, "mixer_state", False)),
        "pattern_identity": bool(_read_value(value, "pattern_identity", False)),
        "sound_palette": bool(_read_value(value, "sound_palette", False)),
    }
    for name in ("sound_roles", "named_elements"):
        rows = _read_value(value, name, ())
        fields[name] = tuple(str(item) for item in rows) if isinstance(rows, (list, tuple)) else ()
    targets = _read_value(value, "targets", ())
    fields["targets"] = tuple(
        str(_read_value(item, "index", _read_value(item, "name", item)))
        for item in targets
    ) if isinstance(targets, (list, tuple)) else ()
    return fields


def _generated_outputs(
    state: object,
    output_kind: Literal["note_sequence", "processing_plan"],
    *,
    operations: Sequence[object] = (),
) -> tuple[ReviewGeneratedOutput, ...]:
    outputs = _read_value(state, "generated_outputs", ())
    if not isinstance(outputs, (list, tuple)):
        return ()
    operation_by_id = {
        str(operation_id): operation
        for operation in operations
        if (operation_id := _read_value(operation, "operation_id")) is not None
    }
    result: list[ReviewGeneratedOutput] = []
    for index, output in enumerate(outputs):
        kind = _read_value(output, "output", _read_value(output, "output_kind"))
        if output_kind == "note_sequence" and kind != "note_sequence":
            continue
        if output_kind == "processing_plan" and kind != "processing_plan":
            continue
        operation_id = _read_value(output, "operation_id", f"output-{index + 1}")
        value = _read_value(output, "value")
        metadata = _source_model_dump(value)
        source_operation = operation_by_id.get(str(operation_id))
        operation_parameters = _read_value(source_operation, "parameters")
        section_id = next(
            (
                candidate
                for candidate in (
                    _read_value(output, "section_id"),
                    _read_value(value, "section_id"),
                    _read_value(metadata, "section_id"),
                    _read_value(source_operation, "section_id"),
                    _read_value(operation_parameters, "section_id"),
                )
                if isinstance(candidate, str) and candidate.strip()
            ),
            None,
        )
        digest = _read_value(value, "digest")
        if not isinstance(digest, str) or len(digest) != 64:
            digest = _canonical_digest(metadata) if metadata else None
        result.append(
            ReviewGeneratedOutput(
                output_id=str(operation_id),
                output_kind=cast(
                    Literal["note_sequence", "palette_assignment", "processing_plan", "pattern", "handoff"],
                    output_kind,
                ),
                role_id=_str_or_none(_read_value(output, "role_id")),
                section_id=section_id,
                digest=digest,
                metadata=FrozenMap(metadata if isinstance(metadata, Mapping) else {}),
            )
        )
    return tuple(result[:MAX_REVIEW_OPERATIONS])


def _source_plan(source: object) -> object:
    state = _source_state(source)
    return _read_value(source, "plan", _read_value(state, "plan", None))


def _plan_operations(source: object) -> tuple[object, ...]:
    plan = _source_plan(source)
    rows = _read_value(plan, "operations", ())
    if not isinstance(rows, (list, tuple)):
        return ()
    return tuple(rows)


def _source_transport(
    source: object,
) -> tuple[float, int, int, Sequence[Any] | Mapping[Any, Any] | None]:
    """Read the best available source transport and supported tempo map.

    The authoritative path is ``run_context.project_checkpoint.transport``;
    the section helper also accepts the older direct state/request/operation
    shapes so snapshots made by an earlier Production Run remain readable.
    """

    details = _section_source_transport(source)
    return (
        details.tempo_bpm or 120.0,
        details.time_signature_numerator or 4,
        details.time_signature_denominator or 4,
        details.tempo_map,
    )


def _marker_sections(source: object) -> tuple[ReviewSection, ...]:
    """Derive conservative section intervals from an explicit marker plan."""

    markers: list[tuple[str, int, float]] = []
    for operation in _plan_operations(source):
        if _read_value(operation, "operation") != "add_section_markers":
            continue
        values = _read_value(operation, "markers", ())
        if not isinstance(values, (list, tuple)):
            continue
        for marker in values:
            name = _read_value(marker, "name")
            bar = _read_value(marker, "bar_number")
            beat = _read_value(marker, "beat_offset", 0.0)
            if not isinstance(name, str) or not name.strip() or type(bar) is not int or bar < 1:
                continue
            if not isinstance(beat, (int, float)) or isinstance(beat, bool):
                beat = 0.0
            markers.append((name.strip(), bar, float(beat)))
    if not markers:
        return ()
    markers.sort(key=lambda item: (item[1], item[2], item[0].casefold()))
    # Marker names ending in _end are boundaries, not another section.  Each
    # remaining marker extends to the next explicit marker (or four bars when
    # it is the final marker because the source does not expose project end).
    tempo, numerator, denominator, tempo_map = _source_transport(source)
    rows: list[ReviewSection] = []
    for index, (name, start_bar, beat) in enumerate(markers):
        lowered = name.casefold()
        if lowered.endswith("_end") or lowered.endswith(" end"):
            continue
        next_marker = next(
            (candidate for candidate in markers[index + 1 :] if candidate[1] > start_bar),
            None,
        )
        next_bar = next_marker[1] if next_marker is not None else start_bar + 4
        next_beat = next_marker[2] if next_marker is not None else 0.0
        if next_bar <= start_bar:
            continue
        section_id = "section-" + "".join(
            character if character.isalnum() or character in "_.:-" else "-"
            for character in name
        ).strip("-")
        section_id = section_id or f"section-{len(rows) + 1}"
        try:
            # Marker beat offsets are expressed in denominator beats.  Convert
            # them to fractional bar positions before integration so a tempo
            # change at (or before) the marker is applied to the within-bar
            # interval as well as to the completed bars before it.
            start_seconds = bars_to_seconds(
                float(start_bar) + beat / float(numerator),
                tempo_bpm=tempo,
                numerator=numerator,
                denominator=denominator,
                tempo_map=tempo_map,
            )
            end_seconds = bars_to_seconds(
                float(next_bar) + next_beat / float(numerator),
                tempo_bpm=tempo,
                numerator=numerator,
                denominator=denominator,
                tempo_map=tempo_map,
            )
        except (TypeError, ValueError):
            continue
        try:
            rows.append(
                ReviewSection(
                    section_id=section_id,
                    name=name,
                    start_bar=start_bar,
                    end_bar=next_bar,
                    start_seconds=start_seconds,
                    end_seconds=max(start_seconds + 0.001, end_seconds),
                    source="section_marker",
                    confidence=0.95,
                )
            )
        except ValueError:
            continue
        if len(rows) >= MAX_REVIEW_SECTIONS:
            break
    return tuple(rows)


def _source_sections(state: object, source: object) -> tuple[ReviewSection, ...]:
    """Copy explicit section records when a source wrapper exposes them."""

    raw = _read_value(state, "sections", _read_value(source, "sections"))
    if raw is None:
        raw, _ = _find_source_sections(source)
    if raw is None:
        raw = ()
    if isinstance(raw, Mapping):
        if any(key in raw for key in ("start_bar", "start_seconds", "start_time")):
            raw = (raw,)
        else:
            entries: list[object] = []
            for key, item in raw.items():
                if isinstance(item, Mapping):
                    item = dict(item)
                    item.setdefault("section_id", key)
                entries.append(item)
            raw = tuple(entries)
    if not isinstance(raw, (list, tuple)):
        return _marker_sections(source)
    tempo, numerator, denominator, tempo_map = _source_transport(source)
    try:
        bar_length = bar_duration_seconds(tempo, numerator, denominator)
    except ValueError:
        return _marker_sections(source)
    result: list[ReviewSection] = []
    for index, item in enumerate(raw[:MAX_REVIEW_SECTIONS]):
        if isinstance(item, ReviewSection):
            result.append(item)
            continue
        data = dict(_section_mapping(item))
        if not data:
            continue
        section_id = data.get("section_id", data.get("id", f"section-{index + 1}"))
        data.setdefault("section_id", section_id)
        data.setdefault("name", data.get("label", section_id))
        start_bar = data.get("start_bar", data.get("bar_start"))
        end_bar = data.get("end_bar", data.get("bar_end"))
        start_seconds = data.get("start_seconds", data.get("start_time"))
        end_seconds = data.get("end_seconds", data.get("end_time"))
        try:
            if start_seconds is None and start_bar is not None:
                start_seconds = bars_to_seconds(
                    float(start_bar),
                    tempo_bpm=tempo,
                    numerator=numerator,
                    denominator=denominator,
                    tempo_map=tempo_map,
                )
            if end_seconds is None and end_bar is not None:
                end_seconds = bars_to_seconds(
                    float(end_bar),
                    tempo_bpm=tempo,
                    numerator=numerator,
                    denominator=denominator,
                    tempo_map=tempo_map,
                )
            if start_seconds is None or end_seconds is None:
                # A section with only one boundary cannot be made truthful;
                # in particular, never turn it into the old 0..1s placeholder.
                continue
            start_value = float(start_seconds)
            end_value = float(end_seconds)
            if not math.isfinite(start_value) or not math.isfinite(end_value):
                continue
            if end_value <= start_value:
                continue
            if start_bar is None:
                start_bar = max(1, int(math.floor(max(0.0, start_value) / bar_length)) + 1)
            if end_bar is None:
                end_bar = max(int(start_bar), int(math.ceil(max(0.0, end_value) / bar_length)))
        except (TypeError, ValueError):
            continue
        data.update(
            {
                "start_bar": start_bar,
                "end_bar": end_bar,
                "start_seconds": start_value,
                "end_seconds": end_value,
                "source": data.get("source", "production_run"),
            }
        )
        # Source wrappers often carry transport and planner-only keys beside
        # the range.  Keep only the ReviewSection contract fields so one bad
        # extra key does not discard an otherwise authoritative boundary.
        data = {
            key: value
            for key, value in data.items()
            if key
            in {
                "section_id",
                "name",
                "start_bar",
                "end_bar",
                "start_seconds",
                "end_seconds",
                "source",
                "confidence",
                "pattern_ids",
                "palette_roles",
                "expected_function",
                "energy_intent",
            }
        }
        try:
            result.append(ReviewSection.model_validate(data, strict=False))
        except ValidationError:
            continue
    return tuple(result) if result else _marker_sections(source)


def _source_pattern_plan(state: object, source: object) -> tuple[PlaylistPlacement, ...]:
    """Retain explicit placement intent exposed by a source wrapper."""

    raw = _read_value(state, "pattern_plan", _read_value(source, "pattern_plan", ()))
    if isinstance(raw, Mapping):
        raw = tuple(raw.values())
    if not isinstance(raw, (list, tuple)):
        raw = ()
    result: list[PlaylistPlacement] = []
    for index, item in enumerate(raw[:MAX_REVIEW_SECTIONS]):
        if isinstance(item, PlaylistPlacement):
            result.append(item)
            continue
        if not isinstance(item, Mapping):
            continue
        data = dict(item)
        data.setdefault("pattern_id", f"pattern-{index + 1}")
        data.setdefault("playlist_track", 1)
        data.setdefault("start_bar", 1)
        data.setdefault("end_bar", data["start_bar"])
        try:
            result.append(PlaylistPlacement.model_validate(data, strict=False))
        except ValidationError:
            continue
    if result:
        return tuple(result)
    # A Production Run plan has explicit pattern preparation/write intent even
    # though it cannot contain Playlist CRUD.  Retain that intent as manual
    # placement rows rather than asking a later reviewer to reconstruct it.
    for operation in _plan_operations(source):
        name = _read_value(operation, "operation")
        if name not in {"prepare_pattern", "write_note_sequence"}:
            continue
        pattern_number = _read_value(operation, "pattern_number")
        if type(pattern_number) is not int or pattern_number < 1:
            continue
        length_beats = _read_value(operation, "length_beats", 16)
        bars = max(1, int((length_beats + 3) // 4)) if type(length_beats) is int else 4
        try:
            result.append(
                PlaylistPlacement(
                    pattern_number=pattern_number,
                    pattern_name=_str_or_none(_read_value(operation, "name")),
                    source_operation_id=_str_or_none(_read_value(operation, "operation_id")),
                    section_id=_str_or_none(_read_value(operation, "section_id")),
                    start_bar=1,
                    end_bar=bars,
                    length_bars=bars,
                )
            )
        except (TypeError, ValueError):
            continue
        if len(result) >= MAX_REVIEW_SECTIONS:
            break
    return tuple(result)


def _processing_receipts(state: object) -> tuple[ReviewGeneratedOutput, ...]:
    """Retain processing receipts without copying mutable Production state."""

    rows = _read_value(state, "receipts", ())
    if not isinstance(rows, (list, tuple)):
        return ()
    result: list[ReviewGeneratedOutput] = []
    for index, receipt in enumerate(rows[:MAX_REVIEW_OPERATIONS]):
        operation = str(_read_value(receipt, "operation", ""))
        if operation not in {
            "plan_processing",
            "apply_processing_plan",
            "apply_semantic_plugin_action",
        }:
            continue
        payload = _source_model_dump(_read_value(receipt, "result"))
        if not isinstance(payload, Mapping):
            payload = {}
        result.append(
            ReviewGeneratedOutput(
                output_id=str(_read_value(receipt, "operation_id", f"processing-{index + 1}")),
                output_kind="processing_plan",
                digest=_canonical_digest(payload) if payload else None,
                metadata=FrozenMap(payload),
            )
        )
    return tuple(result)


def _source_manual_handoffs(state: object) -> tuple[ManualHandoff, ...]:
    """Copy known external actions from outcome/readiness snapshots."""

    values: list[object] = []
    outcome = _read_value(state, "creation_outcome")
    for parent_name in (
        "manual_handoff",
        "manual_handoff_outcome",
        "arrangement_delivery",
        "arrangement_delivery_outcome",
    ):
        parent = _read_value(outcome, parent_name)
        actions = _read_value(parent, "actions", _read_value(parent, "manual_playlist_actions", ()))
        if isinstance(actions, (list, tuple)):
            values.extend(actions)
    readiness = _read_value(state, "readiness_report")
    actions = _read_value(readiness, "manual_actions", ())
    if isinstance(actions, (list, tuple)):
        values.extend(actions)
    result: list[ManualHandoff] = []
    for index, item in enumerate(values[:MAX_REVIEW_SECTIONS]):
        if isinstance(item, ManualHandoff):
            result.append(item)
            continue
        action_id = _read_value(item, "action_id", f"handoff-{index + 1}")
        instruction = _read_value(item, "instruction")
        if not isinstance(action_id, str) or not isinstance(instruction, str) or not instruction.strip():
            continue
        status = _read_value(item, "status", "required")
        status_value = {
            "outstanding": "required",
            "optional": "pending",
            "complete": "completed",
        }.get(str(status), str(status))
        if status_value not in {"required", "pending", "confirmed", "not_verifiable", "completed"}:
            status_value = "required"
        section_id = _read_value(item, "section_id")
        pattern_id = _read_value(item, "pattern_id")
        try:
            result.append(
                ManualHandoff(
                    action_id=action_id,
                    instruction=instruction,
                    status=cast(
                        Literal["required", "pending", "confirmed", "not_verifiable", "completed"],
                        status_value,
                    ),
                    section_id=section_id if isinstance(section_id, str) else None,
                    pattern_id=pattern_id if isinstance(pattern_id, str) else None,
                )
            )
        except ValueError:
            continue
    return tuple(result)


def snapshot_source_run(source_run_id: str, source: object) -> ReviewSourceSnapshot:
    """Copy the bounded source-run facts needed for review continuity."""

    state = _source_state(source)
    request = _read_value(state, "request")
    brief = _read_value(request, "brief", _read_value(source, "brief", f"Source run {source_run_id}"))
    completion_target = _read_value(request, "completion_target", "playable_draft")
    if not isinstance(brief, str) or not brief.strip():
        brief = f"Source run {source_run_id}"
    if not isinstance(completion_target, str) or not completion_target.strip():
        completion_target = "playable_draft"
    source_digest = _read_value(state, "project_state_digest")
    if not isinstance(source_digest, str) or len(source_digest) != 64:
        source_digest = None
    session_fingerprint = _read_value(state, "session_fingerprint")
    if (
        not isinstance(session_fingerprint, str)
        or len(session_fingerprint) != 32
        or any(character not in "0123456789abcdef" for character in session_fingerprint)
    ):
        session_fingerprint = None
    outcome = _read_value(state, "creation_outcome")
    outcome_payload: object = outcome
    if outcome is not None and not isinstance(outcome, BaseModel):
        outcome_payload = _source_model_dump(outcome)
    receipts = _read_value(state, "receipts", ())
    receipt_digests: list[str] = []
    if isinstance(receipts, (list, tuple)):
        for item in receipts[:MAX_REVIEW_OPERATIONS]:
            receipt_digests.append(_canonical_digest(_source_model_dump(item)))
    palette: object = {}
    palette_rows: list[object] = []
    outputs = _read_value(state, "generated_outputs", ())
    if isinstance(outputs, (list, tuple)):
        for output in outputs:
            kind = _read_value(output, "output", "")
            payload = _source_model_dump(_read_value(output, "value"))
            if kind == "sound_palette" and isinstance(payload, Mapping):
                # Retain the complete typed palette when the source run stored
                # one.  Revision planning needs its palette ID, assignments,
                # anchors, and digests rather than a prose reconstruction.
                palette = payload
            elif kind == "palette_assignment" and isinstance(payload, Mapping):
                palette_rows.append(payload)
    if not palette and palette_rows:
        palette = {"assignments": tuple(palette_rows)}
    transport = _section_source_transport(source)
    snapshot_tempo = transport.tempo_bpm
    snapshot_numerator = transport.time_signature_numerator
    snapshot_denominator = transport.time_signature_denominator
    snapshot_denominator_model = cast(
        Literal[1, 2, 4, 8, 16, 32] | None,
        snapshot_denominator,
    )
    snapshot_tempo_changes: tuple[TempoChange, ...] = ()
    if transport.tempo_map is not None:
        try:
            normalized_tempo_map = _normalise_source_tempo_map(
                transport.tempo_map,
                snapshot_tempo or 120.0,
            )
            snapshot_tempo_changes = tuple(
                TempoChange(start_bar=start_bar, tempo_bpm=bpm)
                for start_bar, bpm in normalized_tempo_map
            )
        except (TypeError, ValueError):
            # The source state remains usable for global review even when an
            # older wrapper emitted an unsupported tempo-map shape.  Do not
            # invent a replacement map or section timestamps here.
            snapshot_tempo_changes = ()
    from .models import ReviewPreserveRules

    plan_operations = _plan_operations(source)
    return ReviewSourceSnapshot(
        source_run_id=source_run_id,
        source_state_digest=source_digest,
        session_fingerprint=session_fingerprint,
        tempo_bpm=snapshot_tempo,
        time_signature_numerator=snapshot_numerator,
        time_signature_denominator=snapshot_denominator_model,
        tempo_changes=snapshot_tempo_changes,
        original_brief=str(brief)[:4096],
        completion_target=str(completion_target)[:512],
        preserve=ReviewPreserveRules.model_validate(_preserve_from_source(_read_value(request, "preserve"))),
        sound_palette=FrozenMap(palette if isinstance(palette, Mapping) else {}),
        generated_note_sequences=_generated_outputs(
            state,
            "note_sequence",
            operations=plan_operations,
        ),
        processing_receipts=(
            _generated_outputs(
                state,
                "processing_plan",
                operations=plan_operations,
            )
            or _processing_receipts(state)
        ),
        sections=_source_sections(state, source),
        pattern_plan=_source_pattern_plan(state, source),
        manual_handoffs=_source_manual_handoffs(state),
        creation_outcome=cast(Any, outcome_payload),
        source_receipt_digests=tuple(receipt_digests),
    )


def validate_audio_asset(
    asset: ReviewAudioAsset,
    *,
    max_bytes: int = MAX_REVIEW_ASSET_BYTES,
    max_duration_seconds: int = MAX_REVIEW_AUDIO_SECONDS,
) -> ReviewAudioAsset:
    """Validate one explicit caller-selected file and return refreshed metadata."""

    if not isinstance(asset, ReviewAudioAsset):
        raise TypeError("validate_audio_asset expects a ReviewAudioAsset")
    if asset.path is None:
        if asset.validation_state == "valid" and asset.sha256:
            return asset
        raise ReviewAudioAssetError("audio asset path is required for validation")

    # ``creation_review.assets`` owns the decoded-container/header boundary.
    # Delegate to it when available so a registry attachment cannot bypass
    # format, frame-count, and bounded-path checks by using this lifecycle
    # helper directly.  The small fallback below keeps the session contract
    # usable in installations that import the registry during an upgrade.
    try:
        from .assets import validate_audio_asset as validate_bound_audio_asset
    except (ImportError, AttributeError):
        validate_bound_audio_asset = None
    if validate_bound_audio_asset is not None:
        try:
            checked = validate_bound_audio_asset(
                asset.path,
                asset_kind=asset.asset_kind,
                display_label=asset.display_label,
                role_id=asset.role_id,
                section_id=asset.section_id,
                source_run_id=asset.source_run_id,
                revision_pass_id=asset.revision_pass_id,
                expected_start_seconds=asset.expected_start_seconds,
                declared_offset_seconds=asset.declared_offset_seconds,
                max_file_bytes=max_bytes,
                persist_asset_path=True,
                asset_id=asset.asset_id,
            )
        except Exception as exc:
            raise ReviewAudioAssetError(str(exc)) from exc
        if not isinstance(checked, ReviewAudioAsset):
            try:
                checked = ReviewAudioAsset.model_validate(checked, strict=False)
            except (TypeError, ValueError) as exc:
                raise ReviewAudioAssetError("audio validator returned invalid metadata") from exc
        if asset.sha256 is not None and checked.sha256 != asset.sha256:
            raise ReviewAudioAssetError("audio asset SHA-256 does not match supplied metadata")
        if asset.file_size_bytes is not None and checked.file_size_bytes != asset.file_size_bytes:
            raise ReviewAudioAssetError("audio asset size does not match supplied metadata")
        if asset.sample_rate_hz is not None and checked.sample_rate_hz != asset.sample_rate_hz:
            raise ReviewAudioAssetError("audio asset sample rate does not match supplied metadata")
        if asset.channels is not None and checked.channels != asset.channels:
            raise ReviewAudioAssetError("audio asset channel count does not match supplied metadata")
        if (
            asset.duration_seconds is not None
            and checked.duration_seconds is not None
            and abs(asset.duration_seconds - checked.duration_seconds) > 1e-5
        ):
            raise ReviewAudioAssetError("audio asset duration does not match supplied metadata")
        if checked.duration_seconds is not None and checked.duration_seconds > max_duration_seconds:
            raise ReviewAudioAssetError("audio asset duration exceeds the configured bound")
        return checked
    path = Path(asset.path)
    if not path.is_absolute():
        raise ReviewAudioAssetError("audio asset path must be absolute")
    if path.is_symlink():
        raise ReviewAudioAssetError("audio asset symlinks are not accepted")
    if not path.exists():
        raise ReviewAudioAssetError("audio asset file does not exist")
    if not path.is_file():
        raise ReviewAudioAssetError("audio asset path is a directory or non-file")
    suffix = path.suffix.casefold()
    if suffix not in SUPPORTED_AUDIO_SUFFIXES:
        raise ReviewAudioAssetError(f"unsupported audio format: {suffix or '<none>'}")
    try:
        first_stat = path.stat()
        if first_stat.st_size <= 0:
            raise ReviewAudioAssetError("audio asset file is empty")
        if first_stat.st_size > max_bytes:
            raise ReviewAudioAssetError("audio asset exceeds the configured size bound")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(MAX_AUDIO_READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        second_stat = path.stat()
    except OSError as exc:
        raise ReviewAudioAssetError(f"audio asset could not be read: {exc}") from exc
    if first_stat.st_size != second_stat.st_size or first_stat.st_mtime_ns != second_stat.st_mtime_ns:
        raise ReviewAudioAssetError("audio asset changed while it was being read")
    computed = digest.hexdigest()
    if asset.sha256 is not None and asset.sha256 != computed:
        raise ReviewAudioAssetError("audio asset SHA-256 does not match supplied metadata")
    if asset.duration_seconds is not None and asset.duration_seconds > max_duration_seconds:
        raise ReviewAudioAssetError("audio asset duration exceeds the configured bound")
    return asset.model_copy(
        update={
            "path": os.fspath(path),
            "sha256": computed,
            "file_size_bytes": second_stat.st_size,
            "format": suffix[1:],
            "validation_state": "valid",
        }
    )


class ReviewSessionRegistry:
    """Thread-safe process-local registry with optional durable backing."""

    def __init__(
        self,
        *,
        store: LocalReviewSessionStore | None = None,
        source_runs: Mapping[str, object] | None = None,
        source_lookup: SourceLookup | None = None,
        max_sessions: int = MAX_REVIEW_SESSIONS,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if type(max_sessions) is not int or not 1 <= max_sessions <= MAX_REVIEW_SESSIONS:
            raise ValueError("max_sessions is outside Review Session bounds")
        self.max_sessions = max_sessions
        self._store = store
        self._source_runs = dict(source_runs or {})
        self._source_lookup = source_lookup
        self._source_resolution_configured = source_runs is not None or source_lookup is not None
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, ReviewSession] = {}
        if store is not None:
            self._sessions.update({item.review_session_id: item for item in store.snapshot()})

    @property
    def store(self) -> LocalReviewSessionStore | None:
        return self._store

    def _find_source(self, source_run_id: str, source_run: object | None) -> object | None:
        if source_run is not None:
            return source_run
        if source_run_id in self._source_runs:
            return self._source_runs[source_run_id]
        if self._source_lookup is not None:
            return self._source_lookup(source_run_id)
        # A caller may supply a prevalidated source ID when the source registry
        # lives in another module.  In that mode the immutable ID is retained;
        # an installed resolver enables strict unknown-run rejection.
        return None

    def register_source_run(self, source_run_id: str, source_run: object) -> None:
        status = _source_status(source_run)
        if status != "completed":
            raise UnknownSourceRunError("only completed Production Runs can open a Review Session")
        with self._lock:
            self._source_runs[source_run_id] = source_run

    def _get_locked(self, review_session_id: str) -> ReviewSession | None:
        """Return the current row while the registry lock is held."""

        session = self._sessions.get(review_session_id)
        if session is not None:
            return session
        if self._store is not None:
            session = self._store.get(review_session_id)
            if session is not None:
                self._sessions[review_session_id] = session
        return session

    def _require(self, review_session_id: str) -> ReviewSession:
        with self._lock:
            session = self._get_locked(review_session_id)
            if session is not None:
                return session
        raise UnknownReviewSessionError(f"unknown Review Session: {review_session_id}")

    def get(self, review_session_id: str) -> ReviewSession | None:
        try:
            return self._require(review_session_id)
        except UnknownReviewSessionError:
            return None

    lookup = get

    def snapshot(self) -> tuple[ReviewSession, ...]:
        with self._lock:
            return tuple(
                sorted(self._sessions.values(), key=lambda item: (item.updated_at, item.review_session_id))
            )

    sessions = snapshot

    def _persist(self, session: ReviewSession) -> ReviewSession:
        if not session.request.persist_session:
            return session
        if self._store is None:
            self._store = LocalReviewSessionStore()
        return self._store.save(session)

    def _evict_one_locked(self, *, incoming_persist_session: bool = True) -> str | None:
        """Evict one deterministic oldest row when the process bound is full."""

        if len(self._sessions) < self.max_sessions:
            return None
        terminal = {"accepted", "completed", "rejected", "stopped", "blocked"}
        candidates = tuple(self._sessions.values())
        if not incoming_persist_session:
            # A process-local request must never make a persisted session
            # disappear merely because this registry is at capacity.  Keep
            # the persisted row and reject the incoming request when there is
            # no process-local row that can be evicted safely.
            candidates = tuple(
                item for item in candidates if not item.request.persist_session
            )
            if not candidates:
                raise ReviewSessionLimitError(
                    "Review Session capacity is occupied by persisted sessions"
                )
        candidate = min(
            candidates,
            key=lambda item: (
                0 if item.status in terminal else 1,
                item.updated_at,
                item.review_session_id,
            ),
        )
        self._sessions.pop(candidate.review_session_id, None)
        # Keep the durable view in sync when the registry bound is tighter
        # than the store's configured bound.  A later registry recreation must
        # not resurrect a row that this process deliberately evicted.
        if self._store is not None and candidate.request.persist_session:
            try:
                self._store.delete(candidate.review_session_id, explicit=True)
            except (ReviewSessionError, OSError):
                # Persistence failures remain visible through the store's
                # status and must not make an in-memory eviction nondeterministic.
                pass
        return candidate.review_session_id

    def _replace(self, session: ReviewSession, **updates: object) -> ReviewSession:
        return self._replace_with_persistence(session, True, updates)

    def _replace_local(self, session: ReviewSession, **updates: object) -> ReviewSession:
        """Replace only the process-local row without retrying durable storage."""

        return self._replace_with_persistence(session, False, updates)

    def _replace_with_persistence(
        self,
        session: ReviewSession,
        persist: bool,
        updates: Mapping[str, object],
    ) -> ReviewSession:
        stamp = self._clock()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        else:
            stamp = stamp.astimezone(timezone.utc)
        with self._lock:
            current = self._get_locked(session.review_session_id)
            if current is None:
                raise UnknownReviewSessionError(
                    f"unknown Review Session: {session.review_session_id}"
                )
            # All public registry mutators eventually pass through this
            # method.  Validate status changes here as well as in
            # ``transition`` so direct status assignments in add_* methods
            # cannot skip lifecycle states.
            target_status = updates.get("status", current.status)
            if not isinstance(target_status, str):
                raise InvalidReviewSessionTransition(
                    "Review Session status must be a string"
                )
            _validate_status_transition(current.status, target_status)
            updated = current.model_copy(
                update={
                    **updates,
                    "updated_at": max(stamp, current.created_at),
                }
            )
            persisted = self._persist(updated) if persist else updated
            self._sessions[persisted.review_session_id] = persisted
        return persisted

    def create(
        self,
        request: ReviewSessionRequest,
        *,
        source_run: object | None = None,
        review_session_id: str | None = None,
    ) -> ReviewSession:
        if not isinstance(request, ReviewSessionRequest):
            raise TypeError("create expects a ReviewSessionRequest")
        resolved_source = self._find_source(request.source_run_id, source_run)
        if resolved_source is not None:
            if _source_status(resolved_source) != "completed":
                raise UnknownSourceRunError("only completed Production Runs can open a Review Session")
            source_id = _read_value(_source_state(resolved_source), "run_id", request.source_run_id)
            if source_id != request.source_run_id:
                raise UnknownSourceRunError("source run ID does not match the completed Production Run")
            snapshot = snapshot_source_run(request.source_run_id, resolved_source)
        elif self._source_resolution_configured:
            raise UnknownSourceRunError(f"unknown source Production Run: {request.source_run_id}")
        else:
            snapshot = None
        initial_status, initial_next_action = _initial_feedback_state(
            request.user_feedback
        )
        with self._lock:
            session_id = review_session_id or _id("review")
            # Check identity before capacity management.  A rejected
            # duplicate must be side-effect free, especially when the row
            # would otherwise be the deterministic eviction candidate.
            if self._get_locked(session_id) is not None:
                raise ReviewSessionError(f"Review Session already exists: {session_id}")
            self._evict_one_locked(incoming_persist_session=request.persist_session)
            stamp = self._clock()
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            session = ReviewSession(
                review_session_id=session_id,
                source_run_id=request.source_run_id,
                request=request,
                source_snapshot=snapshot,
                created_at=stamp,
                updated_at=stamp,
                source_creation_outcome=None if snapshot is None else snapshot.creation_outcome,
                source_sound_palette=FrozenMap({}) if snapshot is None else snapshot.sound_palette,
                source_note_sequences=() if snapshot is None else snapshot.generated_note_sequences,
                source_processing_receipts=() if snapshot is None else snapshot.processing_receipts,
                source_sections=() if snapshot is None else snapshot.sections,
                source_pattern_plan=() if snapshot is None else snapshot.pattern_plan,
                source_manual_handoffs=() if snapshot is None else snapshot.manual_handoffs,
                feedback=request.user_feedback,
                status=initial_status,
                current_next_action=(
                    initial_next_action
                    or "Attach one exported full-mix bounce."
                ),
            )
            persisted = self._persist(session)
            self._sessions[session_id] = persisted
            return persisted

    open = create
    create_from_run = create

    def transition(
        self,
        review_session_id: str,
        status: str,
        *,
        next_action: str | None = None,
        warnings: tuple[str, ...] = (),
        blockers: tuple[str, ...] = (),
    ) -> ReviewSession:
        with self._lock:
            session = self._require(review_session_id)
            _validate_status_transition(session.status, status)
            updates: dict[str, object] = {"status": status}
            if next_action is not None:
                updates["current_next_action"] = next_action
            if warnings:
                updates["warnings"] = tuple(warnings[:64])
            if blockers:
                updates["blockers"] = tuple(blockers[:64])
            return self._replace(session, **updates)

    set_status = transition

    def _asset_for_session(
        self,
        session: ReviewSession,
        asset: ReviewAudioAsset,
        *,
        validate: bool,
    ) -> ReviewAudioAsset:
        """Validate ownership and state before retaining one audio asset."""

        if not isinstance(asset, ReviewAudioAsset):
            raise TypeError("asset must be a ReviewAudioAsset")
        if (
            asset.source_run_id is not None
            and asset.source_run_id != session.source_run_id
        ):
            raise ReviewSessionError(
                "audio asset belongs to a different source Production Run"
            )
        if validate:
            asset = validate_audio_asset(asset)
        if asset.validation_state != "valid":
            raise ReviewAudioAssetError(
                "only audio assets with validation_state='valid' may be attached"
            )
        if not asset.sha256:
            raise ReviewAudioAssetError(
                "a valid audio asset must carry a SHA-256 digest"
            )
        if asset.source_run_id is None:
            # Assets selected directly by a caller predate the source-run
            # attribution field.  Bind them to this session at the boundary;
            # an explicitly different source run was rejected above.
            asset = asset.model_copy(
                update={"source_run_id": session.source_run_id}
            )
        return asset

    def attach_asset(self, review_session_id: str, asset: ReviewAudioAsset, *, validate: bool = True) -> ReviewAudioAsset:
        with self._lock:
            session = self._require(review_session_id)
            asset = self._asset_for_session(session, asset, validate=validate)
            prior = next(
                (item for item in session.assets if item.asset_id == asset.asset_id),
                None,
            )
            if prior is not None and prior.sha256 != asset.sha256:
                raise ReviewSessionError(
                    "an existing asset ID cannot be reused for different audio"
                )
            existing = [item for item in session.assets if item.asset_id != asset.asset_id]
            if len(existing) >= MAX_REVIEW_ASSETS and all(item.asset_id != asset.asset_id for item in session.assets):
                raise ReviewSessionLimitError("Review Session asset limit exceeded")
            revision_passes = self._prepare_after_bounce_attachment_locked(
                session, asset
            )
            updates: dict[str, object] = {"assets": tuple([*existing, asset])}
            if revision_passes is not None:
                updates["revision_passes"] = revision_passes
            updated = self._replace(session, **updates)
            return next(item for item in updated.assets if item.asset_id == asset.asset_id)

    add_asset = attach_asset

    def attach_asset_set(self, review_session_id: str, asset_set: ReviewAssetSet, *, validate: bool = True) -> ReviewAssetSet:
        with self._lock:
            session = self._require(review_session_id)
            if not isinstance(asset_set, ReviewAssetSet):
                raise TypeError("asset_set must be a ReviewAssetSet")
            checked = tuple(
                self._asset_for_session(session, item, validate=validate)
                for item in asset_set.assets
            )
            by_id = {item.asset_id: item for item in checked}
            asset_set = asset_set.model_copy(
                update={
                    "candidate_full_mix": (
                        by_id.get(asset_set.candidate_full_mix.asset_id)
                        if asset_set.candidate_full_mix
                        else None
                    ),
                    "before_full_mix": (
                        by_id.get(asset_set.before_full_mix.asset_id)
                        if asset_set.before_full_mix
                        else None
                    ),
                    "after_full_mix": (
                        by_id.get(asset_set.after_full_mix.asset_id)
                        if asset_set.after_full_mix
                        else None
                    ),
                    "reference": (
                        by_id.get(asset_set.reference.asset_id)
                        if asset_set.reference
                        else None
                    ),
                    "synchronized_stems": tuple(
                        by_id[item.asset_id] for item in asset_set.synchronized_stems
                    ),
                    "section_bounces": tuple(
                        by_id[item.asset_id] for item in asset_set.section_bounces
                    ),
                }
            )
            rows = [item for item in session.asset_sets if item.asset_set_id != asset_set.asset_set_id]
            if len(rows) >= 64 and all(item.asset_set_id != asset_set.asset_set_id for item in session.asset_sets):
                raise ReviewSessionLimitError("Review Session asset-set limit exceeded")
            all_assets = {item.asset_id: item for item in session.assets}
            for item in asset_set.assets:
                prior = all_assets.get(item.asset_id)
                if prior is not None and prior.sha256 != item.sha256:
                    raise ReviewSessionError(
                        "an existing asset ID cannot be reused for different audio"
                    )
            all_assets.update({item.asset_id: item for item in asset_set.assets})
            if len(all_assets) > MAX_REVIEW_ASSETS:
                raise ReviewSessionLimitError("Review Session asset limit exceeded")
            revision_passes = self._prepare_after_bounce_attachment_locked(
                session,
                asset_set.after_full_mix,
            )
            updates: dict[str, object] = {
                "asset_sets": tuple([*rows, asset_set]),
                "assets": tuple(all_assets.values()),
            }
            if revision_passes is not None:
                updates["revision_passes"] = revision_passes
            updated = self._replace(
                session,
                **updates,
            )
            return next(item for item in updated.asset_sets if item.asset_set_id == asset_set.asset_set_id)

    add_asset_set = attach_asset_set

    def _prepare_after_bounce_attachment_locked(
        self,
        session: ReviewSession,
        asset: ReviewAudioAsset | None,
    ) -> tuple[RevisionPass, ...] | None:
        """Validate and checkpoint an after-bounce attachment in one update.

        An ``after_full_mix`` is meaningful only as the output of one known
        revision pass.  Returning the replacement pass tuple lets the caller
        include it in the same immutable ``ReviewSession`` replacement as the
        asset/asset-set, so a durable write cannot expose one half of the
        lifecycle transition.
        """

        if asset is None or asset.asset_kind != "after_full_mix":
            return None
        pass_id = asset.revision_pass_id
        if pass_id is None:
            raise ReviewSessionError(
                "after_full_mix assets must declare a revision_pass_id"
            )
        revision_pass = next(
            (
                item
                for item in session.revision_passes
                if item.revision_pass_id == pass_id
            ),
            None,
        )
        if revision_pass is None:
            raise ReviewSessionError(
                f"after_full_mix asset references unknown revision pass: {pass_id}"
            )
        if revision_pass.review_session_id != session.review_session_id:
            raise ReviewSessionError(
                "after_full_mix revision pass belongs to a different Review Session"
            )
        if revision_pass.source_run_id != session.source_run_id:
            raise ReviewSessionError(
                "after_full_mix revision pass belongs to a different source run"
            )

        prior_after = tuple(
            item
            for item in session.assets
            if item.asset_kind == "after_full_mix"
            and item.revision_pass_id == pass_id
        )
        same_asset = any(
            item.asset_id == asset.asset_id and item.sha256 == asset.sha256
            for item in prior_after
        )
        if revision_pass.after_bounce_state == "compared":
            if same_asset:
                # A retry of an already persisted attachment is idempotent;
                # retain the compared checkpoint rather than downgrading it.
                return None
            raise ReviewSessionError(
                "revision pass has already been compared; attach a new bounce under a new revision pass"
            )
        if revision_pass.after_bounce_state == "attached" and not same_asset:
            raise ReviewSessionError(
                "revision pass already has an attached after_full_mix bounce"
            )
        if revision_pass.after_bounce_state == "attached":
            return None
        updated_pass = revision_pass.model_copy(
            update={"after_bounce_state": "attached"}
        )
        return tuple(
            updated_pass if item.revision_pass_id == pass_id else item
            for item in session.revision_passes
        )

    def set_section_map(self, review_session_id: str, section_map: ReviewSectionMap) -> ReviewSession:
        with self._lock:
            return self._replace(self._require(review_session_id), section_map=section_map)

    def add_evaluation(self, review_session_id: str, report: CreationEvaluationReport) -> ReviewSession:
        with self._lock:
            session = self._require(review_session_id)
            if report.review_session_id != review_session_id or report.source_run_id != session.source_run_id:
                raise ReviewSessionError("evaluation does not belong to this Review Session")
            rows = [item for item in session.evaluations if item.evaluation_id != report.evaluation_id]
            if len(rows) >= MAX_REVIEW_EVALUATIONS:
                raise ReviewSessionLimitError("Review Session evaluation limit exceeded")
            return self._replace(session, evaluations=tuple([*rows, report]), status="evaluated")

    record_evaluation = add_evaluation

    def add_feedback(self, review_session_id: str, feedback: CreationFeedback) -> ReviewSession:
        with self._lock:
            session = self._require(review_session_id)
            if feedback.review_session_id not in {None, review_session_id}:
                raise ReviewSessionError("feedback does not belong to this Review Session")
            if feedback.review_session_id is None:
                feedback = feedback.model_copy(update={"review_session_id": review_session_id})
            rows = [item for item in session.feedback if item.feedback_id != feedback.feedback_id]
            if len(rows) >= MAX_REVIEW_FEEDBACK:
                raise ReviewSessionLimitError("Review Session feedback limit exceeded")
            updates: dict[str, object] = {"feedback": tuple([*rows, feedback])}
            if feedback.source == "user_explicit" and (
                feedback.overall_verdict in {
                    "approved",
                    "accepted",
                    "user_approved",
                }
                or feedback.approval_level in {"final", "approved"}
            ):
                updates["status"] = "accepted"
                updates["current_next_action"] = "Review Session accepted; no automatic revision is pending."
            elif feedback.source == "user_explicit" and (
                feedback.overall_verdict in {"rejected", "user_rejected"}
                or feedback.approval_level == "rejected"
            ):
                updates["status"] = "rejected"
                updates["current_next_action"] = (
                    "Provide an explicit revision request or finalize the session."
                )
            elif feedback.source == "user_explicit" and (
                feedback.overall_verdict in {"needs_revision", "user_confirmed_draft"}
                or feedback.approval_level == "draft"
            ):
                updates["current_next_action"] = (
                    "Create one bounded revision plan from the explicit producer feedback."
                    if feedback.overall_verdict == "needs_revision"
                    else "The draft is producer-confirmed; await an explicit approval or revision request."
                )
            return self._replace(session, **updates)

    record_feedback = add_feedback

    def add_revision_plan(self, review_session_id: str, plan: RevisionPlan) -> ReviewSession:
        with self._lock:
            session = self._require(review_session_id)
            if plan.review_session_id != review_session_id or plan.source_run_id != session.source_run_id:
                raise ReviewSessionError("revision plan does not belong to this Review Session")
            if not any(item.evaluation_id == plan.source_evaluation_id for item in session.evaluations):
                raise ReviewSessionError("revision plan references an unknown evaluation")
            rows = [item for item in session.revision_plans if item.revision_plan_id != plan.revision_plan_id]
            if len(rows) >= MAX_REVIEW_PASSES:
                raise ReviewSessionLimitError("Review Session revision-plan limit exceeded")
            return self._replace(session, revision_plans=tuple([*rows, plan]), status="revision_planned")

    record_revision_plan = add_revision_plan

    def _prepare_revision_pass_locked(
        self,
        session: ReviewSession,
        revision_pass: RevisionPass,
    ) -> tuple[RevisionPass, tuple[RevisionPass, ...], str]:
        """Validate one pass and derive its bounded replacement under the lock."""

        if revision_pass.review_session_id not in {"review-session-unknown", session.review_session_id}:
            raise ReviewSessionError("revision pass does not belong to this Review Session")
        if revision_pass.review_session_id == "review-session-unknown":
            revision_pass = revision_pass.model_copy(
                update={"review_session_id": session.review_session_id}
            )
        if revision_pass.source_run_id != session.source_run_id:
            raise ReviewSessionError("revision pass does not belong to this source run")
        if not any(item.revision_plan_id == revision_pass.revision_plan_id for item in session.revision_plans):
            raise ReviewSessionError("revision pass references an unknown plan")
        rows = [
            item
            for item in session.revision_passes
            if item.revision_pass_id != revision_pass.revision_pass_id
        ]
        if len(rows) >= session.request.max_revision_passes:
            raise ReviewSessionLimitError("Review Session revision-pass limit exceeded")
        if len(rows) >= MAX_REVIEW_PASSES:
            raise ReviewSessionLimitError("Review Session revision-pass limit exceeded")
        next_status = {
            "created": "revising",
            "preflight": "revising",
            "revising": "revising",
            "completed": "awaiting_rebounce",
            "awaiting_rebounce": "awaiting_rebounce",
            "blocked": "blocked",
            "stopped": "stopped",
        }[revision_pass.status]
        return revision_pass, tuple([*rows, revision_pass]), next_status

    def add_revision_pass(self, review_session_id: str, revision_pass: RevisionPass) -> ReviewSession:
        with self._lock:
            session = self._require(review_session_id)
            revision_pass, rows, next_status = self._prepare_revision_pass_locked(
                session, revision_pass
            )
            return self._replace(session, revision_passes=rows, status=next_status)

    record_revision_pass = add_revision_pass

    def record_revision_pass_after_persistence_failure(
        self,
        review_session_id: str,
        revision_pass: RevisionPass,
        *,
        persistence_error: BaseException | str | None = None,
    ) -> ReviewSession:
        """Record an already-executed pass locally when durable write failed.

        This is an explicit recovery boundary: it never retries the failed
        store write or replays the revision.  The pass receipt remains
        truthful while the session is blocked until the user resolves local
        persistence and decides what to do next.
        """

        with self._lock:
            session = self._require(review_session_id)
            _, rows, _ = self._prepare_revision_pass_locked(
                session, revision_pass
            )
            error_name = (
                type(persistence_error).__name__
                if isinstance(persistence_error, BaseException)
                else "write failure"
            )
            blocker = (
                "Durable Review Session persistence failed after revision execution "
                f"({error_name}); the receipt is process-local and no replay was attempted."
            )
            blockers = tuple([*session.blockers, blocker][-64:])
            return self._replace_local(
                session,
                revision_passes=rows,
                status="blocked",
                blockers=blockers,
                current_next_action=(
                    "Resolve local persistence before continuing; no revision replay was attempted."
                ),
            )

    # Keep the recovery operation discoverable under the shorter lifecycle
    # vocabulary used by callers while preserving one implementation.
    record_revision_pass_local = record_revision_pass_after_persistence_failure
    record_revision_pass_locally = record_revision_pass_after_persistence_failure
    record_revision_pass_after_store_failure = record_revision_pass_after_persistence_failure

    def add_comparison(
        self,
        review_session_id: str,
        comparison: RevisionComparison,
        *,
        revision_pass_id: str | None = None,
        revision_plan_id: str | None = None,
    ) -> ReviewSession:
        with self._lock:
            session = self._require(review_session_id)
            if not isinstance(comparison, RevisionComparison):
                raise TypeError("comparison must be a RevisionComparison")
            known = {item.asset_id: item for item in session.assets}
            if (
                comparison.before_asset.asset_id not in known
                or comparison.after_asset.asset_id not in known
            ):
                raise ReviewSessionError("comparison assets must be attached to this Review Session")
            for candidate in (comparison.before_asset, comparison.after_asset):
                attached = known[candidate.asset_id]
                if attached.validation_state != "valid":
                    raise ReviewAudioAssetError(
                        "comparison assets must have validation_state='valid'"
                    )
                if candidate.source_run_id not in {None, session.source_run_id}:
                    raise ReviewSessionError(
                        "comparison asset belongs to a different source Production Run"
                    )
                if candidate.sha256 != attached.sha256:
                    raise ReviewSessionError(
                        "comparison asset metadata does not match the attached asset"
                    )
            # Store the canonical attached metadata.  This prevents a caller from
            # smuggling a different path or validation state behind a known ID.
            comparison = comparison.model_copy(
                update={
                    "before_asset": known[comparison.before_asset.asset_id],
                    "after_asset": known[comparison.after_asset.asset_id],
                }
            )
            if comparison.before_asset.asset_kind != "before_full_mix":
                raise ReviewSessionError(
                    "a revision comparison before asset must be before_full_mix"
                )
            after = comparison.after_asset
            if after.asset_kind != "after_full_mix":
                raise ReviewSessionError(
                    "a revision comparison after asset must be after_full_mix"
                )
            after_pass_id = after.revision_pass_id
            if after_pass_id is None:
                raise ReviewSessionError(
                    "after_full_mix comparison assets must declare a revision_pass_id"
                )
            if revision_pass_id is not None and revision_pass_id != after_pass_id:
                raise ReviewSessionError(
                    "comparison revision_pass_id does not match the after_full_mix asset"
                )
            revision_pass = next(
                (
                    item
                    for item in session.revision_passes
                    if item.revision_pass_id == after_pass_id
                ),
                None,
            )
            if revision_pass is None:
                raise ReviewSessionError(
                    f"comparison references unknown revision pass: {after_pass_id}"
                )
            if revision_pass.review_session_id != session.review_session_id:
                raise ReviewSessionError(
                    "comparison revision pass belongs to a different Review Session"
                )
            if revision_pass.source_run_id != session.source_run_id:
                raise ReviewSessionError(
                    "comparison revision pass belongs to a different source run"
                )
            if (
                revision_plan_id is not None
                and revision_pass.revision_plan_id != revision_plan_id
            ):
                raise ReviewSessionError(
                    "comparison revision_plan_id does not match the revision pass"
                )
            if revision_pass.after_bounce_state not in {"attached", "compared"}:
                raise ReviewSessionError(
                    "after_full_mix must be attached before it can be compared"
                )
            rows = [item for item in session.comparisons if item.comparison_id != comparison.comparison_id]
            if len(rows) >= 64:
                raise ReviewSessionLimitError("Review Session comparison limit exceeded")
            compared_pass = revision_pass.model_copy(
                update={"after_bounce_state": "compared"}
            )
            revision_passes = tuple(
                compared_pass if item.revision_pass_id == after_pass_id else item
                for item in session.revision_passes
            )
            return self._replace(
                session,
                comparisons=tuple([*rows, comparison]),
                revision_passes=revision_passes,
                status="comparing",
            )

    record_comparison = add_comparison

    def set_delivery_manifest(self, review_session_id: str, manifest: DeliveryManifest) -> ReviewSession:
        with self._lock:
            session = self._require(review_session_id)
            if manifest.review_session_id != review_session_id or manifest.source_run_id != session.source_run_id:
                raise ReviewSessionError("delivery manifest does not belong to this Review Session")
            rows = [item for item in session.delivery_manifests if item.delivery_id != manifest.delivery_id]
            if len(rows) >= MAX_REVIEW_MANIFESTS:
                raise ReviewSessionLimitError("Review Session delivery-manifest limit exceeded")
            # Exporting a manifest records a handoff artifact; it is not itself a
            # producer approval or a request to terminate an iterative review.
            return self._replace(session, delivery_manifests=tuple([*rows, manifest]))

    add_delivery_manifest = set_delivery_manifest

    def accept(self, review_session_id: str) -> ReviewSession:
        return self.transition(
            review_session_id,
            "accepted",
            next_action="Review Session accepted; no automatic revision is pending.",
        )

    approve = accept

    def reject(self, review_session_id: str, *, next_action: str = "Provide an explicit revision request or finalize manually.") -> ReviewSession:
        return self.transition(review_session_id, "rejected", next_action=next_action)

    def stop(self, review_session_id: str) -> ReviewSession:
        return self.transition(review_session_id, "stopped", next_action="Review Session stopped by the user.")

    def delete(self, review_session_id: str, *, explicit: bool = False) -> bool:
        if not explicit:
            raise ValueError("deleting a Review Session requires explicit=True")
        with self._lock:
            existed = review_session_id in self._sessions
            if self._store is not None:
                existed = self._store.delete(review_session_id, explicit=True) or existed
            self._sessions.pop(review_session_id, None)
            return existed

    def reset_persistence(self, *, explicit: bool = False) -> object:
        if self._store is None:
            raise ReviewSessionError("no local persistence store is configured")
        with self._lock:
            result = self._store.reset(explicit=explicit)
            # A reset must invalidate the registry cache as well as the file.
            # Otherwise a later lookup or capacity decision can resurrect a
            # deleted persisted session from this process's memory.
            self._sessions.clear()
            return result

    reset_store = reset_persistence
    reset = reset_persistence


ReviewSessionStore = ReviewSessionRegistry
CreationReviewSessionRegistry = ReviewSessionRegistry


__all__ = [
    "CreationReviewSessionRegistry",
    "InvalidReviewSessionTransition",
    "ReviewAudioAssetError",
    "ReviewSessionError",
    "ReviewSessionLimitError",
    "ReviewSessionRegistry",
    "ReviewSessionStore",
    "UnknownReviewSessionError",
    "UnknownSourceRunError",
    "SUPPORTED_AUDIO_SUFFIXES",
    "snapshot_source_run",
    "validate_audio_asset",
]
