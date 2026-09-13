"""Truthful, multi-dimensional completion contracts for creation runs."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import AliasChoices, Field, model_validator

from .models import MAX_ACTIONS, MAX_ROLES, MAX_TEXT, CreationPipelineModel, Identifier
from .timing import RunTimingReport


TechnicalExecutionStatus = Literal[
    "verified", "verified_with_limitations", "partial", "blocked", "failed", "stopped"
]
ArrangementDeliveryStatus = Literal[
    "playable",
    "playable_manual_playlist_handoff",
    "patterns_created_not_placed",
    "partial",
    "not_delivered",
]
ProcessingStatus = Literal[
    "processed",
    "restrained_first_pass",
    "dry_by_design",
    "dry_missing_effects",
    "partially_processed",
    "not_requested",
]
AudibleQualityStatus = Literal[
    "not_evaluated",
    "user_confirmed_draft",
    "user_approved",
    "user_rejected",
    "bounce_analysis_available",
    "bounce_analysis_passed",
    "bounce_analysis_needs_revision",
]
ManualHandoffStatus = Literal["none", "outstanding", "completed", "partial"]
OutcomeStatus = Literal["completed", "partial", "blocked", "failed", "stopped"]


class ManualHandoffItem(CreationPipelineModel):
    """One known action outside the supported bridge surface."""

    action_id: Identifier
    instruction: str = Field(min_length=1, max_length=MAX_TEXT)
    status: Literal["outstanding", "completed", "optional"] = "outstanding"
    dimension: str = Field(min_length=1, max_length=64)


class TechnicalExecutionOutcome(CreationPipelineModel):
    """What the executor can prove about the requested operations."""

    status: TechnicalExecutionStatus = Field(
        default="partial", validation_alias=AliasChoices("status", "state")
    )
    expected_operations: int = Field(default=0, ge=0, le=4096)
    attempted_operations: int = Field(default=0, ge=0, le=4096)
    completed_operations: int = Field(default=0, ge=0, le=4096)
    verified_receipts: int = Field(default=0, ge=0, le=4096)
    unknown_outcomes: int = Field(default=0, ge=0, le=4096)
    blockers: tuple[str, ...] = Field(default=(), max_length=MAX_ACTIONS)
    limitations: tuple[str, ...] = Field(default=(), max_length=MAX_ACTIONS)
    summary: str = Field(default="", max_length=MAX_TEXT)

    @model_validator(mode="after")
    def validate_counts(self) -> "TechnicalExecutionOutcome":
        if self.attempted_operations > self.expected_operations and self.expected_operations:
            raise ValueError("attempted_operations cannot exceed expected_operations")
        if self.completed_operations > self.attempted_operations:
            raise ValueError("completed_operations cannot exceed attempted_operations")
        if self.verified_receipts > self.completed_operations:
            raise ValueError("verified_receipts cannot exceed completed_operations")
        return self

    @property
    def state(self) -> TechnicalExecutionStatus:
        return self.status

    @property
    def verified(self) -> bool:
        return self.status in {"verified", "verified_with_limitations"}


class ArrangementDeliveryOutcome(CreationPipelineModel):
    """Whether generated patterns form a delivered, playable arrangement."""

    status: ArrangementDeliveryStatus = Field(
        default="not_delivered", validation_alias=AliasChoices("status", "state")
    )
    pattern_count: int = Field(default=0, ge=0, le=4096)
    sections: tuple[str, ...] = Field(default=(), max_length=MAX_ROLES)
    missing_sections: tuple[str, ...] = Field(default=(), max_length=MAX_ROLES)
    manual_playlist_actions: tuple[ManualHandoffItem, ...] = Field(
        default=(), max_length=MAX_ACTIONS
    )
    summary: str = Field(default="", max_length=MAX_TEXT)

    @property
    def playlist_handoff_required(self) -> bool:
        return bool(
            self.manual_playlist_actions
            or self.status in {"playable_manual_playlist_handoff", "patterns_created_not_placed"}
        )

    @property
    def playable(self) -> bool:
        return self.status in {"playable", "playable_manual_playlist_handoff"}


class ProcessingOutcome(CreationPipelineModel):
    """What processing was applied, and what remained dry or unresolved."""

    status: ProcessingStatus = Field(
        default="not_requested", validation_alias=AliasChoices("status", "state")
    )
    requested_techniques: tuple[str, ...] = Field(default=(), max_length=MAX_ROLES)
    applied_actions: int = Field(default=0, ge=0, le=4096)
    verified_actions: int = Field(default=0, ge=0, le=4096)
    missing_effects: tuple[str, ...] = Field(default=(), max_length=MAX_ACTIONS)
    unresolved_controls: tuple[str, ...] = Field(default=(), max_length=MAX_ACTIONS)
    summary: str = Field(default="", max_length=MAX_TEXT)

    @model_validator(mode="after")
    def validate_applied(self) -> "ProcessingOutcome":
        if self.verified_actions > self.applied_actions:
            raise ValueError("verified_actions cannot exceed applied_actions")
        return self

    @property
    def dry(self) -> bool:
        return self.status in {"dry_by_design", "dry_missing_effects"}


class AudibleQualityOutcome(CreationPipelineModel):
    """Audible status, never inferred from technical receipts."""

    status: AudibleQualityStatus = Field(
        default="not_evaluated", validation_alias=AliasChoices("status", "state")
    )
    evidence_source: Literal[
        "none",
        "user_statement",
        "bounce_analysis",
        "user_statement_and_bounce_analysis",
    ] = "none"
    evidence_note: str | None = Field(default=None, max_length=MAX_TEXT)
    summary: str = Field(default="", max_length=MAX_TEXT)


class ManualHandoffOutcome(CreationPipelineModel):
    """Aggregate state for actions the user must perform outside the bridge."""

    status: ManualHandoffStatus = Field(
        default="none", validation_alias=AliasChoices("status", "state")
    )
    actions: tuple[ManualHandoffItem, ...] = Field(default=(), max_length=MAX_ACTIONS)
    summary: str = Field(default="", max_length=MAX_TEXT)

    @model_validator(mode="after")
    def validate_handoff_status(self) -> "ManualHandoffOutcome":
        outstanding = any(item.status == "outstanding" for item in self.actions)
        if self.status == "none" and self.actions:
            raise ValueError("manual handoff status none cannot contain actions")
        if self.status == "outstanding" and not outstanding:
            raise ValueError("outstanding handoff needs an outstanding action")
        return self


class CreationOutcome(CreationPipelineModel):
    """One consolidated result with independent technical and artistic axes."""

    schema_version: Literal["1.0"] = "1.0"
    run_id: str | None = Field(default=None, max_length=MAX_TEXT)
    status: OutcomeStatus = Field(
        default="partial", validation_alias=AliasChoices("status", "state", "overall_status")
    )
    technical_execution: TechnicalExecutionOutcome = Field(
        default_factory=TechnicalExecutionOutcome,
        validation_alias=AliasChoices(
            "technical_execution", "technical", "technical_execution_outcome"
        ),
    )
    arrangement_delivery: ArrangementDeliveryOutcome = Field(
        default_factory=ArrangementDeliveryOutcome,
        validation_alias=AliasChoices(
            "arrangement_delivery", "arrangement", "arrangement_delivery_outcome"
        ),
    )
    processing: ProcessingOutcome = Field(
        default_factory=ProcessingOutcome,
        validation_alias=AliasChoices("processing", "processing_outcome"),
    )
    audible_quality: AudibleQualityOutcome = Field(
        default_factory=AudibleQualityOutcome,
        validation_alias=AliasChoices(
            "audible_quality", "audible", "audible_quality_outcome"
        ),
    )
    manual_handoff: ManualHandoffOutcome = Field(
        default_factory=ManualHandoffOutcome,
        validation_alias=AliasChoices("manual_handoff", "manual_handoff_outcome"),
    )
    timing: RunTimingReport | None = Field(
        default=None, validation_alias=AliasChoices("timing", "timing_report")
    )
    readiness_state: Literal["ready", "ready_with_limitations", "blocked"] | None = Field(
        default=None, validation_alias=AliasChoices("readiness_state", "readiness_status")
    )
    summary: str = Field(default="", max_length=MAX_TEXT)

    @property
    def technical(self) -> TechnicalExecutionOutcome:
        return self.technical_execution

    @property
    def arrangement(self) -> ArrangementDeliveryOutcome:
        return self.arrangement_delivery

    @property
    def audible(self) -> AudibleQualityOutcome:
        return self.audible_quality

    @property
    def completed(self) -> bool:
        """Backward-compatible generic status; dimensions remain authoritative."""

        return self.status == "completed"

    @property
    def technical_verified(self) -> bool:
        return self.technical_execution.verified

    @property
    def concise_summary(self) -> str:
        """Return the normal short producer-facing summary."""

        technical = self.technical_execution.status.replace("_", " ")
        arrangement = self.arrangement_delivery.status.replace("_", " ")
        processing = self.processing.status.replace("_", " ")
        audible = self.audible_quality.status.replace("_", " ")
        return (
            f"Technical execution: {technical}; Arrangement: {arrangement}; "
            f"Processing: {processing}; Audible quality: {audible}."
        )


def build_creation_outcome(
    *,
    technical: TechnicalExecutionOutcome,
    arrangement: ArrangementDeliveryOutcome,
    processing: ProcessingOutcome,
    audible_quality: AudibleQualityOutcome | None = None,
    manual_handoff: ManualHandoffOutcome | None = None,
    run_id: str | None = None,
    timing: RunTimingReport | None = None,
    readiness_state: Literal["ready", "ready_with_limitations", "blocked"] | None = None,
    summary: str = "",
) -> CreationOutcome:
    """Build a consolidated result without inferring audible approval."""

    if technical.status in {"blocked", "failed", "stopped"}:
        # Spell the mapping out so static type checkers and callers can see
        # that only the overlapping generic outcome states are propagated.
        status: OutcomeStatus = cast(
            OutcomeStatus,
            {
                "blocked": "blocked",
                "failed": "failed",
                "stopped": "stopped",
            }[technical.status],
        )
    elif technical.status == "partial" or arrangement.status in {
        "partial",
        "not_delivered",
        "patterns_created_not_placed",
    }:
        status = "partial"
    else:
        status = "completed"
    return CreationOutcome(
        run_id=run_id,
        status=status,
        technical_execution=technical,
        arrangement_delivery=arrangement,
        processing=processing,
        audible_quality=(
            AudibleQualityOutcome() if audible_quality is None else audible_quality
        ),
        manual_handoff=(
            ManualHandoffOutcome() if manual_handoff is None else manual_handoff
        ),
        timing=timing,
        readiness_state=readiness_state,
        summary=summary,
    )


__all__ = [
    "ArrangementDeliveryOutcome",
    "ArrangementDeliveryStatus",
    "AudibleQualityOutcome",
    "AudibleQualityStatus",
    "CreationOutcome",
    "ManualHandoffItem",
    "ManualHandoffOutcome",
    "ManualHandoffStatus",
    "OutcomeStatus",
    "ProcessingOutcome",
    "ProcessingStatus",
    "TechnicalExecutionOutcome",
    "TechnicalExecutionStatus",
    "build_creation_outcome",
]
