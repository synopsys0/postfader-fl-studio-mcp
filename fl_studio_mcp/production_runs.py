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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import ConfigDict, Field, StrictBool, model_validator

from .contracts import SCHEMA_VERSION, ConnectionInfo, ContractModel, ProjectSummary
from .creation_pipeline.composition_adaptation import (
    CompositionAdaptationReport,
    SoundAwareCompositionResult,
    adapt_note_sequence,
    derive_composition_profile,
)
from .creation_pipeline.context import (
    CreationRunContextSnapshot,
    PianoRollArmingReceipt,
)
from .creation_pipeline.live_readiness import (
    MCP_PROCESS_IDENTITY,
    CollectedCreationReadiness,
    collect_creation_readiness,
    refresh_creation_readiness_from_cache,
)
from .creation_pipeline.models import (
    CreationReadinessReport,
    PianoRollReadiness,
    ReadinessDimension,
    ReadinessEvidence,
)
from .creation_pipeline.models import (
    ReadinessBlocker as CreationReadinessBlocker,
)
from .creation_pipeline.outcomes import (
    ArrangementDeliveryOutcome,
    AudibleQualityOutcome,
    CreationOutcome,
    ManualHandoffItem,
    ManualHandoffOutcome,
    ProcessingOutcome,
    TechnicalExecutionOutcome,
    build_creation_outcome,
)
from .creation_pipeline.phases import (
    PHASE_ORDER,
    CreationPhasePlan,
    build_phase_plan,
    classify_operation_phase,
)
from .creation_pipeline.processing import (
    ProcessingPlan,
    ProcessingPlanReceipt,
    ProcessingRequest,
    SemanticPluginAction,
    plan_processing,
)
from .creation_pipeline.readiness import CreationReadinessService
from .creation_pipeline.semantic_actions import (
    apply_processing_plan,
    apply_semantic_plugin_action,
)
from .creation_pipeline.sound_characteristics import (
    SelectedSoundCharacteristics,
    characteristics_from_palette_assignment,
)
from .creation_pipeline.timing import RunTimingCollector, RunTimingReport
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
from .plugin_atlas import load_bundled_registry
from .readonly_inspector import ReadOnlyInspector
from .sound_selection.executor import (
    SoundSelectionApplyResult,
    convert_plugin_pad_map,
)
from .sound_selection.models import (
    MAX_ROLE_COUNT,
    DrumPadMap,
    SoundFeedbackRequest,
    SoundInventory,
    SoundPaletteAssignment,
    SoundPalettePlan,
    SoundPaletteState,
    SoundPaletteVariationPlan,
    SoundSelectionRequest,
    canonical_digest,
)
from .track_b_contracts import (
    SESSION_FINGERPRINT_PATTERN,
    ChannelGeneratorTarget,
    ExpectedPluginPresetState,
    PluginPadMap,
    PluginTarget,
    TrackBVerifiedMutation,
    VerifiedPatternSelectionWrite,
    VerifiedPluginPresetSelection,
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
MAX_PRODUCTION_OUTPUTS = MAX_PRODUCTION_OPERATIONS * 8
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
    "sound_selection",
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
        default_factory=tuple, max_length=12
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
    sound_palette: StrictBool = False
    sound_roles: tuple[str, ...] = Field(default_factory=tuple, max_length=128)
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
        if any(not item.strip() or len(item) > 64 for item in self.sound_roles):
            raise ValueError(
                "preserve.sound_roles must contain non-empty role IDs up to 64 characters"
            )
        if len({item.casefold() for item in self.sound_roles}) != len(
            self.sound_roles
        ):
            raise ValueError("preserve.sound_roles must not contain duplicates")
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
    allowed_changes: tuple[ChangeCategory, ...] = Field(min_length=1, max_length=12)
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
    output: Literal[
        "note_sequence",
        "sound_palette",
        "palette_assignment",
        "generator_target",
        "drum_map",
        "selected_preset",
        "section_variation",
        "processing_plan",
    ] = "note_sequence"
    role_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_.-]*$",
    )

    @model_validator(mode="after")
    def validate_selector(self) -> "OperationOutputReference":
        role_outputs = {"palette_assignment", "generator_target"}
        if self.output in role_outputs and self.role_id is None:
            raise ValueError(f"{self.output} references require role_id")
        if self.output not in role_outputs and self.role_id is not None:
            raise ValueError("role_id is only valid for role-scoped palette outputs")
        return self


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
    drum_map: DrumPadMap | OperationOutputReference | None = None

    @model_validator(mode="after")
    def validate_drum_map_reference(self) -> "GenerateDrumsOperation":
        if (
            isinstance(self.drum_map, OperationOutputReference)
            and self.drum_map.output != "drum_map"
        ):
            raise ValueError("generate_drums requires a drum_map output reference")
        return self


class AdaptNoteSequenceOperation(ProductionOperationBase):
    """Adapt an earlier tonal sequence to one selected sound and role."""

    operation: Literal["adapt_note_sequence"] = "adapt_note_sequence"
    sequence: OperationOutputReference
    characteristics: SelectedSoundCharacteristics | None = None
    palette_assignment: OperationOutputReference | None = None
    role_kind: Literal[
        "intro",
        "chords",
        "lead",
        "primary_bass",
        "sub_bass",
        "texture",
        "countermelody",
        "generic",
    ] = "generic"
    connected_ai_register: tuple[int, int] | None = None
    connected_ai_polyphony: int | None = Field(default=None, ge=1, le=32)

    @model_validator(mode="after")
    def validate_references(self) -> "AdaptNoteSequenceOperation":
        if self.sequence.output != "note_sequence":
            raise ValueError("adapt_note_sequence requires a note_sequence reference")
        if self.characteristics is None and self.palette_assignment is None:
            raise ValueError(
                "adapt_note_sequence requires characteristics or a palette_assignment"
            )
        if self.palette_assignment is not None:
            if self.palette_assignment.output != "palette_assignment":
                raise ValueError(
                    "adapt_note_sequence palette_assignment requires a palette_assignment output"
                )
            if (
                self.characteristics is not None
                and self.palette_assignment.role_id is not None
                and self.palette_assignment.role_id.casefold()
                != self.characteristics.role_id.casefold()
            ):
                raise ValueError(
                    "sound characteristics must identify the referenced palette role"
                )
        if self.connected_ai_register is not None:
            low, high = self.connected_ai_register
            if not 0 <= low <= high <= 131:
                raise ValueError("connected_ai_register must be within 0..131")
        return self


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
    channel_index: int | OperationOutputReference
    pattern_number: int = Field(ge=1, le=999)
    mode: Literal["append", "replace"] = "append"

    @model_validator(mode="after")
    def validate_channel_reference(self) -> "WriteNoteSequenceOperation":
        if isinstance(self.channel_index, int):
            if self.channel_index < 0:
                raise ValueError("channel_index must be non-negative")
        elif self.channel_index.output not in {
            "generator_target",
            "palette_assignment",
        }:
            raise ValueError(
                "write_note_sequence channel references need a generator_target or palette_assignment output"
            )
        return self


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


class PlanSoundPaletteOperation(ProductionOperationBase):
    operation: Literal["plan_sound_palette"] = "plan_sound_palette"
    request: SoundSelectionRequest


class ApplySoundPaletteOperation(ProductionOperationBase):
    operation: Literal["apply_sound_palette"] = "apply_sound_palette"
    palette: SoundPalettePlan | SoundPaletteVariationPlan | OperationOutputReference
    role_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=128)

    @model_validator(mode="after")
    def validate_palette_reference(self) -> "ApplySoundPaletteOperation":
        if (
            isinstance(self.palette, OperationOutputReference)
            and self.palette.output not in {"sound_palette", "section_variation"}
        ):
            raise ValueError(
                "apply_sound_palette requires a sound_palette or section_variation output"
            )
        if any(not item.strip() or len(item) > 64 for item in self.role_ids):
            raise ValueError("role_ids must contain non-empty IDs up to 64 characters")
        if len({item.casefold() for item in self.role_ids}) != len(self.role_ids):
            raise ValueError("role_ids must not contain duplicates")
        return self


class CreateSoundPaletteVariationOperation(ProductionOperationBase):
    operation: Literal["create_sound_palette_variation"] = (
        "create_sound_palette_variation"
    )
    palette: str | OperationOutputReference
    request: SoundSelectionRequest
    section: str = Field(min_length=1, max_length=128)
    replace_roles: tuple[str, ...] = Field(default_factory=tuple, max_length=128)

    @model_validator(mode="after")
    def validate_variation_reference(self) -> "CreateSoundPaletteVariationOperation":
        if (
            isinstance(self.palette, OperationOutputReference)
            and self.palette.output != "sound_palette"
        ):
            raise ValueError(
                "create_sound_palette_variation requires a sound_palette output"
            )
        if any(not item.strip() or len(item) > 64 for item in self.replace_roles):
            raise ValueError(
                "replace_roles must contain non-empty IDs up to 64 characters"
            )
        if len({item.casefold() for item in self.replace_roles}) != len(
            self.replace_roles
        ):
            raise ValueError("replace_roles must not contain duplicates")
        return self


class SelectPluginPresetOperation(ProductionOperationBase):
    operation: Literal["select_plugin_preset"] = "select_plugin_preset"
    target: PluginTarget | OperationOutputReference
    preset_name: str | None = Field(default=None, min_length=1, max_length=256)
    preset_index: int | None = Field(default=None, ge=0, le=999_999)
    expected_current: ExpectedPluginPresetState | None = None
    target_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    max_navigation_steps: int = Field(default=64, ge=0, le=256)
    settle_tick_limit: int = Field(default=1, ge=1, le=8)

    @model_validator(mode="after")
    def require_preset_identity(self) -> "SelectPluginPresetOperation":
        if self.preset_name is None and self.preset_index is None:
            raise ValueError("preset_name or preset_index is required")
        if (
            isinstance(self.target, OperationOutputReference)
            and self.target.output not in {"generator_target", "palette_assignment"}
        ):
            raise ValueError(
                "select_plugin_preset target requires a generator_target or palette_assignment output"
            )
        return self


class InspectDrumMapOperation(ProductionOperationBase):
    operation: Literal["inspect_drum_map"] = "inspect_drum_map"
    target: PluginTarget | OperationOutputReference
    required_roles: tuple[str, ...] = Field(
        default=("kick", "snare", "closed_hat"), max_length=32
    )

    @model_validator(mode="after")
    def validate_target_reference(self) -> "InspectDrumMapOperation":
        if (
            isinstance(self.target, OperationOutputReference)
            and self.target.output not in {"generator_target", "palette_assignment"}
        ):
            raise ValueError(
                "inspect_drum_map target requires a generator_target or palette_assignment output"
            )
        if any(not item.strip() or len(item) > 64 for item in self.required_roles):
            raise ValueError("required_roles contain an invalid role")
        return self


class SelectDrumKitOperation(ProductionOperationBase):
    operation: Literal["select_drum_kit"] = "select_drum_kit"
    target: PluginTarget | OperationOutputReference
    preset_name: str | None = Field(default=None, min_length=1, max_length=256)
    preset_index: int | None = Field(default=None, ge=0, le=999_999)
    expected_current: ExpectedPluginPresetState | None = None
    target_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    max_navigation_steps: int = Field(default=64, ge=0, le=256)
    settle_tick_limit: int = Field(default=1, ge=1, le=8)
    required_roles: tuple[str, ...] = Field(
        default=("kick", "snare", "closed_hat"), max_length=32
    )

    @model_validator(mode="after")
    def validate_drum_kit(self) -> "SelectDrumKitOperation":
        if self.preset_name is None and self.preset_index is None:
            raise ValueError("preset_name or preset_index is required")
        if (
            isinstance(self.target, OperationOutputReference)
            and self.target.output not in {"generator_target", "palette_assignment"}
        ):
            raise ValueError(
                "select_drum_kit target requires a generator_target or palette_assignment output"
            )
        if any(not item.strip() or len(item) > 64 for item in self.required_roles):
            raise ValueError("required_roles contain an invalid role")
        return self


class RecordSoundFeedbackOperation(ProductionOperationBase):
    operation: Literal["record_sound_feedback"] = "record_sound_feedback"
    feedback: SoundFeedbackRequest


class PlanProcessingOperation(ProductionOperationBase):
    """Plan conservative processing from loaded effects and semantic evidence."""

    operation: Literal["plan_processing"] = "plan_processing"
    request: ProcessingRequest


class ApplyProcessingPlanOperation(ProductionOperationBase):
    """Apply one bounded semantic plan through existing verified setters."""

    operation: Literal["apply_processing_plan"] = "apply_processing_plan"
    plan: ProcessingPlan | OperationOutputReference

    @model_validator(mode="after")
    def validate_plan_reference(self) -> "ApplyProcessingPlanOperation":
        if (
            isinstance(self.plan, OperationOutputReference)
            and self.plan.output != "processing_plan"
        ):
            raise ValueError(
                "apply_processing_plan requires a processing_plan output reference"
            )
        return self


class ApplySemanticPluginActionOperation(ProductionOperationBase):
    """Apply one already-resolved semantic action through a verified setter."""

    operation: Literal["apply_semantic_plugin_action"] = (
        "apply_semantic_plugin_action"
    )
    action: SemanticPluginAction


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
    | AdaptNoteSequenceOperation
    | PreparePatternOperation
    | SelectPatternOperation
    | WriteNoteSequenceOperation
    | TransformPianoRollOperation
    | AddSectionMarkersOperation
    | RecordAutomationValueOperation
    | ApplyVerifiedBatchOperation
    | PlanSoundPaletteOperation
    | ApplySoundPaletteOperation
    | CreateSoundPaletteVariationOperation
    | SelectPluginPresetOperation
    | InspectDrumMapOperation
    | SelectDrumKitOperation
    | RecordSoundFeedbackOperation
    | PlanProcessingOperation
    | ApplyProcessingPlanOperation
    | ApplySemanticPluginActionOperation
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


class SelectedDrumKitReceipt(ProductionRunModel):
    """Verified kit selection plus the map read after that exact preset."""

    selection: VerifiedPluginPresetSelection
    drum_map: DrumPadMap
    required_roles: tuple[str, ...] = Field(max_length=32)
    missing_roles: tuple[str, ...] = Field(default_factory=tuple, max_length=32)

    @property
    def verified(self) -> bool:
        return self.selection.verified and not self.missing_roles


class SoundFeedbackReceipt(ProductionRunModel):
    """Truthful local workflow receipt; this never claims an FL mutation."""

    palette_id: str = Field(min_length=1, max_length=128)
    role_id: str | None = Field(default=None, min_length=1, max_length=64)
    verdict: Literal["accepted", "rejected", "neutral"]
    persisted: bool
    history_path: str | None = Field(default=None, max_length=4096)
    project_mutated: Literal[False] = False


ProductionResultPayload = (
    NoteSequence
    | SoundAwareCompositionResult
    | PatternPreparation
    | VerifiedPatternSelectionWrite
    | PianoRollDispatch
    | ArrangementMarkerReceipt
    | AutomationRecordReceipt
    | VerifiedBatchResult
    | SoundPalettePlan
    | SoundPaletteState
    | SoundPaletteVariationPlan
    | SoundPaletteAssignment
    | SoundSelectionApplyResult
    | DrumPadMap
    | VerifiedPluginPresetSelection
    | SelectedDrumKitReceipt
    | SoundFeedbackReceipt
    | ProcessingPlan
    | ProcessingPlanReceipt
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
            if self.result is None:
                raise ValueError("generated receipts need a typed result")
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
    output: Literal[
        "note_sequence",
        "sound_palette",
        "palette_assignment",
        "generator_target",
        "drum_map",
        "selected_preset",
        "section_variation",
        "composition_adaptation",
        "processing_plan",
    ] = "note_sequence"
    role_id: str | None = Field(default=None, min_length=1, max_length=64)
    value: (
        NoteSequence
        | SoundPalettePlan
        | SoundPaletteState
        | SoundPaletteAssignment
        | ChannelGeneratorTarget
        | DrumPadMap
        | VerifiedPluginPresetSelection
        | SoundPaletteVariationPlan
        | CompositionAdaptationReport
        | ProcessingPlan
    )
    target_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def validate_output_value(self) -> "ProductionGeneratedOutput":
        expected: dict[str, tuple[type[Any], ...]] = {
            "note_sequence": (NoteSequence,),
            "sound_palette": (SoundPalettePlan, SoundPaletteState),
            "palette_assignment": (SoundPaletteAssignment,),
            "generator_target": (ChannelGeneratorTarget,),
            "drum_map": (DrumPadMap,),
            "selected_preset": (VerifiedPluginPresetSelection,),
            "section_variation": (SoundPaletteVariationPlan,),
            "composition_adaptation": (CompositionAdaptationReport,),
            "processing_plan": (ProcessingPlan,),
        }
        if not isinstance(self.value, expected[self.output]):
            raise ValueError(f"{self.output} output carries an incompatible value")
        if self.output in {"palette_assignment", "generator_target"}:
            if self.role_id is None:
                raise ValueError(f"{self.output} output requires role_id")
        elif self.role_id is not None:
            raise ValueError("role_id is only valid for role-scoped outputs")
        return self


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
    expected_mutation_categories: tuple[ChangeCategory, ...] = Field(max_length=12)
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
        default_factory=tuple, max_length=MAX_PRODUCTION_OUTPUTS
    )
    blockers: tuple[ProductionBlocker, ...] = Field(
        default_factory=tuple, max_length=MAX_RUN_BLOCKERS
    )
    warnings: tuple[str, ...] = Field(
        default_factory=tuple, max_length=MAX_RUN_WARNINGS
    )
    final_summary: str | None = Field(default=None, max_length=2048)
    readiness_report: CreationReadinessReport | None = None
    run_context: CreationRunContextSnapshot | None = None
    phase_plan: CreationPhasePlan | None = None
    current_phase: Literal[
        "preflight",
        "palette",
        "composition",
        "note_application",
        "processing",
        "finalization",
    ] | None = None
    timing_report: RunTimingReport | None = None
    creation_outcome: CreationOutcome | None = None
    readiness_preflight_count: int = Field(default=0, ge=0, le=MAX_PRODUCTION_ITERATIONS)
    write_mode_enabled_once: bool = False
    write_mode_active: bool = False
    write_mode_transition_attempted: bool = False
    write_mode_owned_by_run: bool = False
    write_mode_preexisting: bool = False
    write_mode_enable_count: int = Field(default=0, ge=0, le=MAX_PRODUCTION_ITERATIONS)
    write_mode_disable_count: int = Field(default=0, ge=0, le=MAX_PRODUCTION_ITERATIONS)
    write_mode_shutdown_verified: bool | None = None
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
        output_keys = [
            (item.operation_id, item.output, item.role_id)
            for item in self.generated_outputs
        ]
        if len(output_keys) != len(set(output_keys)):
            raise ValueError("generated output selectors must be unique")
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
    write_mode_enable_count: int = Field(default=0, ge=0, le=MAX_PRODUCTION_ITERATIONS)
    write_mode_disable_count: int = Field(default=0, ge=0, le=MAX_PRODUCTION_ITERATIONS)
    write_mode_active: bool = False
    write_mode_shutdown_verified: bool | None = None
    readiness_report: CreationReadinessReport | None = None
    phase_plan: CreationPhasePlan | None = None
    timing_report: RunTimingReport | None = None
    creation_outcome: CreationOutcome | None = None
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
        "sound_selection",
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
        "sound_selection",
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
    drum_map = (
        operation.drum_map
        if isinstance(operation.drum_map, DrumPadMap)
        else None
    )
    return compose_drums(
        style=operation.style,
        bars=operation.bars,
        beats_per_bar=operation.beats_per_bar,
        seed=operation.seed,
        swing=operation.swing,
        tempo_bpm=operation.tempo_bpm,
        drum_map=drum_map,
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
    return _requires_project_write(operation) or isinstance(
        operation, RecordSoundFeedbackOperation
    )


def _requires_project_write(operation: ProductionOperation) -> bool:
    return not isinstance(
        operation,
        (
            *_GENERATOR_TYPES,
            AdaptNoteSequenceOperation,
            PlanSoundPaletteOperation,
            CreateSoundPaletteVariationOperation,
            InspectDrumMapOperation,
            RecordSoundFeedbackOperation,
            PlanProcessingOperation,
        ),
    )


def _output_types(operation: ProductionOperation) -> tuple[str, ...]:
    if isinstance(operation, _GENERATOR_TYPES):
        return ("note_sequence",)
    if isinstance(operation, AdaptNoteSequenceOperation):
        return ("note_sequence", "composition_adaptation")
    if isinstance(operation, (PlanSoundPaletteOperation, ApplySoundPaletteOperation)):
        return ("sound_palette", "palette_assignment", "generator_target")
    if isinstance(operation, CreateSoundPaletteVariationOperation):
        return ("section_variation", "palette_assignment", "generator_target")
    if isinstance(operation, InspectDrumMapOperation):
        return ("drum_map",)
    if isinstance(operation, SelectPluginPresetOperation):
        return ("selected_preset",)
    if isinstance(operation, SelectDrumKitOperation):
        return ("selected_preset", "drum_map")
    if isinstance(operation, RecordSoundFeedbackOperation):
        return ()
    if isinstance(operation, PlanProcessingOperation):
        return ("processing_plan",)
    if isinstance(
        operation,
        (ApplyProcessingPlanOperation, ApplySemanticPluginActionOperation),
    ):
        # The verified receipt is retained in ProductionOperationReceipt; it
        # is not advertised as a referenceable generated output.
        return ()
    if isinstance(operation, PreparePatternOperation):
        return ("pattern_preparation",)
    if isinstance(operation, SelectPatternOperation):
        return ("pattern_selection",)
    if isinstance(operation, _PIANO_ROLL_TYPES):
        return ("piano_roll_dispatch",)
    if isinstance(operation, AddSectionMarkersOperation):
        return ("arrangement_marker_receipt",)
    if isinstance(operation, RecordAutomationValueOperation):
        return ("automation_record_receipt",)
    if isinstance(operation, ApplyVerifiedBatchOperation):
        return ("verified_batch_receipt",)
    if isinstance(operation, UnavailableProductionOperation):
        return ()
    raise AssertionError("unhandled production operation")


def _output_type(operation: ProductionOperation) -> str | None:
    """Compatibility helper for existing single-output validation."""

    outputs = _output_types(operation)
    return outputs[0] if outputs else None


def _operation_categories(
    operation: ProductionOperation,
) -> tuple[ChangeCategory, ...]:
    if isinstance(operation, _GENERATOR_TYPES):
        return ("composition",)
    if isinstance(operation, AdaptNoteSequenceOperation):
        return ("composition",)
    if isinstance(
        operation,
        (
            PlanSoundPaletteOperation,
            ApplySoundPaletteOperation,
            CreateSoundPaletteVariationOperation,
            SelectPluginPresetOperation,
            InspectDrumMapOperation,
            SelectDrumKitOperation,
            RecordSoundFeedbackOperation,
        ),
    ):
        return ("sound_selection",)
    if isinstance(operation, PlanProcessingOperation):
        return ("plugin_parameters",)
    if isinstance(
        operation,
        (ApplyProcessingPlanOperation, ApplySemanticPluginActionOperation),
    ):
        return ("plugin_parameters",)
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
        if isinstance(operation, AdaptNoteSequenceOperation):
            add("sound_aware_composition_adaptation")
            continue
        if isinstance(
            operation,
            (PlanSoundPaletteOperation, CreateSoundPaletteVariationOperation),
        ):
            add("loaded_sound_inventory")
            add("deterministic_sound_palette_planning")
            continue
        if isinstance(operation, InspectDrumMapOperation):
            add("compatible_live_fl_bridge")
            add("plugin_pad_map_readback")
            continue
        if isinstance(operation, RecordSoundFeedbackOperation):
            add("bounded_local_sound_history")
            continue
        if isinstance(operation, PlanProcessingOperation):
            add("loaded_effect_inventory")
            add("plugin_atlas_semantic_adapters")
            continue
        add("compatible_live_fl_bridge")
        add("runtime_write_mode_control")
        add("session_fingerprint_preconditions")
        if isinstance(operation, ApplySoundPaletteOperation):
            add("verified_plugin_preset_selection")
            add("deterministic_sound_palette_application")
        elif isinstance(operation, SelectPluginPresetOperation):
            add("verified_plugin_preset_selection")
        elif isinstance(operation, SelectDrumKitOperation):
            add("verified_plugin_preset_selection")
            add("plugin_pad_map_readback")
        elif isinstance(operation, _PIANO_ROLL_TYPES):
            add("armed_piano_roll_script_bridge")
        elif isinstance(operation, (PreparePatternOperation, SelectPatternOperation)):
            add("pattern_selection_and_metadata")
        elif isinstance(operation, AddSectionMarkersOperation):
            add("arrangement_section_markers")
        elif isinstance(operation, RecordAutomationValueOperation):
            add("public_rec_event_automation")
        elif isinstance(operation, ApplyVerifiedBatchOperation):
            add("closed_verified_batch_writes")
        elif isinstance(
            operation,
            (ApplyProcessingPlanOperation, ApplySemanticPluginActionOperation),
        ):
            add("semantic_plugin_controls")
            add("verified_plugin_parameter_setters")
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
        targets = [ProductionTarget(kind="pattern", index=operation.pattern_number)]
        if isinstance(operation.channel_index, int):
            targets.insert(
                0, ProductionTarget(kind="channel", index=operation.channel_index)
            )
        return targets
    if isinstance(
        operation,
        (SelectPluginPresetOperation, InspectDrumMapOperation, SelectDrumKitOperation),
    ) and not isinstance(operation.target, OperationOutputReference):
        target = operation.target
        if target.kind == "mixer_effect":
            return [ProductionTarget(kind="mixer_track", index=target.track_index)]
        return [ProductionTarget(kind="channel", index=target.channel_index)]
    if isinstance(operation, ApplySoundPaletteOperation) and isinstance(
        operation.palette, (SoundPalettePlan, SoundPaletteVariationPlan)
    ):
        targets: list[ProductionTarget] = []
        selected_roles = {item.casefold() for item in operation.role_ids}
        for assignment in operation.palette.assignments:
            if selected_roles and assignment.role_id.casefold() not in selected_roles:
                continue
            target = assignment.target
            if target is None:
                continue
            if target.kind == "mixer_effect":
                targets.append(
                    ProductionTarget(kind="mixer_track", index=target.track_index)
                )
            else:
                targets.append(
                    ProductionTarget(kind="channel", index=target.channel_index)
                )
        return targets
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
    if isinstance(operation, ApplyProcessingPlanOperation) and isinstance(
        operation.plan, ProcessingPlan
    ):
        return [
            _production_target_from_plugin(action.target)
            for action in operation.plan.actions
        ]
    if isinstance(operation, ApplySemanticPluginActionOperation):
        return [_production_target_from_plugin(operation.action.target)]
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

    unique: list[ProductionBlocker] = []
    seen: set[tuple[object, ...]] = set()
    for blocker in blockers:
        key = (
            blocker.category,
            blocker.code,
            blocker.message,
            blocker.operation_id,
            blocker.evidence,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(blocker)
    if len(unique) <= limit:
        return tuple(unique)
    omitted = len(unique) - (limit - 1)
    return (
        *tuple(unique[: limit - 1]),
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
    project_mutating_remainder = False

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
        if _requires_project_write(operation):
            project_mutating_remainder = True
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
            "sound_selection": preserve.sound_palette,
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
        if isinstance(operation, ApplySoundPaletteOperation) and preserve.sound_roles:
            preserved_roles = {item.casefold() for item in preserve.sound_roles}
            if isinstance(operation.palette, SoundPalettePlan):
                changed_roles = {
                    item.role_id.casefold()
                    for item in operation.palette.assignments
                    if not operation.role_ids
                    or item.role_id.casefold()
                    in {role.casefold() for role in operation.role_ids}
                }
                for role_id in sorted(changed_roles.intersection(preserved_roles)):
                    blockers.append(
                        _blocker(
                            "scope",
                            "preserved_sound_role",
                            f"Operation {operation.operation_id!r} would change preserved sound role {role_id!r}.",
                            operation_id=operation.operation_id,
                        )
                    )
            else:
                warnings.append(
                    f"Operation {operation.operation_id!r} references a planned palette; preserved sound roles will be checked again before its first preset change."
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
        project_mutating_remainder
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

    def estimated_palette_roles(
        operation: ApplySoundPaletteOperation,
        *,
        seen: frozenset[str] = frozenset(),
    ) -> int:
        if operation.role_ids:
            return len(operation.role_ids)
        if isinstance(
            operation.palette, (SoundPalettePlan, SoundPaletteVariationPlan)
        ):
            return len(operation.palette.assignments)
        if operation.operation_id in seen:
            return MAX_ROLE_COUNT
        producer_index = positions.get(operation.palette.operation_id)
        if producer_index is None:
            return MAX_ROLE_COUNT
        producer = operations[producer_index]
        if isinstance(producer, PlanSoundPaletteOperation):
            return len(producer.request.roles)
        if isinstance(producer, CreateSoundPaletteVariationOperation):
            return len(producer.request.roles)
        if isinstance(producer, ApplySoundPaletteOperation):
            return estimated_palette_roles(
                producer, seen=seen | {operation.operation_id}
            )
        return MAX_ROLE_COUNT

    estimated_outputs = 0
    for operation in operations:
        if isinstance(operation, PlanSoundPaletteOperation):
            estimated_outputs += 1 + len(operation.request.roles) * 2
        elif isinstance(operation, ApplySoundPaletteOperation):
            estimated_outputs += 1 + estimated_palette_roles(operation) * 2
        elif isinstance(operation, CreateSoundPaletteVariationOperation):
            estimated_outputs += 1 + len(operation.request.roles) * 2
        else:
            estimated_outputs += len(
                {
                    output
                    for output in _output_types(operation)
                    if output
                    not in {
                        "palette_assignment",
                        "generator_target",
                        "pattern_preparation",
                        "pattern_selection",
                        "piano_roll_dispatch",
                        "arrangement_marker_receipt",
                        "automation_record_receipt",
                        "verified_batch_receipt",
                    }
                }
            )
    if estimated_outputs > MAX_PRODUCTION_OUTPUTS:
        blockers.append(
            _blocker(
                "malformed_plan",
                "generated_output_limit_exceeded",
                f"This plan may produce {estimated_outputs} typed outputs; Production Runs store at most {MAX_PRODUCTION_OUTPUTS}.",
            )
        )

    for operation in operations:
        if not isinstance(operation, UnavailableProductionOperation):
            if isinstance(operation, ApplyProcessingPlanOperation) and isinstance(
                operation.plan, ProcessingPlan
            ):
                required_missing = tuple(
                    item
                    for item in operation.plan.missing_capabilities
                    if item.required
                )
                if required_missing:
                    blockers.append(
                        _blocker(
                            "unavailable_in_project",
                            "required_processing_unavailable",
                            required_missing[0].reason,
                            operation_id=operation.operation_id,
                            evidence=tuple(
                                item.reason for item in required_missing[:16]
                            ),
                        )
                    )
            if isinstance(operation, ApplySemanticPluginActionOperation):
                if (
                    operation.action.resolution.status != "resolved"
                    or operation.action.resolution.control is None
                ):
                    blockers.append(
                        _blocker(
                            "malformed_plan",
                            "semantic_control_unresolved",
                            "A semantic plug-in action must resolve its control before execution.",
                            operation_id=operation.operation_id,
                        )
                    )
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
        for reference in _operation_references(operation):
            producer_index = positions.get(reference.operation_id)
            if producer_index is None or producer_index >= index:
                continue
            producer = operations[producer_index]
            if reference.output not in _output_types(producer):
                blockers.append(
                    _blocker(
                        "malformed_plan",
                        "incompatible_output_reference",
                        f"Operation {operation.operation_id!r} needs {reference.output!r}, but {producer.operation_id!r} produces {', '.join(_output_types(producer)) or 'no typed output'}.",
                        operation_id=operation.operation_id,
                    )
                )
                continue
            if reference.role_id is not None:
                palette: SoundPalettePlan | None = None
                if isinstance(producer, ApplySoundPaletteOperation) and isinstance(
                    producer.palette, SoundPalettePlan
                ):
                    palette = producer.palette
                if palette is not None and not any(
                    item.role_id.casefold() == reference.role_id.casefold()
                    for item in palette.assignments
                ):
                    blockers.append(
                        _blocker(
                            "malformed_plan",
                            "missing_palette_role_reference",
                            f"Operation {operation.operation_id!r} refers to role {reference.role_id!r}, which the earlier palette does not assign.",
                            operation_id=operation.operation_id,
                        )
                    )
        if isinstance(operation, WriteNoteSequenceOperation) and isinstance(
            operation.sequence, OperationOutputReference
        ):
            producer_index = positions.get(operation.sequence.operation_id)
            if producer_index is not None and producer_index < index:
                producer = operations[producer_index]
                if operation.sequence.output in _output_types(producer):
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
        if isinstance(operation, ApplySoundPaletteOperation) and isinstance(
            operation.palette, (SoundPalettePlan, SoundPaletteVariationPlan)
        ):
            if operation.palette.blockers:
                blockers.append(
                    _blocker(
                        "unavailable_in_project",
                        "sound_palette_has_blockers",
                        f"Operation {operation.operation_id!r} cannot apply a palette that has unresolved blockers.",
                        operation_id=operation.operation_id,
                        evidence=operation.palette.blockers[:16],
                    )
                )
        if isinstance(operation, WriteNoteSequenceOperation) and isinstance(
            operation.sequence, NoteSequence
        ):
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
    project_mutating = [
        operation for operation in operations if _requires_project_write(operation)
    ]
    live_reading = [
        operation
        for operation in operations
        if isinstance(
            operation,
            (
                PlanSoundPaletteOperation,
                CreateSoundPaletteVariationOperation,
                InspectDrumMapOperation,
                PlanProcessingOperation,
            ),
        )
    ]
    live_operations = [*project_mutating, *live_reading]
    if not live_operations:
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
    if project_mutating and not connection.bridge_provenance_verified:
        blockers.append(
            _blocker(
                "setup_or_session",
                "bridge_provenance_unverified",
                "The running FL bridge does not match this PostFader package. Reinstall and reload the packaged bridge.",
                evidence=(connection.bridge_provenance,),
            )
        )
    if project_mutating and not connection.runtime_write_mode_control:
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

    if any(isinstance(operation, _PIANO_ROLL_TYPES) for operation in project_mutating):
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

    project_digest = (
        _project_state_digest(project) if project_mutating else None
    )

    track_inspector = TrackBInspector()
    blockers.extend(
        _validate_live_targets(
            request,
            live_operations,
            project,
            connection,
            inspector,
            track_inspector,
        )
    )
    blockers.extend(
        _validate_live_operation_capabilities(
            live_operations,
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
        if (
            project_digest is not None
            and _project_state_digest(final_project) != project_digest
        ):
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


def _readiness_production_blocker(
    blocker: CreationReadinessBlocker,
) -> ProductionBlocker:
    category: BlockerCategory
    if blocker.dimension in {"connection_bridge", "piano_roll"}:
        category = "setup_or_session"
    elif blocker.dimension == "scope_manual_work":
        category = "scope"
    else:
        category = "unavailable_in_project"
    return _blocker(
        category,
        blocker.code,
        blocker.message,
        evidence=tuple(item.detail for item in blocker.evidence[:16]),
    )


def _merge_structural_readiness_blockers(
    report: CreationReadinessReport,
    blockers: tuple[ProductionBlocker, ...] | list[ProductionBlocker],
) -> CreationReadinessReport:
    if not blockers:
        return report
    converted = tuple(
        CreationReadinessBlocker(
            code=blocker.code,
            dimension="scope_manual_work",
            message=blocker.message,
            classification="blocking",
            evidence=tuple(
                ReadinessEvidence(source="production-run-validation", detail=item)
                for item in blocker.evidence
            )
            or (
                ReadinessEvidence(
                    source="production-run-validation", detail=blocker.message
                ),
            ),
        )
        for blocker in blockers
    )
    by_key = {
        (item.dimension, item.code, item.message): item for item in report.blockers
    }
    for item in converted:
        by_key.setdefault((item.dimension, item.code, item.message), item)
    dimensions: list[ReadinessDimension] = []
    for dimension in report.dimensions:
        if dimension.name != "scope_manual_work":
            dimensions.append(dimension)
            continue
        codes = tuple(
            dict.fromkeys(
                (*dimension.blocker_codes, *(item.code for item in converted))
            )
        )
        dimensions.append(
            dimension.model_copy(
                update={
                    "state": "blocked",
                    "summary": "The submitted run has structural, scope, or capability blockers.",
                    "blocker_codes": codes,
                }
            )
        )
    return report.model_copy(
        update={
            "overall_state": "blocked",
            "score": min(report.score, 85.0),
            "dimensions": tuple(dimensions),
            "blockers": tuple(by_key.values()),
        }
    )


def _collect_run_readiness(
    request: ProductionRunRequest,
    plan: ProductionRunPlan,
    *,
    start_index: int = 0,
    structural_blockers: tuple[ProductionBlocker, ...] = (),
) -> tuple[CreationReadinessReport, CollectedCreationReadiness]:
    operations = plan.operations[start_index:]
    required_categories = tuple(
        dict.fromkeys(
            category
            for operation in operations
            if _is_mutating(operation)
            for category in _operation_categories(operation)
        )
    )
    collected = collect_creation_readiness(
        operations=operations,
        completion_target_text=request.completion_target,
        allowed_mutation_categories=request.allowed_changes,
        required_mutation_categories=required_categories,
        preserved_targets=request.preserve.targets,
        unavailable_operations=tuple(
            operation.operation
            for operation in operations
            if isinstance(operation, UnavailableProductionOperation)
        ),
    )
    report = CreationReadinessService().evaluate(collected.readiness_input)
    report = _merge_structural_readiness_blockers(report, structural_blockers)
    return report, collected


def _refresh_cached_readiness_for_continuation(
    collected: CollectedCreationReadiness,
    operations: tuple[ProductionOperation, ...],
    existing_report: CreationReadinessReport | None,
    *,
    request: ProductionRunRequest,
    required_mutation_categories: tuple[ChangeCategory, ...] = (),
    unavailable_operations: tuple[str, ...] = (),
) -> tuple[CreationReadinessReport, CollectedCreationReadiness]:
    """Refresh recoverable setup and re-evaluate the remaining request."""

    cached = collected.readiness_input.piano_roll
    piano_required = any(isinstance(operation, _PIANO_ROLL_TYPES) for operation in operations)
    refreshed = collected
    if piano_required and not cached.armed_this_process:
        try:
            status = PIANO_ROLL.status()
        except Exception:
            status = None
        if status is not None and status.armed_this_session:
            refreshed_piano = PianoRollReadiness(
                required=True,
                apply_script_present=status.script_exists,
                armed_this_process=True,
                authenticated_arming_receipt=True,
                arming_receipt_id=status.last_request_id,
                target_selection_supported=status.automatic_trigger_supported,
                persistence_receipt_supported=status.automatic_trigger_supported,
                manual_action=None,
            )
            arming = PianoRollArmingReceipt(
                receipt_id="piano-"
                + hashlib.sha256(
                    f"{MCP_PROCESS_IDENTITY}:{status.last_request_id or 'armed'}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:24],
                process_identity=MCP_PROCESS_IDENTITY,
                authenticated=True,
                script_present=status.script_exists,
                captured_at=_now(),
            )
            context = refreshed.context_snapshot.model_copy(
                update={"piano_roll_arming_receipt": arming}
            )
            readiness_input = refreshed.readiness_input.model_copy(
                update={
                    "piano_roll": refreshed_piano,
                    "context_snapshot": context,
                }
            )
            refreshed = replace(
                refreshed,
                readiness_input=readiness_input,
                context_snapshot=context,
                target_refresh_count=refreshed.target_refresh_count + 1,
            )

    # ``existing_report`` describes the prior request and is intentionally not
    # reused.  Every request/remaining-plan dependent dimension is rebuilt
    # from the cached live collection; no inventory rescan is performed.
    del existing_report
    return refresh_creation_readiness_from_cache(
        refreshed,
        operations=operations,
        completion_target_text=request.completion_target,
        allowed_mutation_categories=request.allowed_changes,
        required_mutation_categories=required_mutation_categories,
        preserved_targets=request.preserve.targets,
        unavailable_operations=unavailable_operations,
    )


def _creation_plan_needs_readiness(
    operations: tuple[ProductionOperation, ...],
    *,
    start_index: int = 0,
) -> bool:
    """Whether the remaining plan is a multi-stage live creation workflow."""

    remaining = operations[start_index:]
    # Processing planning is read-only, but it still needs the bounded loaded-
    # effect snapshot.  Evaluate it before the write check so a standalone
    # processing operation cannot silently plan against an empty plug-in pool.
    # Sound-palette-only plans retain their existing process-local behavior.
    if any(isinstance(operation, PlanProcessingOperation) for operation in remaining):
        return True
    if not any(_requires_project_write(operation) for operation in remaining):
        return False
    if any(
        isinstance(
            operation,
            (PlanSoundPaletteOperation, CreateSoundPaletteVariationOperation),
        )
        for operation in remaining
    ):
        return True
    has_composition = any(
        isinstance(operation, (*_GENERATOR_TYPES, AdaptNoteSequenceOperation))
        for operation in remaining
    )
    has_note_application = any(
        isinstance(operation, _PIANO_ROLL_TYPES) for operation in remaining
    )
    return has_composition and has_note_application


def _cached_live_validation(
    request: ProductionRunRequest,
    plan: ProductionRunPlan,
    collected: CollectedCreationReadiness,
    *,
    start_index: int,
) -> tuple[list[ProductionBlocker], list[str], str | None, str | None]:
    """Validate operation-specific facts from the one readiness collection."""

    operations = list(plan.operations[start_index:])
    project_mutating = [
        operation for operation in operations if _requires_project_write(operation)
    ]
    live_reading = [
        operation
        for operation in operations
        if isinstance(
            operation,
            (
                PlanSoundPaletteOperation,
                CreateSoundPaletteVariationOperation,
                InspectDrumMapOperation,
                PlanProcessingOperation,
            ),
        )
    ]
    live_operations = [*project_mutating, *live_reading]
    if not live_operations:
        return [], [], None, None

    blockers: list[ProductionBlocker] = []
    connection = collected.connection
    if connection is None:
        connection = collected.readiness_input.connection.connection_info
    if connection is None or not connection.connected or not connection.compatible:
        return (
            [
                _blocker(
                    "setup_or_session",
                    "fl_connection_unavailable",
                    "No compatible FL Studio bridge is connected.",
                )
            ],
            [],
            None,
            None,
        )
    if project_mutating and not connection.bridge_provenance_verified:
        blockers.append(
            _blocker(
                "setup_or_session",
                "bridge_provenance_unverified",
                "The running FL bridge does not match this PostFader package. Reinstall and reload the packaged bridge.",
                evidence=(connection.bridge_provenance,),
            )
        )
    if project_mutating and not connection.runtime_write_mode_control:
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
        return blockers, [], None, None
    project = collected.project
    if project is None:
        blockers.append(
            _blocker(
                "setup_or_session",
                "project_inspection_failed",
                "The live project could not be inspected before execution.",
            )
        )
        return blockers, [], session, None

    patterns = collected.patterns
    for operation in live_operations:
        for target in _operation_targets(operation):
            if target.index is None:
                continue
            if target.kind == "pattern":
                unavailable = (
                    patterns is not None
                    and target.index > patterns.maximum_pattern_number
                )
                if patterns is None:
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

    facts = _LiveFacts(
        connection=connection,
        mixer_names=dict(collected.mixer_names),
        channel_names=dict(collected.channel_names),
        pattern_names=(
            {}
            if patterns is None
            else {item.pattern_number: item.name for item in patterns.patterns}
        ),
        playlist_names=dict(collected.playlist_names),
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
    for operation in live_operations:
        for target in _operation_targets(operation):
            if (
                request.scope.kind == "selected_targets"
                and target.index not in resolved_scope[target.kind]
            ):
                blockers.append(
                    _blocker(
                        "scope",
                        "target_outside_named_scope",
                        f"Operation {operation.operation_id!r} targets {target.kind} {target.index}, outside the resolved selected targets.",
                        operation_id=operation.operation_id,
                    )
                )
            if (
                target.index in resolved_preserve[target.kind]
                and not _target_explicitly_allowed(operation, target)
            ):
                blockers.append(
                    _blocker(
                        "scope",
                        "preserved_named_target",
                        f"Operation {operation.operation_id!r} targets preserved {target.kind} {target.index}.",
                        operation_id=operation.operation_id,
                    )
                )

    inventory = collected.sound_inventory
    loaded_parameter_counts = (
        {}
        if inventory is None
        else {
            (
                item.target.kind,
                getattr(item.target, "track_index", None),
                getattr(item.target, "slot_index", None),
                getattr(item.target, "channel_index", None),
            ): item.reported_parameter_count
            for item in inventory.loaded_targets
        }
    )
    route_destinations = dict(collected.mixer_route_destinations)
    for operation in live_operations:
        if isinstance(operation, ApplyVerifiedBatchOperation):
            for item in operation.operations:
                if item.operation == "plugin_parameter":
                    target = item.target
                    key = (
                        target.kind,
                        getattr(target, "track_index", None),
                        getattr(target, "slot_index", None),
                        getattr(target, "channel_index", None),
                    )
                    count = loaded_parameter_counts.get(key)
                    if key not in loaded_parameter_counts:
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
                elif item.operation == "mixer_send_level" and (
                    item.destination_track_index
                    not in route_destinations.get(item.track_index, ())
                ):
                    blockers.append(
                        _blocker(
                            "unavailable_in_project",
                            "mixer_send_unavailable",
                            f"Batch {operation.operation_id!r} cannot set this send level because the send does not currently exist.",
                            operation_id=operation.operation_id,
                        )
                    )
        if isinstance(operation, RecordAutomationValueOperation) and not (
            project.transport.playing is True
            and project.transport.recording is True
        ):
            blockers.append(
                _blocker(
                    "unavailable_in_project",
                    "automation_capture_not_active",
                    f"Operation {operation.operation_id!r} needs playback and recording to already be active.",
                    operation_id=operation.operation_id,
                )
            )
    project_digest = _project_state_digest(project) if project_mutating else None
    return blockers, [], session, project_digest


def creation_readiness(
    request: ProductionRunRequest,
    plan: ProductionRunPlan,
) -> CreationReadinessReport:
    """Run the same zero-mutation readiness scorecard used by execution."""

    structural, _warnings = _structural_validation(request, plan)
    report, collected = _collect_run_readiness(
        request,
        plan,
        structural_blockers=tuple(structural),
    )
    live: list[ProductionBlocker] = []
    if not structural:
        live, _live_warnings, _session, _project = _cached_live_validation(
            request,
            plan,
            collected,
            start_index=0,
        )
    return _merge_structural_readiness_blockers(
        report,
        _bounded_blockers(live, limit=MAX_VALIDATION_BLOCKERS),
    )


def plan_live_processing(request: ProcessingRequest) -> ProcessingPlan:
    """Inspect loaded effects once and return a read-only semantic plan."""

    collected = collect_creation_readiness(
        operations=(),
        completion_target_text=request.completion_target,
        allowed_mutation_categories=("plugin_parameters",),
        required_mutation_categories=(),
    )
    session = collected.context_snapshot.session_fingerprint
    prepared = request
    if prepared.session_fingerprint is None and session is not None:
        prepared = prepared.model_copy(update={"session_fingerprint": session})
    return plan_processing(
        prepared,
        loaded_plugins=collected.loaded_processing_observations,
        registry=load_bundled_registry(),
    )


# ---------------------------------------------------------------------------
# Existing-operation adapters and run execution
# ---------------------------------------------------------------------------


def _resolve_sequence(
    operation: WriteNoteSequenceOperation,
    outputs: dict[tuple[str, str, str | None], ProductionGeneratedOutput],
) -> NoteSequence:
    if isinstance(operation.sequence, NoteSequence):
        return operation.sequence
    value = _resolve_output_reference(operation.sequence, outputs)
    if not isinstance(value, NoteSequence):
        raise ValueError("the referenced output is not a note sequence")
    return value


def _output_key(
    operation_id: str, output: str, role_id: str | None
) -> tuple[str, str, str | None]:
    return operation_id, output, None if role_id is None else role_id.casefold()


def _resolve_output_record(
    reference: OperationOutputReference,
    outputs: dict[tuple[str, str, str | None], ProductionGeneratedOutput],
) -> ProductionGeneratedOutput:
    output = outputs.get(
        _output_key(reference.operation_id, reference.output, reference.role_id)
    )
    if output is None and reference.role_id is None:
        # Preserve the private adapter shape used by v0.20 tests and callers;
        # registry-owned state always uses the fully typed selector key.
        output = cast(Any, outputs).get(reference.operation_id)
    if output is None:
        raise ValueError(
            f"{reference.output} output {reference.operation_id!r}"
            + (
                ""
                if reference.role_id is None
                else f" for role {reference.role_id!r}"
            )
            + " is unavailable"
        )
    return cast(ProductionGeneratedOutput, output)


def _resolve_output_reference(
    reference: OperationOutputReference,
    outputs: dict[tuple[str, str, str | None], ProductionGeneratedOutput],
) -> object:
    return _resolve_output_record(reference, outputs).value


def _resolve_plugin_target_with_proof(
    value: PluginTarget | OperationOutputReference,
    outputs: dict[tuple[str, str, str | None], ProductionGeneratedOutput],
) -> tuple[PluginTarget, str | None]:
    if not isinstance(value, OperationOutputReference):
        return value, None
    output = _resolve_output_record(value, outputs)
    resolved = output.value
    if isinstance(resolved, ChannelGeneratorTarget):
        return resolved, output.target_fingerprint
    if isinstance(resolved, SoundPaletteAssignment):
        if resolved.target is None:
            raise ValueError(
                f"palette role {resolved.role_id!r} has no loaded plug-in target"
            )
        assignment_fingerprint = resolved.target_fingerprint
        output_fingerprint = output.target_fingerprint
        if (
            assignment_fingerprint is not None
            and output_fingerprint is not None
            and assignment_fingerprint != output_fingerprint
        ):
            raise ValueError(
                f"palette role {resolved.role_id!r} has conflicting target fingerprints"
            )
        return resolved.target, assignment_fingerprint or output_fingerprint
    raise ValueError("the referenced palette output does not identify a plug-in target")


def _resolve_plugin_target(
    value: PluginTarget | OperationOutputReference,
    outputs: dict[tuple[str, str, str | None], ProductionGeneratedOutput],
) -> PluginTarget:
    return _resolve_plugin_target_with_proof(value, outputs)[0]


def _resolve_operation_plugin_target(
    value: PluginTarget | OperationOutputReference,
    expected_fingerprint: str | None,
    outputs: dict[tuple[str, str, str | None], ProductionGeneratedOutput],
) -> tuple[PluginTarget, str | None]:
    target, referenced_fingerprint = _resolve_plugin_target_with_proof(
        value, outputs
    )
    if (
        expected_fingerprint is not None
        and referenced_fingerprint is not None
        and expected_fingerprint != referenced_fingerprint
    ):
        raise ValueError(
            "the operation target_fingerprint does not match the referenced target"
        )
    if (
        isinstance(value, OperationOutputReference)
        and expected_fingerprint is None
        and referenced_fingerprint is None
    ):
        raise ValueError(
            "a referenced plug-in target must carry target_fingerprint proof"
        )
    return target, expected_fingerprint or referenced_fingerprint


def _resolve_channel_index(
    value: int | OperationOutputReference,
    outputs: dict[tuple[str, str, str | None], ProductionGeneratedOutput],
) -> int:
    return _resolve_channel_target(value, outputs)[0]


def _resolve_channel_target(
    value: int | OperationOutputReference,
    outputs: dict[tuple[str, str, str | None], ProductionGeneratedOutput],
) -> tuple[int, str | None]:
    """Resolve a Piano Roll channel and retain any typed target proof."""

    if isinstance(value, int):
        return value, None
    target, target_fingerprint = _resolve_plugin_target_with_proof(value, outputs)
    if not isinstance(target, ChannelGeneratorTarget):
        raise ValueError("Piano Roll writes require a Channel Rack generator target")
    return target.channel_index, target_fingerprint


def _normalise_drum_role(role: str) -> str:
    return "_".join(role.strip().casefold().replace("-", "_").split())


def _resolve_drum_map(
    value: DrumPadMap | OperationOutputReference | None,
    outputs: dict[tuple[str, str, str | None], ProductionGeneratedOutput],
    sound_inventory: SoundInventory | None = None,
    *,
    required_roles: tuple[str, ...] = (),
) -> DrumPadMap | None:
    if isinstance(value, DrumPadMap):
        return value
    if value is None:
        candidates = () if sound_inventory is None else tuple(
            item.pad_map
            for item in sound_inventory.loaded_generators
            if item.pad_map is not None
        )
        unique: dict[str, DrumPadMap] = {}
        for candidate in candidates:
            assert candidate is not None
            unique.setdefault(canonical_digest(candidate), candidate)
        if len(unique) == 1:
            candidate = next(iter(unique.values()))
            mapped = {
                _normalise_drum_role(mapping.role)
                for mapping in candidate.mappings
            }
            declared_missing = {
                _normalise_drum_role(role)
                for role in candidate.missing_roles
            }
            required = {_normalise_drum_role(role) for role in required_roles}
            if required & declared_missing or required - mapped:
                # An implicitly discovered map is optional.  If it cannot
                # cover this style, return None so compose_drums uses its
                # disclosed General MIDI fallback.  Explicit maps still flow
                # through unchanged and retain their strict validation.
                return None
            return candidate
        if len(unique) > 1:
            raise ValueError(
                "multiple loaded drum generators expose pad maps; reference the selected drum_map output explicitly"
            )
        return None
    resolved = _resolve_output_reference(value, outputs)
    if not isinstance(resolved, DrumPadMap):
        raise ValueError("the referenced output is not a drum map")
    return resolved


def _semantic_drum_map(
    observed: PluginPadMap,
    *,
    required_roles: tuple[str, ...],
) -> DrumPadMap:
    result = convert_plugin_pad_map(
        observed,
        observed.plugin.target,
        target_fingerprint=observed.plugin.target_fingerprint,
        required_roles=required_roles,
    )
    if result is None:
        raise ValueError("FL did not return a usable plug-in pad map")
    return result


def _generated_outputs_for(
    operation: ProductionOperation,
    result: ProductionResultPayload,
) -> tuple[ProductionGeneratedOutput, ...]:
    rows: list[ProductionGeneratedOutput] = []

    def add(
        output: str,
        value: object,
        role_id: str | None = None,
        *,
        target_fingerprint: str | None = None,
    ) -> None:
        rows.append(
            ProductionGeneratedOutput(
                operation_id=operation.operation_id,
                output=cast(Any, output),
                role_id=role_id,
                value=cast(Any, value),
                target_fingerprint=target_fingerprint,
            )
        )

    if isinstance(result, NoteSequence):
        add("note_sequence", result)
    elif isinstance(result, SoundAwareCompositionResult):
        add("note_sequence", result.sequence)
        add("composition_adaptation", result.adaptation)
    elif isinstance(result, SoundSelectionApplyResult):
        add("sound_palette", result.state)
        verified_ids = {
            item.assignment_id
            for item in result.assignment_receipts
            if item.verified
        }
        for assignment in result.assignment_scope:
            if assignment.assignment_id not in verified_ids:
                continue
            add(
                "palette_assignment",
                assignment,
                assignment.role_id,
                target_fingerprint=assignment.target_fingerprint,
            )
            if isinstance(assignment.target, ChannelGeneratorTarget):
                add(
                    "generator_target",
                    assignment.target,
                    assignment.role_id,
                    target_fingerprint=assignment.target_fingerprint,
                )
    elif isinstance(result, (SoundPalettePlan, SoundPaletteState)):
        add("sound_palette", result)
        for assignment in result.assignments:
            add(
                "palette_assignment",
                assignment,
                assignment.role_id,
                target_fingerprint=assignment.target_fingerprint,
            )
            if isinstance(assignment.target, ChannelGeneratorTarget):
                add(
                    "generator_target",
                    assignment.target,
                    assignment.role_id,
                    target_fingerprint=assignment.target_fingerprint,
                )
    elif isinstance(result, SoundPaletteVariationPlan):
        add("section_variation", result)
        for assignment in result.assignments:
            add(
                "palette_assignment",
                assignment,
                assignment.role_id,
                target_fingerprint=assignment.target_fingerprint,
            )
            if isinstance(assignment.target, ChannelGeneratorTarget):
                add(
                    "generator_target",
                    assignment.target,
                    assignment.role_id,
                    target_fingerprint=assignment.target_fingerprint,
                )
    elif isinstance(result, DrumPadMap):
        add("drum_map", result)
    elif isinstance(result, VerifiedPluginPresetSelection):
        add("selected_preset", result)
    elif isinstance(result, SelectedDrumKitReceipt):
        add("selected_preset", result.selection)
        if result.verified:
            add("drum_map", result.drum_map)
    elif isinstance(result, ProcessingPlan):
        add("processing_plan", result)
    return tuple(rows)


def _read_only_result_blocker(
    operation: ProductionOperation,
    result: ProductionResultPayload,
) -> ProductionBlocker | None:
    if isinstance(result, SoundPalettePlan) and result.blockers:
        return _blocker(
            "unavailable_in_project",
            "sound_palette_planning_blocked",
            result.blockers[0],
            operation_id=operation.operation_id,
            evidence=result.blockers[:16],
        )
    if isinstance(result, SoundPaletteVariationPlan) and result.blockers:
        return _blocker(
            "unavailable_in_project",
            "sound_palette_variation_blocked",
            result.blockers[0],
            operation_id=operation.operation_id,
            evidence=result.blockers[:16],
        )
    if isinstance(operation, InspectDrumMapOperation) and isinstance(
        result, DrumPadMap
    ):
        missing = result.missing_required(operation.required_roles)
        if missing:
            return _blocker(
                "unavailable_in_project",
                "required_drum_roles_unmapped",
                "The selected drum instrument does not expose mappings for: "
                + ", ".join(missing)
                + ".",
                operation_id=operation.operation_id,
                evidence=tuple(missing),
            )
    if isinstance(result, ProcessingPlan):
        required = tuple(
            item for item in result.missing_capabilities if item.required
        )
        if required:
            return _blocker(
                "unavailable_in_project",
                "required_processing_unavailable",
                required[0].reason,
                operation_id=operation.operation_id,
                evidence=tuple(item.reason for item in required[:16]),
            )
    return None


def _operation_dependencies(operation: ProductionOperation) -> tuple[str, ...]:
    dependencies = list(operation.after)
    dependencies.extend(
        reference.operation_id for reference in _operation_references(operation)
    )
    return tuple(dict.fromkeys(dependencies))


def _operation_references(
    operation: ProductionOperation,
) -> tuple[OperationOutputReference, ...]:
    references: list[OperationOutputReference] = []
    if isinstance(operation, GenerateDrumsOperation) and isinstance(
        operation.drum_map, OperationOutputReference
    ):
        references.append(operation.drum_map)
    if isinstance(operation, AdaptNoteSequenceOperation):
        references.append(operation.sequence)
        if operation.palette_assignment is not None:
            references.append(operation.palette_assignment)
    if isinstance(operation, WriteNoteSequenceOperation):
        if isinstance(operation.sequence, OperationOutputReference):
            references.append(operation.sequence)
        if isinstance(operation.channel_index, OperationOutputReference):
            references.append(operation.channel_index)
    if isinstance(operation, ApplySoundPaletteOperation) and isinstance(
        operation.palette, OperationOutputReference
    ):
        references.append(operation.palette)
    if isinstance(operation, CreateSoundPaletteVariationOperation) and isinstance(
        operation.palette, OperationOutputReference
    ):
        references.append(operation.palette)
    if isinstance(
        operation,
        (
            SelectPluginPresetOperation,
            InspectDrumMapOperation,
            SelectDrumKitOperation,
        ),
    ) and isinstance(operation.target, OperationOutputReference):
        references.append(operation.target)
    if isinstance(operation, ApplyProcessingPlanOperation) and isinstance(
        operation.plan, OperationOutputReference
    ):
        references.append(operation.plan)
    return tuple(references)


def _semantic_session_matches(*, action: SemanticPluginAction) -> bool:
    """Narrow session guard used immediately before one semantic write."""

    connection = ReadOnlyInspector().connection_info()
    return bool(
        connection.connected
        and connection.compatible
        and connection.bridge_provenance_verified
        and connection.verified_writes_enabled
        and action.session_fingerprint is not None
        and connection.session_fingerprint == action.session_fingerprint
    )


def _semantic_target_matches(*, action: SemanticPluginAction) -> bool:
    """Refresh only the selected effect and compare its observation identity."""

    control = action.resolution.control
    if control is None or action.target_fingerprint is None:
        return False
    page = TrackBInspector().plugin_parameters(
        target=action.target,
        offset=control.parameter_index,
        limit=1,
        allow_master=action.allow_master,
    )
    return page.plugin.target_fingerprint == action.target_fingerprint


def _apply_semantic_processing_plan(plan: ProcessingPlan) -> ProcessingPlanReceipt:
    controller = TrackBController()
    return apply_processing_plan(
        plan,
        setter_callbacks={
            "display": controller.set_plugin_parameter_display,
            "option": controller.set_plugin_parameter_option,
            "normalized": controller.set_plugin_parameter,
        },
        session_checker=_semantic_session_matches,
        target_checker=_semantic_target_matches,
    )


def _apply_one_semantic_action(
    action: SemanticPluginAction,
) -> ProcessingPlanReceipt:
    controller = TrackBController()
    return apply_semantic_plugin_action(
        action,
        setter_callbacks={
            "display": controller.set_plugin_parameter_display,
            "option": controller.set_plugin_parameter_option,
            "normalized": controller.set_plugin_parameter,
        },
        session_checker=_semantic_session_matches,
        target_checker=_semantic_target_matches,
    )


def _dispatch_operation(
    operation: ProductionOperation,
    *,
    session_fingerprint: str | None,
    outputs: dict[tuple[str, str, str | None], ProductionGeneratedOutput],
    sound_inventory: SoundInventory | None = None,
    processing_observations: tuple[dict[str, Any], ...] = (),
) -> ProductionResultPayload:
    """Adapt one production operation to an existing PostFader implementation."""

    if isinstance(operation, GenerateDrumsOperation):
        return compose_drums(
            style=operation.style,
            bars=operation.bars,
            beats_per_bar=operation.beats_per_bar,
            seed=operation.seed,
            swing=operation.swing,
            tempo_bpm=operation.tempo_bpm,
            drum_map=_resolve_drum_map(
                operation.drum_map,
                outputs,
                sound_inventory,
                required_roles=(
                    "kick",
                    "snare",
                    "closed_hat",
                    *(("open_hat",) if operation.style == "house" else ()),
                ),
            ),
        )
    if isinstance(operation, AdaptNoteSequenceOperation):
        source = _resolve_output_reference(operation.sequence, outputs)
        if not isinstance(source, NoteSequence):
            raise ValueError("the referenced output is not a note sequence")
        characteristics = operation.characteristics
        if operation.palette_assignment is not None:
            assignment = _resolve_output_reference(
                operation.palette_assignment, outputs
            )
            if not isinstance(assignment, SoundPaletteAssignment):
                raise ValueError("the referenced output is not a palette assignment")
            if characteristics is None:
                characteristics = characteristics_from_palette_assignment(assignment)
            if assignment.role_id.casefold() != characteristics.role_id.casefold():
                raise ValueError(
                    "the selected-sound characteristics do not match the palette role"
                )
            if (
                characteristics.assignment_id is not None
                and characteristics.assignment_id
                != assignment.assignment_id
            ):
                raise ValueError(
                    "the selected-sound characteristics identify another assignment"
                )
        if characteristics is None:
            raise ValueError("selected-sound characteristics are unavailable")
        profile = derive_composition_profile(
            characteristics,
            role_kind=operation.role_kind,
            connected_ai_register=operation.connected_ai_register,
            connected_ai_polyphony=operation.connected_ai_polyphony,
        )
        return adapt_note_sequence(source, profile)
    if isinstance(operation, _GENERATOR_TYPES):
        return _generate_sequence(operation)
    if isinstance(operation, PlanSoundPaletteOperation):
        from .sound_selection.executor import SOUND_SELECTION

        return SOUND_SELECTION.plan(operation.request, inventory=sound_inventory)
    if isinstance(operation, PlanProcessingOperation):
        request = operation.request
        if request.session_fingerprint is None and session_fingerprint is not None:
            request = request.model_copy(
                update={"session_fingerprint": session_fingerprint}
            )
        return plan_processing(
            request,
            loaded_plugins=processing_observations,
            registry=load_bundled_registry(),
        )
    if isinstance(operation, CreateSoundPaletteVariationOperation):
        from .sound_selection.executor import SOUND_SELECTION

        if isinstance(operation.palette, str):
            palette_id = operation.palette
        else:
            source = _resolve_output_reference(operation.palette, outputs)
            if not isinstance(source, (SoundPalettePlan, SoundPaletteState)):
                raise ValueError("the referenced output is not a sound palette")
            palette_id = source.palette_id
        variation_kwargs: dict[str, Any] = {
            "section": operation.section,
            "replace_roles": operation.replace_roles,
        }
        if sound_inventory is not None:
            variation_kwargs["inventory"] = sound_inventory
        return SOUND_SELECTION.create_variation(
            palette_id,
            operation.request,
            **variation_kwargs,
        )
    if isinstance(operation, InspectDrumMapOperation):
        target = _resolve_plugin_target(operation.target, outputs)
        observed = TrackBInspector().inspect_plugin_pad_map(target=target)
        return _semantic_drum_map(
            observed,
            required_roles=operation.required_roles,
        )
    if isinstance(operation, RecordSoundFeedbackOperation):
        from .sound_selection.executor import SOUND_SELECTION

        recorded = SOUND_SELECTION.record_feedback(operation.feedback)
        if isinstance(recorded, SoundFeedbackReceipt):
            return recorded
        persisted = bool(getattr(recorded, "persisted", recorded))
        history_path = getattr(recorded, "history_path", None)
        if history_path is None:
            history_path = getattr(getattr(recorded, "history", None), "path", None)
        return SoundFeedbackReceipt(
            palette_id=operation.feedback.palette_id,
            role_id=operation.feedback.role_id,
            verdict=operation.feedback.verdict,
            persisted=persisted,
            history_path=history_path,
        )
    if isinstance(operation, UnavailableProductionOperation):
        raise ValueError(_UNAVAILABLE_OPERATION_MESSAGES[operation.operation])
    if session_fingerprint is None:
        raise ValueError("a project mutation requires the captured session fingerprint")
    if isinstance(operation, ApplySoundPaletteOperation):
        from .sound_selection.executor import SOUND_SELECTION

        if isinstance(
            operation.palette, (SoundPalettePlan, SoundPaletteVariationPlan)
        ):
            palette: SoundPalettePlan | SoundPaletteVariationPlan | str = (
                operation.palette
            )
        else:
            source = _resolve_output_reference(operation.palette, outputs)
            if isinstance(source, SoundPalettePlan):
                palette = source
            elif isinstance(source, SoundPaletteState):
                palette = source.palette_id
            elif isinstance(source, SoundPaletteVariationPlan):
                palette = source
            else:
                raise ValueError(
                    "the referenced output is not a sound palette or section variation"
                )
        apply_kwargs: dict[str, Any] = {
            "session_fingerprint": session_fingerprint,
            "authorized_to_modify": True,
            "role_ids": operation.role_ids,
            "write_mode_already_enabled": True,
        }
        if sound_inventory is not None:
            apply_kwargs["inventory"] = sound_inventory
        result = SOUND_SELECTION.apply(palette, **apply_kwargs)
        if not isinstance(result, SoundSelectionApplyResult):
            raise ValueError("Sound Selection returned an invalid application result")
        return result
    if isinstance(operation, ApplyProcessingPlanOperation):
        if isinstance(operation.plan, ProcessingPlan):
            processing_plan = operation.plan
        else:
            resolved_plan = _resolve_output_reference(operation.plan, outputs)
            if not isinstance(resolved_plan, ProcessingPlan):
                raise ValueError("the referenced output is not a processing plan")
            processing_plan = resolved_plan
        if (
            processing_plan.session_fingerprint is not None
            and processing_plan.session_fingerprint != session_fingerprint
        ):
            raise ValueError(
                "the processing plan belongs to another FL Studio session"
            )
        if any(
            action.session_fingerprint is not None
            and action.session_fingerprint != session_fingerprint
            for action in processing_plan.actions
        ):
            raise ValueError(
                "a semantic processing action belongs to another FL Studio session"
            )
        processing_plan = processing_plan.model_copy(
            update={
                "session_fingerprint": session_fingerprint,
                "actions": tuple(
                    (
                        action
                        if action.session_fingerprint is not None
                        else action.model_copy(
                            update={"session_fingerprint": session_fingerprint}
                        )
                    )
                    for action in processing_plan.actions
                ),
            }
        )
        return _apply_semantic_processing_plan(processing_plan)
    if isinstance(operation, ApplySemanticPluginActionOperation):
        action = operation.action
        if (
            action.session_fingerprint is not None
            and action.session_fingerprint != session_fingerprint
        ):
            raise ValueError(
                "the semantic processing action belongs to another FL Studio session"
            )
        if action.session_fingerprint is None:
            action = action.model_copy(
                update={"session_fingerprint": session_fingerprint}
            )
        return _apply_one_semantic_action(action)
    if isinstance(operation, SelectPluginPresetOperation):
        target, target_fingerprint = _resolve_operation_plugin_target(
            operation.target, operation.target_fingerprint, outputs
        )
        return TrackBController().select_plugin_preset(
            target=target,
            preset_name=operation.preset_name,
            preset_index=operation.preset_index,
            expected_current=operation.expected_current,
            session_fingerprint=session_fingerprint,
            target_fingerprint=target_fingerprint,
            max_navigation_steps=operation.max_navigation_steps,
            settle_tick_limit=operation.settle_tick_limit,
        )
    if isinstance(operation, SelectDrumKitOperation):
        target, target_fingerprint = _resolve_operation_plugin_target(
            operation.target, operation.target_fingerprint, outputs
        )
        selection = TrackBController().select_plugin_preset(
            target=target,
            preset_name=operation.preset_name,
            preset_index=operation.preset_index,
            expected_current=operation.expected_current,
            session_fingerprint=session_fingerprint,
            target_fingerprint=target_fingerprint,
            max_navigation_steps=operation.max_navigation_steps,
            settle_tick_limit=operation.settle_tick_limit,
        )
        if not selection.verified:
            return selection
        observed = TrackBInspector().inspect_plugin_pad_map(target=target)
        if observed.session_fingerprint != session_fingerprint:
            raise ValueError("FL changed sessions before the selected drum map was read")
        observed_fingerprint = observed.plugin.target_fingerprint
        if (
            target_fingerprint is not None
            and observed_fingerprint != target_fingerprint
        ):
            raise ValueError(
                "FL changed the selected drum target before its pad map was read"
            )
        drum_map = _semantic_drum_map(
            observed,
            required_roles=operation.required_roles,
        )
        return SelectedDrumKitReceipt(
            selection=selection,
            drum_map=drum_map,
            required_roles=operation.required_roles,
            missing_roles=drum_map.missing_required(operation.required_roles),
        )
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
        channel_index, target_fingerprint = _resolve_channel_target(
            operation.channel_index, outputs
        )
        if target_fingerprint is None:
            return write_piano_roll_notes(
                sequence.notes,
                channel_index=channel_index,
                pattern_number=operation.pattern_number,
                mode=operation.mode,
                auto_trigger=True,
                session_fingerprint=session_fingerprint,
            )
        return write_piano_roll_notes(
            sequence.notes,
            channel_index=channel_index,
            pattern_number=operation.pattern_number,
            mode=operation.mode,
            auto_trigger=True,
            session_fingerprint=session_fingerprint,
            target_fingerprint=target_fingerprint,
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


def _production_target_from_plugin(target: PluginTarget) -> ProductionTarget:
    if target.kind == "mixer_effect":
        return ProductionTarget(kind="mixer_track", index=target.track_index)
    return ProductionTarget(kind="channel", index=target.channel_index)


def _runtime_target_allowed(
    request: ProductionRunRequest,
    operation: ProductionOperation,
    target: PluginTarget,
) -> ProductionBlocker | None:
    production_target = _production_target_from_plugin(target)
    for preserved in request.preserve.targets:
        if _same_index_target(production_target, preserved) and not _target_explicitly_allowed(
            operation, production_target
        ):
            return _blocker(
                "scope",
                "preserved_target",
                f"Operation {operation.operation_id!r} targets preserved {production_target.kind} {production_target.index}.",
                operation_id=operation.operation_id,
            )
    if request.scope.kind != "selected_targets":
        return None
    indexed = tuple(
        item
        for item in request.scope.targets
        if item.kind == production_target.kind and item.index is not None
    )
    if any(_same_index_target(production_target, item) for item in indexed):
        return None
    named = tuple(
        item
        for item in request.scope.targets
        if item.kind == production_target.kind and item.name is not None
    )
    if named:
        try:
            if production_target.kind == "channel":
                live_names = {
                    item.channel_index: item.name
                    for item in TrackBInspector().list_channels().channels
                }
            else:
                live_names = {
                    item.index: item.name
                    for item in ReadOnlyInspector()
                    .list_mixer_tracks(only_used=False, include_peaks=False)
                    .tracks
                }
        except Exception as exc:
            return _blocker(
                "setup_or_session",
                "dynamic_sound_target_check_failed",
                f"Operation {operation.operation_id!r} could not resolve its selected target before writing: {exc}",
                operation_id=operation.operation_id,
            )
        name = live_names.get(cast(int, production_target.index), "")
        if any(item.name and item.name.casefold() == name.casefold() for item in named):
            return None
    return _blocker(
        "scope",
        "target_outside_scope",
        f"Operation {operation.operation_id!r} resolved to {production_target.kind} {production_target.index}, outside the selected targets.",
        operation_id=operation.operation_id,
    )


def _runtime_preflight_blocker(
    request: ProductionRunRequest,
    operation: ProductionOperation,
    outputs: dict[tuple[str, str, str | None], ProductionGeneratedOutput],
    *,
    expected_session: str | None,
) -> ProductionBlocker | None:
    """Resolve dynamic references and enforce scope before write mode changes."""

    try:
        if isinstance(operation, ApplySoundPaletteOperation):
            if isinstance(
                operation.palette,
                (SoundPalettePlan, SoundPaletteVariationPlan),
            ):
                palette: SoundPalettePlan | SoundPaletteState | SoundPaletteVariationPlan = (
                    operation.palette
                )
            else:
                resolved = _resolve_output_reference(operation.palette, outputs)
                if not isinstance(
                    resolved,
                    (SoundPalettePlan, SoundPaletteState, SoundPaletteVariationPlan),
                ):
                    raise ValueError(
                        "the referenced output is not a sound palette or section variation"
                    )
                palette = resolved
            palette_blockers = palette.blockers
            if isinstance(palette, SoundPaletteVariationPlan):
                from .sound_selection.executor import SOUND_SELECTION

                lookup = SOUND_SELECTION.lookup(palette.base_palette_id)
                if not lookup.found or lookup.state is None:
                    return _blocker(
                        "setup_or_session",
                        "sound_palette_process_state_missing",
                        "This section variation needs its process-local base Sound Palette. Plan it again in this MCP process before applying the variation.",
                        operation_id=operation.operation_id,
                    )
                palette_session = lookup.state.session_identity
                palette_blockers = tuple(
                    dict.fromkeys((*lookup.state.blockers, *palette.blockers))
                )
            elif isinstance(palette, SoundPalettePlan):
                palette_session = palette.inventory_session_fingerprint
            else:
                palette_session = palette.session_identity
            if palette_blockers:
                return _blocker(
                    "unavailable_in_project",
                    "sound_palette_has_blockers",
                    palette_blockers[0],
                    operation_id=operation.operation_id,
                    evidence=palette_blockers[:16],
                )
            if (
                expected_session is not None
                and palette_session is not None
                and palette_session != expected_session
            ):
                return _blocker(
                    "setup_or_session",
                    "sound_palette_session_changed",
                    "The sound palette belongs to a different FL bridge session.",
                    operation_id=operation.operation_id,
                )
            selected_roles = {item.casefold() for item in operation.role_ids}
            available_roles = {
                item.role_id.casefold() for item in palette.assignments
            }
            missing_roles = selected_roles - available_roles
            if missing_roles:
                return _blocker(
                    "malformed_plan",
                    "sound_palette_role_missing",
                    "The sound palette does not assign requested roles: "
                    + ", ".join(sorted(missing_roles))
                    + ".",
                    operation_id=operation.operation_id,
                    evidence=tuple(sorted(missing_roles)),
                )
            preserved_roles = {
                item.casefold() for item in request.preserve.sound_roles
            }
            for assignment in palette.assignments:
                if selected_roles and assignment.role_id.casefold() not in selected_roles:
                    continue
                if assignment.role_id.casefold() in preserved_roles:
                    return _blocker(
                        "scope",
                        "preserved_sound_role",
                        f"Operation {operation.operation_id!r} would change preserved sound role {assignment.role_id!r}.",
                        operation_id=operation.operation_id,
                    )
                if assignment.target is None:
                    return _blocker(
                        "unavailable_in_project",
                        "sound_assignment_target_missing",
                        f"Sound role {assignment.role_id!r} has no loaded target.",
                        operation_id=operation.operation_id,
                    )
                denied = _runtime_target_allowed(
                    request, operation, assignment.target
                )
                if denied is not None:
                    return denied
            return None
        if isinstance(operation, WriteNoteSequenceOperation) and isinstance(
            operation.channel_index, OperationOutputReference
        ):
            target, target_fingerprint = _resolve_plugin_target_with_proof(
                operation.channel_index, outputs
            )
            if not isinstance(target, ChannelGeneratorTarget):
                raise ValueError(
                    "Piano Roll writes require a Channel Rack generator target"
                )
            if target_fingerprint is None:
                return _blocker(
                    "malformed_plan",
                    "target_fingerprint_unavailable",
                    f"Operation {operation.operation_id!r} references a plug-in target without target_fingerprint proof.",
                    operation_id=operation.operation_id,
                )
            return _runtime_target_allowed(request, operation, target)
        if isinstance(
            operation, (SelectPluginPresetOperation, SelectDrumKitOperation)
        ) and isinstance(
            operation.target, OperationOutputReference
        ):
            target, target_fingerprint = _resolve_plugin_target_with_proof(
                operation.target, outputs
            )
            if (
                operation.target_fingerprint is not None
                and target_fingerprint is not None
                and operation.target_fingerprint != target_fingerprint
            ):
                return _blocker(
                    "malformed_plan",
                    "target_fingerprint_mismatch",
                    f"Operation {operation.operation_id!r} target_fingerprint does not match its referenced target.",
                    operation_id=operation.operation_id,
                )
            if operation.target_fingerprint is None and target_fingerprint is None:
                return _blocker(
                    "malformed_plan",
                    "target_fingerprint_unavailable",
                    f"Operation {operation.operation_id!r} references a plug-in target without target_fingerprint proof.",
                    operation_id=operation.operation_id,
                )
            return _runtime_target_allowed(request, operation, target)
        if isinstance(operation, ApplyProcessingPlanOperation):
            if isinstance(operation.plan, ProcessingPlan):
                semantic_plan = operation.plan
            else:
                resolved = _resolve_output_reference(operation.plan, outputs)
                if not isinstance(resolved, ProcessingPlan):
                    raise ValueError(
                        "the referenced output is not a processing plan"
                    )
                semantic_plan = resolved
            for action in semantic_plan.actions:
                if action.target_fingerprint is None:
                    return _blocker(
                        "malformed_plan",
                        "semantic_target_fingerprint_unavailable",
                        f"Semantic action {action.action_id!r} lacks target-fingerprint proof.",
                        operation_id=operation.operation_id,
                    )
                denied = _runtime_target_allowed(request, operation, action.target)
                if denied is not None:
                    return denied
            return None
        if isinstance(operation, ApplySemanticPluginActionOperation):
            if operation.action.target_fingerprint is None:
                return _blocker(
                    "malformed_plan",
                    "semantic_target_fingerprint_unavailable",
                    f"Semantic action {operation.action.action_id!r} lacks target-fingerprint proof.",
                    operation_id=operation.operation_id,
                )
            return _runtime_target_allowed(
                request, operation, operation.action.target
            )
    except Exception as exc:
        return _blocker(
            "malformed_plan",
            "dynamic_output_reference_unavailable",
            f"Operation {operation.operation_id!r} could not resolve its earlier output: {exc}",
            operation_id=operation.operation_id,
        )
    return None


def _capture_project_state(
    expected_session: str,
    *,
    require_write_mode: bool = True,
) -> tuple[str | None, str]:
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
    if require_write_mode and not connection.verified_writes_enabled:
        return None, "Write mode is no longer active for this Production Run."
    return _project_state_digest(project), ""


def _current_session_matches(
    expected: str,
    expected_project_state_digest: str | None = None,
) -> tuple[bool, str]:
    if expected_project_state_digest is None:
        try:
            connection = ReadOnlyInspector().connection_info()
        except Exception as exc:
            return False, f"The live FL session could not be checked: {exc}"
        if not connection.connected or not connection.compatible:
            return (
                False,
                connection.error
                or connection.compatibility_reason
                or "No compatible FL Studio bridge is connected.",
            )
        if not connection.bridge_provenance_verified:
            return False, "The running FL bridge no longer matches this package."
        if connection.session_fingerprint != expected:
            return False, "FL Studio reloaded the bridge during this run."
        if not connection.verified_writes_enabled:
            return False, "Write mode is no longer active for this Production Run."
        return True, ""
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

    if isinstance(result, ProcessingPlanReceipt):
        if result.verified is True and result.completed:
            return True, True, ""
        if not result.outcome_known or result.verified is None:
            return (
                False,
                False,
                "Semantic processing stopped after an unknown write outcome; no action was replayed.",
            )
        return (
            True,
            False,
            "Semantic processing stopped because a control, session, target, or readback was not verified.",
        )
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
    if isinstance(result, SoundSelectionApplyResult):
        receipts = {
            item.assignment_id: item for item in result.assignment_receipts
        }
        failed = tuple(
            assignment.role_id
            for assignment in result.assignment_scope
            if assignment.assignment_id not in receipts
            or not receipts[assignment.assignment_id].verified
        )
        verified = not failed and not result.blockers
        if verified:
            return True, True, ""
        return (
            True,
            False,
            result.blockers[0]
            if result.blockers
            else "Sound Selection did not verify palette roles: "
            + ", ".join(failed)
            + ".",
        )
    if isinstance(result, SoundPaletteState):
        failed = tuple(item for item in result.apply_receipts if not item.verified)
        verified = result.status == "applied" and not failed and bool(
            result.apply_receipts or not result.assignments
        )
        if verified:
            return True, True, ""
        return (
            True,
            False,
            result.blockers[0]
            if result.blockers
            else "Sound Selection did not verify every requested preset assignment.",
        )
    if isinstance(result, SelectedDrumKitReceipt):
        if result.verified:
            return True, True, ""
        return (
            True,
            False,
            "The drum preset changed, but required pad roles are not mapped: "
            + ", ".join(result.missing_roles)
            + ".",
        )
    if isinstance(result, SoundFeedbackReceipt):
        return True, True, ""
    if isinstance(result, (PatternPreparation, TrackBVerifiedMutation)):
        if result.verified:
            return True, True, ""
        return True, False, "FL did not verify this mutation on readback."
    if isinstance(result, PianoRollDispatch):
        if result.application_verified:
            return True, True, ""
        return (
            False,
            False,
            "FL did not return authenticated Piano Roll script-runtime evidence. Confirm the visible result before continuing this run.",
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
    outputs: dict[
        tuple[str, str, str | None], ProductionGeneratedOutput
    ] = field(default_factory=dict)
    stop_requested: bool = False
    claimed: bool = False
    in_flight_operation_id: str | None = None
    readiness: CollectedCreationReadiness | None = None
    timing: RunTimingCollector = field(default_factory=RunTimingCollector)
    active_phase: str | None = None
    # The absolute mode command may reach FL before its transport reply fails.
    # Keep this process-local uncertainty separate from the public active flag
    # so termination still performs one same-session shutdown attempt.
    write_mode_enable_pending: bool = False


class ProductionRunRegistry:
    """Bounded, deterministic, thread-safe process-local run storage."""

    def __init__(self, *, max_runs: int = MAX_PRODUCTION_RUNS) -> None:
        if type(max_runs) is not int or not 1 <= max_runs <= MAX_PRODUCTION_RUNS:
            raise ValueError(f"max_runs must be within 1..{MAX_PRODUCTION_RUNS}")
        self.max_runs = max_runs
        self._lock = threading.RLock()
        self._runs: dict[str, _RunRecord] = {}
        self._write_mode_owner_run_id: str | None = None

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
            write_mode_enable_count=state.write_mode_enable_count,
            write_mode_disable_count=state.write_mode_disable_count,
            write_mode_active=state.write_mode_active,
            write_mode_shutdown_verified=state.write_mode_shutdown_verified,
            readiness_report=state.readiness_report,
            phase_plan=state.phase_plan,
            timing_report=state.timing_report,
            creation_outcome=state.creation_outcome,
        )

    def _update_phase(
        self,
        record: _RunRecord,
        phase: str,
        status: str,
        *,
        blocker_codes: tuple[str, ...] = (),
    ) -> None:
        plan = record.state.phase_plan
        if plan is None:
            return
        rows = []
        completed = frozenset(record.state.completed_operations)
        for row in plan.phases:
            if row.phase != phase:
                rows.append(row)
                continue
            completed_ids = tuple(
                operation_id
                for operation_id in row.operation_ids
                if operation_id in completed
            )
            rows.append(
                row.model_copy(
                    update={
                        "status": status,
                        "completed_operation_ids": completed_ids,
                        "blocker_codes": blocker_codes,
                    }
                )
            )
        record.state = record.state.model_copy(
            update={
                "phase_plan": plan.model_copy(update={"phases": tuple(rows)}),
                "current_phase": phase,
                "updated_at": _now(),
            }
        )

    def _phase_boundary_blocker(
        self,
        record: _RunRecord,
        *,
        phase: str,
        start_index: int,
    ) -> ProductionBlocker | None:
        """Validate the cached phase inputs without repeating full readiness."""

        context = record.state.run_context
        if context is None:
            return None
        if (
            context.mcp_process_identity is not None
            and context.mcp_process_identity != MCP_PROCESS_IDENTITY
        ):
            return _blocker(
                "setup_or_session",
                "creation_process_context_lost",
                "This creation run belongs to another MCP process. Start a new run so PostFader can inspect the current project once.",
            )
        session = record.state.session_fingerprint
        if not context.matches_session(session):
            return _blocker(
                "setup_or_session",
                "cached_session_mismatch",
                "The cached creation context no longer matches this FL Studio session.",
            )
        if record.state.write_mode_active and session is not None:
            matches, reason = _current_session_matches(session, None)
            if not matches:
                return _blocker(
                    "setup_or_session",
                    "phase_session_precondition_failed",
                    reason,
                )

        phase_operations: list[ProductionOperation] = []
        for operation in record.plan.operations[start_index:]:
            if classify_operation_phase(operation.operation) != phase:
                break
            phase_operations.append(operation)
        for operation in phase_operations:
            for reference in _operation_references(operation):
                producer_index = next(
                    (
                        index
                        for index, producer in enumerate(record.plan.operations)
                        if producer.operation_id == reference.operation_id
                    ),
                    None,
                )
                if (
                    producer_index is not None
                    and producer_index >= start_index
                    and classify_operation_phase(
                        record.plan.operations[producer_index].operation
                    )
                    == phase
                ):
                    # Same-phase outputs are checked immediately before their
                    # consumer, after the earlier producer has run.
                    continue
                try:
                    _resolve_output_reference(reference, record.outputs)
                except Exception as exc:
                    return _blocker(
                        "malformed_plan",
                        "phase_output_unavailable",
                        f"Phase {phase!r} cannot start because an earlier typed output is unavailable: {exc}",
                        operation_id=operation.operation_id,
                    )

        if phase == "note_application" and any(
            isinstance(operation, _PIANO_ROLL_TYPES)
            for operation in phase_operations
        ):
            arming = context.piano_roll_arming_receipt
            if (
                arming is None
                or not arming.authenticated
                or arming.process_identity != MCP_PROCESS_IDENTITY
            ):
                return _blocker(
                    "setup_or_session",
                    "piano_roll_context_unavailable",
                    "Piano Roll writing needs one setup action: run Postfader Apply once in an FL Piano Roll, then continue this run.",
                )
        return None

    def _begin_phase(self, record: _RunRecord, phase: str) -> None:
        if record.active_phase == phase:
            return
        if record.active_phase is not None:
            self._end_phase(record)
        try:
            record.timing.start_phase(phase)
        except ValueError:
            # A continuation can return to its blocked phase. Merge the new
            # timing slice into that phase's immutable report row; completed
            # operation receipts remain append-only.
            record.timing.resume_phase(phase)
        record.active_phase = phase
        self._update_phase(record, phase, "running")

    def _end_phase(
        self,
        record: _RunRecord,
        *,
        status: str = "completed",
        blocker_codes: tuple[str, ...] = (),
    ) -> None:
        phase = record.active_phase
        if phase is None:
            return
        record.timing.end_phase(phase)
        record.active_phase = None
        phase_plan = record.state.phase_plan
        if status == "completed" and phase_plan is not None:
            row = next(item for item in phase_plan.phases if item.phase == phase)
            if not set(row.operation_ids).issubset(record.state.completed_operations):
                status = "pending"
        self._update_phase(
            record,
            phase,
            status,
            blocker_codes=blocker_codes,
        )

    def _build_creation_outcome(self, record: _RunRecord) -> CreationOutcome:
        state = record.state
        unknown = sum(not item.outcome_known for item in state.receipts)
        verified = sum(item.verified for item in state.receipts)
        technical_status: str
        if state.status == "completed":
            technical_status = (
                "verified_with_limitations"
                if state.warnings
                or (
                    state.readiness_report is not None
                    and state.readiness_report.overall_state
                    == "ready_with_limitations"
                )
                else "verified"
            )
        elif state.status in {"blocked", "failed", "stopped"}:
            technical_status = state.status
        else:
            technical_status = "partial"
        technical = TechnicalExecutionOutcome(
            status=cast(Any, technical_status),
            expected_operations=state.total_operations,
            attempted_operations=len(state.receipts),
            completed_operations=len(state.completed_operations),
            verified_receipts=verified,
            unknown_outcomes=unknown,
            blockers=tuple(item.message for item in state.blockers[:64]),
            limitations=(
                ()
                if state.readiness_report is None
                else tuple(item.message for item in state.readiness_report.limitations)
            ),
        )
        note_receipts = tuple(
            receipt
            for receipt in state.receipts
            if receipt.operation == "write_note_sequence" and receipt.verified
        )
        pattern_receipts = tuple(
            receipt
            for receipt in state.receipts
            if receipt.operation == "prepare_pattern" and receipt.verified
        )
        manual_text = (
            ()
            if state.readiness_report is None
            else tuple(
                item.instruction
                for item in state.readiness_report.manual_actions
                if not item.completed
            )
        )
        if note_receipts and not manual_text:
            manual_text = (
                "Place the created pattern clips on their intended Playlist tracks; PostFader cannot create or move Playlist clips through FL's API.",
            )
        handoff_items = tuple(
            ManualHandoffItem(
                action_id=f"handoff-{index}",
                dimension="arrangement",
                instruction=text,
            )
            for index, text in enumerate(manual_text, start=1)
        )
        if note_receipts and manual_text:
            arrangement_status = "patterns_created_not_placed"
        elif note_receipts or pattern_receipts:
            arrangement_status = "partial"
        else:
            arrangement_status = "not_delivered"
        pattern_numbers = {
            result.pattern_number
            for receipt in (*note_receipts, *pattern_receipts)
            if (result := receipt.result) is not None
            and isinstance(result, PatternPreparation)
        }
        pattern_numbers.update(
            result.target.pattern_number
            for receipt in note_receipts
            if isinstance((result := receipt.result), PianoRollDispatch)
            and result.target is not None
        )
        arrangement = ArrangementDeliveryOutcome(
            status=cast(Any, arrangement_status),
            pattern_count=len(pattern_numbers),
            manual_playlist_actions=handoff_items,
        )
        # Only the semantic processing operations own this outcome dimension.
        # ``apply_verified_batch`` is a general-purpose write surface: mixer,
        # tempo, routing, and even direct (non-semantic) plug-in parameter
        # writes must not masquerade as semantic processing.
        processing_operation_ids = {
            operation.operation_id
            for operation in record.plan.operations
            if isinstance(
                operation,
                (
                    PlanProcessingOperation,
                    ApplyProcessingPlanOperation,
                    ApplySemanticPluginActionOperation,
                ),
            )
        }
        processing_operation_names = {
            "plan_processing",
            "apply_processing_plan",
            "apply_semantic_plugin_action",
        }
        processing_ops = tuple(
            item
            for item in state.receipts
            if item.operation in processing_operation_names
        )
        semantic_receipts = tuple(
            item.result
            for item in processing_ops
            if isinstance(item.result, ProcessingPlanReceipt)
        )
        # A processing request is explicit in the closed operation plan.  A
        # free-text completion target is not enough to make unrelated batch
        # writes look like processing that was requested or applied.
        requested_processing = bool(processing_operation_ids)
        inline_processing_plans = tuple(
            operation.plan
            for operation in record.plan.operations
            if isinstance(operation, ApplyProcessingPlanOperation)
            and isinstance(operation.plan, ProcessingPlan)
        )
        missing_effects = tuple(
            dict.fromkeys(
                (
                    *(
                        ()
                        if not requested_processing or state.readiness_report is None
                        else tuple(
                            item.message
                            for item in (
                                *state.readiness_report.limitations,
                                *state.readiness_report.blockers,
                            )
                            if item.dimension == "mixer_effects"
                        )
                    ),
                    *(
                        item.reason
                        for plan in inline_processing_plans
                        for item in plan.missing_capabilities
                    ),
                    *(
                        item.reason
                        for output in state.generated_outputs
                        if output.output == "processing_plan"
                        and isinstance(output.value, ProcessingPlan)
                        for item in output.value.missing_capabilities
                    ),
                )
            )
        )
        applied_actions = sum(item.attempted_count for item in semantic_receipts)
        verified_actions = sum(
            result.status == "verified"
            for receipt in semantic_receipts
            for result in receipt.results
        )
        unresolved_controls = tuple(
            dict.fromkeys(
                (
                    *(
                        f"{result.action_id}: {result.status}"
                        for receipt in semantic_receipts
                        for result in receipt.results
                        if result.status != "verified"
                    ),
                    *(
                        f"{receipt.operation_id}: unknown_outcome"
                        for receipt in processing_ops
                        if not receipt.outcome_known
                    ),
                )
            )
        )
        processing_stopped = any(
            not receipt.completed or receipt.verified is not True
            for receipt in semantic_receipts
        ) or any(
            not receipt.outcome_known
            or receipt.status == "unverified"
            for receipt in processing_ops
        )
        if (
            applied_actions
            and verified_actions == applied_actions
            and not missing_effects
            and not processing_stopped
        ):
            processing_status = "restrained_first_pass"
        elif processing_stopped or applied_actions:
            processing_status = "partially_processed"
        elif requested_processing and missing_effects:
            processing_status = "dry_missing_effects"
        elif requested_processing:
            processing_status = "dry_by_design"
        else:
            processing_status = "not_requested"
        processing = ProcessingOutcome(
            status=cast(Any, processing_status),
            applied_actions=applied_actions,
            verified_actions=verified_actions,
            missing_effects=missing_effects,
            unresolved_controls=unresolved_controls,
        )
        handoff = ManualHandoffOutcome(
            status="outstanding" if handoff_items else "none",
            actions=handoff_items,
        )
        return build_creation_outcome(
            run_id=state.run_id,
            technical=technical,
            arrangement=arrangement,
            processing=processing,
            audible_quality=AudibleQualityOutcome(status="not_evaluated"),
            manual_handoff=handoff,
            timing=state.timing_report,
            readiness_state=(
                None
                if state.readiness_report is None
                else state.readiness_report.overall_state
            ),
        )

    def _finalize_run(self, record: _RunRecord) -> None:
        if record.readiness is not None and record.state.session_fingerprint is not None:
            digest, reason = _capture_project_state(
                record.state.session_fingerprint,
                require_write_mode=False,
            )
            if digest is not None:
                record.state = record.state.model_copy(
                    update={
                        "project_state_digest": digest,
                        "updated_at": _now(),
                    }
                )
            elif record.state.status == "completed":
                self._finish_blocked(
                    record,
                    _blocker(
                        "setup_or_session",
                        "final_project_checkpoint_failed",
                        "The run finished its operations, but PostFader could not verify the final FL session checkpoint.",
                        evidence=(reason or "project checkpoint unavailable",),
                    ),
                )
        if record.active_phase is not None:
            blocked = record.state.status in {"blocked", "failed", "stopped"}
            self._end_phase(
                record,
                status="blocked" if blocked else "completed",
                blocker_codes=(
                    tuple(item.code for item in record.state.blockers[-16:])
                    if blocked
                    else ()
                ),
            )
        self._begin_phase(record, "finalization")
        self._end_phase(record, status="completed")
        for phase in PHASE_ORDER:
            plan = record.state.phase_plan
            if plan is None:
                break
            row = next(item for item in plan.phases if item.phase == phase)
            if row.status == "pending":
                self._update_phase(record, phase, "skipped")
        timing = record.timing.report()
        warnings = _bounded_warnings(
            [
                *record.state.warnings,
                *(item.message for item in timing.warnings),
            ]
        )
        record.state = record.state.model_copy(
            update={"timing_report": timing, "warnings": warnings}
        )
        outcome = self._build_creation_outcome(record)
        prior_summary = record.state.final_summary
        summary = outcome.concise_summary
        if record.state.status != "completed" and prior_summary:
            summary += " " + prior_summary
        summary += " The project was not saved automatically."
        record.state = record.state.model_copy(
            update={
                "creation_outcome": outcome,
                "final_summary": summary,
                "updated_at": _now(),
            }
        )

    def execute(
        self, request: ProductionRunRequest, plan: ProductionRunPlan
    ) -> ProductionRunResult:
        record = self._create_record(request, plan)
        try:
            phase_rows = tuple(
                (operation.operation_id, operation.operation)
                for index, operation in enumerate(record.plan.operations)
                if operation.operation_id
                not in {
                    earlier.operation_id
                    for earlier in record.plan.operations[:index]
                }
            )
            phase_plan = build_phase_plan(phase_rows)
            with self._lock:
                record.state = record.state.model_copy(
                    update={"phase_plan": phase_plan, "current_phase": "preflight"}
                )
            self._begin_phase(record, "preflight")
            needs_live_readiness = _creation_plan_needs_readiness(
                record.plan.operations
            )
            validation = validate_production_run(
                record.state.request,
                record.plan,
                inspect_live=not needs_live_readiness,
            )
            readiness_report: CreationReadinessReport | None = None
            readiness_blockers: tuple[ProductionBlocker, ...] = ()
            if needs_live_readiness and validation.valid:
                try:
                    readiness_report, collected = _collect_run_readiness(
                        record.state.request,
                        record.plan,
                    )
                except Exception as exc:
                    readiness_blockers = (
                        _blocker(
                            "setup_or_session",
                            "creation_readiness_failed",
                            f"Creation readiness could not inspect the complete project setup: {exc}",
                        ),
                    )
                else:
                    record.readiness = collected
                    record.timing.record_full_inventory_scan(
                        collected.full_inventory_scan_count
                    )
                    record.timing.record_target_refresh(
                        collected.target_refresh_count
                    )
                    record.timing.record_preset_enumeration(
                        collected.preset_enumeration_count
                    )
                    cached_blockers, cached_warnings, session, project_digest = (
                        _cached_live_validation(
                            record.state.request,
                            record.plan,
                            collected,
                            start_index=0,
                        )
                    )
                    readiness_blockers = _bounded_blockers(
                        [
                            *(
                                _readiness_production_blocker(item)
                                for item in readiness_report.blockers
                            ),
                            *cached_blockers,
                        ],
                        limit=MAX_VALIDATION_BLOCKERS,
                    )
                    validation = validation.model_copy(
                        update={
                            "session_fingerprint": session,
                            "project_state_digest": project_digest,
                            "warnings": _bounded_warnings(
                                [*validation.warnings, *cached_warnings]
                            ),
                        }
                    )
            all_blockers = _bounded_blockers(
                [*validation.blockers, *readiness_blockers],
                limit=MAX_VALIDATION_BLOCKERS,
            )
            if all_blockers and validation.valid:
                validation = validation.model_copy(
                    update={
                        "valid": False,
                        "executable": False,
                        "blockers": all_blockers,
                    }
                )
            elif all_blockers != validation.blockers:
                validation = validation.model_copy(update={"blockers": all_blockers})
            self._end_phase(
                record,
                status="blocked" if not validation.valid else "completed",
                blocker_codes=tuple(item.code for item in all_blockers[:16]),
            )
            with self._lock:
                if record.stop_requested or record.state.status == "stopped":
                    self._finalize_run(record)
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
                            "session_fingerprint": validation.session_fingerprint,
                            "project_state_digest": validation.project_state_digest,
                            "readiness_report": readiness_report,
                            "run_context": (
                                None
                                if record.readiness is None
                                else record.readiness.context_snapshot
                            ),
                            "readiness_preflight_count": int(
                                record.readiness is not None
                            ),
                            "final_summary": (
                                validation.blockers[0].message
                                if validation.blockers
                                else "This run could not be validated."
                            ),
                        }
                    )
                    record.state = state
                    self._finalize_run(record)
                    return self._result(record.state)
                state = record.state.model_copy(
                    update={
                        "status": "validated",
                        "updated_at": _now(),
                        "session_fingerprint": validation.session_fingerprint,
                        "project_state_digest": validation.project_state_digest,
                        "warnings": validation.warnings,
                        "readiness_report": readiness_report,
                        "run_context": (
                            None
                            if record.readiness is None
                            else record.readiness.context_snapshot
                        ),
                        "readiness_preflight_count": int(
                            record.readiness is not None
                        ),
                    }
                )
                record.state = state
            try:
                self._execute_validated(
                    record, start_index=0, validation=validation
                )
            except Exception as exc:
                self._finish_blocked(
                    record,
                    _blocker(
                        "unknown_outcome",
                        "run_execution_failed",
                        "The run stopped after an unexpected internal execution failure. No operation was replayed.",
                        evidence=(f"{type(exc).__name__}: {exc}"[:512],),
                    ),
                )
            finally:
                self._shutdown_write_mode(record)
            self._finalize_run(record)
            return self._result(record.state)
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
            _requires_project_write(operation)
            for operation in record.plan.operations[start_index:]
        )
        if not mutation_pending:
            return None
        session = validation.session_fingerprint
        if session is None:
            blocker = _blocker(
                "setup_or_session",
                "session_fingerprint_unavailable",
                "This run cannot start because FL did not provide a session fingerprint.",
            )
            return self._finish_blocked(record, blocker)
        with self._lock:
            if record.stop_requested or record.state.status == "stopped":
                return record.state
            already_active = record.state.write_mode_active
            if self._write_mode_owner_run_id not in {None, record.state.run_id}:
                return self._finish_blocked(
                    record,
                    _blocker(
                        "setup_or_session",
                        "write_mode_owned_by_another_run",
                        "Another Production Run currently owns the FL write boundary. Continue after that run finishes.",
                    ),
                )
        if already_active:
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
                "Write mode became inactive while this run was executing. The run stopped before another project change.",
            )
            return self._finish_blocked(record, blocker)
        with self._lock:
            if record.stop_requested or record.state.status == "stopped":
                return record.state
            if self._write_mode_owner_run_id not in {None, record.state.run_id}:
                return self._finish_blocked(
                    record,
                    _blocker(
                        "setup_or_session",
                        "write_mode_owned_by_another_run",
                        "Another Production Run currently owns the FL write boundary. Continue after that run finishes.",
                    ),
                )
            self._write_mode_owner_run_id = record.state.run_id
            record.write_mode_enable_pending = True
            record.state = record.state.model_copy(
                update={
                    "write_mode_transition_attempted": True,
                    "write_mode_owned_by_run": True,
                    "updated_at": _now(),
                }
            )
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
        preexisting = bool(getattr(change, "before_enabled", False))
        with self._lock:
            record.write_mode_enable_pending = False
            if preexisting:
                self._write_mode_owner_run_id = None
            state = record.state.model_copy(
                update={
                    "write_mode_enabled_once": not preexisting,
                    "write_mode_active": True,
                    "write_mode_owned_by_run": not preexisting,
                    "write_mode_preexisting": preexisting,
                    "write_mode_enable_count": (
                        record.state.write_mode_enable_count
                        + (0 if preexisting else 1)
                    ),
                    "write_mode_shutdown_verified": None,
                    "updated_at": _now(),
                }
            )
            record.state = state
            if record.active_phase is not None:
                record.timing.record_write_mode_transition()
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

    def _shutdown_write_mode(self, record: _RunRecord) -> ProductionRunState:
        """Disable the run-scoped write gate once execution terminates."""

        with self._lock:
            state = record.state
            if not state.write_mode_active and not record.write_mode_enable_pending:
                return state
            if not state.write_mode_owned_by_run:
                record.state = state.model_copy(
                    update={
                        "write_mode_active": False,
                        "write_mode_shutdown_verified": True,
                        "updated_at": _now(),
                    }
                )
                return record.state
            session = state.session_fingerprint
        try:
            change = WriteModeManager().set_write_mode(
                enabled=False,
                confirm_user_present=False,
                session_fingerprint=session,
            )
            # Runtime returns the strict WriteModeChange contract.  A few
            # compatibility test doubles predate requested_enabled; retain
            # their narrow session proof while requiring the full contract's
            # explicit disabled-state readback in production.
            full_contract = hasattr(change, "requested_enabled")
            if change.session_fingerprint != session or (
                full_contract
                and (
                    change.requested_enabled is not False
                    or change.after_enabled is not False
                )
            ):
                raise RuntimeError(
                    "the write-mode shutdown receipt did not confirm the same session in read-only mode"
                )
        except Exception as exc:
            blocker = _blocker(
                "setup_or_session",
                "write_mode_shutdown_failed",
                "The run stopped, but PostFader could not verify that write mode was disabled. Disable it before another task.",
                evidence=(f"{type(exc).__name__}: {exc}"[:512],),
            )
            with self._lock:
                current = record.state
                update: dict[str, Any] = {
                    "write_mode_shutdown_verified": False,
                    "updated_at": _now(),
                    "warnings": _bounded_warnings(
                        [*current.warnings, blocker.message]
                    ),
                    "blockers": _bounded_blockers(
                        [*current.blockers, blocker], limit=MAX_RUN_BLOCKERS
                    ),
                }
                if current.status == "completed":
                    update.update(
                        status="blocked",
                        finished_at=_now(),
                        final_summary=blocker.message,
                    )
                state = current.model_copy(update=update)
                record.state = state
                return state
        with self._lock:
            current = record.state
            if self._write_mode_owner_run_id == current.run_id:
                self._write_mode_owner_run_id = None
            record.write_mode_enable_pending = False
            state = current.model_copy(
                update={
                    "write_mode_active": False,
                    "write_mode_owned_by_run": False,
                    "write_mode_disable_count": current.write_mode_disable_count + 1,
                    "write_mode_shutdown_verified": True,
                    "updated_at": _now(),
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
        if _requires_project_write(operation) and session_fingerprint is not None:
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
            (
                "unknown_outcome"
                if _requires_project_write(operation)
                else "malformed_plan"
            ),
            "operation_failed_unknown",
            (
                f"Operation {operation.operation_id!r} stopped with an unknown mutation outcome. It was not retried."
                if _requires_project_write(operation)
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
        session = record.state.session_fingerprint
        for index in range(start_index, len(record.plan.operations)):
            operation = record.plan.operations[index]
            phase = classify_operation_phase(operation.operation)
            if record.active_phase != phase:
                if record.active_phase is not None:
                    self._end_phase(record)
                boundary_blocker = self._phase_boundary_blocker(
                    record,
                    phase=phase,
                    start_index=index,
                )
                if boundary_blocker is not None:
                    self._update_phase(
                        record,
                        phase,
                        "blocked",
                        blocker_codes=(boundary_blocker.code,),
                    )
                    state = self._finish_blocked(record, boundary_blocker)
                    return self._result(state)
            self._begin_phase(record, phase)
            if record.active_phase == phase:
                record.timing.record_operation()
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

            if _requires_project_write(operation):
                runtime_blocker = _runtime_preflight_blocker(
                    record.state.request,
                    operation,
                    record.outputs,
                    expected_session=record.state.session_fingerprint,
                )
                if runtime_blocker is not None:
                    state = self._finish_blocked(record, runtime_blocker)
                    return self._result(state)
                if not record.state.write_mode_active:
                    mode_failure = self._enable_write_mode(
                        record,
                        validation=validation,
                        start_index=index,
                    )
                    if mode_failure is not None:
                        return self._result(mode_failure)
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
                    (
                        record.state.project_state_digest
                        if record.readiness is None
                        else None
                    ),
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
                        sound_inventory=(
                            None
                            if record.readiness is None
                            else record.readiness.sound_inventory
                        ),
                        processing_observations=(
                            ()
                            if record.readiness is None
                            else record.readiness.loaded_processing_observations
                        ),
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

            if not _is_mutating(operation):
                if (
                    isinstance(operation, PlanSoundPaletteOperation)
                    and record.active_phase == phase
                ):
                    # The full loaded inventory came from preflight; planning
                    # consumes that immutable snapshot without another scan.
                    pass
                generated = _generated_outputs_for(operation, result)
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
                    if (
                        len(record.state.generated_outputs) + len(generated)
                        > MAX_PRODUCTION_OUTPUTS
                    ):
                        state = self._finish_blocked(
                            record,
                            _blocker(
                                "malformed_plan",
                                "generated_output_limit_exceeded",
                                "This run reached its bounded typed-output limit before another project change.",
                                operation_id=operation.operation_id,
                            ),
                        )
                        return self._result(state)
                    for output in generated:
                        record.outputs[
                            _output_key(
                                output.operation_id,
                                output.output,
                                output.role_id,
                            )
                        ] = output
                    state = record.state.model_copy(
                        update={
                            "receipts": (*record.state.receipts, receipt),
                            "generated_outputs": (
                                *record.state.generated_outputs,
                                *generated,
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
                blocker = _read_only_result_blocker(operation, result)
                if blocker is not None:
                    state = self._finish_blocked(
                        record,
                        blocker,
                        current_operation_index=index + 1,
                    )
                    return self._result(state)
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
            if (
                isinstance(operation, WriteNoteSequenceOperation)
                and record.active_phase == phase
            ):
                record.timing.record_piano_roll_dispatch()
            if isinstance(
                operation,
                (SelectPluginPresetOperation, SelectDrumKitOperation),
            ) and record.active_phase == phase:
                selection = (
                    result.selection
                    if isinstance(result, SelectedDrumKitReceipt)
                    else result
                )
                steps = (
                    selection.navigation_steps
                    if isinstance(selection, VerifiedPluginPresetSelection)
                    else 0
                )
                record.timing.record_preset_navigation(steps)
            if (
                isinstance(operation, ApplySoundPaletteOperation)
                and isinstance(result, SoundSelectionApplyResult)
                and record.active_phase == phase
            ):
                record.timing.record_preset_navigation(
                    sum(item.navigation_steps for item in result.receipts)
                )
            if (
                isinstance(
                    operation,
                    (
                        ApplyProcessingPlanOperation,
                        ApplySemanticPluginActionOperation,
                    ),
                )
                and isinstance(result, ProcessingPlanReceipt)
                and record.active_phase == phase
            ):
                record.timing.record_target_refresh(
                    sum(
                        item.status
                        not in {
                            "blocked",
                            "unresolved_control",
                            "stale_session",
                        }
                        for item in result.results
                    )
                )
            checkpoint_digest: str | None = None
            checkpoint_reason = ""
            if _requires_project_write(operation) and session is not None:
                if record.readiness is None:
                    checkpoint_digest, checkpoint_reason = _capture_project_state(
                        session
                    )
                else:
                    matches, checkpoint_reason = _current_session_matches(
                        session, None
                    )
                    if matches:
                        # The cached creation path deliberately avoids a full
                        # project summary after every mutation.  A single
                        # read-only checkpoint is captured at finalization.
                        checkpoint_digest = record.state.project_state_digest
            generated = _generated_outputs_for(operation, result) if verified else ()
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
                if (
                    len(record.state.generated_outputs) + len(generated)
                    > MAX_PRODUCTION_OUTPUTS
                ):
                    state = self._finish_blocked(
                        record,
                        _blocker(
                            "malformed_plan",
                            "generated_output_limit_exceeded",
                            "This run reached its bounded typed-output limit before another project change.",
                            operation_id=operation.operation_id,
                        ),
                        current_operation_index=index + 1,
                    )
                    return self._result(state)
                for output in generated:
                    record.outputs[
                        _output_key(
                            output.operation_id,
                            output.output,
                            output.role_id,
                        )
                    ] = output
                update: dict[str, Any] = {
                    "receipts": (*record.state.receipts, receipt),
                    "generated_outputs": (
                        *record.state.generated_outputs,
                        *generated,
                    ),
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
            if (
                verified
                and _requires_project_write(operation)
                and checkpoint_digest is None
            ):
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
            cached_creation_path = record.readiness is not None or (
                _creation_plan_needs_readiness(
                    plan.operations,
                    start_index=start_index,
                )
            )
            validation = validate_production_run(
                request,
                plan,
                inspect_live=not cached_creation_path,
                start_index=start_index,
                completed_operation_ids=completed_operation_ids,
            )
            continued_readiness_report = record.state.readiness_report
            if cached_creation_path and validation.valid:
                current_digest: str | None = None
                checkpoint_reason = ""
                if prior_session is not None:
                    current_digest, checkpoint_reason = _capture_project_state(
                        prior_session,
                        require_write_mode=False,
                    )
                    if current_digest is None:
                        validation = validation.model_copy(
                            update={
                                "valid": False,
                                "executable": False,
                                "blockers": _bounded_blockers(
                                    [
                                        *validation.blockers,
                                        _blocker(
                                            "setup_or_session",
                                            "continued_session_changed",
                                            checkpoint_reason
                                            or "This run no longer matches the current FL Studio session.",
                                        ),
                                    ],
                                    limit=MAX_VALIDATION_BLOCKERS,
                                ),
                            }
                        )
                    elif (
                        prior_project_digest is not None
                        and current_digest != prior_project_digest
                    ):
                        validation = validation.model_copy(
                            update={
                                "valid": False,
                                "executable": False,
                                "blockers": _bounded_blockers(
                                    [
                                        *validation.blockers,
                                        _blocker(
                                            "setup_or_session",
                                            "continued_project_state_changed",
                                            "The open project changed after this run's last verified checkpoint. Inspect the new state and submit a revised run.",
                                        ),
                                    ],
                                    limit=MAX_VALIDATION_BLOCKERS,
                                ),
                            }
                        )
                if validation.valid:
                    if record.readiness is None:
                        try:
                            continued_readiness_report, collected = (
                                _collect_run_readiness(
                                    request,
                                    plan,
                                    start_index=start_index,
                                )
                            )
                        except Exception as exc:
                            validation = validation.model_copy(
                                update={
                                    "valid": False,
                                    "executable": False,
                                    "blockers": (
                                        _blocker(
                                            "setup_or_session",
                                            "creation_readiness_failed",
                                            f"Creation readiness could not inspect the remaining setup: {exc}",
                                        ),
                                    ),
                                }
                            )
                        else:
                            record.readiness = collected
                            record.timing.record_full_inventory_scan(
                                collected.full_inventory_scan_count
                            )
                            record.timing.record_target_refresh(
                                collected.target_refresh_count
                            )
                            record.timing.record_preset_enumeration(
                                collected.preset_enumeration_count
                            )
                    else:
                        prior_target_refresh_count = record.readiness.target_refresh_count
                        continued_readiness_report, refreshed = (
                            _refresh_cached_readiness_for_continuation(
                                record.readiness,
                                plan.operations[start_index:],
                                continued_readiness_report,
                                request=request,
                                required_mutation_categories=tuple(
                                    dict.fromkeys(
                                        category
                                        for operation in plan.operations[start_index:]
                                        if _is_mutating(operation)
                                        for category in _operation_categories(operation)
                                    )
                                ),
                                unavailable_operations=tuple(
                                    operation.operation
                                    for operation in plan.operations[start_index:]
                                    if isinstance(
                                        operation, UnavailableProductionOperation
                                    )
                                ),
                            )
                        )
                        if (
                            refreshed.target_refresh_count
                            > prior_target_refresh_count
                        ):
                            record.timing.record_target_refresh(
                                refreshed.target_refresh_count
                                - prior_target_refresh_count
                            )
                        if refreshed is not record.readiness:
                            record.readiness = refreshed
                    if validation.valid and record.readiness is not None:
                        if continued_readiness_report is None:
                            continued_readiness_report = (
                                CreationReadinessService().evaluate(
                                    record.readiness.readiness_input
                                )
                            )
                        cached_blockers, cached_warnings, cached_session, _ = (
                            _cached_live_validation(
                                request,
                                plan,
                                record.readiness,
                                start_index=start_index,
                            )
                        )
                        readiness_blockers = tuple(
                            _readiness_production_blocker(item)
                            for item in continued_readiness_report.blockers
                        )
                        combined = _bounded_blockers(
                            [
                                *validation.blockers,
                                *readiness_blockers,
                                *cached_blockers,
                            ],
                            limit=MAX_VALIDATION_BLOCKERS,
                        )
                        validation = validation.model_copy(
                            update={
                                "valid": not combined,
                                "executable": not combined,
                                "session_fingerprint": prior_session
                                or cached_session,
                                "project_state_digest": current_digest,
                                "blockers": combined,
                                "warnings": _bounded_warnings(
                                    [*validation.warnings, *cached_warnings]
                                ),
                            }
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
                refreshed_phase_plan = build_phase_plan(
                    (operation.operation_id, operation.operation)
                    for index, operation in enumerate(plan.operations)
                    if operation.operation_id
                    not in {
                        earlier.operation_id for earlier in plan.operations[:index]
                    }
                )
                completed_ids = frozenset(record.state.completed_operations)
                refreshed_phase_plan = refreshed_phase_plan.model_copy(
                    update={
                        "phases": tuple(
                            row.model_copy(
                                update={
                                    "status": (
                                        "skipped"
                                        if not row.operation_ids
                                        and row.phase
                                        not in {"preflight", "finalization"}
                                        else "completed"
                                        if row.phase == "preflight"
                                        or (
                                            row.operation_ids
                                            and set(row.operation_ids).issubset(
                                                completed_ids
                                            )
                                        )
                                        else "pending"
                                    ),
                                    "completed_operation_ids": tuple(
                                        item
                                        for item in row.operation_ids
                                        if item in completed_ids
                                    ),
                                    "blocker_codes": (),
                                }
                            )
                            for row in refreshed_phase_plan.phases
                        )
                    }
                )
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
                        "phase_plan": refreshed_phase_plan,
                        "current_phase": (
                            "finalization"
                            if start_index >= len(plan.operations)
                            else classify_operation_phase(
                                plan.operations[start_index].operation
                            )
                        ),
                        "creation_outcome": None,
                        "readiness_report": continued_readiness_report,
                        "run_context": (
                            record.state.run_context
                            if record.readiness is None
                            else record.readiness.context_snapshot
                        ),
                        "readiness_preflight_count": (
                            record.state.readiness_preflight_count
                            + int(
                                record.state.run_context is None
                                and record.readiness is not None
                            )
                        ),
                    }
                )
                record.plan = plan
                record.state = next_state
            if not validation.valid:
                self._finalize_run(record)
                return self._result(record.state)
            try:
                self._execute_validated(
                    record,
                    start_index=start_index,
                    validation=validation,
                )
            except Exception as exc:
                self._finish_blocked(
                    record,
                    _blocker(
                        "unknown_outcome",
                        "continued_run_execution_failed",
                        "The continued run stopped after an unexpected internal failure. No operation was replayed.",
                        evidence=(f"{type(exc).__name__}: {exc}"[:512],),
                    ),
                )
            finally:
                self._shutdown_write_mode(record)
            self._finalize_run(record)
            return self._result(record.state)
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
            # An enable request is itself an in-flight write-boundary
            # transition.  Let the execution thread observe the stop and run
            # the ordered shutdown after that request returns; disabling here
            # could otherwise race ahead of the pending enable reply.
            can_finalize = (
                record.in_flight_operation_id is None
                and not record.write_mode_enable_pending
            )
        if can_finalize:
            self._shutdown_write_mode(record)
            self._finalize_run(record)
        return self._result(record.state)


PRODUCTION_RUNS = ProductionRunRegistry()
