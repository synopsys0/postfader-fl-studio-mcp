"""Bounded semantic processing planning for a creation run.

This module is deliberately a pure planning layer.  It consumes an already
captured loaded-plug-in observation and a read-only :class:`AtlasRegistry`; it
never asks FL Studio for an inventory and it never dispatches a bridge command.
The mutation half of the subsystem lives in :mod:`semantic_actions` and takes
only injected, existing verified-setter callbacks.

The contracts here are intentionally a little more verbose than a plug-in
profile.  A product in Plugin Atlas is static knowledge, a loaded target is a
session observation, and a semantic action is a plan that still needs a
verified setter at application time.  Keeping those layers separate prevents
an Atlas-only product from being presented as an effect that is actually
available in the current project.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, cast

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)

from ..plugin_atlas import (
    AdapterControl,
    AtlasRegistry,
    ControlAdapter,
    ProductKnowledge,
    RuntimeMatch,
    RuntimeParameterObservation,
    RuntimePluginInstance,
    match_runtime,
    normalize_search_text,
)
from ..track_b_contracts import (
    ChannelGeneratorTarget,
    MixerEffectTarget,
    PluginTarget,
    TargetedPluginSummary,
)


PROCESSING_SCHEMA_VERSION = "1.0"

MAX_PROCESSING_TEXT = 256
MAX_PROCESSING_REASON = 1024
MAX_PROCESSING_LIST = 128
MAX_PROCESSING_CONTROLS = 64
MAX_PROCESSING_ACTIONS = 256
MAX_PROCESSING_BATCH = 128
MAX_PROCESSING_STRENGTH = 1.0
MAX_PARAMETER_INDEX = 8191

CompletionTarget = Literal[
    "composition_only",
    "playable_draft",
    "first_pass_production",
    "restrained_first_pass",
    "mix_ready",
    "polished_mix_ready",
    "custom",
]
CoverageState = Literal[
    "effect_covered",
    "dry_by_design",
    "missing_requested_effect",
    "unresolved_effect",
    "loaded_but_unresolved",
    "loaded_and_controllable",
    "not_applicable",
    "partially_covered",
]
ProcessingStatus = Literal[
    "processed",
    "partially_processed",
    "dry_by_design",
    "dry_missing_effects",
    "blocked",
]
SetterName = Literal[
    "fl_set_plugin_param_display",
    "fl_set_plugin_param_option",
    "fl_set_plugin_param",
]
ControlResolutionState = Literal["resolved", "unresolved"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _freeze(value: object) -> object:
    """Recursively freeze JSON-like input before strict validation."""

    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        # Writer receipts are opaque objects owned by the existing verified
        # writer.  Keep their identity and exact payload; copying or walking
        # them would silently destroy the "preserve earlier receipts" contract.
        return {
            key: (
                item
                if key == "receipt"
                else tuple(item)
                if key == "receipts" and isinstance(item, (list, tuple))
                else _freeze(item)
            )
            for key, item in value.items()
        }
    return value


class ProcessingModel(BaseModel):
    """Strict, immutable, bounded model base for the semantic layer."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        populate_by_name=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _freeze_collections(cls, value: object) -> object:
        if isinstance(value, dict):
            # Keep the wire contract strict while accepting the concise
            # aliases commonly used by creation-pipeline callers.
            aliases = {
                "playable": "playable_draft",
                "draft": "playable_draft",
                "first_pass": "restrained_first_pass",
                "restrained_first_pass_production": "restrained_first_pass",
                "composition": "composition_only",
                "polished": "polished_mix_ready",
            }
            copied = dict(value)
            if cls.__name__ == "LoadedProcessingCapability":
                # Interoperate with the read-only foundation's vocabulary
                # without introducing a second runtime inventory model.
                if "plugin_name" not in copied and "product_name" in copied:
                    copied["plugin_name"] = copied.pop("product_name")
                else:
                    copied.pop("product_name", None)
                if "control_evidence" not in copied and "controllable" in copied:
                    copied["control_evidence"] = copied.pop("controllable")
                else:
                    copied.pop("controllable", None)
                if "controls" not in copied and "semantic_controls" in copied:
                    copied["controls"] = copied.pop("semantic_controls")
                else:
                    copied.pop("semantic_controls", None)
                if "product_id" not in copied and "atlas_product_id" in copied:
                    copied["product_id"] = copied.pop("atlas_product_id")
                else:
                    copied.pop("atlas_product_id", None)
                if "adapter_available" not in copied and copied.get("adapter_id") is not None:
                    copied["adapter_available"] = True
                if "required" not in copied and "required_for_completion" in copied:
                    copied["required"] = copied["required_for_completion"]
                if "required_for_completion" not in copied and "required" in copied:
                    copied["required_for_completion"] = copied["required"]
                if "target" not in copied:
                    track_index = copied.get("track_index")
                    slot_index = copied.get("slot_index")
                    channel_index = copied.get("channel_index")
                    if type(track_index) is int and type(slot_index) is int:
                        copied["target"] = {
                            "kind": "mixer_effect",
                            "track_index": track_index,
                            "slot_index": slot_index,
                            "allow_master": track_index == 0,
                        }
                    elif type(channel_index) is int:
                        copied["target"] = {
                            "kind": "channel_generator",
                            "channel_index": channel_index,
                        }
            if cls.__name__ == "MissingProcessingCapability":
                if "required" not in copied and "required_for_completion" in copied:
                    copied["required"] = copied["required_for_completion"]
                if "required_for_completion" not in copied and "required" in copied:
                    copied["required_for_completion"] = copied["required"]
            if cls.__name__ == "EffectCoverageReport":
                if copied.get("dry_by_design") is True and "state" not in copied:
                    copied["state"] = "dry_by_design"
                if copied.get("dry_by_design") is True and "processing_state" not in copied:
                    copied["processing_state"] = "dry_by_design"
            if copied.get("completion_target") in aliases:
                copied["completion_target"] = aliases[copied["completion_target"]]
            return _freeze(copied)
        return _freeze(value)


BoundedText = Annotated[str, Field(min_length=1, max_length=MAX_PROCESSING_TEXT)]
OptionalText = Annotated[str, Field(max_length=MAX_PROCESSING_TEXT)]


class SemanticControlValue(ProcessingModel):
    """A user-facing value for one semantic plug-in control.

    ``display_value`` is the plug-in's own numeric display unit.  ``option``
    is a literal enumerated display value.  ``normalized_value`` is accepted
    only alongside a non-empty ``normalized_mapping``; the mapping identifier
    is evidence that an adapter has an established curve, not a caller's guess
    at FL's parameter normalization.
    """

    control_role: BoundedText = Field(
        validation_alias=AliasChoices("control_role", "semantic_role", "role")
    )
    display_value: float | str | int | None = None
    display_unit: OptionalText | None = None
    display_text: OptionalText | None = None
    option: OptionalText | None = None
    normalized_value: float | int | None = None
    normalized_mapping: OptionalText | None = None
    parameter: int | BoundedText | None = None

    @model_validator(mode="after")
    def validate_value(self) -> SemanticControlValue:
        provided = (
            self.display_value is not None
            or self.display_text is not None
            or self.option is not None
            or self.normalized_value is not None
        )
        if not provided:
            raise ValueError("a semantic control needs a requested value")
        if isinstance(self.display_value, bool) or isinstance(
            self.normalized_value, bool
        ):
            raise ValueError("boolean values are not valid semantic control values")
        if self.normalized_value is not None:
            number = float(self.normalized_value)
            if not math.isfinite(number) or not 0.0 <= number <= 1.0:
                raise ValueError("normalized_value must be a finite number within 0..1")
            if not self.normalized_mapping:
                raise ValueError(
                    "normalized_value requires an established normalized_mapping"
                )
        if self.display_value is not None and isinstance(self.display_value, str):
            if not self.display_value.strip():
                raise ValueError("display_value text must not be empty")
        if self.option is not None and not self.option.strip():
            raise ValueError("option must not be empty")
        return self

    @property
    def requested_option(self) -> str | None:
        return self.option


class ProcessingGoal(ProcessingModel):
    """One bounded artistic processing goal for a role or project target."""

    goal_id: BoundedText = "goal"
    role: BoundedText = "project"
    goal: BoundedText = Field(
        validation_alias=AliasChoices("goal", "intent", "processing_goal")
    )
    technique: OptionalText | None = None
    target: PluginTarget | None = None
    controls: tuple[SemanticControlValue, ...] = Field(
        default=(), max_length=MAX_PROCESSING_CONTROLS
    )
    strength: float = Field(default=0.5, ge=0.0, le=MAX_PROCESSING_STRENGTH)
    required: bool = False
    rationale: OptionalText | None = None

    @property
    def intent(self) -> str:
        """Compatibility vocabulary used by the existing mixing surface."""

        return self.goal


class RoleProcessingRequest(ProcessingModel):
    """Requested processing and effect expectations for one musical role."""

    role: BoundedText
    target: PluginTarget | None = None
    goals: tuple[ProcessingGoal, ...] = Field(
        default=(),
        max_length=MAX_PROCESSING_LIST,
        validation_alias=AliasChoices("goals", "requested_goals"),
    )
    requested_techniques: tuple[BoundedText, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    dry_by_design: bool = False
    processing_required: bool = False

    @model_validator(mode="after")
    def role_matches_nested_goals(self) -> RoleProcessingRequest:
        for goal in self.goals:
            if goal.role not in {self.role, "project"}:
                raise ValueError(
                    "a role processing request cannot contain a goal for another role"
                )
        return self


class ProcessingRequest(ProcessingModel):
    """A project-level processing request suitable for one Production Run."""

    request_id: BoundedText = "processing-request"
    completion_target: CompletionTarget = "restrained_first_pass"
    roles: tuple[RoleProcessingRequest, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    goals: tuple[ProcessingGoal, ...] = Field(
        default=(),
        max_length=MAX_PROCESSING_LIST,
        validation_alias=AliasChoices("goals", "requested_goals"),
    )
    # These convenience fields keep a one-role call compact while retaining a
    # single canonical multi-role contract above.
    role: BoundedText | None = None
    target: PluginTarget | None = None
    goal: BoundedText | None = None
    controls: tuple[SemanticControlValue, ...] = Field(
        default=(), max_length=MAX_PROCESSING_CONTROLS
    )
    requested_techniques: tuple[BoundedText, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    processing_required: bool = False
    dry_by_design: bool = False
    session_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{32}$"
    )
    allow_master: bool = False

    @model_validator(mode="after")
    def require_processing_source(self) -> ProcessingRequest:
        if self.goal is not None and self.role is None:
            raise ValueError("a convenience goal requires a role")
        return self

    def expanded_goals(self) -> tuple[ProcessingGoal, ...]:
        """Return deterministic role-aware goals without mutating the request."""

        result: list[ProcessingGoal] = list(self.goals)
        for role_request in self.roles:
            if role_request.goals:
                result.extend(role_request.goals)
            else:
                for index, technique in enumerate(
                    role_request.requested_techniques, start=1
                ):
                    result.append(
                        ProcessingGoal(
                            goal_id=f"{role_request.role}-{index}",
                            role=role_request.role,
                            goal=technique,
                            target=role_request.target,
                            required=role_request.processing_required,
                        )
                    )
        if self.goal is not None:
            result.append(
                ProcessingGoal(
                    goal_id=f"{self.role}-goal",
                    role=cast(str, self.role),
                    goal=self.goal,
                    target=self.target,
                    controls=self.controls,
                    required=self.processing_required,
                )
            )
        elif self.role is not None:
            for index, technique in enumerate(self.requested_techniques, start=1):
                result.append(
                    ProcessingGoal(
                        goal_id=f"{self.role}-{index}",
                        role=self.role,
                        goal=technique,
                        target=self.target,
                        required=self.processing_required,
                    )
                )
        return tuple(result)


class LoadedProcessingCapability(ProcessingModel):
    """One currently loaded target plus Atlas/adapter evidence.

    This is an observation input, not an ownership or installation claim.  A
    target is always required, which makes an Atlas product without a live
    target impossible to represent as loaded.
    """

    target: PluginTarget
    plugin_name: BoundedText = Field(
        validation_alias=AliasChoices("plugin_name", "name")
    )
    role_id: OptionalText | None = None
    track_index: int | None = Field(default=None, ge=0)
    slot_index: int | None = Field(default=None, ge=0, le=9)
    channel_index: int | None = Field(default=None, ge=0)
    target_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    product_id: str | None = Field(default=None, max_length=MAX_PROCESSING_TEXT)
    adapter_id: str | None = Field(default=None, max_length=MAX_PROCESSING_TEXT)
    category: str | None = Field(default=None, max_length=MAX_PROCESSING_TEXT)
    supported_techniques: tuple[BoundedText, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    controls: tuple[BoundedText, ...] = Field(
        default=(), max_length=MAX_PROCESSING_CONTROLS
    )
    unresolved_controls: tuple[BoundedText, ...] = Field(
        default=(), max_length=MAX_PROCESSING_CONTROLS
    )
    runtime_parameters: tuple[RuntimeParameterObservation, ...] = Field(
        default=(), max_length=MAX_PARAMETER_INDEX + 1
    )
    control_evidence: bool = False
    adapter_available: bool = False
    atlas_match: bool = False
    warnings: tuple[OptionalText, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )

    @model_validator(mode="after")
    def normalize_evidence_flags(self) -> LoadedProcessingCapability:
        if self.control_evidence and not self.adapter_available:
            raise ValueError("control evidence requires an available adapter")
        if isinstance(self.target, MixerEffectTarget):
            if self.track_index is not None and self.track_index != self.target.track_index:
                raise ValueError("track_index contradicts the loaded mixer target")
            if self.slot_index is not None and self.slot_index != self.target.slot_index:
                raise ValueError("slot_index contradicts the loaded mixer target")
            if self.channel_index is not None:
                raise ValueError("channel_index cannot accompany a mixer target")
        elif self.channel_index is not None and self.channel_index != self.target.channel_index:
            raise ValueError("channel_index contradicts the loaded generator target")
        return self

    @property
    def target_key(self) -> str:
        if isinstance(self.target, MixerEffectTarget):
            return f"mixer_effect:{self.target.track_index}:{self.target.slot_index}"
        return f"channel_generator:{self.target.channel_index}"

    @property
    def product_name(self) -> str:
        return self.plugin_name

    @property
    def semantic_controls(self) -> tuple[str, ...]:
        return self.controls

    @property
    def controllable(self) -> bool:
        return self.control_evidence


class MissingProcessingCapability(ProcessingModel):
    """One requested technique/category that cannot be applied safely."""

    role: BoundedText = "project"
    category: BoundedText
    requested_techniques: tuple[BoundedText, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    reason: str = Field(min_length=1, max_length=MAX_PROCESSING_REASON)
    required: bool = Field(
        default=False,
        validation_alias=AliasChoices("required", "required_for_completion"),
    )
    required_for_completion: bool = False
    target: PluginTarget | None = None


class RoleEffectCoverage(ProcessingModel):
    """Effect readiness for one role, with honest missing/unresolved states."""

    role: BoundedText = Field(
        validation_alias=AliasChoices("role", "role_id")
    )
    role_id: BoundedText | None = None
    target: PluginTarget | None = None
    loaded_effects: tuple[LoadedProcessingCapability, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    product_matches: tuple[str, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    adapter_matches: tuple[str, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    available_semantic_adapters: tuple[str, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    supported_techniques: tuple[str, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    unresolved_controls: tuple[str, ...] = Field(
        default=(), max_length=MAX_PROCESSING_CONTROLS
    )
    requested_techniques: tuple[str, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    missing_capabilities: tuple[MissingProcessingCapability, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    state: CoverageState = "not_applicable"
    dry_playback_intentional: bool = False
    dry_playback_allowed: bool = True
    processing_required: bool = False
    processing_required_for_completion: bool = False
    limitations: tuple[str, ...] = Field(default=(), max_length=MAX_PROCESSING_LIST)

    @property
    def coverage_state(self) -> CoverageState:
        return self.state

    @property
    def effect_covered(self) -> bool:
        return self.state in {"effect_covered", "loaded_and_controllable"}


class EffectCoverageReport(ProcessingModel):
    """Project-level effect coverage evaluated from one loaded inventory."""

    schema_version: Literal["1.0"] = PROCESSING_SCHEMA_VERSION
    observed_at: datetime = Field(default_factory=_now)
    completion_target: CompletionTarget = "playable_draft"
    requested_categories: tuple[str, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    roles: tuple[RoleEffectCoverage, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    loaded_capabilities: tuple[LoadedProcessingCapability, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    missing_capabilities: tuple[MissingProcessingCapability, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    state: CoverageState = "not_applicable"
    processing_state: ProcessingStatus = "dry_by_design"
    processing_required_for_completion: bool = False
    can_produce_dry_draft: bool = True
    dry_by_design: bool = False
    required_processing_missing: bool = False
    warnings: tuple[str, ...] = Field(default=(), max_length=MAX_PROCESSING_LIST)

    @property
    def coverage_state(self) -> CoverageState:
        return self.state


class ResolvedSemanticControl(ProcessingModel):
    """Adapter-backed control identity and setter choice."""

    control_role: BoundedText
    control_id: BoundedText
    parameter_index: int = Field(ge=0, le=MAX_PARAMETER_INDEX)
    parameter_name: str | None = Field(default=None, max_length=MAX_PROCESSING_TEXT)
    display_unit: str | None = Field(default=None, max_length=MAX_PROCESSING_TEXT)
    setter: SetterName
    display_value: float | None = None
    display_text: str | None = Field(default=None, max_length=MAX_PROCESSING_TEXT)
    option: str | None = Field(default=None, max_length=MAX_PROCESSING_TEXT)
    normalized_value: float | None = Field(default=None, ge=0.0, le=1.0)
    normalized_mapping: str | None = Field(
        default=None, max_length=MAX_PROCESSING_TEXT
    )
    verification_basis: Literal["readback_on_a_later_fl_idle_tick"] = (
        "readback_on_a_later_fl_idle_tick"
    )
    status: ControlResolutionState = "resolved"
    reason: str | None = Field(default=None, max_length=MAX_PROCESSING_REASON)


class SemanticControlResolution(ProcessingModel):
    """Result of resolving a semantic request, including unresolved evidence."""

    request: SemanticControlValue
    control: ResolvedSemanticControl | None = None
    status: ControlResolutionState
    reason: str | None = Field(default=None, max_length=MAX_PROCESSING_REASON)


class ProcessingCandidate(ProcessingModel):
    """One loaded, adapter-compatible candidate for a processing goal."""

    candidate_id: BoundedText
    goal_id: BoundedText
    role: BoundedText
    target: PluginTarget
    target_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    plugin_name: BoundedText
    product_id: BoundedText | None = None
    adapter_id: BoundedText | None = None
    category: BoundedText | None = None
    supported_techniques: tuple[str, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    control_resolutions: tuple[SemanticControlResolution, ...] = Field(
        default=(), max_length=MAX_PROCESSING_CONTROLS
    )
    score: float = Field(ge=0.0, le=1.0)
    valid: bool = False
    state: Literal["compatible", "unresolved", "incompatible"]
    reasons: tuple[str, ...] = Field(default=(), max_length=MAX_PROCESSING_LIST)


class SemanticPluginAction(ProcessingModel):
    """One semantic mutation request, still requiring an injected writer."""

    action_id: BoundedText
    goal_id: BoundedText
    role: BoundedText
    target: PluginTarget
    target_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    plugin_name: BoundedText
    product_id: BoundedText | None = None
    adapter_id: BoundedText | None = None
    control: SemanticControlValue
    resolution: SemanticControlResolution
    depends_on: tuple[BoundedText, ...] = Field(
        default=(), max_length=MAX_PROCESSING_ACTIONS
    )
    session_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{32}$"
    )
    allow_master: bool = False
    rationale: str = Field(
        default="Semantic processing is conservative and audible quality is not evaluated.",
        max_length=MAX_PROCESSING_REASON,
    )

    @property
    def resolved_control(self) -> ResolvedSemanticControl | None:
        return self.resolution.control


class ProcessingPlan(ProcessingModel):
    """Immutable bounded processing plan for one request/run."""

    schema_version: Literal["1.0"] = PROCESSING_SCHEMA_VERSION
    plan_id: BoundedText
    request_id: BoundedText
    created_at: datetime = Field(default_factory=_now)
    completion_target: CompletionTarget
    session_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{32}$"
    )
    candidates: tuple[ProcessingCandidate, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    actions: tuple[SemanticPluginAction, ...] = Field(
        default=(), max_length=MAX_PROCESSING_ACTIONS
    )
    missing_capabilities: tuple[MissingProcessingCapability, ...] = Field(
        default=(), max_length=MAX_PROCESSING_LIST
    )
    warnings: tuple[str, ...] = Field(default=(), max_length=MAX_PROCESSING_LIST)
    max_actions: int = Field(default=MAX_PROCESSING_BATCH, ge=1, le=MAX_PROCESSING_ACTIONS)


class ProcessingActionReceipt(ProcessingModel):
    """Outcome of one planned action; ``receipt`` preserves the writer object."""

    action_id: BoundedText
    status: Literal[
        "verified",
        "unverified",
        "unknown",
        "stale_target",
        "stale_session",
        "unresolved_control",
        "missing_setter",
        "blocked",
    ]
    outcome_known: bool
    verified: bool | None = None
    receipt: Any = None
    blocked_by: tuple[str, ...] = Field(default=(), max_length=MAX_PROCESSING_ACTIONS)
    warning: str | None = Field(default=None, max_length=MAX_PROCESSING_REASON)

    @model_validator(mode="after")
    def validate_outcome(self) -> ProcessingActionReceipt:
        if self.status == "verified":
            if not self.outcome_known or self.verified is not True:
                raise ValueError("verified processing actions require verified readback")
        elif self.status == "unknown":
            if self.outcome_known or self.verified is not None:
                raise ValueError("unknown processing actions require an unknown outcome")
        elif not self.outcome_known or self.verified is not False:
            raise ValueError(
                "safe-stop processing action statuses require a known unverified outcome"
            )
        if self.blocked_by and self.status != "blocked":
            raise ValueError("blocked_by is only valid for a blocked action")
        return self


class ProcessingPlanReceipt(ProcessingModel):
    """Consolidated one-shot application result with no rollback claim."""

    schema_version: Literal["1.0"] = PROCESSING_SCHEMA_VERSION
    applied_at: datetime = Field(default_factory=_now)
    plan_id: BoundedText
    requested_count: int = Field(ge=0, le=MAX_PROCESSING_ACTIONS)
    attempted_count: int = Field(ge=0, le=MAX_PROCESSING_ACTIONS)
    completed: bool
    stopped: bool
    stopped_on: str | None = Field(default=None, max_length=MAX_PROCESSING_TEXT)
    verified: bool | None = None
    outcome_known: bool = True
    results: tuple[ProcessingActionReceipt, ...] = Field(
        default=(), max_length=MAX_PROCESSING_ACTIONS
    )
    receipts: tuple[Any, ...] = Field(default=(), max_length=MAX_PROCESSING_ACTIONS)
    warnings: tuple[str, ...] = Field(default=(), max_length=MAX_PROCESSING_LIST)

    @model_validator(mode="after")
    def validate_plan_outcome(self) -> ProcessingPlanReceipt:
        if self.attempted_count > self.requested_count:
            raise ValueError("attempted_count cannot exceed requested_count")
        if len(self.results) > self.requested_count:
            raise ValueError("processing results cannot exceed requested actions")
        if len(self.receipts) > self.attempted_count:
            raise ValueError("writer receipts cannot exceed attempted actions")
        if self.completed:
            if self.stopped or len(self.results) != self.requested_count:
                raise ValueError(
                    "completed processing requires one result per requested action"
                )
            if not self.outcome_known or self.verified is not True:
                raise ValueError("completed processing requires verified known outcomes")
            if any(item.status != "verified" for item in self.results):
                raise ValueError("completed processing results must all be verified")
        if self.stopped and self.completed:
            raise ValueError("stopped processing cannot be completed")
        if not self.outcome_known and self.verified is not None:
            raise ValueError("an unknown plan outcome cannot claim verification")
        if any(not item.outcome_known for item in self.results) and self.outcome_known:
            raise ValueError("plan outcome_known contradicts an unknown action result")
        return self


# Goal-to-category knowledge is intentionally small and conservative.  The
# adapter's own supported_intents remains authoritative when it is present.
GOAL_CATEGORIES: dict[str, tuple[str, ...]] = {
    "reduce_mud": ("equalizer",),
    "tame_harshness": ("equalizer",),
    "add_presence": ("equalizer",),
    "add_air": ("equalizer",),
    "tighten_low_end": ("equalizer", "compressor"),
    "control_dynamics": ("compressor",),
    "add_punch": ("compressor",),
    "level_vocal": ("compressor",),
    "limit_peaks": ("limiter",),
    "add_depth": ("reverb",),
    "shorten_space": ("reverb",),
    "rhythmic_echo": ("delay",),
    "keep_low_end_centered": ("equalizer",),
    "restrained_section_contrast": ("reverb", "delay", "compressor"),
}


def _canonical(value: str) -> str:
    return normalize_search_text(value).replace(" ", "_")


def _target_key(target: PluginTarget) -> str:
    if isinstance(target, MixerEffectTarget):
        return f"mixer_effect:{target.track_index}:{target.slot_index}"
    return f"channel_generator:{target.channel_index}"


def _candidate_id(goal_id: str, target: PluginTarget) -> str:
    material = f"{goal_id}:{_target_key(target)}".encode("utf-8")
    return f"candidate-{hashlib.sha256(material).hexdigest()[:24]}"


def _parameter_matches_control(
    parameter: RuntimeParameterObservation, control: AdapterControl
) -> bool:
    if control.parameter_index is not None and parameter.index == control.parameter_index:
        return True
    observed = {
        _canonical(text)
        for text in (parameter.name, parameter.display)
        if text
    }
    expected = {
        _canonical(text)
        for text in (*control.names, *control.display_names)
        if text
    }
    return bool(observed & expected)


def _control_names(adapter: ControlAdapter) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            control.role or control.control_id
            for control in adapter.controls
            if control.role or control.control_id
        )
    )


def _direct_target(value: object) -> PluginTarget | None:
    if isinstance(value, (MixerEffectTarget, ChannelGeneratorTarget)):
        return value
    if isinstance(value, Mapping):
        try:
            return TypeAdapter(PluginTarget).validate_python(dict(value), strict=True)
        except (TypeError, ValueError):
            return None
    return None


def _runtime_from_loaded(
    value: object,
) -> tuple[PluginTarget, str, str | None, tuple[RuntimeParameterObservation, ...], str | None, str | None] | None:
    """Coerce supported read-only inventory shapes without touching FL."""

    if isinstance(value, LoadedProcessingCapability):
        return (
            value.target,
            value.plugin_name,
            value.target_fingerprint,
            value.runtime_parameters,
            value.product_id,
            value.adapter_id,
        )
    if isinstance(value, TargetedPluginSummary):
        return (
            value.target,
            value.name,
            value.target_fingerprint,
            (),
            None,
            None,
        )
    # The foundation models intentionally keep effect coverage independent of
    # target unions.  Accept that observation shape as input when an
    # integration passes it directly, while still requiring a concrete loaded
    # target before it can become a capability.
    if hasattr(value, "product_name"):
        target = _direct_target(getattr(value, "target", None))
        if target is None:
            track_index = getattr(value, "track_index", None)
            slot_index = getattr(value, "slot_index", None)
            channel_index = getattr(value, "channel_index", None)
            if type(track_index) is int and type(slot_index) is int:
                target = MixerEffectTarget(
                    track_index=track_index,
                    slot_index=slot_index,
                    allow_master=track_index == 0,
                )
            elif type(channel_index) is int:
                target = ChannelGeneratorTarget(channel_index=channel_index)
        plugin_name = getattr(value, "product_name", None)
        if target is not None and isinstance(plugin_name, str) and plugin_name.strip():
            return (
                target,
                plugin_name.strip(),
                getattr(value, "target_fingerprint", None),
                tuple(getattr(value, "runtime_parameters", ())),
                getattr(value, "product_id", None) or getattr(value, "atlas_product_id", None),
                getattr(value, "adapter_id", None),
            )
    # AtlasLoadedPluginRecord is kept duck-typed so this module does not
    # import its MCP-facing module or trigger a live inspection.
    if hasattr(value, "target") and hasattr(value, "plugin"):
        target = _direct_target(getattr(value, "target"))
        plugin = getattr(value, "plugin")
        if target is not None and hasattr(plugin, "name"):
            best = getattr(value, "best_match", None)
            runtime = getattr(value, "runtime", None)
            return (
                target,
                str(plugin.name),
                getattr(plugin, "target_fingerprint", None),
                tuple(getattr(runtime, "parameters", ())),
                getattr(best, "product_id", None),
                getattr(best, "adapter_id", None),
            )
    if not isinstance(value, Mapping):
        return None
    source: Mapping[str, Any] = value
    # A serialized joined capability is already a read-only observation.  Keep
    # its explicit control-proof flags instead of throwing them away and
    # trying to recompute proof from a summary that may not include parameter
    # rows.  Raw runtime rows (the normal Production Run input) do not carry
    # these flags and continue through the matcher below.
    if "control_evidence" in source or "controllable" in source:
        try:
            resolved = LoadedProcessingCapability.model_validate(source, strict=True)
        except (TypeError, ValueError):
            pass
        else:
            return (
                resolved.target,
                resolved.plugin_name,
                resolved.target_fingerprint,
                resolved.runtime_parameters,
                resolved.product_id,
                resolved.adapter_id,
            )
    nested = source.get("plugin")
    if isinstance(nested, Mapping):
        merged = dict(nested)
        merged.update({key: item for key, item in source.items() if key != "plugin"})
        source = merged
    target = _direct_target(source.get("target"))
    if target is None:
        track_index = source.get("track_index")
        slot_index = source.get("slot_index")
        channel_index = source.get("channel_index")
        if type(track_index) is int and type(slot_index) is int:
            try:
                allow_master = source.get("allow_master")
                target = MixerEffectTarget(
                    track_index=track_index,
                    slot_index=slot_index,
                    allow_master=(
                        allow_master
                        if type(allow_master) is bool
                        else track_index == 0
                    ),
                )
            except ValueError:
                target = None
        elif type(channel_index) is int:
            try:
                target = ChannelGeneratorTarget(channel_index=channel_index)
            except ValueError:
                target = None
    name = source.get("plugin_name", source.get("product_name", source.get("name")))
    if target is None or not isinstance(name, str) or not name.strip():
        return None
    params_raw = source.get("runtime_parameters", source.get("parameters", ()))
    parameters: list[RuntimeParameterObservation] = []
    if isinstance(params_raw, Sequence) and not isinstance(
        params_raw, (str, bytes, bytearray)
    ):
        for row in params_raw:
            try:
                parameters.append(
                    row
                    if isinstance(row, RuntimeParameterObservation)
                    else RuntimeParameterObservation.model_validate(row, strict=True)
                )
            except (TypeError, ValueError):
                continue
    return (
        target,
        name.strip(),
        source.get("target_fingerprint"),
        tuple(parameters),
        source.get("product_id") if isinstance(source.get("product_id"), str) else None,
        source.get("adapter_id") if isinstance(source.get("adapter_id"), str) else None,
    )


def _loaded_capability(
    value: object,
    registry: AtlasRegistry,
) -> LoadedProcessingCapability | None:
    # The foundation readiness layer has a deliberately smaller capability
    # record with ``product_name``/``controllable`` fields.  When an
    # integration passes that immutable record directly, round-trip it into
    # this module's richer contract so an explicit joined proof is retained.
    if not isinstance(value, LoadedProcessingCapability) and hasattr(
        value, "model_dump"
    ) and hasattr(value, "product_name"):
        try:
            payload = cast(Any, value).model_dump(mode="python")
            if isinstance(payload, Mapping) and (
                "controllable" in payload or "control_evidence" in payload
            ):
                resolved = LoadedProcessingCapability.model_validate(
                    payload, strict=True
                )
        except (TypeError, ValueError):
            pass
        else:
            return resolved
    if isinstance(value, Mapping) and (
        "control_evidence" in value or "controllable" in value
    ):
        # Preserve a serialized, already-joined observation just as we do for
        # the model instance.  The strict model still requires a concrete
        # loaded target and validates all bounds before it is accepted.
        try:
            resolved = LoadedProcessingCapability.model_validate(value, strict=True)
        except (TypeError, ValueError):
            pass
        else:
            return resolved
    parsed = _runtime_from_loaded(value)
    if parsed is None:
        return None
    # A caller may pass an already resolved capability from a read-only Atlas
    # join.  Preserve its explicit evidence flags; recomputing it from the
    # summary would discard control proof because summaries intentionally do
    # not carry parameter rows.
    if isinstance(value, LoadedProcessingCapability):
        return value
    target, plugin_name, target_fp, parameters, explicit_product, explicit_adapter = parsed
    runtime = RuntimePluginInstance(
        instance_id=_target_key(target),
        name=plugin_name,
        parameters=parameters,
    )
    matches = match_runtime(runtime, registry, limit=8, include_weak=True)
    match: RuntimeMatch | None = matches[0] if matches else None
    product_id = explicit_product or (match.product_id if match else None)
    adapter_id = explicit_adapter or (match.adapter_id if match else None)
    product: ProductKnowledge | None = (
        registry.product(product_id) if product_id is not None else registry.find_product(plugin_name)
    )
    if product_id is None and product is not None:
        product_id = product.product_id
    adapter: ControlAdapter | None = (
        registry.adapter(adapter_id)
        if adapter_id is not None
        else (
            registry.adapters_for_product(product.product_id)[0]
            if product is not None and registry.adapters_for_product(product.product_id)
            else None
        )
    )
    if (
        product is not None
        and adapter is not None
        and adapter.product_id != product.product_id
    ):
        # An explicit adapter ID is not enough to establish compatibility;
        # its Atlas product relation must agree with the loaded product.
        adapter = None
        adapter_id = None
    if adapter_id is None and adapter is not None:
        adapter_id = adapter.adapter_id
    # Runtime parameter evidence is required to claim that the loaded target's
    # controls are actually observed.  A static adapter alone is unresolved.
    evidence = bool(match and match.parameter_evidence)
    if not parameters and explicit_adapter is not None:
        evidence = False
    if parameters and adapter is not None:
        evidence = all(
            any(_parameter_matches_control(row, control) for row in parameters)
            for control in adapter.controls
            if control.required
        ) and any(_parameter_matches_control(row, control) for row in parameters for control in adapter.controls)
    controls = () if adapter is None else _control_names(adapter)
    category = None if adapter is None else adapter.category
    supported = () if adapter is None else adapter.supported_intents
    unresolved: tuple[str, ...] = ()
    if adapter is not None and parameters:
        unresolved = tuple(
            control.role or control.control_id
            for control in adapter.controls
            if not any(_parameter_matches_control(row, control) for row in parameters)
        )
    warnings: list[str] = []
    if product is None:
        warnings.append("loaded plug-in did not resolve to a Plugin Atlas product")
    elif adapter is None:
        warnings.append("Atlas product is known but no compatible semantic adapter is available")
    elif not evidence:
        warnings.append("adapter controls were not proven by runtime parameter evidence")
    return LoadedProcessingCapability(
        target=target,
        plugin_name=plugin_name,
        target_fingerprint=target_fp,
        product_id=product_id,
        adapter_id=adapter_id,
        category=category,
        supported_techniques=supported,
        controls=controls,
        unresolved_controls=unresolved,
        runtime_parameters=parameters,
        control_evidence=evidence,
        adapter_available=adapter is not None,
        atlas_match=product is not None,
        warnings=tuple(warnings),
    )


def resolve_loaded_capabilities(
    loaded_plugins: Iterable[object],
    *,
    registry: AtlasRegistry,
) -> tuple[LoadedProcessingCapability, ...]:
    """Resolve an injected inventory to loaded-only Atlas capabilities."""

    if not isinstance(registry, AtlasRegistry):
        raise TypeError("registry must be an AtlasRegistry")
    # Track B returns ``TargetedLoadedPluginInventory`` while the Atlas MCP
    # join returns an object with the same ``plugins`` collection.  Accept
    # either already-captured container without asking an inspector to scan;
    # the semantic layer remains a pure consumer of the supplied snapshot.
    if hasattr(loaded_plugins, "plugins"):
        loaded_plugins = getattr(loaded_plugins, "plugins")
    result: list[LoadedProcessingCapability] = []
    seen: set[str] = set()
    for item in loaded_plugins:
        capability = _loaded_capability(item, registry)
        if capability is None or capability.target_key in seen:
            continue
        seen.add(capability.target_key)
        result.append(capability)
    result.sort(key=lambda item: item.target_key)
    return tuple(result)


def _goal_categories(goal: ProcessingGoal) -> tuple[str, ...]:
    canonical = _canonical(goal.goal)
    if canonical in GOAL_CATEGORIES:
        return GOAL_CATEGORIES[canonical]
    if goal.technique:
        technique = _canonical(goal.technique)
        if technique in GOAL_CATEGORIES:
            return GOAL_CATEGORIES[technique]
        return (technique,)
    return (canonical,)


def _target_requires_processing(target: CompletionTarget) -> bool:
    """Whether the completion target asks for processing beyond a draft."""

    return target in {
        "restrained_first_pass",
        "first_pass_production",
        "mix_ready",
        "polished_mix_ready",
        "custom",
    }


def _capability_supports_goal(
    capability: LoadedProcessingCapability,
    goal: ProcessingGoal,
) -> bool:
    if not capability.atlas_match or not capability.adapter_available:
        return False
    categories = _goal_categories(goal)
    if _canonical(goal.goal) not in GOAL_CATEGORIES and goal.technique is None:
        return False
    if capability.category not in categories:
        return False
    if not capability.control_evidence:
        return False
    supported = {_canonical(value) for value in capability.supported_techniques}
    requested = {_canonical(goal.goal), _canonical(goal.technique or "")}
    requested.discard("")
    return not supported or bool(supported & requested) or goal.technique is None


def _select_candidates(
    goal: ProcessingGoal,
    capabilities: Sequence[LoadedProcessingCapability],
    registry: AtlasRegistry,
) -> tuple[ProcessingCandidate, ...]:
    candidates: list[ProcessingCandidate] = []
    for capability in capabilities:
        if goal.target is not None and capability.target != goal.target:
            continue
        product = registry.product(capability.product_id) if capability.product_id else None
        adapter = registry.adapter(capability.adapter_id) if capability.adapter_id else None
        if (
            product is not None
            and adapter is not None
            and adapter.product_id != product.product_id
        ):
            adapter = None
        category_match = capability.category in _goal_categories(goal)
        intent_match = _capability_supports_goal(capability, goal)
        resolutions: tuple[SemanticControlResolution, ...] = ()
        if adapter is not None:
            resolutions = tuple(
                resolve_semantic_control(
                    control,
                    adapter=adapter,
                    runtime_parameters=capability.runtime_parameters,
                )
                for control in goal.controls
            )
        unresolved = any(item.status != "resolved" for item in resolutions)
        valid = bool(product and adapter and category_match and intent_match and not unresolved)
        score = 0.0
        reasons: list[str] = []
        if product is not None:
            score += 0.25
            reasons.append("loaded name resolves to an Atlas product")
        if adapter is not None:
            score += 0.25
            reasons.append("loaded target has a compatible semantic adapter")
        if capability.control_evidence:
            score += 0.30
            reasons.append("runtime parameter evidence proves adapter controls")
        if category_match:
            score += 0.10
        else:
            reasons.append("loaded effect category is unrelated to the requested goal")
        if intent_match:
            score += 0.10
        if unresolved:
            reasons.append("one or more requested controls are unresolved")
        candidates.append(
            ProcessingCandidate(
                candidate_id=_candidate_id(goal.goal_id, capability.target),
                goal_id=goal.goal_id,
                role=goal.role,
                target=capability.target,
                target_fingerprint=capability.target_fingerprint,
                plugin_name=capability.plugin_name,
                product_id=None if product is None else product.product_id,
                adapter_id=None if adapter is None else adapter.adapter_id,
                category=capability.category,
                supported_techniques=capability.supported_techniques,
                control_resolutions=resolutions,
                score=min(1.0, score),
                valid=valid,
                state=("compatible" if valid else "unresolved" if product and adapter else "incompatible"),
                reasons=tuple(reasons),
            )
        )
    candidates.sort(key=lambda item: (-item.score, item.candidate_id))
    return tuple(candidates)


def _missing_for_goal(
    goal: ProcessingGoal,
    capabilities: Sequence[LoadedProcessingCapability],
) -> MissingProcessingCapability:
    categories = _goal_categories(goal)
    category = categories[0] if categories else "unknown"
    same_target = [
        item
        for item in capabilities
        if goal.target is None or item.target == goal.target
    ]
    if not same_target:
        reason = "no loaded effect target matched the requested processing category"
    elif not any(item.atlas_match for item in same_target):
        reason = "a loaded effect was observed, but no Plugin Atlas product matched it"
    elif not any(item.adapter_available for item in same_target):
        reason = "the loaded Atlas product has no compatible semantic adapter"
    elif not any(item.control_evidence for item in same_target):
        reason = "the loaded adapter lacks runtime control evidence"
    else:
        reason = "loaded controls could not be resolved for this goal"
    return MissingProcessingCapability(
        role=goal.role,
        category=category,
        requested_techniques=(goal.technique or goal.goal,),
        reason=reason,
        required=goal.required,
        target=goal.target,
    )


def evaluate_effect_coverage(
    request: ProcessingRequest | Sequence[RoleProcessingRequest | ProcessingGoal],
    *,
    loaded_plugins: Iterable[object] = (),
    loaded_effects: Iterable[object] | None = None,
    completion_target: CompletionTarget | None = None,
    registry: AtlasRegistry,
) -> EffectCoverageReport:
    """Evaluate role/project effect coverage from an injected observation.

    The function is deterministic and read-only.  In particular, omitting
    ``loaded_plugins`` means that no effect is observed; it does not trigger a
    live Track B scan and it never turns an Atlas-only product into an available
    effect.
    """

    if isinstance(request, ProcessingRequest):
        if completion_target is not None and completion_target != request.completion_target:
            raise ValueError(
                "completion_target conflicts with the ProcessingRequest target"
            )
        completion_target = request.completion_target
        goals = request.expanded_goals()
        explicit_dry = request.dry_by_design
        processing_required = request.processing_required or _target_requires_processing(
            completion_target
        )
        role_sources: dict[str, tuple[ProcessingGoal, ...]] = {}
        for goal in goals:
            role_sources.setdefault(goal.role, ())
            role_sources[goal.role] = (*role_sources[goal.role], goal)
        for role_request in request.roles:
            if role_request.dry_by_design:
                role_sources.setdefault(role_request.role, ())
        role_requests = tuple(
            RoleProcessingRequest(
                role=role,
                target=next((goal.target for goal in role_goals if goal.target), None),
                goals=role_goals,
                requested_techniques=tuple(goal.goal for goal in role_goals),
                dry_by_design=explicit_dry,
                processing_required=processing_required,
            )
            for role, role_goals in sorted(role_sources.items())
        )
        if not role_requests and explicit_dry:
            role_requests = (RoleProcessingRequest(role="project", dry_by_design=True),)
    else:
        role_requests_list: list[RoleProcessingRequest] = []
        for item in request:
            if isinstance(item, RoleProcessingRequest):
                role_requests_list.append(item)
            elif isinstance(item, ProcessingGoal):
                role_requests_list.append(
                    RoleProcessingRequest(
                        role=item.role,
                        target=item.target,
                        goals=(item,),
                        requested_techniques=(item.goal,),
                        processing_required=item.required,
                    )
                )
            else:
                raise TypeError("coverage request items must be roles or processing goals")
        completion_target = "restrained_first_pass"
        completion_target = completion_target or "restrained_first_pass"
        processing_required = _target_requires_processing(completion_target)
        role_requests = tuple(role_requests_list)
    if loaded_effects is not None:
        loaded_plugins = loaded_effects
    capabilities = resolve_loaded_capabilities(loaded_plugins, registry=registry)
    role_reports: list[RoleEffectCoverage] = []
    missing: list[MissingProcessingCapability] = []
    for role_request in role_requests:
        goals = role_request.goals
        requested = tuple(
            dict.fromkeys(
                technique
                for technique in (
                    *role_request.requested_techniques,
                    *(goal.goal for goal in goals),
                )
            )
        )
        categories = tuple(
            dict.fromkeys(
                category
                for goal in goals
                for category in _goal_categories(goal)
            )
        )
        targeted = tuple(
            capability
            for capability in capabilities
            if role_request.target is None or capability.target == role_request.target
        )
        matching = tuple(
            capability
            for capability in targeted
            if capability.category in categories
        )
        product_matches = tuple(
            dict.fromkeys(
                item.product_id for item in matching if item.product_id is not None
            )
        )
        adapter_matches = tuple(
            dict.fromkeys(
                item.adapter_id for item in matching if item.adapter_id is not None
            )
        )
        supported = tuple(
            dict.fromkeys(
                technique
                for item in matching
                for technique in item.supported_techniques
            )
        )
        unresolved_controls = tuple(
            dict.fromkeys(
                control
                for item in matching
                for control in item.unresolved_controls
            )
        )
        if role_request.dry_by_design or not requested:
            state: CoverageState = "dry_by_design"
            intentional = True
            limitation = ()
        elif not matching:
            state = "missing_requested_effect"
            intentional = False
            limitation = ("No loaded compatible effect covers the requested technique.",)
        elif not any(item.atlas_match and item.adapter_available for item in matching):
            state = "unresolved_effect"
            intentional = False
            limitation = ("Loaded effect identity or semantic adapter could not be established.",)
        elif not any(item.control_evidence for item in matching) or unresolved_controls:
            state = "unresolved_effect"
            intentional = False
            limitation = ("Loaded effect controls are not sufficiently proven for semantic writes.",)
        else:
            state = "effect_covered"
            intentional = False
            limitation = ()
        role_report = RoleEffectCoverage(
            role=role_request.role,
            role_id=role_request.role,
            target=role_request.target,
            loaded_effects=targeted,
            product_matches=product_matches,
            adapter_matches=adapter_matches,
            available_semantic_adapters=adapter_matches,
            supported_techniques=supported,
            unresolved_controls=unresolved_controls,
            requested_techniques=requested,
            state=state,
            dry_playback_intentional=intentional,
            dry_playback_allowed=not (
                processing_required or role_request.processing_required
            ),
            processing_required=processing_required or role_request.processing_required,
            processing_required_for_completion=(
                processing_required or role_request.processing_required
            ),
            missing_capabilities=tuple(
                _missing_for_goal(goal, targeted)
                for goal in goals
                if not any(
                    _capability_supports_goal(capability, goal)
                    for capability in targeted
                )
            ),
            limitations=limitation,
        )
        role_reports.append(role_report)
        if state in {"missing_requested_effect", "unresolved_effect"}:
            for goal in goals or (
                ProcessingGoal(
                    role=role_request.role,
                    goal=(requested[0] if requested else "processing"),
                    target=role_request.target,
                    required=role_request.processing_required,
                ),
            ):
                missing.append(_missing_for_goal(goal, targeted))
    states = tuple(item.state for item in role_reports)
    if not states or all(state == "dry_by_design" for state in states):
        project_state: CoverageState = "dry_by_design"
        processing_state: ProcessingStatus = "dry_by_design"
    elif all(state == "effect_covered" for state in states):
        project_state = "effect_covered"
        processing_state = "processed"
    elif any(state == "missing_requested_effect" for state in states):
        project_state = "missing_requested_effect"
        processing_state = "dry_missing_effects"
    else:
        project_state = "unresolved_effect"
        processing_state = "dry_missing_effects"
    required_missing = processing_required and bool(missing)
    requested_categories = tuple(
        dict.fromkeys(
            category
            for report in role_reports
            for technique in report.requested_techniques
            for category in GOAL_CATEGORIES.get(_canonical(technique), (_canonical(technique),))
        )
    )
    warnings = [
        "Effect coverage is based on injected read-only observations; it does not evaluate audible quality.",
        "Atlas-only products are not treated as loaded effects.",
    ]
    return EffectCoverageReport(
        completion_target=completion_target,
        requested_categories=requested_categories,
        roles=tuple(role_reports),
        loaded_capabilities=capabilities,
        missing_capabilities=tuple(missing),
        state=project_state,
        processing_state=processing_state,
        processing_required_for_completion=processing_required,
        can_produce_dry_draft=True,
        dry_by_design=project_state == "dry_by_design",
        required_processing_missing=required_missing,
        warnings=tuple(warnings),
    )


def _display_number(value: float | str | int | None) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        number = float(value)
        return (number, None) if math.isfinite(number) else (None, None)
    text = value.strip()
    match = re.fullmatch(
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:\s*([A-Za-z%][A-Za-z0-9%/_-]*))?",
        text,
    )
    if match is None:
        return None, text
    return float(match.group(1)), text


def resolve_semantic_control(
    request: SemanticControlValue,
    *,
    adapter: ControlAdapter,
    runtime_parameters: Sequence[RuntimeParameterObservation | Mapping[str, Any]] = (),
) -> SemanticControlResolution:
    """Resolve one semantic control without guessing indices or curves."""

    if not isinstance(request, SemanticControlValue):
        raise TypeError("request must be a SemanticControlValue")
    if not isinstance(adapter, ControlAdapter):
        raise TypeError("adapter must be a ControlAdapter")
    observed_parameters: list[RuntimeParameterObservation] = []
    for row in runtime_parameters:
        if isinstance(row, RuntimeParameterObservation):
            observed_parameters.append(row)
            continue
        try:
            observed_parameters.append(
                RuntimeParameterObservation.model_validate(row, strict=True)
            )
        except (TypeError, ValueError) as error:
            return SemanticControlResolution(
                request=request,
                status="unresolved",
                reason=f"runtime control observation is malformed: {error}",
            )
    wanted = _canonical(request.control_role)

    def matches_control(control: AdapterControl) -> bool:
        """Match qualified requests to the adapter's local role vocabulary.

        Bundled adapters intentionally use concise roles such as ``decay``
        while the public semantic request examples use qualified roles such
        as ``reverb.decay``.  The adapter remains authoritative: qualification
        is accepted only when its final component exactly names one of that
        adapter's declared controls, and ambiguity is still rejected.
        """

        identifiers = tuple(
            _canonical(value)
            for value in (control.role, control.control_id)
            if value
        )
        if wanted in identifiers:
            return True
        # ``normalize_search_text`` strips punctuation before the final
        # underscore canonicalization, so ``reverb.decay`` arrives here as
        # ``reverb_decay``.
        return "_" in wanted and any(
            wanted.endswith(f"_{identifier}") for identifier in identifiers
        )

    controls = tuple(
        control for control in adapter.controls if matches_control(control)
    )
    if len(controls) != 1:
        reason = (
            "semantic control role is unknown to the adapter"
            if not controls
            else "semantic control role is ambiguous in the adapter"
        )
        return SemanticControlResolution(request=request, status="unresolved", reason=reason)
    control = controls[0]
    parameter_index: int | None = None
    parameter_name: str | None = None
    if isinstance(request.parameter, int) and not isinstance(request.parameter, bool):
        if request.parameter < 0 or request.parameter > MAX_PARAMETER_INDEX:
            return SemanticControlResolution(
                request=request,
                status="unresolved",
                reason="requested parameter index is outside the bounded FL range",
            )
        parameter_index = request.parameter
    elif isinstance(request.parameter, str):
        parameter_matches = [
            row
            for row in observed_parameters
            if _canonical(row.name or "") == _canonical(request.parameter)
        ]
        if len(parameter_matches) == 1:
            parameter_index = parameter_matches[0].index
            parameter_name = parameter_matches[0].name
        elif len(parameter_matches) > 1:
            return SemanticControlResolution(
                request=request,
                status="unresolved",
                reason="requested parameter name matched more than one runtime control",
            )
        else:
            return SemanticControlResolution(
                request=request,
                status="unresolved",
                reason="requested parameter name was not observed at runtime",
            )
    elif control.parameter_index is not None:
        parameter_index = control.parameter_index
    else:
        parameter_matches = [
            row
            for row in observed_parameters
            if _parameter_matches_control(row, control)
        ]
        if len(parameter_matches) == 1:
            parameter_index = parameter_matches[0].index
            parameter_name = parameter_matches[0].name
        elif len(parameter_matches) > 1:
            # Multiple exact labels are common for repeated EQ bands.  Atlas
            # must provide an explicit index/name selector in that case.
            return SemanticControlResolution(
                request=request,
                status="unresolved",
                reason="adapter control matched multiple runtime parameters; index is not guessable",
            )
    if parameter_index is None:
        return SemanticControlResolution(
            request=request,
            status="unresolved",
            reason="adapter did not establish an exact parameter index",
        )
    # Enumerated options are literal values.  If Atlas lists options, reject a
    # non-member before mutation; if it does not, the existing verified option
    # setter remains the authority and will report the discovered enumeration.
    option = request.option
    is_enum = (
        control.kind == "enumerated"
        or bool(control.options)
        or control.preferred_write_tool == "fl_set_plugin_param_option"
    )
    display_source = (
        request.display_value
        if request.display_value is not None
        else request.display_text
    )
    if option is None and is_enum and isinstance(display_source, str):
        exact_option = next(
            (
                item
                for item in control.options
                if item.casefold() == display_source.strip().casefold()
            ),
            None,
        )
        if exact_option is not None:
            option = display_source.strip()
    if option is not None and is_enum:
        if control.options and not any(
            item.casefold() == option.casefold() for item in control.options
        ):
            return SemanticControlResolution(
                request=request,
                status="unresolved",
                reason="requested option is not an exact option established by the adapter",
            )
        established_option = next(
            (
                item
                for item in control.options
                if item.casefold() == option.casefold()
            ),
            option,
        )
        return SemanticControlResolution(
            request=request,
            control=ResolvedSemanticControl(
                control_role=request.control_role,
                control_id=control.control_id,
                parameter_index=parameter_index,
                parameter_name=parameter_name,
                display_unit=control.unit,
                setter="fl_set_plugin_param_option",
                # Pass the Atlas spelling to the exact-option setter.  A
                # case-insensitive request is convenient at the semantic
                # boundary, but FL's option text is a literal write value.
                option=established_option,
            ),
            status="resolved",
        )
    display_number, display_text = _display_number(display_source)
    if display_number is not None:
        # A numeric display request is always preferable to a normalized curve
        # when the existing verified display setter can express it.
        return SemanticControlResolution(
            request=request,
            control=ResolvedSemanticControl(
                control_role=request.control_role,
                control_id=control.control_id,
                parameter_index=parameter_index,
                parameter_name=parameter_name,
                display_unit=request.display_unit or control.unit,
                setter="fl_set_plugin_param_display",
                display_value=display_number,
                display_text=display_text,
            ),
            status="resolved",
        )
    if request.normalized_value is not None and request.normalized_mapping:
        preferred = control.preferred_write_tool
        if preferred not in {"fl_set_plugin_param", "unknown"}:
            # A display/option-capable control still cannot receive a guessed
            # normalized value.  Require the caller to supply that preferred
            # semantic representation instead.
            return SemanticControlResolution(
                request=request,
                status="unresolved",
                reason="normalized value is not the adapter's preferred established representation",
            )
        return SemanticControlResolution(
            request=request,
            control=ResolvedSemanticControl(
                control_role=request.control_role,
                control_id=control.control_id,
                parameter_index=parameter_index,
                parameter_name=parameter_name,
                display_unit=control.unit,
                setter="fl_set_plugin_param",
                normalized_value=float(request.normalized_value),
                normalized_mapping=request.normalized_mapping,
            ),
            status="resolved",
        )
    reason = (
        "display value is not numeric and no exact option or established normalized mapping was supplied"
        if display_source is not None
        else "no supported semantic representation was supplied"
    )
    return SemanticControlResolution(request=request, status="unresolved", reason=reason)


def plan_processing(
    request: ProcessingRequest,
    *,
    loaded_plugins: Iterable[object] = (),
    loaded_effects: Iterable[object] | None = None,
    registry: AtlasRegistry,
) -> ProcessingPlan:
    """Build a deterministic processing plan from loaded compatible effects."""

    if not isinstance(request, ProcessingRequest):
        raise TypeError("request must be a ProcessingRequest")
    if loaded_effects is not None:
        loaded_plugins = loaded_effects
    capabilities = resolve_loaded_capabilities(loaded_plugins, registry=registry)
    goals = request.expanded_goals()
    candidates: list[ProcessingCandidate] = []
    actions: list[SemanticPluginAction] = []
    missing: list[MissingProcessingCapability] = []
    previous_by_candidate: dict[str, str] = {}
    for goal in goals:
        goal_candidates = _select_candidates(goal, capabilities, registry)
        candidates.extend(goal_candidates)
        selected = next((candidate for candidate in goal_candidates if candidate.valid), None)
        if selected is None:
            # Keep optional gaps visible in the plan as well as in the
            # coverage report.  ``required`` controls whether readiness must
            # stop; it must not hide a requested category from the final
            # structured result.
            missing.append(_missing_for_goal(goal, capabilities))
            continue
        # A read-only inventory may carry ``allow_master=true`` so its target
        # can be represented safely.  That observation is not authorization to
        # mutate the Master bus; the run request must opt in explicitly.
        if (
            isinstance(selected.target, MixerEffectTarget)
            and selected.target.track_index == 0
            and not request.allow_master
        ):
            missing.append(
                MissingProcessingCapability(
                    role=goal.role,
                    category=_goal_categories(goal)[0],
                    requested_techniques=(goal.technique or goal.goal,),
                    reason="Master processing requires explicit allow_master authorization",
                    required=goal.required,
                    target=selected.target,
                )
            )
            continue
        for index, control in enumerate(goal.controls, start=1):
            resolution = (
                selected.control_resolutions[index - 1]
                if index - 1 < len(selected.control_resolutions)
                else SemanticControlResolution(
                    request=control,
                    status="unresolved",
                    reason="selected candidate did not carry a control resolution",
                )
            )
            # This guard keeps malformed injected records from creating a
            # mutation action with an unknown control.
            if resolution.status != "resolved":
                continue
            action_id = f"{goal.goal_id}-{index}"
            dependencies = () if selected.candidate_id not in previous_by_candidate else (
                previous_by_candidate[selected.candidate_id],
            )
            previous_by_candidate[selected.candidate_id] = action_id
            actions.append(
                SemanticPluginAction(
                    action_id=action_id,
                    goal_id=goal.goal_id,
                    role=goal.role,
                    target=selected.target,
                    target_fingerprint=selected.target_fingerprint,
                    plugin_name=selected.plugin_name,
                    product_id=selected.product_id,
                    adapter_id=selected.adapter_id,
                    control=control,
                    resolution=resolution,
                    depends_on=dependencies,
                    session_fingerprint=request.session_fingerprint,
                    allow_master=(
                        request.allow_master
                        or (
                            isinstance(selected.target, MixerEffectTarget)
                            and selected.target.allow_master
                        )
                    ),
                    rationale=(
                        goal.rationale
                        or "Restrained first-pass semantic processing; audible quality is not evaluated."
                    ),
                )
            )
    # Keep the action surface bounded and deterministic even if a caller
    # supplied a larger request.  The plan is still useful as a report.
    if len(actions) > MAX_PROCESSING_ACTIONS:
        actions = actions[:MAX_PROCESSING_ACTIONS]
    plan_material = "|".join((request.request_id, *(item.action_id for item in actions)))
    plan_id = f"plan-{hashlib.sha256(plan_material.encode('utf-8')).hexdigest()[:24]}"
    warnings = [
        "Processing actions use existing verified setters supplied by the caller; no FL API is invoked while planning.",
        "The first-pass policy is conservative and does not treat metadata reasoning as audible proof.",
    ]
    if missing:
        warnings.append("One or more requested processing goals have missing or unresolved loaded effects.")
    return ProcessingPlan(
        plan_id=plan_id,
        request_id=request.request_id,
        completion_target=request.completion_target,
        session_fingerprint=request.session_fingerprint,
        candidates=tuple(candidates[:MAX_PROCESSING_LIST]),
        actions=tuple(actions),
        missing_capabilities=tuple(missing[:MAX_PROCESSING_LIST]),
        warnings=tuple(warnings),
    )


# Short aliases make the pure services easy to discover while keeping one
# implementation and one vocabulary for integration callers.
build_processing_plan = plan_processing
build_effect_coverage = evaluate_effect_coverage
assess_effect_coverage = evaluate_effect_coverage
resolve_effect_coverage = evaluate_effect_coverage
inspect_effect_coverage = evaluate_effect_coverage
resolve_loaded_processing_capabilities = resolve_loaded_capabilities
plan_processing_request = plan_processing
create_processing_plan = plan_processing


def resolve_processing_candidates(
    goal: ProcessingGoal,
    loaded_plugins: Iterable[object] = (),
    *,
    registry: AtlasRegistry,
) -> tuple[ProcessingCandidate, ...]:
    """Return ranked candidates for one goal using only loaded observations."""

    if not isinstance(goal, ProcessingGoal):
        raise TypeError("goal must be a ProcessingGoal")
    capabilities = resolve_loaded_capabilities(loaded_plugins, registry=registry)
    return _select_candidates(goal, capabilities, registry)


__all__ = [
    "CompletionTarget",
    "CoverageState",
    "EffectCoverageReport",
    "GOAL_CATEGORIES",
    "LoadedProcessingCapability",
    "MissingProcessingCapability",
    "ProcessingActionReceipt",
    "ProcessingCandidate",
    "ProcessingGoal",
    "ProcessingModel",
    "ProcessingPlan",
    "ProcessingPlanReceipt",
    "ProcessingRequest",
    "ProcessingStatus",
    "ResolvedSemanticControl",
    "RoleEffectCoverage",
    "RoleProcessingRequest",
    "SemanticControlResolution",
    "SemanticControlValue",
    "SemanticPluginAction",
    "build_processing_plan",
    "build_effect_coverage",
    "assess_effect_coverage",
    "evaluate_effect_coverage",
    "inspect_effect_coverage",
    "plan_processing",
    "resolve_effect_coverage",
    "resolve_loaded_capabilities",
    "resolve_loaded_processing_capabilities",
    "resolve_processing_candidates",
    "resolve_semantic_control",
    "plan_processing_request",
    "create_processing_plan",
]
