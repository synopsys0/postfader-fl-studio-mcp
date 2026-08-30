"""Task-scoped orchestration for bounded, truthful production workflows.

The connected MCP client remains the creative planner.  This module accepts a
closed typed plan, validates every dependency and project boundary before the
first mutation, then adapts its operations to PostFader's existing creative
and verified-write implementations.  Runs are ordered and non-atomic: an
ambiguous or unverified mutation stops immediately, earlier receipts remain
truthful, and no retry, rollback, render, save, or unsupported FL operation is
invented.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import ConfigDict, Field, StrictBool, model_validator

from .contracts import SCHEMA_VERSION, ConnectionInfo, ContractModel, ProjectSummary
from .creative import (
    MAX_PIANO_ROLL_NOTES,
    PIANO_ROLL,
    ArrangementMarkerReceipt,
    AutomationRecordReceipt,
    NoteSequence,
    PatternPreparation,
    PianoRollDispatch,
    PianoRollTransform,
    SectionMarker,
    add_section_markers,
    compose_bassline,
    compose_chord_progression,
    compose_drums,
    compose_melody,
    prepare_empty_pattern,
    record_automation_value,
    transform_piano_roll,
    write_piano_roll_notes,
)
from .performance import TrackBController, TrackBInspector
from .readonly_inspector import ReadOnlyInspector
from .track_b_contracts import (
    SESSION_FINGERPRINT_PATTERN,
    TrackBVerifiedMutation,
    VerifiedPatternSelectionWrite,
)
from .verified_writer import WriteModeManager
from .workflows import (
    MAX_BATCH_OPERATIONS,
    BatchOperation,
    VerifiedBatchExecutor,
    VerifiedBatchResult,
    validate_batch_operations,
)


MAX_PRODUCTION_OPERATIONS = 64
MAX_PRODUCTION_ITERATIONS = 16
MAX_PRODUCTION_RUNS = 64
MAX_RUN_TEXT = 1024
MAX_TARGETS = 64
MAX_VALIDATION_BLOCKERS = 64
MAX_RUN_BLOCKERS = MAX_VALIDATION_BLOCKERS + MAX_PRODUCTION_ITERATIONS
MAX_RUN_WARNINGS = 32

InteractionPolicy: TypeAlias = Literal[
    "plan_only", "execute_once", "execute_until_blocked"
]
RunStatus: TypeAlias = Literal[
    "created", "validated", "running", "blocked", "completed", "failed", "stopped"
]
ChangeCategory: TypeAlias = Literal[
    "composition",
    "notes",
    "pattern_metadata",
    "arrangement",
    "automation",
    "mixer",
    "channels",
    "playlist_metadata",
    "tempo",
    "routing",
    "plugin_parameters",
]
TargetKind: TypeAlias = Literal["mixer_track", "channel", "pattern", "playlist_track"]
BlockerCategory: TypeAlias = Literal[
    "malformed_plan",
    "unsupported_by_postfader",
    "unsupported_by_fl_studio",
    "unavailable_in_project",
    "setup_or_session",
    "authorization",
    "scope",
    "unverified_mutation",
    "unknown_outcome",
    "stopped",
    "iteration_limit",
]
PitchClass: TypeAlias = Annotated[int, Field(ge=0, le=11)]
PitchInterval: TypeAlias = Annotated[int, Field(ge=0, le=11)]
RootName: TypeAlias = Annotated[str, Field(min_length=1, max_length=8)]
RootValue: TypeAlias = PitchClass | RootName
ChordToken: TypeAlias = Annotated[str, Field(min_length=1, max_length=16)]
GeneratorSeed: TypeAlias = Annotated[
    int,
    Field(ge=-(2**31), le=2**31 - 1),
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProductionRunModel(ContractModel):
    """Immutable base for plans, state, reports, and receipts."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ProductionTarget(ProductionRunModel):
    """A bounded project target used by scope and preservation rules."""

    kind: TargetKind
    index: int | None = Field(default=None, ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def require_address(self) -> "ProductionTarget":
        if self.index is None and self.name is None:
            raise ValueError("a production target needs an index and/or name")
        if self.kind in {"pattern", "playlist_track"} and self.index == 0:
            raise ValueError(f"{self.kind} indices start at 1")
        return self


class ProductionScope(ProductionRunModel):
    """The portion of the live project this request may change."""

    kind: Literal[
        "whole_project",
        "mix_only",
        "arrangement_only",
        "note_content_only",
        "selected_targets",
    ] = "whole_project"
    description: str = Field(min_length=1, max_length=MAX_RUN_TEXT)
    targets: tuple[ProductionTarget, ...] = Field(
        default_factory=tuple, max_length=MAX_TARGETS
    )
    additional_allowed_changes: tuple[ChangeCategory, ...] = Field(
        default_factory=tuple, max_length=11
    )

    @model_validator(mode="after")
    def require_selected_targets(self) -> "ProductionScope":
        if self.kind == "selected_targets" and not self.targets:
            raise ValueError("selected_targets scope needs at least one target")
        if len(set(self.additional_allowed_changes)) != len(
            self.additional_allowed_changes
        ):
            raise ValueError("additional_allowed_changes must not contain duplicates")
        return self


class ProductionPreservation(ProductionRunModel):
    """Project elements that the submitted operations must leave unchanged."""

    tempo: StrictBool = False
    note_content: StrictBool = False
    arrangement: StrictBool = False
    mixer_state: StrictBool = False
    pattern_identity: StrictBool = False
    targets: tuple[ProductionTarget, ...] = Field(
        default_factory=tuple, max_length=MAX_TARGETS
    )
    named_elements: tuple[str, ...] = Field(default_factory=tuple, max_length=64)

    @model_validator(mode="after")
    def validate_named_elements(self) -> "ProductionPreservation":
        if any(not item.strip() or len(item) > 128 for item in self.named_elements):
            raise ValueError(
                "preserve.named_elements must contain non-empty text up to 128 characters"
            )
        return self


class CreativeDirection(ProductionRunModel):
    genre: str | None = Field(default=None, min_length=1, max_length=128)
    mood: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    references: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    energy: str | None = Field(default=None, min_length=1, max_length=256)
    arrangement_direction: str | None = Field(
        default=None, min_length=1, max_length=512
    )
    production_notes: str | None = Field(
        default=None, min_length=1, max_length=MAX_RUN_TEXT
    )

    @model_validator(mode="after")
    def validate_short_lists(self) -> "CreativeDirection":
        for label, values, limit in (
            ("mood", self.mood, 64),
            ("references", self.references, 256),
        ):
            if any(not item.strip() or len(item) > limit for item in values):
                raise ValueError(
                    f"creative_direction.{label} entries must contain 1..{limit} characters"
                )
        return self


class ProductionRunRequest(ProductionRunModel):
    """One connected-AI interpretation of the present user's request."""

    brief: str = Field(min_length=1, max_length=MAX_RUN_TEXT)
    scope: ProductionScope
    preserve: ProductionPreservation = Field(default_factory=ProductionPreservation)
    allowed_changes: tuple[ChangeCategory, ...] = Field(min_length=1, max_length=11)
    creative_direction: CreativeDirection = Field(default_factory=CreativeDirection)
    completion_target: str = Field(min_length=1, max_length=MAX_RUN_TEXT)
    interaction_policy: InteractionPolicy = "execute_once"
    max_operations: int = Field(default=32, ge=1, le=MAX_PRODUCTION_OPERATIONS)
    max_iterations: int = Field(default=4, ge=1, le=MAX_PRODUCTION_ITERATIONS)
    authorized_to_modify: StrictBool = False

    @model_validator(mode="after")
    def validate_allowed_changes(self) -> "ProductionRunRequest":
        if len(set(self.allowed_changes)) != len(self.allowed_changes):
            raise ValueError("allowed_changes must not contain duplicates")
        return self


class OperationOutputReference(ProductionRunModel):
    """Opaque reference to one typed output from an earlier operation."""

    reference: Literal["operation_output"] = "operation_output"
    operation_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    output: Literal["note_sequence"] = "note_sequence"


class ProductionOperationBase(ProductionRunModel):
    operation_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    operation: str
    after: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    allow_preserved_targets: tuple[ProductionTarget, ...] = Field(
        default_factory=tuple, max_length=MAX_TARGETS
    )

    @model_validator(mode="after")
    def validate_dependencies(self) -> "ProductionOperationBase":
        if len(set(self.after)) != len(self.after):
            raise ValueError("operation dependencies must not contain duplicates")
        for dependency in self.after:
            if not dependency or len(dependency) > 64:
                raise ValueError("operation dependencies must contain 1..64 characters")
        return self


class GenerateChordProgressionOperation(ProductionOperationBase):
    operation: Literal["generate_chord_progression"] = "generate_chord_progression"
    progression: tuple[ChordToken, ...] = Field(min_length=1, max_length=64)
    root: RootValue = "C"
    collection: str = Field(default="major", min_length=1, max_length=64)
    custom_intervals: tuple[PitchInterval, ...] | None = Field(
        default=None, max_length=12
    )
    beats_per_chord: float = Field(default=4.0, ge=0.125, le=32.0)
    octave: int = Field(default=4, ge=0, le=8)
    voicing: Literal["close", "open", "drop2"] = "close"
    velocity: float = Field(default=0.78, ge=0.0, le=1.0)
    tempo_bpm: float = Field(default=120.0, ge=10.0, le=522.0)


class GenerateMelodyOperation(ProductionOperationBase):
    operation: Literal["generate_melody"] = "generate_melody"
    root: RootValue = "C"
    collection: str = Field(default="major", min_length=1, max_length=64)
    custom_intervals: tuple[PitchInterval, ...] | None = Field(
        default=None, max_length=12
    )
    bars: int = Field(default=4, ge=1, le=64)
    beats_per_bar: int = Field(default=4, ge=1, le=16)
    density: float = Field(default=0.65, ge=0.05, le=1.0)
    register_low: int = Field(default=60, ge=0, le=130)
    register_high: int = Field(default=84, ge=1, le=131)
    contour: Literal["balanced", "rising", "falling", "arch", "wave"] = "balanced"
    seed: GeneratorSeed = 0
    tempo_bpm: float = Field(default=120.0, ge=10.0, le=522.0)

    @model_validator(mode="after")
    def validate_register(self) -> "GenerateMelodyOperation":
        if self.register_low >= self.register_high:
            raise ValueError("melody register_low must be below register_high")
        return self


class GenerateBasslineOperation(ProductionOperationBase):
    operation: Literal["generate_bassline"] = "generate_bassline"
    progression: tuple[ChordToken, ...] = Field(min_length=1, max_length=64)
    root: RootValue = "C"
    collection: str = Field(default="major", min_length=1, max_length=64)
    custom_intervals: tuple[PitchInterval, ...] | None = Field(
        default=None, max_length=12
    )
    beats_per_chord: float = Field(default=4.0, ge=0.5, le=32.0)
    octave: int = Field(default=2, ge=0, le=7)
    style: Literal["roots", "eighths", "octaves", "walking"] = "roots"
    seed: GeneratorSeed = 0
    tempo_bpm: float = Field(default=120.0, ge=10.0, le=522.0)


class GenerateDrumsOperation(ProductionOperationBase):
    operation: Literal["generate_drums"] = "generate_drums"
    style: Literal["house", "hiphop", "trap", "pop", "dnb"] = "house"
    bars: int = Field(default=4, ge=1, le=64)
    beats_per_bar: int = Field(default=4, ge=1, le=16)
    seed: GeneratorSeed = 0
    swing: float = Field(default=0.0, ge=0.0, le=0.49)
    tempo_bpm: float = Field(default=120.0, ge=10.0, le=522.0)


class PreparePatternOperation(ProductionOperationBase):
    operation: Literal["prepare_pattern"] = "prepare_pattern"
    pattern_number: int = Field(ge=1, le=999)
    name: str = Field(min_length=1, max_length=64)
    length_beats: int = Field(default=16, ge=1, le=4096)
    color: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)


class SelectPatternOperation(ProductionOperationBase):
    operation: Literal["select_pattern"] = "select_pattern"
    pattern_number: int = Field(ge=1, le=999)


class WriteNoteSequenceOperation(ProductionOperationBase):
    operation: Literal["write_note_sequence"] = "write_note_sequence"
    sequence: NoteSequence | OperationOutputReference
    channel_index: int = Field(ge=0)
    pattern_number: int = Field(ge=1, le=999)
    mode: Literal["append", "replace"] = "append"


class TransformPianoRollOperation(ProductionOperationBase):
    operation: Literal["transform_piano_roll"] = "transform_piano_roll"
    transform: PianoRollTransform
    channel_index: int = Field(ge=0)
    pattern_number: int = Field(ge=1, le=999)


class AddSectionMarkersOperation(ProductionOperationBase):
    operation: Literal["add_section_markers"] = "add_section_markers"
    markers: tuple[SectionMarker, ...] = Field(min_length=1, max_length=32)


class RecordAutomationValueOperation(ProductionOperationBase):
    operation: Literal["record_automation_value"] = "record_automation_value"
    target_kind: Literal["mixer", "channel"]
    target_index: int = Field(ge=0)
    property: Literal["volume", "pan", "stereo_separation"]
    value_normalized: float = Field(ge=0.0, le=1.0)
    allow_master: StrictBool = False
    expected_before: float | None = Field(default=None, ge=0.0, le=1.0)


class ApplyVerifiedBatchOperation(ProductionOperationBase):
    operation: Literal["apply_verified_batch"] = "apply_verified_batch"
    operations: tuple[BatchOperation, ...] = Field(
        min_length=1, max_length=MAX_BATCH_OPERATIONS
    )


class UnavailableProductionOperation(ProductionOperationBase):
    """Closed capability probes that Production Runs must reject before mutation."""

    operation: Literal[
        "create_playlist_clip",
        "render_project",
        "save_project",
        "insert_plugin",
    ]


ProductionOperation = Annotated[
    GenerateChordProgressionOperation
    | GenerateMelodyOperation
    | GenerateBasslineOperation
    | GenerateDrumsOperation
    | PreparePatternOperation
    | SelectPatternOperation
    | WriteNoteSequenceOperation
    | TransformPianoRollOperation
    | AddSectionMarkersOperation
    | RecordAutomationValueOperation
    | ApplyVerifiedBatchOperation
    | UnavailableProductionOperation,
    Field(discriminator="operation"),
]


class ProductionRunPlan(ProductionRunModel):
    """One bounded ordered plan from the connected creative planner."""

    plan_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    operations: tuple[ProductionOperation, ...] = Field(
        min_length=1, max_length=MAX_PRODUCTION_OPERATIONS
    )


ProductionResultPayload = (
    NoteSequence
    | PatternPreparation
    | VerifiedPatternSelectionWrite
    | PianoRollDispatch
    | ArrangementMarkerReceipt
    | AutomationRecordReceipt
    | VerifiedBatchResult
)


class ProductionOperationReceipt(ProductionRunModel):
    operation_index: int = Field(ge=0, le=MAX_PRODUCTION_OPERATIONS - 1)
    operation_id: str = Field(min_length=1, max_length=64)
    operation: str = Field(min_length=1, max_length=64)
    status: Literal["generated", "verified", "unverified", "error_unknown"]
    mutating: bool
    outcome_known: bool
    verified: bool
    result: ProductionResultPayload | None = None
    error: str | None = Field(default=None, max_length=2048)
    automatic_replay_attempted: Literal[False] = False
    rollback_attempted: Literal[False] = False
    project_saved: Literal[False] = False

    @model_validator(mode="after")
    def validate_status(self) -> "ProductionOperationReceipt":
        if self.status == "generated":
            if self.mutating or not self.outcome_known or not self.verified:
                raise ValueError(
                    "generated receipts must be known, verified, and read-only"
                )
            if not isinstance(self.result, NoteSequence):
                raise ValueError("generated receipts need a NoteSequence result")
        elif self.status == "verified":
            if not self.mutating or not self.outcome_known or not self.verified:
                raise ValueError("verified receipts must describe a verified mutation")
            if self.result is None:
                raise ValueError("verified receipts need a typed result")
        elif self.status == "unverified":
            if not self.mutating or self.verified or self.result is None:
                raise ValueError("unverified receipts need a typed mutation result")
        elif (
            self.outcome_known
            or self.verified
            or self.result is not None
            or not self.error
        ):
            raise ValueError("error_unknown receipts need only a bounded error")
        return self


class ProductionGeneratedOutput(ProductionRunModel):
    operation_id: str = Field(min_length=1, max_length=64)
    output: Literal["note_sequence"] = "note_sequence"
    value: NoteSequence


class ProductionBlocker(ProductionRunModel):
    category: BlockerCategory
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=1024)
    operation_id: str | None = Field(default=None, min_length=1, max_length=64)
    evidence: tuple[str, ...] = Field(default_factory=tuple, max_length=16)

    @model_validator(mode="after")
    def validate_evidence(self) -> "ProductionBlocker":
        if any(not item or len(item) > 512 for item in self.evidence):
            raise ValueError("blocker evidence entries must contain 1..512 characters")
        return self


class ProductionRunValidation(ProductionRunModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    validated_at: datetime
    valid: bool
    executable: bool
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolved_operation_order: tuple[str, ...] = Field(
        max_length=MAX_PRODUCTION_OPERATIONS
    )
    required_capabilities: tuple[str, ...] = Field(max_length=16)
    unsupported_operations: tuple[str, ...] = Field(
        default_factory=tuple, max_length=MAX_PRODUCTION_OPERATIONS
    )
    expected_mutation_categories: tuple[ChangeCategory, ...] = Field(max_length=11)
    session_fingerprint: str | None = Field(
        default=None, pattern=SESSION_FINGERPRINT_PATTERN
    )
    project_state_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    blockers: tuple[ProductionBlocker, ...] = Field(
        default_factory=tuple, max_length=MAX_VALIDATION_BLOCKERS
    )
    warnings: tuple[str, ...] = Field(
        default_factory=tuple, max_length=MAX_RUN_WARNINGS
    )


class ProductionRunState(ProductionRunModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request: ProductionRunRequest
    plan_id: str = Field(min_length=1, max_length=64)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    session_fingerprint: str | None = Field(
        default=None, pattern=SESSION_FINGERPRINT_PATTERN
    )
    project_state_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    iteration: int = Field(ge=1, le=MAX_PRODUCTION_ITERATIONS)
    current_operation_index: int = Field(ge=0, le=MAX_PRODUCTION_OPERATIONS)
    total_operations: int = Field(ge=0, le=MAX_PRODUCTION_OPERATIONS)
    completed_operations: tuple[str, ...] = Field(
        default_factory=tuple, max_length=MAX_PRODUCTION_OPERATIONS
    )
    receipts: tuple[ProductionOperationReceipt, ...] = Field(
        default_factory=tuple, max_length=MAX_PRODUCTION_OPERATIONS
    )
    generated_outputs: tuple[ProductionGeneratedOutput, ...] = Field(
        default_factory=tuple, max_length=MAX_PRODUCTION_OPERATIONS
    )
    blockers: tuple[ProductionBlocker, ...] = Field(
        default_factory=tuple, max_length=MAX_RUN_BLOCKERS
    )
    warnings: tuple[str, ...] = Field(
        default_factory=tuple, max_length=MAX_RUN_WARNINGS
    )
    final_summary: str | None = Field(default=None, max_length=2048)
    write_mode_enabled_once: bool = False
    automatic_replay_attempted: Literal[False] = False
    rollback_attempted: Literal[False] = False
    project_saved: Literal[False] = False
    process_local: Literal[True] = True

    @model_validator(mode="after")
    def validate_progress(self) -> "ProductionRunState":
        if self.current_operation_index > self.total_operations:
            raise ValueError("current_operation_index exceeds total_operations")
        receipt_ids = [item.operation_id for item in self.receipts]
        if len(receipt_ids) != len(set(receipt_ids)):
            raise ValueError("operation receipts must not be rewritten or duplicated")
        if not set(self.completed_operations).issubset(receipt_ids):
            raise ValueError("completed operations must have receipts")
        output_ids = [item.operation_id for item in self.generated_outputs]
        if len(output_ids) != len(set(output_ids)):
            raise ValueError("generated outputs must have unique operation IDs")
        return self


class ProductionRunResult(ProductionRunModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: RunStatus
    completed_count: int = Field(ge=0, le=MAX_PRODUCTION_OPERATIONS)
    attempted_count: int = Field(ge=0, le=MAX_PRODUCTION_OPERATIONS)
    total_operations: int = Field(ge=0, le=MAX_PRODUCTION_OPERATIONS)
    summary: str = Field(min_length=1, max_length=2048)
    blockers: tuple[ProductionBlocker, ...] = Field(
        default_factory=tuple, max_length=MAX_RUN_BLOCKERS
    )
    warnings: tuple[str, ...] = Field(
        default_factory=tuple, max_length=MAX_RUN_WARNINGS
    )
    rollback_attempted: Literal[False] = False
    project_saved: Literal[False] = False


class ProductionRunLookup(ProductionRunModel):
    found: bool
    process_local: Literal[True] = True
    message: str = Field(min_length=1, max_length=512)
    state: ProductionRunState | None = None

    @model_validator(mode="after")
    def validate_lookup(self) -> "ProductionRunLookup":
        if self.found != (self.state is not None):
            raise ValueError("found must match whether state is present")
        return self


class ProductionRunDelta(ProductionRunModel):
    mode: Literal["append", "replace_remaining"] = "replace_remaining"
    operations: tuple[ProductionOperation, ...] = Field(
        default_factory=tuple, max_length=MAX_PRODUCTION_OPERATIONS
    )
    request: ProductionRunRequest | None = None

    @model_validator(mode="after")
    def require_append_operations(self) -> "ProductionRunDelta":
        if self.mode == "append" and not self.operations:
            raise ValueError("append continuation needs at least one operation")
        return self


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------


_GENERATOR_TYPES = (
    GenerateChordProgressionOperation,
    GenerateMelodyOperation,
    GenerateBasslineOperation,
    GenerateDrumsOperation,
)
_PIANO_ROLL_TYPES = (WriteNoteSequenceOperation, TransformPianoRollOperation)

_SCOPE_BASE_CHANGES: dict[str, set[ChangeCategory]] = {
    "whole_project": {
        "composition",
        "notes",
        "pattern_metadata",
        "arrangement",
        "automation",
        "mixer",
        "channels",
        "playlist_metadata",
        "tempo",
        "routing",
        "plugin_parameters",
    },
    "mix_only": {
        "mixer",
        "channels",
        "automation",
        "routing",
        "plugin_parameters",
    },
    "arrangement_only": {
        "arrangement",
        "pattern_metadata",
        "playlist_metadata",
    },
    "note_content_only": {"composition", "notes", "pattern_metadata"},
    "selected_targets": {
        "composition",
        "notes",
        "pattern_metadata",
        "automation",
        "mixer",
        "channels",
        "playlist_metadata",
        "routing",
        "plugin_parameters",
    },
}

_BATCH_CATEGORY: dict[str, tuple[ChangeCategory, ...]] = {
    "mixer_volume": ("mixer",),
    "mixer_volume_db": ("mixer",),
    "mixer_pan": ("mixer",),
    "mixer_mute": ("mixer",),
    "mixer_solo": ("mixer",),
    "mixer_arm": ("mixer",),
    "mixer_color": ("mixer",),
    "mixer_stereo_separation": ("mixer",),
    "mixer_name": ("mixer",),
    "mixer_send": ("routing",),
    "mixer_send_level": ("routing",),
    "mixer_eq": ("mixer",),
    "plugin_parameter": ("plugin_parameters",),
    "channel_mix": ("channels",),
    "channel_solo": ("channels",),
    "channel_pitch": ("channels", "notes"),
    "channel_identity": ("channels",),
    "channel_route": ("routing",),
    "pattern_identity": ("pattern_metadata",),
    "pattern_length": ("pattern_metadata",),
    "playlist_identity": ("playlist_metadata",),
    "playlist_state": ("playlist_metadata",),
    "tempo": ("tempo",),
}

_UNAVAILABLE_OPERATION_MESSAGES: dict[str, str] = {
    "create_playlist_clip": (
        "This run is blocked because FL Studio does not currently expose Playlist "
        "clip creation to PostFader."
    ),
    "render_project": (
        "This run is blocked because FL Studio does not expose project rendering "
        "through its controller API. Export a bounce from FL Studio instead."
    ),
    "save_project": (
        "This run is blocked because PostFader does not save FL Studio projects. "
        "Save the project in FL Studio when you are ready."
    ),
    "insert_plugin": (
        "This run is blocked because FL Studio does not currently expose plug-in "
        "insertion to PostFader. It can control supported plug-ins already loaded."
    ),
}


def _generate_sequence(
    operation: GenerateChordProgressionOperation
    | GenerateMelodyOperation
    | GenerateBasslineOperation
    | GenerateDrumsOperation,
) -> NoteSequence:
    """Adapt a generator operation to the existing deterministic creative engine."""

    if isinstance(operation, GenerateChordProgressionOperation):
        return compose_chord_progression(
            operation.progression,
            root=operation.root,
            collection=operation.collection,
            custom_intervals=operation.custom_intervals,
            beats_per_chord=operation.beats_per_chord,
            octave=operation.octave,
            voicing=operation.voicing,
            velocity=operation.velocity,
            tempo_bpm=operation.tempo_bpm,
        )
    if isinstance(operation, GenerateMelodyOperation):
        return compose_melody(
            root=operation.root,
            collection=operation.collection,
            custom_intervals=operation.custom_intervals,
            bars=operation.bars,
            beats_per_bar=operation.beats_per_bar,
            density=operation.density,
            register_low=operation.register_low,
            register_high=operation.register_high,
            contour=operation.contour,
            seed=operation.seed,
            tempo_bpm=operation.tempo_bpm,
        )
    if isinstance(operation, GenerateBasslineOperation):
        return compose_bassline(
            operation.progression,
            root=operation.root,
            collection=operation.collection,
            custom_intervals=operation.custom_intervals,
            beats_per_chord=operation.beats_per_chord,
            octave=operation.octave,
            style=operation.style,
            seed=operation.seed,
            tempo_bpm=operation.tempo_bpm,
        )
    return compose_drums(
        style=operation.style,
        bars=operation.bars,
        beats_per_bar=operation.beats_per_bar,
        seed=operation.seed,
        swing=operation.swing,
        tempo_bpm=operation.tempo_bpm,
    )


def production_plan_digest(plan: ProductionRunPlan) -> str:
    """Return the canonical digest used by validation, state, and continuation."""

    payload = plan.model_dump(mode="json", exclude_none=False)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _project_state_digest(project: ProjectSummary) -> str:
    """Digest the stable project-summary evidence FL currently exposes.

    This is deliberately not described as a full project-content hash. It
    catches session-local changes visible through project metadata, counts,
    tempo, transport, dirty state, and undo-history coordinates.
    """

    payload = {
        "project_title": project.project_title,
        "project_author": project.project_author,
        "project_genre": project.project_genre,
        "tempo_bpm": project.tempo_bpm,
        "ppq": project.ppq,
        "mixer_track_count": project.mixer_track_count,
        "channel_count": project.channel_count,
        "pattern_count": project.pattern_count,
        "playlist_track_count": project.playlist_track_count,
        "dirty_flag": project.dirty_flag,
        "undo_history_position": project.undo_history_position,
        "undo_history_count": project.undo_history_count,
        "transport": {
            "playing": project.transport.playing,
            "recording": project.transport.recording,
            "metronome_enabled": project.transport.metronome_enabled,
            "precount_enabled": project.transport.precount_enabled,
            "time_signature_numerator": project.transport.time_signature_numerator,
            "tempo_bpm": project.transport.tempo_bpm,
            "song_length_ms": project.transport.song_length_ms,
            "loop_mode": project.transport.loop_mode,
        },
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _is_mutating(operation: ProductionOperation) -> bool:
    return not isinstance(operation, _GENERATOR_TYPES)


def _output_type(operation: ProductionOperation) -> str | None:
    if isinstance(operation, _GENERATOR_TYPES):
        return "note_sequence"
    if isinstance(operation, PreparePatternOperation):
        return "pattern_preparation"
    if isinstance(operation, SelectPatternOperation):
        return "pattern_selection"
    if isinstance(operation, _PIANO_ROLL_TYPES):
        return "piano_roll_dispatch"
    if isinstance(operation, AddSectionMarkersOperation):
        return "arrangement_marker_receipt"
    if isinstance(operation, RecordAutomationValueOperation):
        return "automation_record_receipt"
    if isinstance(operation, ApplyVerifiedBatchOperation):
        return "verified_batch_receipt"
    if isinstance(operation, UnavailableProductionOperation):
        return None
    raise AssertionError("unhandled production operation")


def _operation_categories(
    operation: ProductionOperation,
) -> tuple[ChangeCategory, ...]:
    if isinstance(operation, _GENERATOR_TYPES):
        return ("composition",)
    if isinstance(operation, (PreparePatternOperation, SelectPatternOperation)):
        return ("pattern_metadata",)
    if isinstance(operation, _PIANO_ROLL_TYPES):
        return ("notes",)
    if isinstance(operation, AddSectionMarkersOperation):
        return ("arrangement",)
    if isinstance(operation, RecordAutomationValueOperation):
        return ("automation",)
    if isinstance(operation, ApplyVerifiedBatchOperation):
        categories: list[ChangeCategory] = []
        for item in operation.operations:
            for category in _BATCH_CATEGORY[item.operation]:
                if category not in categories:
                    categories.append(category)
        return tuple(categories)
    if isinstance(operation, UnavailableProductionOperation):
        return ()
    raise AssertionError("unhandled production operation")


def _required_capabilities(
    operations: tuple[ProductionOperation, ...], start_index: int
) -> tuple[str, ...]:
    capabilities: list[str] = []

    def add(value: str) -> None:
        if value not in capabilities:
            capabilities.append(value)

    for operation in operations[start_index:]:
        if isinstance(operation, UnavailableProductionOperation):
            continue
        if isinstance(operation, _GENERATOR_TYPES):
            add("host_side_creative_generation")
            continue
        add("compatible_live_fl_bridge")
        add("runtime_write_mode_control")
        add("session_fingerprint_preconditions")
        if isinstance(operation, _PIANO_ROLL_TYPES):
            add("armed_piano_roll_script_bridge")
        elif isinstance(operation, (PreparePatternOperation, SelectPatternOperation)):
            add("pattern_selection_and_metadata")
        elif isinstance(operation, AddSectionMarkersOperation):
            add("arrangement_section_markers")
        elif isinstance(operation, RecordAutomationValueOperation):
            add("public_rec_event_automation")
        elif isinstance(operation, ApplyVerifiedBatchOperation):
            add("closed_verified_batch_writes")
    return tuple(capabilities)


def _target_key(target: ProductionTarget) -> tuple[str, int | None, str | None]:
    return (
        target.kind,
        target.index,
        None if target.name is None else target.name.casefold(),
    )


def _batch_targets(item: BatchOperation) -> list[ProductionTarget]:
    name = item.operation
    raw = cast(Any, item)
    targets: list[ProductionTarget] = []
    if name.startswith("mixer_"):
        targets.append(
            ProductionTarget(kind="mixer_track", index=cast(int, raw.track_index))
        )
        if name in {"mixer_send", "mixer_send_level"}:
            targets.append(
                ProductionTarget(
                    kind="mixer_track",
                    index=cast(int, raw.destination_track_index),
                )
            )
    elif name == "plugin_parameter":
        target = raw.target
        if target.kind == "mixer_effect":
            targets.append(
                ProductionTarget(kind="mixer_track", index=target.track_index)
            )
        else:
            targets.append(ProductionTarget(kind="channel", index=target.channel_index))
    elif name.startswith("channel_"):
        targets.append(
            ProductionTarget(kind="channel", index=cast(int, raw.channel_index))
        )
        if name == "channel_route" and raw.mixer_destination >= 0:
            targets.append(
                ProductionTarget(kind="mixer_track", index=raw.mixer_destination)
            )
    elif name.startswith("pattern_"):
        targets.append(
            ProductionTarget(kind="pattern", index=cast(int, raw.pattern_number))
        )
    elif name.startswith("playlist_"):
        targets.append(
            ProductionTarget(kind="playlist_track", index=cast(int, raw.track_index))
        )
    return targets


def _operation_targets(operation: ProductionOperation) -> list[ProductionTarget]:
    if isinstance(operation, (PreparePatternOperation, SelectPatternOperation)):
        return [ProductionTarget(kind="pattern", index=operation.pattern_number)]
    if isinstance(operation, _PIANO_ROLL_TYPES):
        return [
            ProductionTarget(kind="channel", index=operation.channel_index),
            ProductionTarget(kind="pattern", index=operation.pattern_number),
        ]
    if isinstance(operation, RecordAutomationValueOperation):
        return [
            ProductionTarget(
                kind=("mixer_track" if operation.target_kind == "mixer" else "channel"),
                index=operation.target_index,
            )
        ]
    if isinstance(operation, ApplyVerifiedBatchOperation):
        targets: list[ProductionTarget] = []
        for item in operation.operations:
            targets.extend(_batch_targets(item))
        return targets
    return []


def _same_index_target(left: ProductionTarget, right: ProductionTarget) -> bool:
    return (
        left.kind == right.kind
        and left.index is not None
        and right.index is not None
        and left.index == right.index
    )


def _target_explicitly_allowed(
    operation: ProductionOperation, target: ProductionTarget
) -> bool:
    for allowed in operation.allow_preserved_targets:
        if _same_index_target(allowed, target):
            return True
        if (
            allowed.kind == target.kind
            and allowed.name is not None
            and target.name is not None
            and allowed.name.casefold() == target.name.casefold()
        ):
            return True
    return False


def _blocker(
    category: BlockerCategory,
    code: str,
    message: str,
    *,
    operation_id: str | None = None,
    evidence: tuple[str, ...] = (),
) -> ProductionBlocker:
    bounded_message = str(message)[:1024]
    bounded_evidence = tuple(
        item
        for item in (str(value)[:512] for value in evidence[:16])
        if item
    )
    return ProductionBlocker(
        category=category,
        code=code,
        message=bounded_message or "Production Run validation failed.",
        operation_id=operation_id,
        evidence=bounded_evidence,
    )


def _bounded_blockers(
    blockers: list[ProductionBlocker] | tuple[ProductionBlocker, ...],
    *,
    limit: int,
) -> tuple[ProductionBlocker, ...]:
    """Bound diagnostic history while preserving proof that details were omitted."""

    if len(blockers) <= limit:
        return tuple(blockers)
    omitted = len(blockers) - (limit - 1)
    return (
        *tuple(blockers[: limit - 1]),
        _blocker(
            "malformed_plan",
            "additional_blockers_omitted",
            f"{omitted} additional validation blockers were omitted from this bounded report.",
            evidence=(f"reported_limit={limit}",),
        ),
    )


def _bounded_warnings(warnings: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if len(warnings) <= MAX_RUN_WARNINGS:
        return tuple(warnings)
    omitted = len(warnings) - (MAX_RUN_WARNINGS - 1)
    return (
        *tuple(warnings[: MAX_RUN_WARNINGS - 1]),
        f"{omitted} additional warnings were omitted from this bounded report.",
    )


def _validate_scope_and_authorization(
    request: ProductionRunRequest,
    operations: tuple[ProductionOperation, ...],
    *,
    start_index: int,
) -> tuple[list[ProductionBlocker], list[str]]:
    blockers: list[ProductionBlocker] = []
    warnings: list[str] = []
    allowed_by_scope = set(_SCOPE_BASE_CHANGES[request.scope.kind])
    allowed_by_scope.update(request.scope.additional_allowed_changes)
    explicit_changes = set(request.allowed_changes)
    mutating_remainder = False

    for operation in operations[start_index:]:
        if isinstance(operation, UnavailableProductionOperation):
            continue
        if isinstance(operation, ApplyVerifiedBatchOperation):
            try:
                validate_batch_operations(list(operation.operations))
            except Exception as exc:
                blockers.append(
                    _blocker(
                        "malformed_plan",
                        "invalid_verified_batch",
                        f"Verified batch {operation.operation_id!r} is invalid: {exc}",
                        operation_id=operation.operation_id,
                    )
                )
        if not _is_mutating(operation):
            continue
        mutating_remainder = True
        categories = _operation_categories(operation)
        if request.interaction_policy == "plan_only":
            blockers.append(
                _blocker(
                    "scope",
                    "plan_only_mutation",
                    f"Plan-only operation {operation.operation_id!r} would change the project. Keep this run to generation operations or validate the proposed mutating plan without executing it.",
                    operation_id=operation.operation_id,
                )
            )
        for category in categories:
            if category not in explicit_changes:
                blockers.append(
                    _blocker(
                        "scope",
                        "change_not_allowed",
                        f"Operation {operation.operation_id!r} needs the {category!r} change category, which this request did not allow.",
                        operation_id=operation.operation_id,
                    )
                )
            if category not in allowed_by_scope:
                blockers.append(
                    _blocker(
                        "scope",
                        "outside_run_scope",
                        f"Operation {operation.operation_id!r} changes {category!r}, outside the {request.scope.kind!r} scope.",
                        operation_id=operation.operation_id,
                    )
                )
        preserve = request.preserve
        forbidden = {
            "tempo": preserve.tempo,
            "notes": preserve.note_content,
            "arrangement": preserve.arrangement,
            "playlist_metadata": preserve.arrangement,
            "mixer": preserve.mixer_state,
            "channels": preserve.mixer_state,
            "routing": preserve.mixer_state,
            "plugin_parameters": preserve.mixer_state,
            "automation": preserve.mixer_state,
            "pattern_metadata": preserve.pattern_identity,
        }
        for category in categories:
            if forbidden.get(category, False):
                blockers.append(
                    _blocker(
                        "scope",
                        "preserved_category",
                        f"Operation {operation.operation_id!r} would change preserved {category.replace('_', ' ')}.",
                        operation_id=operation.operation_id,
                    )
                )
        targets = _operation_targets(operation)
        for target in targets:
            for preserved in request.preserve.targets:
                if _same_index_target(
                    target, preserved
                ) and not _target_explicitly_allowed(operation, target):
                    blockers.append(
                        _blocker(
                            "scope",
                            "preserved_target",
                            f"Operation {operation.operation_id!r} targets preserved {target.kind} {target.index}.",
                            operation_id=operation.operation_id,
                        )
                    )
        if request.scope.kind == "selected_targets":
            for target in targets:
                indexed_rules = [
                    rule
                    for rule in request.scope.targets
                    if rule.kind == target.kind and rule.index is not None
                ]
                named_rules_exist = any(
                    rule.kind == target.kind and rule.name is not None
                    for rule in request.scope.targets
                )
                if (
                    indexed_rules
                    and not named_rules_exist
                    and not any(
                        _same_index_target(target, rule) for rule in indexed_rules
                    )
                ):
                    blockers.append(
                        _blocker(
                            "scope",
                            "target_outside_scope",
                            f"Operation {operation.operation_id!r} targets {target.kind} {target.index}, outside the selected targets.",
                            operation_id=operation.operation_id,
                        )
                    )
                elif not indexed_rules and not named_rules_exist:
                    blockers.append(
                        _blocker(
                            "scope",
                            "target_kind_outside_scope",
                            f"Operation {operation.operation_id!r} targets a {target.kind}, but that target kind is absent from the selected scope.",
                            operation_id=operation.operation_id,
                        )
                    )

    if (
        mutating_remainder
        and request.interaction_policy != "plan_only"
        and not request.authorized_to_modify
    ):
        blockers.append(
            _blocker(
                "authorization",
                "modification_not_authorized",
                "This request does not authorize changes to the open project.",
            )
        )
    if request.preserve.named_elements and not request.preserve.targets:
        warnings.append(
            "Named preserved elements are descriptive only until the plan also identifies their mixer, channel, pattern, or Playlist targets."
        )
    return blockers, warnings


def _structural_validation(
    request: ProductionRunRequest,
    plan: ProductionRunPlan,
    *,
    start_index: int = 0,
    completed_operation_ids: frozenset[str] | None = None,
) -> tuple[list[ProductionBlocker], list[str]]:
    blockers: list[ProductionBlocker] = []
    warnings: list[str] = []
    operations = plan.operations
    if not 0 <= start_index <= len(operations):
        raise ValueError("start_index is outside the production plan")
    if len(operations) > request.max_operations:
        blockers.append(
            _blocker(
                "malformed_plan",
                "operation_limit_exceeded",
                f"This plan has {len(operations)} operations; the request allows at most {request.max_operations}.",
            )
        )

    ids = [item.operation_id for item in operations]
    positions: dict[str, int] = {}
    duplicate_ids: set[str] = set()
    for index, operation_id in enumerate(ids):
        if operation_id in positions:
            duplicate_ids.add(operation_id)
        else:
            positions[operation_id] = index
    for operation_id in sorted(duplicate_ids):
        blockers.append(
            _blocker(
                "malformed_plan",
                "duplicate_operation_id",
                f"Operation ID {operation_id!r} is duplicated.",
                operation_id=operation_id,
            )
        )

    for operation in operations:
        if not isinstance(operation, UnavailableProductionOperation):
            continue
        blockers.append(
            _blocker(
                (
                    "unsupported_by_postfader"
                    if operation.operation == "save_project"
                    else "unsupported_by_fl_studio"
                ),
                "unsupported_production_operation",
                _UNAVAILABLE_OPERATION_MESSAGES[operation.operation],
                operation_id=operation.operation_id,
                evidence=(f"operation={operation.operation}",),
            )
        )

    generated_note_counts: dict[str, int] = {}
    for operation in operations:
        if not isinstance(operation, _GENERATOR_TYPES):
            continue
        try:
            generated = _generate_sequence(operation)
        except Exception as exc:
            blockers.append(
                _blocker(
                    "malformed_plan",
                    "invalid_generator_operation",
                    f"Operation {operation.operation_id!r} cannot generate its declared output: {exc}",
                    operation_id=operation.operation_id,
                )
            )
        else:
            generated_note_counts[operation.operation_id] = generated.note_count

    for index, operation in enumerate(operations):
        for dependency in _operation_dependencies(operation):
            dependency_index = positions.get(dependency)
            if dependency_index is None:
                blockers.append(
                    _blocker(
                        "malformed_plan",
                        "missing_operation_reference",
                        f"Operation {operation.operation_id!r} refers to missing operation {dependency!r}.",
                        operation_id=operation.operation_id,
                    )
                )
            elif dependency_index >= index:
                blockers.append(
                    _blocker(
                        "malformed_plan",
                        "future_operation_reference",
                        f"Operation {operation.operation_id!r} refers to {dependency!r}, which is not earlier in the plan.",
                        operation_id=operation.operation_id,
                    )
                )
            elif (
                completed_operation_ids is not None
                and index >= start_index
                and dependency_index < start_index
                and dependency not in completed_operation_ids
            ):
                blockers.append(
                    _blocker(
                        "malformed_plan",
                        "dependency_not_completed",
                        f"Operation {operation.operation_id!r} depends on {dependency!r}, which did not complete successfully.",
                        operation_id=operation.operation_id,
                    )
                )
        if isinstance(operation, WriteNoteSequenceOperation) and isinstance(
            operation.sequence, OperationOutputReference
        ):
            producer_index = positions.get(operation.sequence.operation_id)
            if producer_index is not None and producer_index < index:
                producer = operations[producer_index]
                if _output_type(producer) != operation.sequence.output:
                    blockers.append(
                        _blocker(
                            "malformed_plan",
                            "incompatible_output_reference",
                            f"Operation {operation.operation_id!r} needs a note sequence, but {producer.operation_id!r} produces {_output_type(producer)!r}.",
                            operation_id=operation.operation_id,
                        )
                    )
                else:
                    note_count = generated_note_counts.get(producer.operation_id)
                    if note_count is not None and not (
                        1 <= note_count <= MAX_PIANO_ROLL_NOTES
                    ):
                        blockers.append(
                            _blocker(
                                "malformed_plan",
                                "piano_roll_note_limit_exceeded",
                                f"Operation {operation.operation_id!r} references {note_count} notes, but one Piano Roll write supports 1..{MAX_PIANO_ROLL_NOTES}.",
                                operation_id=operation.operation_id,
                            )
                        )
        elif isinstance(operation, WriteNoteSequenceOperation):
            note_count = operation.sequence.note_count
            if not 1 <= note_count <= MAX_PIANO_ROLL_NOTES:
                blockers.append(
                    _blocker(
                        "malformed_plan",
                        "piano_roll_note_limit_exceeded",
                        f"Operation {operation.operation_id!r} supplies {note_count} notes, but one Piano Roll write supports 1..{MAX_PIANO_ROLL_NOTES}.",
                        operation_id=operation.operation_id,
                    )
                )

    scope_blockers, scope_warnings = _validate_scope_and_authorization(
        request,
        operations,
        start_index=start_index,
    )
    blockers.extend(scope_blockers)
    warnings.extend(scope_warnings)
    return blockers, warnings


@dataclass(frozen=True)
class _LiveFacts:
    connection: ConnectionInfo
    mixer_names: dict[int, str] = field(default_factory=dict)
    channel_names: dict[int, str] = field(default_factory=dict)
    pattern_names: dict[int, str] = field(default_factory=dict)
    playlist_names: dict[int, str] = field(default_factory=dict)

    def names_for(self, kind: TargetKind) -> dict[int, str]:
        if kind == "mixer_track":
            return self.mixer_names
        if kind == "channel":
            return self.channel_names
        if kind == "pattern":
            return self.pattern_names
        return self.playlist_names


def _resolve_named_target(
    target: ProductionTarget, facts: _LiveFacts
) -> tuple[int | None, ProductionBlocker | None]:
    if target.name is None:
        return target.index, None
    matches = [
        index
        for index, name in facts.names_for(target.kind).items()
        if name.casefold() == target.name.casefold()
    ]
    if target.index is not None:
        if matches != [target.index]:
            return None, _blocker(
                "unavailable_in_project",
                "named_target_mismatch",
                f"The live {target.kind} named {target.name!r} does not uniquely match index {target.index}.",
            )
        return target.index, None
    if not matches:
        return None, _blocker(
            "unavailable_in_project",
            "named_target_missing",
            f"No live {target.kind} is named {target.name!r}.",
        )
    if len(matches) > 1:
        return None, _blocker(
            "unavailable_in_project",
            "named_target_ambiguous",
            f"More than one live {target.kind} is named {target.name!r}; use an index.",
        )
    return matches[0], None


def _needs_named_facts(request: ProductionRunRequest) -> bool:
    return any(
        target.name is not None
        for target in (*request.scope.targets, *request.preserve.targets)
    )


def _validate_live_targets(
    request: ProductionRunRequest,
    operations: list[ProductionOperation],
    project: ProjectSummary,
    connection: ConnectionInfo,
    inspector: ReadOnlyInspector,
    track_inspector: TrackBInspector,
) -> list[ProductionBlocker]:
    """Resolve live target bounds and named scope/preservation rules."""

    blockers: list[ProductionBlocker] = []
    pattern_inventory = None
    pattern_inventory_attempted = False
    needs_pattern_inventory = any(
        target.kind == "pattern"
        for operation in operations
        for target in _operation_targets(operation)
    ) or any(
        target.kind == "pattern" and target.name is not None
        for target in (*request.scope.targets, *request.preserve.targets)
    )
    if needs_pattern_inventory:
        pattern_inventory_attempted = True
        try:
            pattern_inventory = track_inspector.list_patterns()
        except Exception as exc:
            blockers.append(
                _blocker(
                    "setup_or_session",
                    "pattern_inventory_failed",
                    f"Available patterns could not be inspected before execution: {exc}",
                )
            )

    for operation in operations:
        for target in _operation_targets(operation):
            if target.index is None:
                continue
            if target.kind == "pattern":
                unavailable = (
                    pattern_inventory is not None
                    and target.index > pattern_inventory.maximum_pattern_number
                )
                if pattern_inventory is None:
                    continue
            else:
                count = {
                    "mixer_track": project.mixer_track_count,
                    "channel": project.channel_count,
                    "playlist_track": project.playlist_track_count,
                }[target.kind]
                unavailable = count is None or target.index >= count + (
                    1 if target.kind == "playlist_track" else 0
                )
            if unavailable:
                blockers.append(
                    _blocker(
                        "unavailable_in_project",
                        "target_index_unavailable",
                        f"Operation {operation.operation_id!r} targets {target.kind} {target.index}, which is not available in the current project.",
                        operation_id=operation.operation_id,
                    )
                )

    mixer_names: dict[int, str] = {}
    channel_names: dict[int, str] = {}
    pattern_names: dict[int, str] = {}
    playlist_names: dict[int, str] = {}
    if _needs_named_facts(request):
        try:
            mixer_names = {
                item.index: item.name
                for item in inspector.list_mixer_tracks(
                    only_used=False, include_peaks=False
                ).tracks
            }
            channel_names = {
                item.channel_index: item.name
                for item in track_inspector.list_channels().channels
            }
            if pattern_inventory is None and not pattern_inventory_attempted:
                pattern_inventory = track_inspector.list_patterns()
            if pattern_inventory is not None:
                pattern_names = {
                    item.pattern_number: item.name
                    for item in pattern_inventory.patterns
                }
            playlist_names = {
                item.track_index: item.name
                for item in track_inspector.list_playlist_tracks().tracks
            }
        except Exception as exc:
            blockers.append(
                _blocker(
                    "setup_or_session",
                    "named_target_inspection_failed",
                    f"Named scope targets could not be resolved before execution: {exc}",
                )
            )
    facts = _LiveFacts(
        connection=connection,
        mixer_names=mixer_names,
        channel_names=channel_names,
        pattern_names=pattern_names,
        playlist_names=playlist_names,
    )

    resolved_scope: dict[TargetKind, set[int]] = {
        "mixer_track": set(),
        "channel": set(),
        "pattern": set(),
        "playlist_track": set(),
    }
    resolved_preserve: dict[TargetKind, set[int]] = {
        "mixer_track": set(),
        "channel": set(),
        "pattern": set(),
        "playlist_track": set(),
    }
    for collection, destination in (
        (request.scope.targets, resolved_scope),
        (request.preserve.targets, resolved_preserve),
    ):
        for rule in collection:
            index, error = _resolve_named_target(rule, facts)
            if error is not None:
                blockers.append(error)
            elif index is not None:
                destination[rule.kind].add(index)

    for operation in operations:
        targets = _operation_targets(operation)
        if request.scope.kind == "selected_targets":
            for target in targets:
                if target.index not in resolved_scope[target.kind]:
                    blockers.append(
                        _blocker(
                            "scope",
                            "target_outside_named_scope",
                            f"Operation {operation.operation_id!r} targets {target.kind} {target.index}, outside the resolved selected targets.",
                            operation_id=operation.operation_id,
                        )
                    )
        for target in targets:
            if target.index in resolved_preserve[
                target.kind
            ] and not _target_explicitly_allowed(operation, target):
                blockers.append(
                    _blocker(
                        "scope",
                        "preserved_named_target",
                        f"Operation {operation.operation_id!r} targets preserved {target.kind} {target.index}.",
                        operation_id=operation.operation_id,
                    )
                )
    return blockers


def _validate_live_operation_capabilities(
    operations: list[ProductionOperation],
    project: ProjectSummary,
    inspector: ReadOnlyInspector,
    track_inspector: TrackBInspector,
) -> list[ProductionBlocker]:
    """Validate live prerequisites that are specific to operation semantics."""

    blockers: list[ProductionBlocker] = []
    prepare_operations = [
        operation
        for operation in operations
        if isinstance(operation, PreparePatternOperation)
    ]
    for operation in prepare_operations:
        try:
            found = track_inspector.find_empty_pattern(
                start_pattern_number=operation.pattern_number
            ).empty_pattern_number
        except Exception as exc:
            blockers.append(
                _blocker(
                    "unavailable_in_project",
                    "empty_pattern_check_failed",
                    f"Operation {operation.operation_id!r} could not inspect empty patterns: {exc}",
                    operation_id=operation.operation_id,
                )
            )
            continue
        if found is None:
            blockers.append(
                _blocker(
                    "unavailable_in_project",
                    "empty_pattern_unavailable",
                    f"Operation {operation.operation_id!r} needs an empty pattern, but FL did not report one in range.",
                    operation_id=operation.operation_id,
                )
            )
        elif found != operation.pattern_number:
            blockers.append(
                _blocker(
                    "unavailable_in_project",
                    "pattern_not_empty",
                    f"Operation {operation.operation_id!r} targets pattern {operation.pattern_number}, but FL reported pattern {found} as the first empty pattern at or after it.",
                    operation_id=operation.operation_id,
                )
            )

    batch_operations = [
        operation
        for operation in operations
        if isinstance(operation, ApplyVerifiedBatchOperation)
    ]
    plugin_items = [
        (operation, item)
        for operation in batch_operations
        for item in operation.operations
        if item.operation == "plugin_parameter"
    ]
    if plugin_items:
        try:
            inventory = track_inspector.scan_loaded_plugins(only_used=False)
        except Exception as exc:
            blockers.append(
                _blocker(
                    "setup_or_session",
                    "plugin_inventory_failed",
                    f"Loaded plug-ins could not be inspected before execution: {exc}",
                )
            )
        else:
            loaded = {
                (
                    item.target.kind,
                    getattr(item.target, "track_index", None),
                    getattr(item.target, "slot_index", None),
                    getattr(item.target, "channel_index", None),
                ): item.reported_parameter_count
                for item in inventory.plugins
            }
            for operation, item in plugin_items:
                target = item.target
                key = (
                    target.kind,
                    getattr(target, "track_index", None),
                    getattr(target, "slot_index", None),
                    getattr(target, "channel_index", None),
                )
                count = loaded.get(key)
                if key not in loaded:
                    blockers.append(
                        _blocker(
                            "unavailable_in_project",
                            "plugin_target_unavailable",
                            f"Batch {operation.operation_id!r} targets a plug-in that is not loaded in the current project.",
                            operation_id=operation.operation_id,
                        )
                    )
                elif count is None or item.parameter_index >= count:
                    blockers.append(
                        _blocker(
                            "unavailable_in_project",
                            "plugin_parameter_unavailable",
                            f"Batch {operation.operation_id!r} targets plug-in parameter {item.parameter_index}, outside the loaded plug-in's reported range.",
                            operation_id=operation.operation_id,
                        )
                    )

    for operation in batch_operations:
        for item in operation.operations:
            if item.operation != "mixer_send_level":
                continue
            try:
                inspection = inspector.inspect_mixer_track(item.track_index)
            except Exception as exc:
                blockers.append(
                    _blocker(
                        "unavailable_in_project",
                        "mixer_send_inspection_failed",
                        f"Batch {operation.operation_id!r} could not inspect its existing send: {exc}",
                        operation_id=operation.operation_id,
                    )
                )
                continue
            if item.destination_track_index not in {
                route.destination_track_index for route in inspection.routes
            }:
                blockers.append(
                    _blocker(
                        "unavailable_in_project",
                        "mixer_send_unavailable",
                        f"Batch {operation.operation_id!r} cannot set this send level because the send does not currently exist.",
                        operation_id=operation.operation_id,
                    )
                )

    for operation in operations:
        if isinstance(operation, RecordAutomationValueOperation) and not (
            project.transport.playing is True and project.transport.recording is True
        ):
            blockers.append(
                _blocker(
                    "unavailable_in_project",
                    "automation_capture_not_active",
                    f"Operation {operation.operation_id!r} needs playback and recording to already be active.",
                    operation_id=operation.operation_id,
                )
            )
    return blockers


def _live_validation(
    request: ProductionRunRequest,
    plan: ProductionRunPlan,
    *,
    start_index: int,
) -> tuple[list[ProductionBlocker], list[str], str | None, str | None]:
    """Inspect only current capabilities needed by the unexecuted remainder."""

    operations = plan.operations[start_index:]
    mutating = [operation for operation in operations if _is_mutating(operation)]
    if not mutating or request.interaction_policy == "plan_only":
        return [], [], None, None

    blockers: list[ProductionBlocker] = []
    warnings: list[str] = []
    inspector = ReadOnlyInspector()
    connection = inspector.connection_info()
    if not connection.connected or not connection.compatible:
        return (
            [
                _blocker(
                    "setup_or_session",
                    "fl_connection_unavailable",
                    connection.error
                    or connection.compatibility_reason
                    or "No compatible FL Studio bridge is connected.",
                )
            ],
            warnings,
            None,
            None,
        )
    if not connection.bridge_provenance_verified:
        blockers.append(
            _blocker(
                "setup_or_session",
                "bridge_provenance_unverified",
                "The running FL bridge does not match this PostFader package. Reinstall and reload the packaged bridge.",
                evidence=(connection.bridge_provenance,),
            )
        )
    if not connection.runtime_write_mode_control:
        blockers.append(
            _blocker(
                "setup_or_session",
                "write_mode_control_unavailable",
                "The running FL bridge cannot enable writes for this run. Install and reload the current packaged bridge.",
            )
        )
    session = connection.session_fingerprint
    if session is None:
        blockers.append(
            _blocker(
                "setup_or_session",
                "session_fingerprint_unavailable",
                "The running FL bridge did not provide a valid session fingerprint.",
            )
        )
        return blockers, warnings, None, None

    if any(isinstance(operation, _PIANO_ROLL_TYPES) for operation in mutating):
        try:
            piano = PIANO_ROLL.status()
        except Exception as exc:
            blockers.append(
                _blocker(
                    "setup_or_session",
                    "piano_roll_setup_unavailable",
                    f"Piano Roll setup could not be inspected: {exc}",
                )
            )
        else:
            if not piano.armed_this_session:
                blockers.append(
                    _blocker(
                        "setup_or_session",
                        "piano_roll_bridge_not_armed",
                        "Piano Roll writing needs one setup step: prepare the Postfader Apply script, run it once from an FL Piano Roll, then confirm it for this MCP process.",
                        evidence=(piano.setup_instruction,),
                    )
                )

    try:
        project = inspector.project_summary()
    except Exception as exc:
        blockers.append(
            _blocker(
                "setup_or_session",
                "project_inspection_failed",
                f"The live project could not be inspected before execution: {exc}",
            )
        )
        return blockers, warnings, session, None

    project_digest = _project_state_digest(project)

    track_inspector = TrackBInspector()
    blockers.extend(
        _validate_live_targets(
            request,
            mutating,
            project,
            connection,
            inspector,
            track_inspector,
        )
    )
    blockers.extend(
        _validate_live_operation_capabilities(
            mutating,
            project,
            inspector,
            track_inspector,
        )
    )

    final_connection = inspector.connection_info()
    if final_connection.session_fingerprint != session:
        blockers.append(
            _blocker(
                "setup_or_session",
                "session_changed_during_validation",
                "FL Studio reloaded the bridge while this run was being validated. Inspect the project again before execution.",
            )
        )
    try:
        final_project = inspector.project_summary()
    except Exception as exc:
        blockers.append(
            _blocker(
                "setup_or_session",
                "project_recheck_failed",
                f"The project could not be rechecked after run validation: {exc}",
            )
        )
    else:
        if _project_state_digest(final_project) != project_digest:
            blockers.append(
                _blocker(
                    "setup_or_session",
                    "project_changed_during_validation",
                    "The open project changed while this run was being validated. Inspect it again before execution.",
                )
            )
    return blockers, warnings, session, project_digest


def validate_production_run(
    request: ProductionRunRequest,
    plan: ProductionRunPlan,
    *,
    inspect_live: bool = True,
    start_index: int = 0,
    completed_operation_ids: frozenset[str] | None = None,
) -> ProductionRunValidation:
    """Validate the complete plan without enabling writes or mutating FL Studio."""

    structural, warnings = _structural_validation(
        request,
        plan,
        start_index=start_index,
        completed_operation_ids=completed_operation_ids,
    )
    session: str | None = None
    project_digest: str | None = None
    live: list[ProductionBlocker] = []
    if inspect_live and not structural:
        live, live_warnings, session, project_digest = _live_validation(
            request, plan, start_index=start_index
        )
        warnings.extend(live_warnings)
    all_blockers = structural + live
    blockers = _bounded_blockers(
        all_blockers,
        limit=MAX_VALIDATION_BLOCKERS,
    )
    mutation_categories: list[ChangeCategory] = []
    for operation in plan.operations[start_index:]:
        if isinstance(operation, UnavailableProductionOperation):
            continue
        if not _is_mutating(operation):
            continue
        for category in _operation_categories(operation):
            if category not in mutation_categories:
                mutation_categories.append(category)
    valid = not all_blockers
    return ProductionRunValidation(
        validated_at=_now(),
        valid=valid,
        executable=valid,
        plan_digest=production_plan_digest(plan),
        resolved_operation_order=tuple(item.operation_id for item in plan.operations),
        required_capabilities=_required_capabilities(plan.operations, start_index),
        unsupported_operations=tuple(
            f"{operation.operation_id}:{operation.operation}"
            for operation in plan.operations[start_index:]
            if isinstance(operation, UnavailableProductionOperation)
        ),
        expected_mutation_categories=tuple(mutation_categories),
        session_fingerprint=session,
        project_state_digest=project_digest,
        blockers=blockers,
        warnings=_bounded_warnings(warnings),
    )


# ---------------------------------------------------------------------------
# Existing-operation adapters and run execution
# ---------------------------------------------------------------------------


def _resolve_sequence(
    operation: WriteNoteSequenceOperation,
    outputs: dict[str, ProductionGeneratedOutput],
) -> NoteSequence:
    if isinstance(operation.sequence, NoteSequence):
        return operation.sequence
    output = outputs.get(operation.sequence.operation_id)
    if output is None or output.output != operation.sequence.output:
        # Complete validation and ordered execution should make this
        # unreachable. Keep it fail-closed in case registry state is corrupt.
        raise ValueError(
            f"note sequence output {operation.sequence.operation_id!r} is unavailable"
        )
    return output.value


def _operation_dependencies(operation: ProductionOperation) -> tuple[str, ...]:
    dependencies = list(operation.after)
    if isinstance(operation, WriteNoteSequenceOperation) and isinstance(
        operation.sequence, OperationOutputReference
    ):
        dependencies.append(operation.sequence.operation_id)
    return tuple(dict.fromkeys(dependencies))


def _dispatch_operation(
    operation: ProductionOperation,
    *,
    session_fingerprint: str | None,
    outputs: dict[str, ProductionGeneratedOutput],
) -> ProductionResultPayload:
    """Adapt one production operation to an existing PostFader implementation."""

    if isinstance(operation, _GENERATOR_TYPES):
        return _generate_sequence(operation)
    if isinstance(operation, UnavailableProductionOperation):
        raise ValueError(_UNAVAILABLE_OPERATION_MESSAGES[operation.operation])
    if session_fingerprint is None:
        raise ValueError("a project mutation requires the captured session fingerprint")
    if isinstance(operation, PreparePatternOperation):
        return prepare_empty_pattern(
            name=operation.name,
            length_beats=operation.length_beats,
            color=operation.color,
            start_pattern_number=operation.pattern_number,
            expected_pattern_number=operation.pattern_number,
            session_fingerprint=session_fingerprint,
        )
    if isinstance(operation, SelectPatternOperation):
        return TrackBController().select_pattern(
            pattern_number=operation.pattern_number,
            session_fingerprint=session_fingerprint,
        )
    if isinstance(operation, WriteNoteSequenceOperation):
        sequence = _resolve_sequence(operation, outputs)
        return write_piano_roll_notes(
            sequence.notes,
            channel_index=operation.channel_index,
            pattern_number=operation.pattern_number,
            mode=operation.mode,
            auto_trigger=True,
            session_fingerprint=session_fingerprint,
        )
    if isinstance(operation, TransformPianoRollOperation):
        return transform_piano_roll(
            operation.transform,
            channel_index=operation.channel_index,
            pattern_number=operation.pattern_number,
            auto_trigger=True,
            session_fingerprint=session_fingerprint,
        )
    if isinstance(operation, AddSectionMarkersOperation):
        return add_section_markers(
            operation.markers, session_fingerprint=session_fingerprint
        )
    if isinstance(operation, RecordAutomationValueOperation):
        return record_automation_value(
            target_kind=operation.target_kind,
            target_index=operation.target_index,
            property=operation.property,
            value_normalized=operation.value_normalized,
            allow_master=operation.allow_master,
            expected_before=operation.expected_before,
            session_fingerprint=session_fingerprint,
        )
    if isinstance(operation, ApplyVerifiedBatchOperation):
        return VerifiedBatchExecutor().apply(
            operations=list(operation.operations),
            stop_on_unverified=True,
            session_fingerprint=session_fingerprint,
        )
    raise AssertionError("unhandled production operation")


def _capture_project_state(expected_session: str) -> tuple[str | None, str]:
    try:
        project = ReadOnlyInspector().project_summary()
    except Exception as exc:
        return None, f"The live project could not be checked: {exc}"
    connection = project.connection
    if not connection.connected or not connection.compatible:
        return (
            None,
            connection.error
            or connection.compatibility_reason
            or "No compatible FL Studio bridge is connected.",
        )
    if not connection.bridge_provenance_verified:
        return None, "The running FL bridge no longer matches this PostFader package."
    if connection.session_fingerprint != expected_session:
        return None, "FL Studio reloaded the bridge since this run was validated."
    if not connection.verified_writes_enabled:
        return None, "Write mode is no longer active for this Production Run."
    return _project_state_digest(project), ""


def _current_session_matches(
    expected: str,
    expected_project_state_digest: str | None = None,
) -> tuple[bool, str]:
    digest, reason = _capture_project_state(expected)
    if digest is None:
        return False, reason
    if (
        expected_project_state_digest is not None
        and digest != expected_project_state_digest
    ):
        return (
            False,
            "The open project changed since this run's last verified checkpoint.",
        )
    return True, ""


def _classify_mutation_result(
    result: ProductionResultPayload,
) -> tuple[bool, bool, str]:
    """Return outcome-known, verified, and a concise failure explanation."""

    if isinstance(result, VerifiedBatchResult):
        if result.verified:
            return True, True, ""
        unknown = any(not item.outcome_known for item in result.results)
        return (
            not unknown,
            False,
            (
                "The verified batch stopped because one mutation outcome is unknown."
                if unknown
                else "The verified batch returned an unverified mutation receipt."
            ),
        )
    if isinstance(result, (PatternPreparation, TrackBVerifiedMutation)):
        if result.verified:
            return True, True, ""
        return True, False, "FL did not verify this mutation on readback."
    if isinstance(result, PianoRollDispatch):
        return (
            False,
            False,
            "FL Studio cannot provide authoritative Piano Roll note readback. Confirm the visible result before continuing this run.",
        )
    if isinstance(result, ArrangementMarkerReceipt):
        return (
            False,
            False,
            "FL verified marker names but does not expose marker-time readback, so this run stopped before another change.",
        )
    if isinstance(result, AutomationRecordReceipt):
        return (
            False,
            False,
            "FL verified the controlled value but does not expose automation-point readback, so this run stopped before another change.",
        )
    raise AssertionError("a generated result was classified as a mutation")


@dataclass
class _RunRecord:
    plan: ProductionRunPlan
    state: ProductionRunState
    outputs: dict[str, ProductionGeneratedOutput] = field(default_factory=dict)
    stop_requested: bool = False
    claimed: bool = False
    in_flight_operation_id: str | None = None


class ProductionRunRegistry:
    """Bounded, deterministic, thread-safe process-local run storage."""

    def __init__(self, *, max_runs: int = MAX_PRODUCTION_RUNS) -> None:
        if type(max_runs) is not int or not 1 <= max_runs <= MAX_PRODUCTION_RUNS:
            raise ValueError(f"max_runs must be within 1..{MAX_PRODUCTION_RUNS}")
        self.max_runs = max_runs
        self._lock = threading.RLock()
        self._runs: dict[str, _RunRecord] = {}

    @staticmethod
    def _missing_message(run_id: str) -> str:
        return (
            f"Production run {run_id!r} was not found in this MCP process. "
            "It may have expired or belonged to a previous MCP process."
        )

    def _evict_one_locked(self) -> None:
        candidates = [
            record
            for record in self._runs.values()
            if not record.claimed and record.state.status != "running"
        ]
        if not candidates:
            raise ValueError(
                "production run registry is full while all runs are active"
            )
        oldest = min(
            candidates,
            key=lambda record: (record.state.created_at, record.state.run_id),
        )
        del self._runs[oldest.state.run_id]

    def _create_record(
        self, request: ProductionRunRequest, plan: ProductionRunPlan
    ) -> _RunRecord:
        isolated_request = ProductionRunRequest.model_validate(
            request.model_dump(mode="python")
        )
        isolated_plan = ProductionRunPlan.model_validate(plan.model_dump(mode="python"))
        now = _now()
        state = ProductionRunState(
            run_id=secrets.token_hex(16),
            request=isolated_request,
            plan_id=isolated_plan.plan_id,
            plan_digest=production_plan_digest(isolated_plan),
            status="created",
            created_at=now,
            updated_at=now,
            iteration=1,
            current_operation_index=0,
            total_operations=len(isolated_plan.operations),
        )
        record = _RunRecord(plan=isolated_plan, state=state, claimed=True)
        with self._lock:
            if len(self._runs) >= self.max_runs:
                self._evict_one_locked()
            self._runs[state.run_id] = record
        return record

    def _replace_state(self, record: _RunRecord, state: ProductionRunState) -> None:
        with self._lock:
            current = self._runs.get(state.run_id)
            if current is not record:
                raise RuntimeError("production run registry state changed unexpectedly")
            record.state = state

    def _release(self, record: _RunRecord) -> None:
        with self._lock:
            record.claimed = False

    def get(self, run_id: str) -> ProductionRunLookup:
        with self._lock:
            record = self._runs.get(run_id)
            state = (
                None
                if record is None
                else ProductionRunState.model_validate(
                    record.state.model_dump(mode="python")
                )
            )
        if state is None:
            return ProductionRunLookup(
                found=False,
                message=self._missing_message(run_id),
            )
        return ProductionRunLookup(
            found=True,
            message=f"Production run {run_id} is {state.status}.",
            state=state,
        )

    @staticmethod
    def _result(state: ProductionRunState) -> ProductionRunResult:
        summary = state.final_summary or f"Production run is {state.status}."
        return ProductionRunResult(
            run_id=state.run_id,
            status=state.status,
            completed_count=len(state.completed_operations),
            attempted_count=len(state.receipts),
            total_operations=state.total_operations,
            summary=summary,
            blockers=state.blockers,
            warnings=state.warnings,
        )

    def execute(
        self, request: ProductionRunRequest, plan: ProductionRunPlan
    ) -> ProductionRunResult:
        record = self._create_record(request, plan)
        try:
            validation = validate_production_run(
                record.state.request, record.plan, inspect_live=True
            )
            with self._lock:
                if record.stop_requested or record.state.status == "stopped":
                    return self._result(record.state)
                if not validation.valid:
                    now = _now()
                    state = record.state.model_copy(
                        update={
                            "status": "blocked",
                            "updated_at": now,
                            "finished_at": now,
                            "blockers": validation.blockers,
                            "warnings": validation.warnings,
                            "final_summary": (
                                validation.blockers[0].message
                                if validation.blockers
                                else "This run could not be validated."
                            ),
                        }
                    )
                    record.state = state
                    return self._result(state)
                state = record.state.model_copy(
                    update={
                        "status": "validated",
                        "updated_at": _now(),
                        "session_fingerprint": validation.session_fingerprint,
                        "project_state_digest": validation.project_state_digest,
                        "warnings": validation.warnings,
                    }
                )
                record.state = state
            return self._execute_validated(record, start_index=0, validation=validation)
        finally:
            self._release(record)

    def _enable_write_mode(
        self,
        record: _RunRecord,
        *,
        validation: ProductionRunValidation,
        start_index: int,
    ) -> ProductionRunState | None:
        mutation_pending = any(
            _is_mutating(operation)
            for operation in record.plan.operations[start_index:]
        )
        if not mutation_pending:
            return None
        with self._lock:
            if record.stop_requested or record.state.status == "stopped":
                return record.state
        session = validation.session_fingerprint
        if session is None:
            blocker = _blocker(
                "setup_or_session",
                "session_fingerprint_unavailable",
                "This run cannot start because FL did not provide a session fingerprint.",
            )
            return self._finish_blocked(record, blocker)
        if record.state.write_mode_enabled_once:
            connection = ReadOnlyInspector().connection_info()
            if (
                connection.connected
                and connection.compatible
                and connection.session_fingerprint == session
                and connection.verified_writes_enabled
                and connection.bridge_provenance_verified
            ):
                return None
            blocker = _blocker(
                "setup_or_session",
                "run_write_mode_no_longer_active",
                "Write mode is no longer active for this run and will not be enabled a second time. Start a new authorized run for further project changes.",
            )
            return self._finish_blocked(record, blocker)
        try:
            change = WriteModeManager().set_write_mode(
                enabled=True,
                confirm_user_present=True,
                session_fingerprint=session,
            )
        except Exception as exc:
            blocker = _blocker(
                "setup_or_session",
                "write_mode_enable_failed",
                f"PostFader could not enable writes for this run: {exc}",
            )
            return self._finish_blocked(record, blocker)
        if change.session_fingerprint != session or not change.after_enabled:
            blocker = _blocker(
                "setup_or_session",
                "session_changed_while_enabling_writes",
                "FL Studio changed sessions while PostFader enabled writes for this run.",
            )
            return self._finish_blocked(record, blocker)
        with self._lock:
            state = record.state.model_copy(
                update={
                    "write_mode_enabled_once": True,
                    "updated_at": _now(),
                }
            )
            record.state = state
            if record.stop_requested or state.status == "stopped":
                return state
        return None

    def _finish_blocked(
        self,
        record: _RunRecord,
        blocker: ProductionBlocker,
        *,
        current_operation_index: int | None = None,
    ) -> ProductionRunState:
        with self._lock:
            now = _now()
            stopped = record.stop_requested or record.state.status == "stopped"
            state = record.state.model_copy(
                update={
                    "status": "stopped" if stopped else "blocked",
                    "updated_at": now,
                    "finished_at": now,
                    "current_operation_index": (
                        record.state.current_operation_index
                        if current_operation_index is None
                        else current_operation_index
                    ),
                    "blockers": _bounded_blockers(
                        [*record.state.blockers, blocker],
                        limit=MAX_RUN_BLOCKERS,
                    ),
                    "final_summary": (
                        record.state.final_summary
                        if stopped and record.state.final_summary
                        else blocker.message
                    ),
                }
            )
            record.state = state
            return state

    def _record_unknown_failure(
        self,
        record: _RunRecord,
        operation: ProductionOperation,
        *,
        operation_index: int,
        error: Exception,
        session_fingerprint: str | None,
    ) -> ProductionRunResult:
        receipt = ProductionOperationReceipt(
            operation_index=operation_index,
            operation_id=operation.operation_id,
            operation=operation.operation,
            status="error_unknown",
            mutating=_is_mutating(operation),
            outcome_known=False,
            verified=False,
            error=f"{type(error).__name__}: {error}"[:2048],
        )
        checkpoint: str | None = None
        if _is_mutating(operation) and session_fingerprint is not None:
            checkpoint, _checkpoint_reason = _capture_project_state(
                session_fingerprint
            )
        with self._lock:
            failure_update: dict[str, Any] = {
                "receipts": (*record.state.receipts, receipt),
                "current_operation_index": operation_index + 1,
                "updated_at": _now(),
            }
            if checkpoint is not None:
                failure_update["project_state_digest"] = checkpoint
            state = record.state.model_copy(update=failure_update)
            record.state = state
        blocker = _blocker(
            "unknown_outcome" if _is_mutating(operation) else "malformed_plan",
            "operation_failed_unknown",
            (
                f"Operation {operation.operation_id!r} stopped with an unknown mutation outcome. It was not retried."
                if _is_mutating(operation)
                else f"Operation {operation.operation_id!r} failed: {error}"
            ),
            operation_id=operation.operation_id,
            evidence=(receipt.error or "unknown error",),
        )
        state = self._finish_blocked(
            record, blocker, current_operation_index=operation_index + 1
        )
        return self._result(state)

    def _execute_validated(
        self,
        record: _RunRecord,
        *,
        start_index: int,
        validation: ProductionRunValidation,
    ) -> ProductionRunResult:
        with self._lock:
            if record.stop_requested or record.state.status == "stopped":
                return self._result(record.state)
            now = _now()
            state = record.state.model_copy(
                update={
                    "status": "running",
                    "started_at": record.state.started_at or now,
                    "finished_at": None,
                    "updated_at": now,
                    "blockers": (),
                    "final_summary": None,
                }
            )
            record.state = state
        mode_failure = self._enable_write_mode(
            record, validation=validation, start_index=start_index
        )
        if mode_failure is not None:
            return self._result(mode_failure)

        session = record.state.session_fingerprint
        for index in range(start_index, len(record.plan.operations)):
            operation = record.plan.operations[index]
            with self._lock:
                if record.stop_requested or record.state.status == "stopped":
                    return self._result(record.state)

                completed_operations = frozenset(record.state.completed_operations)

            incomplete_dependencies = tuple(
                dependency
                for dependency in _operation_dependencies(operation)
                if dependency not in completed_operations
            )
            if incomplete_dependencies:
                state = self._finish_blocked(
                    record,
                    _blocker(
                        "malformed_plan",
                        "dependency_not_completed",
                        f"Operation {operation.operation_id!r} cannot run because these dependencies did not complete: {', '.join(incomplete_dependencies)}.",
                        operation_id=operation.operation_id,
                        evidence=tuple(incomplete_dependencies),
                    ),
                )
                return self._result(state)

            if _is_mutating(operation):
                if session is None:
                    state = self._finish_blocked(
                        record,
                        _blocker(
                            "setup_or_session",
                            "session_fingerprint_unavailable",
                            "This run lost its FL session fingerprint before mutation.",
                            operation_id=operation.operation_id,
                        ),
                    )
                    return self._result(state)
                matches, reason = _current_session_matches(
                    session,
                    record.state.project_state_digest,
                )
                if not matches:
                    state = self._finish_blocked(
                        record,
                        _blocker(
                            "setup_or_session",
                            "session_precondition_failed",
                            reason,
                            operation_id=operation.operation_id,
                        ),
                    )
                    return self._result(state)

            with self._lock:
                if record.stop_requested or record.state.status == "stopped":
                    return self._result(record.state)
                record.in_flight_operation_id = operation.operation_id
            try:
                try:
                    result = _dispatch_operation(
                        operation,
                        session_fingerprint=session,
                        outputs=record.outputs,
                    )
                finally:
                    with self._lock:
                        record.in_flight_operation_id = None
            except Exception as exc:
                return self._record_unknown_failure(
                    record,
                    operation,
                    operation_index=index,
                    error=exc,
                    session_fingerprint=session,
                )

            if isinstance(result, NoteSequence):
                output = ProductionGeneratedOutput(
                    operation_id=operation.operation_id,
                    value=result,
                )
                receipt = ProductionOperationReceipt(
                    operation_index=index,
                    operation_id=operation.operation_id,
                    operation=operation.operation,
                    status="generated",
                    mutating=False,
                    outcome_known=True,
                    verified=True,
                    result=result,
                )
                with self._lock:
                    record.outputs[operation.operation_id] = output
                    state = record.state.model_copy(
                        update={
                            "receipts": (*record.state.receipts, receipt),
                            "generated_outputs": (
                                *record.state.generated_outputs,
                                output,
                            ),
                            "completed_operations": (
                                *record.state.completed_operations,
                                operation.operation_id,
                            ),
                            "current_operation_index": index + 1,
                            "updated_at": _now(),
                        }
                    )
                    record.state = state
                continue

            try:
                outcome_known, verified, explanation = _classify_mutation_result(
                    result
                )
            except Exception as exc:
                return self._record_unknown_failure(
                    record,
                    operation,
                    operation_index=index,
                    error=exc,
                    session_fingerprint=session,
                )
            checkpoint_digest: str | None = None
            checkpoint_reason = ""
            if session is not None:
                checkpoint_digest, checkpoint_reason = _capture_project_state(session)
            receipt = ProductionOperationReceipt(
                operation_index=index,
                operation_id=operation.operation_id,
                operation=operation.operation,
                status="verified" if verified else "unverified",
                mutating=True,
                outcome_known=outcome_known,
                verified=verified,
                result=result,
            )
            with self._lock:
                update: dict[str, Any] = {
                    "receipts": (*record.state.receipts, receipt),
                    "current_operation_index": index + 1,
                    "updated_at": _now(),
                }
                if verified:
                    update["completed_operations"] = (
                        *record.state.completed_operations,
                        operation.operation_id,
                    )
                if checkpoint_digest is not None:
                    update["project_state_digest"] = checkpoint_digest
                state = record.state.model_copy(update=update)
                record.state = state
            if verified and checkpoint_digest is None:
                state = self._finish_blocked(
                    record,
                    _blocker(
                        "setup_or_session",
                        "project_checkpoint_failed",
                        "The operation verified, but PostFader could not capture the next project-state checkpoint. The run stopped before another change.",
                        operation_id=operation.operation_id,
                        evidence=(
                            checkpoint_reason or "project checkpoint unavailable",
                        ),
                    ),
                    current_operation_index=index + 1,
                )
                return self._result(state)
            if not verified:
                state = self._finish_blocked(
                    record,
                    _blocker(
                        ("unverified_mutation" if outcome_known else "unknown_outcome"),
                        (
                            "unverified_mutation"
                            if outcome_known
                            else "ambiguous_mutation"
                        ),
                        explanation,
                        operation_id=operation.operation_id,
                    ),
                    current_operation_index=index + 1,
                )
                return self._result(state)

        with self._lock:
            if record.stop_requested or record.state.status == "stopped":
                return self._result(record.state)
            finished = _now()
            completed = record.state.model_copy(
                update={
                    "status": "completed",
                    "updated_at": finished,
                    "finished_at": finished,
                    "current_operation_index": len(record.plan.operations),
                    "final_summary": (
                        f"Completed {len(record.state.completed_operations)} operations. "
                        "The project was not saved automatically."
                    ),
                }
            )
            record.state = completed
            return self._result(completed)

    def continue_run(
        self, run_id: str, delta: ProductionRunDelta
    ) -> ProductionRunResult:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise ValueError(self._missing_message(run_id))
            if record.claimed or record.state.status == "running":
                raise ValueError("this Production Run is already executing")
            if record.state.status == "stopped" or record.stop_requested:
                raise ValueError(
                    "this Production Run was stopped; create a new run for future changes"
                )
            request = delta.request or record.state.request
            if record.state.iteration >= request.max_iterations:
                blocker = _blocker(
                    "iteration_limit",
                    "iteration_limit_reached",
                    f"This run has reached its {request.max_iterations}-iteration limit.",
                )
                state = record.state.model_copy(
                    update={
                        "status": "blocked",
                        "updated_at": _now(),
                        "finished_at": _now(),
                        "blockers": _bounded_blockers(
                            [*record.state.blockers, blocker],
                            limit=MAX_RUN_BLOCKERS,
                        ),
                        "final_summary": blocker.message,
                    }
                )
                record.state = state
                return self._result(state)
            start_index = record.state.current_operation_index
            if delta.mode == "append":
                operations = (*record.plan.operations, *delta.operations)
            else:
                operations = (
                    *record.plan.operations[:start_index],
                    *delta.operations,
                )
            if delta.mode == "replace_remaining" and not delta.operations:
                finished = _now()
                blocker = _blocker(
                    "stopped",
                    "remainder_removed",
                    "The unexecuted remainder was removed. Earlier project changes remain applied; the project was not saved automatically.",
                )
                state = record.state.model_copy(
                    update={
                        "request": ProductionRunRequest.model_validate(
                            request.model_dump(mode="python")
                        ),
                        "status": "stopped",
                        "updated_at": finished,
                        "finished_at": finished,
                        "iteration": record.state.iteration + 1,
                        "total_operations": record.state.current_operation_index,
                        "blockers": _bounded_blockers(
                            [*record.state.blockers, blocker],
                            limit=MAX_RUN_BLOCKERS,
                        ),
                        "final_summary": blocker.message,
                    }
                )
                record.stop_requested = True
                record.state = state
                return self._result(state)
            plan = ProductionRunPlan(
                plan_id=record.plan.plan_id,
                operations=operations,
            )
            plan = ProductionRunPlan.model_validate(plan.model_dump(mode="python"))
            request = ProductionRunRequest.model_validate(
                request.model_dump(mode="python")
            )
            completed_operation_ids = frozenset(record.state.completed_operations)
            prior_session = record.state.session_fingerprint
            prior_project_digest = record.state.project_state_digest
            record.claimed = True

        try:
            validation = validate_production_run(
                request,
                plan,
                inspect_live=True,
                start_index=start_index,
                completed_operation_ids=completed_operation_ids,
            )
            if (
                prior_session is not None
                and validation.session_fingerprint is not None
                and validation.session_fingerprint != prior_session
            ):
                mismatch = _blocker(
                    "setup_or_session",
                    "continued_session_changed",
                    "This run belongs to an earlier FL bridge session and cannot continue against the current one.",
                )
                validation = validation.model_copy(
                    update={
                        "valid": False,
                        "executable": False,
                        "blockers": _bounded_blockers(
                            [*validation.blockers, mismatch],
                            limit=MAX_VALIDATION_BLOCKERS,
                        ),
                    }
                )
            if (
                prior_project_digest is not None
                and validation.project_state_digest is not None
                and validation.project_state_digest != prior_project_digest
            ):
                mismatch = _blocker(
                    "setup_or_session",
                    "continued_project_state_changed",
                    "The open project changed after this run's last verified checkpoint. Inspect the new state and submit a revised run.",
                )
                validation = validation.model_copy(
                    update={
                        "valid": False,
                        "executable": False,
                        "blockers": _bounded_blockers(
                            [*validation.blockers, mismatch],
                            limit=MAX_VALIDATION_BLOCKERS,
                        ),
                    }
                )
            with self._lock:
                if record.stop_requested or record.state.status == "stopped":
                    return self._result(record.state)
                next_state = record.state.model_copy(
                    update={
                        "request": request,
                        "plan_digest": validation.plan_digest,
                        "status": "validated" if validation.valid else "blocked",
                        "updated_at": _now(),
                        "finished_at": None if validation.valid else _now(),
                        "iteration": record.state.iteration + 1,
                        "total_operations": len(plan.operations),
                        "blockers": validation.blockers,
                        "warnings": _bounded_warnings(
                            [*record.state.warnings, *validation.warnings]
                        ),
                        "final_summary": (
                            None
                            if validation.valid
                            else validation.blockers[0].message
                        ),
                        "session_fingerprint": prior_session
                        or validation.session_fingerprint,
                        "project_state_digest": (
                            validation.project_state_digest
                            if validation.valid
                            and validation.project_state_digest is not None
                            else prior_project_digest
                        ),
                    }
                )
                record.plan = plan
                record.state = next_state
            if not validation.valid:
                return self._result(next_state)
            return self._execute_validated(
                record,
                start_index=start_index,
                validation=validation,
            )
        finally:
            self._release(record)

    def stop(self, run_id: str) -> ProductionRunResult:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise ValueError(self._missing_message(run_id))
            if record.state.status == "stopped" and record.stop_requested:
                return self._result(record.state)
            record.stop_requested = True
            now = _now()
            in_flight = record.in_flight_operation_id
            blocker = _blocker(
                "stopped",
                "run_stopped",
                (
                    f"This Production Run is stopped. Operation {in_flight!r} was already in flight and may finish; no later operation will start. Nothing was undone."
                    if in_flight is not None
                    else "This Production Run is stopped. Earlier project changes remain applied; nothing was undone."
                ),
            )
            state = record.state.model_copy(
                update={
                    "status": "stopped",
                    "updated_at": now,
                    "finished_at": now,
                    "blockers": _bounded_blockers(
                        [*record.state.blockers, blocker],
                        limit=MAX_RUN_BLOCKERS,
                    ),
                    "final_summary": blocker.message,
                }
            )
            record.state = state
        return self._result(state)


PRODUCTION_RUNS = ProductionRunRegistry()
