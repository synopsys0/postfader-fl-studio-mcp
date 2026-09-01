"""Closed creation-phase classification and dependency validation."""

from __future__ import annotations

from typing import Iterable, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


CreationPhase: TypeAlias = Literal[
    "preflight",
    "palette",
    "composition",
    "note_application",
    "processing",
    "finalization",
]

PHASE_ORDER: tuple[CreationPhase, ...] = (
    "preflight",
    "palette",
    "composition",
    "note_application",
    "processing",
    "finalization",
)


class PhaseModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class PhaseExecutionState(PhaseModel):
    phase: CreationPhase
    status: Literal[
        "pending", "running", "completed", "skipped", "blocked", "failed", "stopped"
    ]
    operation_ids: tuple[str, ...] = Field(default=(), max_length=64)
    completed_operation_ids: tuple[str, ...] = Field(default=(), max_length=64)
    blocker_codes: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_completed_subset(self) -> "PhaseExecutionState":
        if not set(self.completed_operation_ids).issubset(self.operation_ids):
            raise ValueError("completed phase operations must belong to the phase")
        if self.status == "completed" and set(self.completed_operation_ids) != set(
            self.operation_ids
        ):
            raise ValueError("completed phases need every operation receipt")
        if self.status == "blocked" and not self.blocker_codes:
            raise ValueError("blocked phases need a blocker code")
        return self


class CreationPhasePlan(PhaseModel):
    phases: tuple[PhaseExecutionState, ...] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def validate_order(self) -> "CreationPhasePlan":
        names = [item.phase for item in self.phases]
        if len(names) != len(set(names)):
            raise ValueError("creation phases must be unique")
        positions = [PHASE_ORDER.index(name) for name in names]
        if positions != sorted(positions):
            raise ValueError("creation phases must follow the closed phase order")
        if names[0] != "preflight" or names[-1] != "finalization":
            raise ValueError("phase plans begin with preflight and end with finalization")
        operation_ids = [
            operation_id for phase in self.phases for operation_id in phase.operation_ids
        ]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("an operation may belong to only one creation phase")
        return self


def classify_operation_phase(operation_name: str) -> CreationPhase:
    """Map the closed Production Run operation name to one internal phase."""

    if operation_name in {
        "plan_sound_palette",
        "apply_sound_palette",
        "create_sound_palette_variation",
        "select_plugin_preset",
        "select_drum_kit",
        "inspect_drum_map",
    }:
        return "palette"
    if operation_name in {
        "generate_chord_progression",
        "generate_melody",
        "generate_bassline",
        "generate_drums",
        "adapt_note_sequence",
    }:
        return "composition"
    if operation_name in {
        "prepare_pattern",
        "select_pattern",
        "write_note_sequence",
        "transform_piano_roll",
        "add_section_markers",
    }:
        return "note_application"
    if operation_name in {
        "plan_processing",
        "apply_processing_plan",
        "apply_semantic_plugin_action",
        "record_automation_value",
        "apply_verified_batch",
    }:
        return "processing"
    return "finalization"


def build_phase_plan(
    operations: Iterable[tuple[str, str]],
) -> CreationPhasePlan:
    """Group ordered ``(operation_id, operation_name)`` pairs by phase."""

    grouped: dict[CreationPhase, list[str]] = {phase: [] for phase in PHASE_ORDER}
    for operation_id, operation_name in operations:
        grouped[classify_operation_phase(operation_name)].append(operation_id)
    phases = [
        PhaseExecutionState(
            phase="preflight",
            status="pending",
        )
    ]
    phases.extend(
        PhaseExecutionState(
            phase=phase,
            status="pending" if grouped[phase] else "skipped",
            operation_ids=tuple(grouped[phase]),
        )
        for phase in PHASE_ORDER[1:-1]
    )
    phases.append(PhaseExecutionState(phase="finalization", status="pending"))
    return CreationPhasePlan(phases=tuple(phases))
