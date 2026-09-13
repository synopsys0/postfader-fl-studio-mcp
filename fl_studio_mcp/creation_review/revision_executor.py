"""One-shot revision execution over the existing Production Run kernel.

This module is an adapter, not another mutation engine.  A closed review plan
is preflighted once, translated into existing :mod:`production_runs`
operations, and handed to ``ProductionRunRegistry.execute``.  The registry
continues to own the write-mode boundary, readback, receipts, and shutdown.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, cast

from pydantic import Field, model_validator

from .. import production_runs as runs
from ..creation_pipeline.processing import ProcessingPlan
from ..creative import CreativeNote, NoteSequence, SectionMarker, make_sequence
from ..sound_selection.models import SoundSelectionRequest
from ..track_b_contracts import ChannelGeneratorTarget, MixerEffectTarget
from ..workflows import BatchChannelMix, BatchMixerVolumeDb
from .models import (
    CreationReviewModel,
    EvaluationState,
    FrozenMap,
    ManualHandoff,
    PlaylistPlacement,
    ReviewGeneratedOutput,
    RevisionOperation,
    RevisionOperationReceipt,
    RevisionPass,
    RevisionPlan,
    RevisionRequest,
    _canonical_digest,
)
from .revision_planner import validate_revision_plan


MAX_REVISION_BLOCKERS = 64
MAX_REVISION_WARNINGS = 64
MAX_RECEIPTS = 64
_LOCAL_OPERATION_KINDS = frozenset(
    {"record_feedback_lock", "create_playlist_handoff_delta"}
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RevisionReadiness(CreationReviewModel):
    """All independently known blockers before a revision pass."""

    schema_version: str = "1.0"
    state: str = "ready"
    checked_at: datetime = Field(default_factory=_now)
    source_run_available: bool = True
    source_evaluation_available: bool = True
    session_matches: bool = True
    target_fingerprints_match: bool = True
    stored_sequences_available: bool = True
    palette_continuity: bool = True
    pattern_targets_available: bool = True
    piano_roll_ready: bool = True
    effects_ready: bool = True
    manual_handoff_required: bool = False
    blockers: tuple[str, ...] = Field(default=(), max_length=MAX_REVISION_BLOCKERS)
    warnings: tuple[str, ...] = Field(default=(), max_length=MAX_REVISION_WARNINGS)
    expected_export_action: str | None = Field(default=None, max_length=1024)
    preflight_count: int = Field(default=1, ge=1, le=1)

    @model_validator(mode="after")
    def validate_state(self) -> "RevisionReadiness":
        if self.state not in {"ready", "ready_with_limitations", "blocked"}:
            raise ValueError("revision readiness state is invalid")
        if self.state == "blocked" and not self.blockers:
            raise ValueError("blocked revision readiness needs blockers")
        if self.state != "blocked" and self.blockers:
            raise ValueError("ready revision readiness cannot contain blockers")
        return self


@dataclass(frozen=True)
class RevisionExecutionContext:
    """Runtime-only adapter inputs; it never stores audio bytes."""

    review_session_id: str
    source_run_id: str
    source_evaluation_id: str
    session_fingerprint: str | None = None
    project_state_digest: str | None = None
    target_fingerprints: Mapping[str, str] = field(default_factory=dict)
    sequence_digests: Mapping[str, str] = field(default_factory=dict)
    sequences: Mapping[str, Any] = field(default_factory=dict)
    palette_assignments: Mapping[str, Any] = field(default_factory=dict)
    processing_plans: Mapping[str, Any] = field(default_factory=dict)
    palette_requests: Mapping[str, Any] = field(default_factory=dict)
    operation_adapters: Mapping[str, Any] = field(default_factory=dict)
    project_key: str | None = None
    source_run_available: bool = True
    source_evaluation_available: bool = True
    target_fingerprints_match: bool = True
    stored_sequences_available: bool = True
    palette_continuity: bool = True
    pattern_targets_available: bool = True
    piano_roll_ready: bool = True
    effects_ready: bool = True
    effect_controls: Mapping[str, Any] = field(default_factory=dict)
    palette_id: str | None = None
    palette_digest: str | None = None
    completed_revision_operation_ids: tuple[str, ...] = ()
    completed_revision_operations: int = 0
    completed_revision_passes: int = 0
    maximum_revision_passes: int | None = None
    maximum_revision_operations: int | None = None


@dataclass(frozen=True)
class RevisionAdapterResult:
    """Translated production operations and non-mutating local handoffs."""

    operations: tuple[Any, ...]
    local_operations: tuple[RevisionOperation, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _parameters(operation: Any) -> dict[str, Any]:
    value = getattr(operation, "parameters", {})
    if isinstance(value, FrozenMap):
        value = value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _parameter(operation: Any, name: str, default: Any = None) -> Any:
    return _parameters(operation).get(name, default)


def _playlist_placements(
    operation: RevisionOperation,
) -> tuple[PlaylistPlacement, ...]:
    values = getattr(operation, "placements", ())
    if not isinstance(values, tuple):
        return ()
    return tuple(item for item in values if isinstance(item, PlaylistPlacement))


def _context(context: Any, name: str, default: Any = None) -> Any:
    if isinstance(context, Mapping):
        return context.get(name, default)
    return getattr(context, name, default)


def _target_for_assignment(assignment: Any) -> Any:
    if isinstance(assignment, Mapping):
        target = assignment.get("target")
        if target is not None:
            return target
        # Palette snapshots sometimes persist the typed target fields at the
        # assignment root. Treat that shape as a target without guessing any
        # arbitrary project object.
        if assignment.get("kind") in {"mixer_effect", "channel_generator"}:
            return assignment
        return assignment.get("plugin_target")
    return getattr(assignment, "target", None)


def _coerce_plugin_target(target: Any) -> Any:
    if isinstance(target, (MixerEffectTarget, ChannelGeneratorTarget)):
        return target
    if not isinstance(target, Mapping):
        return target
    try:
        if target.get("kind") == "mixer_effect":
            return MixerEffectTarget.model_validate(target, strict=False)
        if target.get("kind") == "channel_generator":
            return ChannelGeneratorTarget.model_validate(target, strict=False)
    except Exception:
        return target
    return target


def _stored_sequence(context: Any, operation: RevisionOperation) -> NoteSequence | None:
    """Recover only a PostFader-persisted NoteSequence for a note revision."""

    parameters = _parameters(operation)
    source_id = parameters.get("source_operation_id") or operation.role_id
    if not isinstance(source_id, str):
        return None
    value = (_context(context, "sequences", {}) or {}).get(source_id)
    if isinstance(value, NoteSequence):
        return value
    if isinstance(value, ReviewGeneratedOutput):
        value = value.metadata.to_dict()
    elif hasattr(value, "metadata"):
        metadata = getattr(value, "metadata")
        value = metadata.to_dict() if hasattr(metadata, "to_dict") else metadata
    if isinstance(value, Mapping):
        nested = value.get("sequence")
        if isinstance(nested, Mapping):
            value = nested
        try:
            return NoteSequence.model_validate(value, strict=False)
        except (TypeError, ValueError):
            return None
    return None


def _bounded_number(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
    name: str,
) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be within {minimum:g}..{maximum:g}")
    return result


def _transform_stored_sequence(
    sequence: NoteSequence,
    operation: RevisionOperation,
) -> NoteSequence:
    """Apply a deterministic identity-preserving transform to stored notes.

    The source is the immutable sequence snapshot captured from the completed
    Production Run.  No arbitrary Piano Roll content is read or inferred.
    """

    parameters = _parameters(operation)
    transform_value = parameters.get("transform")
    transform = (
        dict(transform_value)
        if isinstance(transform_value, Mapping)
        else {"operation": transform_value}
        if isinstance(transform_value, str)
        else {}
    )
    transform_name = str(transform.get("operation") or "")

    semitones = parameters.get("semitones", transform.get("semitones", 0))
    if (
        operation.operation == "change_section_register"
        and parameters.get("octaves") is not None
    ):
        semitones = _bounded_number(
            parameters["octaves"],
            default=0.0,
            minimum=-4.0,
            maximum=4.0,
            name="octaves",
        ) * 12
    if transform_name == "octave_shift":
        semitones = _bounded_number(
            transform.get("octaves", parameters.get("octaves", 0)),
            default=0.0,
            minimum=-4.0,
            maximum=4.0,
            name="octaves",
        ) * 12
    semitone_shift = int(
        _bounded_number(
            semitones,
            default=0.0,
            minimum=-48.0,
            maximum=48.0,
            name="semitones",
        )
    )

    density_value = parameters.get("density_scale", transform.get("density_scale"))
    density_scale = _bounded_number(
        density_value,
        default=1.0,
        minimum=0.25,
        maximum=2.0,
        name="density_scale",
    )
    articulation_scale = _bounded_number(
        parameters.get(
            "articulation_scale",
            parameters.get(
                "note_length_scale", transform.get("note_length_scale")
            ),
        ),
        default=1.0,
        minimum=0.25,
        maximum=2.0,
        name="articulation_scale",
    )
    velocity_scale = _bounded_number(
        parameters.get("velocity_scale", transform.get("velocity_scale")),
        default=1.0,
        minimum=0.1,
        maximum=2.0,
        name="velocity_scale",
    )
    velocity_delta = _bounded_number(
        parameters.get(
            "velocity_delta", parameters.get("velocity_amount", 0.0)
        ),
        default=0.0,
        minimum=-1.0,
        maximum=1.0,
        name="velocity_delta",
    )
    rhythm_offset = _bounded_number(
        parameters.get(
            "rhythmic_displacement_beats",
            parameters.get("offset_beats", transform.get("offset_beats", 0.0)),
        ),
        default=0.0,
        minimum=-16.0,
        maximum=16.0,
        name="rhythmic_displacement_beats",
    )
    register_low = int(
        _bounded_number(
            parameters.get("register_low"),
            default=0.0,
            minimum=0.0,
            maximum=131.0,
            name="register_low",
        )
    )
    register_high = int(
        _bounded_number(
            parameters.get("register_high"),
            default=131.0,
            minimum=0.0,
            maximum=131.0,
            name="register_high",
        )
    )
    if register_high < register_low:
        raise ValueError("register_high must not be below register_low")

    if transform_name == "quantize":
        grid = _bounded_number(
            transform.get("grid_beats", parameters.get("grid_beats")),
            default=0.25,
            minimum=1.0 / 64.0,
            maximum=16.0,
            name="grid_beats",
        )
    else:
        grid = None

    notes = list(sequence.notes)
    if density_scale < 1.0 and notes:
        target_count = max(1, round(len(notes) * density_scale))
        keep = {
            round(index * (len(notes) - 1) / max(1, target_count - 1))
            for index in range(target_count)
        }
        notes = [note for index, note in enumerate(notes) if index in keep]
    elif density_scale > 1.0 and notes:
        additions = min(len(notes), round(len(notes) * (density_scale - 1.0)))
        for note in notes[:additions]:
            offset = min(max(note.duration_beats * 0.5, 1.0 / 64.0), 0.5)
            notes.append(
                note.model_copy(
                    update={
                        "start_beats": min(4096.0, note.start_beats + offset),
                        "duration_beats": max(
                            0.01, min(note.duration_beats, offset * 0.9)
                        ),
                    }
                )
            )

    if operation.operation == "change_section_voicing":
        inversion = int(
            _bounded_number(
                parameters.get("inversion"),
                default=1.0,
                minimum=-4.0,
                maximum=4.0,
                name="inversion",
            )
        )
        grouped: dict[float, list[CreativeNote]] = {}
        for note in notes:
            grouped.setdefault(note.start_beats, []).append(note)
        voiced: list[CreativeNote] = []
        for start in sorted(grouped):
            chord = sorted(grouped[start], key=lambda item: item.pitch)
            for _ in range(abs(inversion)):
                if not chord:
                    break
                if inversion > 0:
                    chord[0] = chord[0].model_copy(
                        update={"pitch": min(131, chord[0].pitch + 12)}
                    )
                elif inversion < 0:
                    chord[-1] = chord[-1].model_copy(
                        update={"pitch": max(0, chord[-1].pitch - 12)}
                    )
                chord.sort(key=lambda item: item.pitch)
            voiced.extend(chord)
        notes = voiced

    transformed: list[CreativeNote] = []
    for index, note in enumerate(notes):
        start = note.start_beats + rhythm_offset
        if grid is not None:
            start = round(start / grid) * grid
        if transform_name == "humanize":
            timing = _bounded_number(
                transform.get("timing_beats", parameters.get("timing_beats")),
                default=0.0,
                minimum=0.0,
                maximum=0.25,
                name="timing_beats",
            )
            start += timing if index % 2 else -timing
        pitch = max(register_low, min(register_high, note.pitch + semitone_shift))
        transformed.append(
            note.model_copy(
                update={
                    "pitch": pitch,
                    "start_beats": max(0.0, min(4096.0, round(start, 6))),
                    "duration_beats": max(
                        0.01,
                        min(256.0, round(note.duration_beats * articulation_scale, 6)),
                    ),
                    "velocity": max(
                        0.0,
                        min(1.0, round(note.velocity * velocity_scale + velocity_delta, 6)),
                    ),
                }
            )
        )

    if transform_name == "duplicate":
        repeats = int(
            _bounded_number(
                transform.get("repeats", parameters.get("repeats")),
                default=1.0,
                minimum=1.0,
                maximum=16.0,
                name="repeats",
            )
        )
        offset = _bounded_number(
            transform.get("offset_beats", parameters.get("offset_beats")),
            default=max(sequence.duration_beats, 0.25),
            minimum=1.0 / 64.0,
            maximum=4096.0,
            name="offset_beats",
        )
        source = tuple(transformed)
        for repeat in range(1, repeats + 1):
            for note in source:
                start = note.start_beats + offset * repeat
                if start <= 4096.0:
                    transformed.append(
                        note.model_copy(update={"start_beats": round(start, 6)})
                    )

    return make_sequence(
        name=f"{sequence.name} revision",
        generator=sequence.generator,
        notes=transformed,
        tempo_bpm=sequence.tempo_bpm,
        numerator=sequence.time_signature_numerator,
        denominator=sequence.time_signature_denominator,
        seed=sequence.seed,
        pitch_collection=sequence.pitch_collection,
        warnings=(
            *sequence.warnings,
            "Revision derived from the stored PostFader NoteSequence; arbitrary Piano Roll notes were not inferred.",
        ),
    )


def _digest_id(plan_id: str, started_at: datetime) -> str:
    return hashlib.sha256(f"{plan_id}:{started_at.isoformat()}".encode()).hexdigest()[:24]


def _messages(value: Any, field_name: str = "message") -> tuple[str, ...]:
    output: list[str] = []
    for item in value or ():
        if isinstance(item, str):
            output.append(item)
        elif isinstance(item, Mapping):
            output.append(str(item.get(field_name) or item.get("error") or item))
        else:
            output.append(str(getattr(item, field_name, item)))
    return tuple(output)


class RevisionExecutor:
    """Apply one complete revision plan through a Production Run registry."""

    def __init__(
        self,
        *,
        run_registry: Any = None,
        readiness_checker: Callable[..., Any] | None = None,
        session_capturer: Callable[..., Any] | None = None,
        run_executor: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.run_registry = run_registry
        self.readiness_checker = readiness_checker
        self.session_capturer = session_capturer
        self.run_executor = run_executor
        self.clock = clock

    def _registry(self) -> Any:
        return self.run_registry if self.run_registry is not None else runs.ProductionRunRegistry()

    @staticmethod
    def _call_once(callback: Callable[..., Any], values: tuple[Any, ...]) -> Any:
        """Call an injected seam once, adapting only its positional arity."""

        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            return callback(*values)
        parameters = tuple(signature.parameters.values())
        if any(item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters):
            return callback(*values)
        positional = tuple(
            item
            for item in parameters
            if item.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        )
        return callback(*values[: len(positional)])

    def _assignment(self, context: Any, role_id: str | None, assignment_id: str | None = None) -> Any:
        assignments = _context(context, "palette_assignments", {}) or {}
        if assignment_id and assignment_id in assignments:
            return assignments[assignment_id]
        if role_id and role_id in assignments:
            return assignments[role_id]
        wanted = (role_id or assignment_id or "").casefold()
        for key, value in assignments.items():
            if str(key).casefold() == wanted or str(getattr(value, "role_id", "")).casefold() == wanted:
                return value
        return None

    def _default_preflight(self, plan: RevisionPlan, request: RevisionRequest, context: Any) -> RevisionReadiness:
        blockers: list[str] = []
        warnings: list[str] = []
        flags = (
            ("source_run_available", "source Production Run is unavailable"),
            ("source_evaluation_available", "source evaluation is unavailable"),
            ("target_fingerprints_match", "required project target fingerprints are stale"),
            ("stored_sequences_available", "stored generated NoteSequences are unavailable"),
            ("palette_continuity", "source Sound Palette continuity is unavailable"),
            ("pattern_targets_available", "required pattern targets are unavailable"),
            ("piano_roll_ready", "Piano Roll targets are not ready"),
            ("effects_ready", "required effect controls are unavailable"),
        )
        for name, message in flags:
            if _context(context, name, True) is False:
                blockers.append(message)
        for name, expected, message in (
            ("review_session_id", plan.review_session_id, "source Review Session does not match the revision plan"),
            ("source_run_id", plan.source_run_id, "source Production Run does not match the revision plan"),
            ("source_evaluation_id", plan.source_evaluation_id, "source evaluation does not match the revision plan"),
        ):
            actual = _context(context, name)
            if actual is not None and actual != expected:
                blockers.append(message)

        sequence_digests = _context(context, "sequence_digests", {}) or {}
        sequences = _context(context, "sequences", {}) or {}
        processing_plans = _context(context, "processing_plans", {}) or {}
        target_fingerprints = _context(context, "target_fingerprints", {}) or {}
        effect_controls = _context(context, "effect_controls", {}) or {}
        injected_adapters = _context(context, "operation_adapters", {}) or {}
        palette_digest = _context(context, "palette_digest")
        for operation in plan.operations:
            parameters = _parameters(operation)
            if operation.source_sequence_digest:
                source_id = parameters.get("source_operation_id") or operation.role_id
                actual = sequence_digests.get(source_id)
                if actual is not None and actual != operation.source_sequence_digest:
                    blockers.append(f"stored NoteSequence digest is stale for {operation.operation_id!r}")
                elif (
                    actual is None
                    and (source_id is None or source_id not in sequences)
                ):
                    blockers.append(f"stored NoteSequence is unavailable for {operation.operation_id!r}")
            if operation.source_palette_digest:
                if palette_digest is None:
                    blockers.append(
                        f"source Sound Palette digest is unavailable for {operation.operation_id!r}"
                    )
                elif palette_digest != operation.source_palette_digest:
                    blockers.append(
                        f"source Sound Palette digest is stale for {operation.operation_id!r}"
                    )
            target_id = parameters.get("target_id")
            expected_fingerprint = parameters.get("target_fingerprint")
            if (
                target_id
                and expected_fingerprint
                and target_fingerprints.get(target_id) != expected_fingerprint
            ):
                blockers.append(f"required target fingerprint is stale for {operation.operation_id!r}")
            role = operation.role_id or parameters.get("role_id")
            assignment_id = parameters.get("assignment_id")
            if operation.operation in {"change_sound_assignment", "change_drum_kit", "adjust_role_level"} and (role or assignment_id):
                if self._assignment(context, role, assignment_id) is None:
                    blockers.append(f"palette assignment is unavailable for role {role or assignment_id!r}")
            plan_id = parameters.get("plan_id")
            if operation.operation in {"apply_semantic_processing", "replace_processing_plan"}:
                if parameters.get("plan") is None and plan_id not in processing_plans:
                    blockers.append(f"semantic processing plan {plan_id!r} is unavailable")
            if (
                operation.effect_control_id
                and operation.effect_control_id not in effect_controls
                and operation.operation_id not in injected_adapters
            ):
                blockers.append(f"semantic effect control is unavailable for {operation.operation_id!r}")
        if _context(context, "session_fingerprint") is None:
            warnings.append("A live session fingerprint will be captured immediately before the revision run.")
        has_handoff = any(item.operation == "create_playlist_handoff_delta" for item in plan.operations)
        if has_handoff and request.manual_handoff_allowance:
            warnings.append("Playlist placement remains a manual handoff and cannot be verified through FL Studio's public API.")
        return RevisionReadiness(
            state="blocked" if blockers else "ready_with_limitations" if warnings else "ready",
            source_run_available=_context(context, "source_run_available", True),
            source_evaluation_available=_context(context, "source_evaluation_available", True),
            target_fingerprints_match=_context(context, "target_fingerprints_match", True),
            stored_sequences_available=not any("NoteSequence" in item for item in blockers),
            palette_continuity=not any("palette assignment" in item for item in blockers),
            pattern_targets_available=_context(context, "pattern_targets_available", True),
            piano_roll_ready=_context(context, "piano_roll_ready", True),
            effects_ready=not any("effect control" in item or "processing plan" in item for item in blockers),
            manual_handoff_required=has_handoff,
            blockers=tuple(dict.fromkeys(blockers)),
            warnings=tuple(dict.fromkeys(warnings)),
            expected_export_action="Export the revised full mix with the same settings used for the before bounce.",
        )

    def preflight(self, plan: RevisionPlan, request: RevisionRequest, context: Any) -> RevisionReadiness:
        if self.readiness_checker is None:
            return self._default_preflight(plan, request, context)
        value = self._call_once(self.readiness_checker, (plan, request, context))
        if isinstance(value, RevisionReadiness):
            return value
        return RevisionReadiness.model_validate(value, strict=False)

    def adapt_operations(self, plan: RevisionPlan, request: RevisionRequest, context: Any) -> RevisionAdapterResult:
        """Translate review operations into existing Production Run operations."""

        del request
        mapped: list[Any] = []
        local: list[RevisionOperation] = []
        blockers: list[str] = []
        warnings: list[str] = []
        source_dependencies: dict[str, tuple[str, ...]] = {}
        injected = _context(context, "operation_adapters", {}) or {}
        for operation in plan.operations:
            operation_id = operation.operation_id
            source_dependencies[operation_id] = operation.after
            if operation_id in injected:
                adapter = injected[operation_id]
                try:
                    adapter = self._call_once(adapter, (operation, context)) if callable(adapter) else adapter
                except Exception as exc:
                    blockers.append(f"operation {operation_id!r} adapter failed: {type(exc).__name__}: {exc}")
                    continue
                if isinstance(adapter, RevisionAdapterResult):
                    mapped.extend(adapter.operations)
                    local.extend(adapter.local_operations)
                    blockers.extend(adapter.blockers)
                    warnings.extend(adapter.warnings)
                    continue
                if adapter is not None:
                    if isinstance(adapter, (tuple, list)):
                        mapped.extend(adapter)
                    else:
                        mapped.append(adapter)
                else:
                    blockers.append(
                        f"operation {operation_id!r} adapter returned no Production Run operation"
                    )
                continue
            if operation.operation in {"record_feedback_lock", "create_playlist_handoff_delta"}:
                local.append(operation)
                continue
            parameters = _parameters(operation)
            if operation.operation in {
                "transform_generated_sequence",
                "create_section_note_variation",
                "change_section_density",
                "change_section_register",
                "change_section_voicing",
                "change_section_rhythm",
                "change_section_velocity",
                "change_section_articulation",
            }:
                channel_index = parameters.get("channel_index")
                pattern_number = parameters.get("pattern_number")
                if not isinstance(channel_index, int) or not isinstance(pattern_number, int):
                    blockers.append(f"operation {operation_id!r} needs a channel and pattern target")
                    continue
                sequence = _stored_sequence(context, operation)
                if sequence is None:
                    blockers.append(
                        f"operation {operation_id!r} needs its stored PostFader NoteSequence; arbitrary Piano Roll notes are not available"
                    )
                    continue
                try:
                    revised = _transform_stored_sequence(sequence, operation)
                except (TypeError, ValueError) as exc:
                    blockers.append(
                        f"operation {operation_id!r} has an invalid stored-note transform: {exc}"
                    )
                    continue
                if revised.note_count < 1:
                    blockers.append(
                        f"operation {operation_id!r} produced no notes; an empty replacement was not dispatched"
                    )
                    continue
                mapped.append(
                    runs.WriteNoteSequenceOperation(
                        operation_id=operation_id,
                        sequence=revised,
                        channel_index=channel_index,
                        pattern_number=pattern_number,
                        mode="replace",
                        target_fingerprint=parameters.get("target_fingerprint"),
                    )
                )
                continue
            if operation.operation in {"change_sound_assignment", "change_drum_kit"}:
                role = operation.role_id or parameters.get("role_id")
                assignment = self._assignment(context, role, parameters.get("assignment_id"))
                target = _coerce_plugin_target(_target_for_assignment(assignment))
                preset_name = parameters.get("preset_name")
                preset_index = parameters.get("preset_index")
                if isinstance(assignment, Mapping):
                    preset_name = (
                        preset_name
                        or assignment.get("selected_preset")
                        or assignment.get("preset_name")
                    )
                    preset_index = (
                        preset_index
                        if preset_index is not None
                        else assignment.get(
                            "selected_preset_index",
                            assignment.get("preset_index"),
                        )
                    )
                else:
                    preset_name = (
                        preset_name
                        or getattr(assignment, "selected_preset", None)
                        or getattr(assignment, "preset_name", None)
                    )
                    preset_index = (
                        preset_index
                        if preset_index is not None
                        else getattr(
                            assignment,
                            "selected_preset_index",
                            getattr(assignment, "preset_index", None),
                        )
                    )
                if target is None or (preset_name is None and preset_index is None):
                    blockers.append(f"operation {operation_id!r} cannot resolve a palette target and preset")
                    continue
                fingerprint = parameters.get("target_fingerprint") or getattr(
                    assignment, "target_fingerprint", None
                )
                if isinstance(assignment, Mapping):
                    fingerprint = fingerprint or assignment.get("target_fingerprint")
                operation_type = runs.SelectDrumKitOperation if operation.operation == "change_drum_kit" else runs.SelectPluginPresetOperation
                kwargs = {"operation_id": operation_id, "target": target, "preset_name": preset_name, "preset_index": preset_index, "target_fingerprint": fingerprint}
                mapped.append(operation_type(**kwargs))
                continue
            if operation.operation == "adjust_channel_mix":
                channel_index = parameters.get("channel_index")
                if not isinstance(channel_index, int):
                    blockers.append(f"operation {operation_id!r} needs channel_index")
                    continue
                if all(parameters.get(name) is None for name in ("volume_normalized", "pan", "muted")):
                    blockers.append(f"operation {operation_id!r} needs a channel mix value")
                    continue
                mapped.append(runs.ApplyVerifiedBatchOperation(operation_id=operation_id, operations=(BatchChannelMix(operation_id=f"{operation_id}.mix", channel_index=channel_index, volume_normalized=parameters.get("volume_normalized"), pan=parameters.get("pan"), muted=parameters.get("muted")),)))
                continue
            if operation.operation == "adjust_role_level":
                role = operation.role_id or parameters.get("role_id")
                assignment = self._assignment(context, role, parameters.get("assignment_id"))
                target = _coerce_plugin_target(parameters.get("target") or _target_for_assignment(assignment))
                target_level = parameters.get("target_level_db", parameters.get("level_db"))
                if isinstance(target, MixerEffectTarget) and isinstance(target_level, (int, float)):
                    mapped.append(runs.ApplyVerifiedBatchOperation(operation_id=operation_id, operations=(BatchMixerVolumeDb(operation_id=f"{operation_id}.level", track_index=target.track_index, volume_db=target_level, allow_master=target.allow_master),)))
                else:
                    blockers.append(f"operation {operation_id!r} needs a resolved mixer target and level")
                continue
            if operation.operation in {"apply_semantic_processing", "replace_processing_plan"}:
                processing_plan = parameters.get("plan")
                if processing_plan is None:
                    processing_plan = (_context(context, "processing_plans", {}) or {}).get(parameters.get("plan_id"))
                if not isinstance(processing_plan, ProcessingPlan):
                    try:
                        processing_plan = ProcessingPlan.model_validate(processing_plan, strict=False) if isinstance(processing_plan, Mapping) else None
                    except Exception:
                        processing_plan = None
                if processing_plan is None:
                    blockers.append(f"operation {operation_id!r} cannot resolve a semantic ProcessingPlan")
                    continue
                mapped.append(runs.ApplyProcessingPlanOperation(operation_id=operation_id, plan=processing_plan))
                continue
            if operation.operation == "create_sound_palette_variation":
                section = parameters.get("section_id") or operation.section_id
                request_value = parameters.get("request") or (_context(context, "palette_requests", {}) or {}).get(section)
                if not isinstance(request_value, SoundSelectionRequest):
                    try:
                        request_value = SoundSelectionRequest.model_validate(request_value, strict=False) if isinstance(request_value, Mapping) else None
                    except Exception:
                        request_value = None
                if request_value is None or not isinstance(section, str):
                    blockers.append(f"operation {operation_id!r} cannot resolve the Sound Selection request")
                    continue
                mapped.append(runs.CreateSoundPaletteVariationOperation(operation_id=operation_id, palette=_context(context, "palette_id", None) or "source-palette", request=request_value, section=section, replace_roles=tuple(parameters.get("role_ids", ()))))
                continue
            if operation.operation == "update_section_markers":
                section = parameters.get("section_id") or operation.section_id
                start_bar = parameters.get("start_bar")
                end_bar = parameters.get("end_bar")
                if not isinstance(section, str) or not isinstance(start_bar, int) or not isinstance(end_bar, int) or end_bar < start_bar:
                    blockers.append(f"operation {operation_id!r} needs valid section marker bounds")
                    continue
                mapped.append(runs.AddSectionMarkersOperation(operation_id=operation_id, markers=(SectionMarker(name=section, bar_number=start_bar), SectionMarker(name=f"{section}_end", bar_number=end_bar))))
                continue
            blockers.append(f"operation {operation_id!r} requires a source-specific adapter or unsupported capability")
        mapped_ids = {item.operation_id for item in mapped}
        normalized: list[Any] = []
        for item in mapped:
            dependencies = tuple(dep for dep in source_dependencies.get(item.operation_id, getattr(item, "after", ())) if dep in mapped_ids)
            if dependencies != getattr(item, "after", ()):
                item = item.model_copy(update={"after": dependencies})
            normalized.append(item)
        return RevisionAdapterResult(tuple(normalized), tuple(local), tuple(dict.fromkeys(blockers)), tuple(dict.fromkeys(warnings)))

    @staticmethod
    def _production_request(
        request: RevisionRequest,
        operations: Sequence[Any],
        *,
        authorized_to_modify: bool | None = None,
        accepted_locks: Sequence[Any] = (),
        expected_session_fingerprint: str | None = None,
        expected_project_state_digest: str | None = None,
    ) -> runs.ProductionRunRequest:
        categories: list[str] = []
        for operation in operations:
            for category in runs._operation_categories(operation):
                if category not in categories:
                    categories.append(category)
        # Revision operation names are deliberately not passed through: the
        # Production Run request accepts its own closed ChangeCategory union.
        allowed = cast(
            tuple[runs.ChangeCategory, ...],
            tuple(categories) or ("composition",),
        )
        locks = tuple((*request.accepted_element_locks, *accepted_locks))
        active_locks = tuple(lock for lock in locks if not lock.released)
        preserve = runs.ProductionPreservation(
            # Production Runs only expose broad preservation switches.  The
            # review planner already enforces role/section lock granularity;
            # only an explicit overall lock is safe to project onto a global
            # Production Run preservation rule.
            note_content=any(
                lock.scope == "overall" and "note_content" in lock.lock_types
                for lock in active_locks
            ),
            arrangement=any(
                lock.scope == "overall"
                and "section_placement" in lock.lock_types
                for lock in active_locks
            ),
            sound_palette=any(
                lock.scope == "overall"
                and "sound_assignment" in lock.lock_types
                for lock in active_locks
            ),
            sound_roles=tuple(
                sorted(
                    {
                        lock.role_id
                        for lock in active_locks
                        if lock.scope == "overall"
                        and lock.role_id
                        and "sound_assignment" in lock.lock_types
                    }
                )
            ),
            named_elements=tuple(request.preserved_elements[:64]),
        )
        return runs.ProductionRunRequest(
            brief=request.requested_objective,
            scope=runs.ProductionScope(kind="whole_project", description="One bounded Review Session revision pass."),
            preserve=preserve,
            allowed_changes=allowed,
            completion_target="A verified bounded revision pass.",
            interaction_policy="execute_once",
            max_operations=max(1, min(runs.MAX_PRODUCTION_OPERATIONS, len(operations))),
            max_iterations=1,
            authorized_to_modify=request.authorized_to_modify if authorized_to_modify is None else authorized_to_modify,
            expected_session_fingerprint=expected_session_fingerprint,
            expected_project_state_digest=expected_project_state_digest,
        )

    @staticmethod
    def _result_receipts(result: Any, registry: Any) -> tuple[Any, ...]:
        receipts = result.get("receipts") if isinstance(result, Mapping) else getattr(result, "receipts", None)
        if receipts is not None:
            return tuple(receipts)
        run_id = result.get("run_id") if isinstance(result, Mapping) else getattr(result, "run_id", None)
        if run_id is None or not hasattr(registry, "get"):
            return ()
        try:
            lookup = registry.get(run_id)
            state = getattr(lookup, "state", None)
            return () if state is None else tuple(getattr(state, "receipts", ()))
        except Exception:
            return ()

    @staticmethod
    def _review_receipts(
        plan: RevisionPlan,
        receipts: Sequence[Any],
        local_operations: Sequence[RevisionOperation] = (),
    ) -> tuple[RevisionOperationReceipt, ...]:
        by_id = {item.operation_id: item for item in receipts if getattr(item, "operation_id", None)}
        local_by_id = {item.operation_id: item for item in local_operations}
        output: list[RevisionOperationReceipt] = []
        for operation in plan.operations[:MAX_RECEIPTS]:
            if operation.operation in _LOCAL_OPERATION_KINDS or operation.operation_id in local_by_id:
                is_lock = operation.operation == "record_feedback_lock"
                output.append(
                    RevisionOperationReceipt(
                        operation_id=operation.operation_id,
                        operation=operation.operation,
                        status="verified" if is_lock else "planned",
                        mutating=False,
                        outcome_known=True,
                        verified=is_lock,
                        finding_ids=operation.finding_ids,
                        warnings=(
                            ()
                            if is_lock
                            else (
                                "Playlist placement remains a manual handoff and is not verified by FL Studio.",
                            )
                        ),
                    )
                )
                continue
            source = by_id.get(operation.operation_id)
            status = getattr(source, "status", "skipped")
            if status == "generated":
                status = "skipped"
            if status not in {
                "verified",
                "unverified",
                "error_unknown",
                "skipped",
                "planned",
            }:
                status = "skipped"
            receipt_status = cast(
                Literal["planned", "verified", "unverified", "error_unknown", "skipped"],
                status,
            )
            output.append(
                RevisionOperationReceipt(
                    operation_id=operation.operation_id,
                    operation=operation.operation,
                    status=receipt_status,
                    mutating=operation.operation not in {"record_feedback_lock", "create_playlist_handoff_delta"},
                    outcome_known=bool(getattr(source, "outcome_known", False)),
                    verified=bool(getattr(source, "verified", False)),
                    finding_ids=operation.finding_ids,
                    error=getattr(source, "error", None),
                    warnings=tuple(getattr(source, "warnings", ())),
                )
            )
        return tuple(output)

    def _pass(
        self,
        plan: RevisionPlan,
        readiness: RevisionReadiness,
        started_at: datetime,
        *,
        status: str,
        blockers: Sequence[str] = (),
        warnings: Sequence[str] = (),
        result: Any = None,
        registry: Any = None,
        local: Sequence[RevisionOperation] = (),
        authorization_count: int = 0,
    ) -> RevisionPass:
        receipts = self._result_receipts(result, registry) if result is not None else ()
        result_blockers = _messages(result.get("blockers") if isinstance(result, Mapping) else getattr(result, "blockers", ()))
        result_warnings = _messages(result.get("warnings") if isinstance(result, Mapping) else getattr(result, "warnings", ()), field_name="message")
        all_blockers = tuple(dict.fromkeys((*readiness.blockers, *blockers, *result_blockers)))
        all_warnings = tuple(dict.fromkeys((*readiness.warnings, *warnings, *result_warnings)))
        result_run_id = result.get("run_id") if isinstance(result, Mapping) else getattr(result, "run_id", None)
        generated_outputs: list[ReviewGeneratedOutput] = []
        plan_operations = {item.operation_id: item for item in plan.operations}
        for item in (result.get("generated_outputs", ()) if isinstance(result, Mapping) else getattr(result, "generated_outputs", ())):
            output_id = getattr(item, "operation_id", None) or getattr(item, "output_id", None)
            if not output_id:
                continue
            source_operation = plan_operations.get(str(output_id))
            raw_kind = getattr(item, "output", None) or getattr(item, "output_kind", "pattern")
            output_kind = {
                "sound_palette": "palette_assignment",
                "selected_preset": "palette_assignment",
            }.get(raw_kind, raw_kind)
            if output_kind not in {"note_sequence", "palette_assignment", "processing_plan", "pattern", "handoff"}:
                output_kind = "pattern"
            value = getattr(item, "value", None)
            model_dump = getattr(value, "model_dump", None)
            if callable(model_dump):
                dumped_value = model_dump(mode="json", exclude_none=False)
                metadata_value: dict[str, object] = (
                    dict(dumped_value) if isinstance(dumped_value, Mapping) else {}
                )
            elif isinstance(value, Mapping):
                metadata_value = dict(value)
            else:
                metadata_value = {}
            output_digest = None
            for name in (
                "note_digest_sha256",
                "preset_identity_digest",
                "plan_digest",
                "request_digest",
            ):
                candidate = getattr(value, name, None)
                if isinstance(candidate, str) and len(candidate) == 64:
                    output_digest = candidate
                    break
            if output_digest is None and metadata_value:
                output_digest = _canonical_digest(metadata_value)
            role_id = getattr(item, "role_id", None)
            if role_id is None and source_operation is not None:
                role_id = source_operation.role_id
            generated_outputs.append(
                ReviewGeneratedOutput(
                    output_id=str(output_id),
                    output_kind=cast(
                        Literal[
                            "note_sequence",
                            "palette_assignment",
                            "processing_plan",
                            "pattern",
                            "handoff",
                        ],
                        output_kind,
                    ),
                    role_id=role_id,
                    section_id=(
                        None
                        if source_operation is None
                        else source_operation.section_id
                    ),
                    digest=output_digest,
                    metadata=FrozenMap(metadata_value),
                )
            )
        for item in local:
            if item.operation != "create_playlist_handoff_delta":
                continue
            placements = _playlist_placements(item)
            placement_payload = tuple(
                placement.model_dump(mode="json", exclude_none=False)
                for placement in placements
            )
            generated_outputs.append(
                ReviewGeneratedOutput(
                    output_id=item.operation_id,
                    output_kind="handoff",
                    section_id=item.section_id,
                    digest=_canonical_digest(placement_payload),
                    metadata=FrozenMap({"placements": placement_payload}),
                )
            )
        handoff_rows: list[ManualHandoff] = []
        for item in local:
            if item.operation != "create_playlist_handoff_delta":
                continue
            placements = _playlist_placements(item)
            handoff_rows.append(
                ManualHandoff(
                    action_id=item.operation_id,
                    instruction=(
                        "Apply the exact Playlist handoff row manually; placement is not exposed by FL Studio's public API."
                        if len(placements) == 1
                        else "Apply the exact Playlist handoff rows manually; placement is not exposed by FL Studio's public API."
                    ),
                    status="not_verifiable",
                    section_id=item.section_id,
                    pattern_id=placements[0].pattern_id if placements else None,
                    evidence=tuple(
                        f"{placement.pattern_name or placement.pattern_id or placement.pattern_number} -> "
                        f"Playlist track {placement.intended_playlist_track_number or 'unspecified'}, "
                        f"bars {placement.start_bar}-{placement.end_bar}"
                        for placement in placements
                    ),
                )
            )
        handoffs = tuple(handoff_rows)
        result_status = result.get("status") if isinstance(result, Mapping) else getattr(result, "status", None)
        if result is None:
            technical = None
        else:
            technical = EvaluationState(state="verified" if result_status == "completed" else "unknown", summary="Production Run execution outcome retained.")
        has_processing = any(item.operation in {"apply_semantic_processing", "replace_processing_plan"} for item in plan.operations)
        processing = technical if has_processing and result is not None else None
        arrangement = EvaluationState(state="not_evaluated", summary="Playlist placement requires a manual handoff.") if handoffs else None
        timing_value = result.get("timing_report") if isinstance(result, Mapping) else getattr(result, "timing_report", None)
        if timing_value is not None and hasattr(timing_value, "model_dump"):
            timing_value = timing_value.model_dump(mode="json")
        timing = {"status": status}
        if isinstance(timing_value, Mapping):
            timing.update(timing_value)
        roles = {str(item.role_id or _parameter(item, "role_id")) for item in plan.operations if item.role_id or _parameter(item, "role_id")}
        sections = {str(item.section_id or _parameter(item, "section_id")) for item in plan.operations if item.section_id or _parameter(item, "section_id")}
        write_enable = result.get("write_mode_enable_count", 0) if isinstance(result, Mapping) else getattr(result, "write_mode_enable_count", 0)
        write_disable = result.get("write_mode_disable_count", 0) if isinstance(result, Mapping) else getattr(result, "write_mode_disable_count", 0)
        shutdown_verified = result.get("write_mode_shutdown_verified") if isinstance(result, Mapping) else getattr(result, "write_mode_shutdown_verified", None)
        result_session = (
            result.get("session_fingerprint")
            if isinstance(result, Mapping)
            else getattr(result, "session_fingerprint", None)
        )
        result_project = (
            result.get("project_state_digest")
            if isinstance(result, Mapping)
            else getattr(result, "project_state_digest", None)
        )
        return RevisionPass(
            revision_pass_id=f"pass-{_digest_id(plan.revision_plan_id, started_at)}",
            review_session_id=plan.review_session_id,
            source_evaluation_id=plan.source_evaluation_id,
            revision_plan_id=plan.revision_plan_id,
            source_run_id=plan.source_run_id,
            source_production_run_id=result_run_id,
            continuation_run_id=result_run_id,
            session_fingerprint=result_session,
            project_state_digest=result_project,
            status=cast(
                Literal[
                    "created",
                    "preflight",
                    "revising",
                    "completed",
                    "awaiting_rebounce",
                    "blocked",
                    "stopped",
                ],
                status,
            ),
            started_at=started_at,
            finished_at=self.clock(),
            authorization_count=min(1, max(0, int(authorization_count))),
            write_mode_enable_count=min(1, max(0, int(write_enable or 0))),
            write_mode_disable_count=min(1, max(0, int(write_disable or 0))),
            shutdown_verified=shutdown_verified,
            preflight_result=FrozenMap(
                {
                    "state": readiness.state,
                    "blockers": all_blockers,
                    "warnings": all_warnings,
                }
            ),
            operation_receipts=self._review_receipts(plan, receipts, local),
            generated_outputs=tuple(generated_outputs),
            affected_roles=tuple(sorted(roles)),
            affected_sections=tuple(sorted(sections)),
            retained_anchors=tuple(lock.lock_id for lock in plan.protected_elements),
            manual_handoffs=handoffs,
            technical_outcome=technical,
            arrangement_outcome=arrangement,
            processing_outcome=processing,
            timing=FrozenMap(timing),
            warnings=all_warnings,
            blockers=all_blockers,
            after_bounce_state="awaiting" if status == "awaiting_rebounce" else "not_requested",
        )

    def apply(
        self,
        plan: RevisionPlan,
        request: RevisionRequest,
        context: Any,
        *,
        current_authorization: bool | None = None,
    ) -> RevisionPass:
        """Validate and execute one revision pass, with no mutation on failure."""

        started_at = self.clock()
        validation = validate_revision_plan(
            plan,
            request=request,
            source_review_session_id=_context(context, "review_session_id"),
            known_finding_ids=_context(context, "known_finding_ids", ()),
            known_feedback_ids=_context(context, "known_feedback_ids", ()),
            available_sequence_digests=_context(context, "sequence_digests", {}),
            source_note_sequence_digests=_context(context, "sequence_digests", {}),
            source_palette_assignments=tuple(
                (_context(context, "palette_assignments", {}) or {}).keys()
            ),
            available_effect_controls=tuple(
                (_context(context, "effect_controls", {}) or {}).keys()
            ),
            source_run_available=_context(context, "source_run_available", True),
            source_evaluation_available=_context(
                context, "source_evaluation_available", True
            ),
            completed_revision_operation_ids=_context(
                context, "completed_revision_operation_ids", ()
            ),
            completed_revision_operations=_context(
                context, "completed_revision_operations", 0
            ),
            completed_revision_passes=_context(context, "completed_revision_passes", 0),
            maximum_revision_passes=_context(context, "maximum_revision_passes"),
            maximum_revision_operations=_context(
                context, "maximum_revision_operations"
            ),
            source_palette_digest=_context(context, "palette_digest"),
            target_fingerprints=_context(context, "target_fingerprints", {}),
        )
        if not validation.valid:
            readiness = RevisionReadiness(state="blocked", blockers=validation.blockers, warnings=validation.warnings)
            return self._pass(plan, readiness, started_at, status="blocked")
        readiness = self.preflight(plan, request, context)
        if readiness.blockers:
            return self._pass(plan, readiness, started_at, status="blocked")
        authorized = request.authorized_to_modify if current_authorization is None else current_authorization
        if not authorized and any(
            item.operation not in _LOCAL_OPERATION_KINDS for item in plan.operations
        ):
            return self._pass(
                plan,
                readiness,
                started_at,
                status="blocked",
                blockers=("one task-scoped authorization is required to apply this revision",),
                authorization_count=0,
            )
        adapted = self.adapt_operations(plan, request, context)
        if adapted.blockers:
            return self._pass(
                plan,
                readiness,
                started_at,
                status="blocked",
                blockers=adapted.blockers,
                warnings=adapted.warnings,
                local=adapted.local_operations,
                authorization_count=1 if authorized else 0,
            )
        if not adapted.operations:
            return self._pass(
                plan,
                readiness,
                started_at,
                status="completed",
                warnings=adapted.warnings,
                local=adapted.local_operations,
                authorization_count=1 if authorized else 0,
            )

        expected_session = _context(context, "session_fingerprint")
        expected_project = _context(context, "project_state_digest")
        captured_session = expected_session
        captured_project = expected_project
        if self.session_capturer is not None:
            try:
                captured = self._call_once(self.session_capturer, (context,))
            except Exception as exc:
                return self._pass(
                    plan,
                    readiness,
                    started_at,
                    status="blocked",
                    blockers=(
                        f"could not capture the live FL Studio session before revision: {type(exc).__name__}: {exc}",
                    ),
                    local=adapted.local_operations,
                    authorization_count=1 if authorized else 0,
                )
            captured_session = captured.get("session_fingerprint") if isinstance(captured, Mapping) else getattr(captured, "session_fingerprint", captured)
            if isinstance(captured, Mapping):
                captured_project = captured.get("project_state_digest", expected_project)
            else:
                captured_project = getattr(captured, "project_state_digest", expected_project)
        if expected_session is not None and captured_session != expected_session:
            return self._pass(plan, readiness, started_at, status="blocked", blockers=("FL Studio session changed before revision execution",), local=adapted.local_operations, authorization_count=1)
        if expected_project is not None and captured_project != expected_project:
            return self._pass(plan, readiness, started_at, status="blocked", blockers=("project state changed before revision execution",), local=adapted.local_operations, authorization_count=1)
        try:
            production_request = self._production_request(
                request,
                adapted.operations,
                authorized_to_modify=bool(authorized),
                accepted_locks=plan.protected_elements,
                expected_session_fingerprint=expected_session,
                expected_project_state_digest=expected_project,
            )
            production_plan = runs.ProductionRunPlan(
                plan_id=f"review-{plan.revision_plan_id}",
                operations=tuple(adapted.operations),
            )
        except Exception as exc:
            return self._pass(
                plan,
                readiness,
                started_at,
                status="blocked",
                blockers=(
                    f"revision operations could not be adapted to a Production Run: {type(exc).__name__}: {exc}",
                ),
                local=adapted.local_operations,
                authorization_count=1 if authorized else 0,
            )
        registry = self._registry()
        try:
            result = self._call_once(self.run_executor, (production_request, production_plan)) if self.run_executor is not None else registry.execute(production_request, production_plan)
        except Exception as exc:
            return self._pass(plan, readiness, started_at, status="blocked", blockers=(f"revision execution stopped before a known result: {type(exc).__name__}: {exc}",), local=adapted.local_operations, authorization_count=1)
        result_status = result.get("status") if isinstance(result, Mapping) else getattr(result, "status", "blocked")
        status = "awaiting_rebounce" if result_status == "completed" else "stopped" if result_status == "stopped" else "blocked"
        return self._pass(plan, readiness, started_at, status=status, result=result, registry=registry, local=adapted.local_operations, authorization_count=1)

    execute = apply


def apply_revision(
    plan: RevisionPlan,
    request: RevisionRequest,
    context: Any,
    *,
    run_registry: Any = None,
    readiness_checker: Callable[..., Any] | None = None,
    session_capturer: Callable[..., Any] | None = None,
    run_executor: Callable[..., Any] | None = None,
    current_authorization: bool | None = None,
) -> RevisionPass:
    """Convenience wrapper for one bounded revision pass."""

    return RevisionExecutor(
        run_registry=run_registry,
        readiness_checker=readiness_checker,
        session_capturer=session_capturer,
        run_executor=run_executor,
    ).apply(plan, request, context, current_authorization=current_authorization)


__all__ = [
    "RevisionAdapterResult",
    "RevisionExecutionContext",
    "RevisionExecutor",
    "RevisionReadiness",
    "apply_revision",
]
