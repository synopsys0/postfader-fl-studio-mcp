"""Immutable contracts shared by the creation-pipeline foundation.

The creation pipeline is intentionally built on observations.  Nothing in
this module talks to FL Studio or turns a readiness fact into a write.  The
records are small, bounded snapshots that can safely cross the MCP boundary
and can be reused by several phases of one run.

The rest of PostFader has a few older ``ContractModel`` variants which are
permissive for compatibility.  New creation-pipeline records use the stricter
base below: unknown fields are rejected, scalar coercion is disabled, and all
collections are tuples so a frozen model is immutable all the way down.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, cast

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    model_validator,
)

from ..contracts import ConnectionInfo, ProjectSummary
from ..sound_selection.models import DrumPadMap
from ..track_b_contracts import SESSION_FINGERPRINT_PATTERN, PluginTarget


CREATION_PIPELINE_SCHEMA_VERSION = "1.0"
SHA256_PATTERN = r"^[0-9a-f]{64}$"

MAX_ID_LENGTH = 128
MAX_SHORT_TEXT = 256
MAX_TEXT = 2048
MAX_LONG_TEXT = 4096
MAX_DIMENSIONS = 16
MAX_ISSUES = 128
MAX_EVIDENCE = 32
MAX_TARGETS = 256
MAX_ROLES = 128
MAX_PATTERNS = 512
MAX_EFFECTS = 256
MAX_TECHNIQUES = 64
MAX_ACTIONS = 64
MAX_PHASES = 16

Digest = Annotated[str, Field(pattern=SHA256_PATTERN)]
Identifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_ID_LENGTH,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
RoleIdentifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
Fingerprint = Annotated[str, Field(min_length=1, max_length=128)]

ReadinessState = Literal["ready", "ready_with_limitations", "blocked"]
IssueClassification = Literal[
    "blocking", "limitation", "manual_handoff", "optional_enhancement"
]
ReadinessDimensionName = Literal[
    "connection_bridge",
    "piano_roll",
    "instrument_pool",
    "drum_coverage",
    "patterns_arrangement",
    "mixer_effects",
    "scope_manual_work",
]

EffectCoverageState = Literal[
    "effect_covered",
    "dry_by_design",
    "missing_requested_effect",
    "loaded_but_unresolved",
    "loaded_and_controllable",
    "unresolved_effect",
    "not_applicable",
]
CompletionTarget = Literal[
    "composition_only",
    "playable_draft",
    "first_pass_production",
    "restrained_first_pass",
    "mix_ready",
    "polished_mix_ready",
    "custom",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _freeze_collections(value: object) -> object:
    """Copy JSON-like arrays before strict Pydantic validation.

    Pydantic's ``frozen`` option prevents assigning a new top-level value but
    does not freeze a nested Python list.  Creation contracts therefore use a
    before-validator to turn every input list into a tuple.  Mappings are not
    part of the public contracts, but recursively copying them here keeps
    validators from retaining a caller-owned mutable object.
    """

    if isinstance(value, list):
        return tuple(_freeze_collections(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_collections(item) for item in value)
    if isinstance(value, dict):
        return {key: _freeze_collections(item) for key, item in value.items()}
    return value


class CreationPipelineModel(BaseModel):
    """Strict, deeply collection-immutable creation contract base."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        validate_default=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _freeze_input_collections(cls, value: object) -> object:
        return _freeze_collections(value)


class ReadinessEvidence(CreationPipelineModel):
    """One bounded observation supporting a readiness claim."""

    source: str = Field(min_length=1, max_length=MAX_SHORT_TEXT)
    detail: str = Field(min_length=1, max_length=MAX_TEXT)
    observed_at: datetime | None = None
    revision: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)


class ReadinessBlocker(CreationPipelineModel):
    """An independently detectable condition that may stop a creation run."""

    code: Identifier
    dimension: ReadinessDimensionName
    message: str = Field(min_length=1, max_length=MAX_TEXT)
    classification: IssueClassification = Field(
        default="blocking",
        validation_alias=AliasChoices("classification", "severity", "kind"),
    )
    evidence: tuple[ReadinessEvidence, ...] = Field(default=(), max_length=MAX_EVIDENCE)
    manual_action_id: Identifier | None = None

    @property
    def blocking(self) -> bool:
        """Whether this issue prevents the requested run from proceeding."""

        return self.classification == "blocking"

    @property
    def severity(self) -> IssueClassification:
        """Compatibility spelling used by some integration callers."""

        return self.classification

    @property
    def kind(self) -> IssueClassification:
        return self.classification

    @property
    def reason(self) -> str:
        return self.message


class ReadinessLimitation(CreationPipelineModel):
    """A truthful limitation that does not necessarily prevent composition."""

    code: Identifier
    dimension: ReadinessDimensionName
    message: str = Field(min_length=1, max_length=MAX_TEXT)
    classification: IssueClassification = Field(
        default="limitation",
        validation_alias=AliasChoices("classification", "severity", "kind"),
    )
    evidence: tuple[ReadinessEvidence, ...] = Field(default=(), max_length=MAX_EVIDENCE)

    @property
    def kind(self) -> IssueClassification:
        return self.classification

    @property
    def reason(self) -> str:
        return self.message


class ReadinessManualAction(CreationPipelineModel):
    """One setup or delivery action that is known before any mutation."""

    action_id: Identifier
    dimension: ReadinessDimensionName
    instruction: str = Field(
        min_length=1,
        max_length=MAX_TEXT,
        validation_alias=AliasChoices("instruction", "action", "description"),
    )
    required: bool = True
    completed: bool = False
    blocking: bool = False
    evidence: tuple[ReadinessEvidence, ...] = Field(default=(), max_length=MAX_EVIDENCE)

    @property
    def action(self) -> str:
        return self.instruction


class ReadinessDimension(CreationPipelineModel):
    """Status and evidence for one independent readiness dimension."""

    name: ReadinessDimensionName = Field(
        validation_alias=AliasChoices("name", "dimension")
    )
    state: ReadinessState = Field(
        default="ready", validation_alias=AliasChoices("state", "status")
    )
    summary: str = Field(min_length=1, max_length=MAX_TEXT)
    evidence: tuple[ReadinessEvidence, ...] = Field(default=(), max_length=MAX_EVIDENCE)
    blocker_codes: tuple[Identifier, ...] = Field(default=(), max_length=MAX_ISSUES)
    limitation_codes: tuple[Identifier, ...] = Field(default=(), max_length=MAX_ISSUES)
    manual_action_ids: tuple[Identifier, ...] = Field(default=(), max_length=MAX_ACTIONS)

    @property
    def dimension(self) -> ReadinessDimensionName:
        return self.name

    @property
    def status(self) -> ReadinessState:
        return self.state

    @property
    def id(self) -> ReadinessDimensionName:
        return self.name


class ConnectionReadiness(CreationPipelineModel):
    """Connection facts needed by a run, including process-local checks."""

    connection_info: ConnectionInfo | None = Field(
        default=None, validation_alias=AliasChoices("connection_info", "connection")
    )
    # Direct fields mirror the repository's ``ConnectionInfo`` for callers
    # that already have a flattened observation.  Integrations may use either
    # form; the evaluator gives the nested observation precedence.
    connected: bool | None = None
    compatible: bool | None = None
    compatibility_reason: str | None = Field(default=None, max_length=MAX_TEXT)
    session_fingerprint: str | None = Field(
        default=None, pattern=SESSION_FINGERPRINT_PATTERN
    )
    mcp_process_identity: Fingerprint | None = Field(
        default=None, validation_alias=AliasChoices("mcp_process_identity", "mcp_process_id")
    )
    package_source_revision: str | None = Field(
        default=None,
        max_length=MAX_SHORT_TEXT,
        validation_alias=AliasChoices("package_source_revision", "package_revision"),
    )
    deployed_bridge_revision: str | None = Field(
        default=None,
        max_length=MAX_SHORT_TEXT,
        validation_alias=AliasChoices("deployed_bridge_revision", "deployed_revision"),
    )
    running_bridge_revision: str | None = Field(
        default=None,
        max_length=MAX_SHORT_TEXT,
        validation_alias=AliasChoices("running_bridge_revision", "running_revision"),
    )
    midi_endpoint: str | None = Field(
        default=None, validation_alias=AliasChoices("midi_endpoint", "midi_port"), max_length=MAX_SHORT_TEXT
    )
    midi_input_available: bool | None = None
    midi_output_available: bool | None = None
    queue_healthy: bool | None = None
    supported_fl_build: bool | None = None
    scripting_api_version: int | None = Field(default=None, ge=0, le=1_000_000)
    runtime_write_mode_control: bool | None = None
    current_write_state: Literal["disabled", "enabled", "unknown"] = Field(
        default="unknown", validation_alias=AliasChoices("current_write_state", "current_write_mode")
    )
    require_process_identity: bool = False
    require_midi: bool = False

class PianoRollReadiness(CreationPipelineModel):
    """Read-only capabilities and setup state for Piano Roll writes."""

    required: bool = True
    apply_script_present: bool = Field(
        default=False, validation_alias=AliasChoices("apply_script_present", "script_exists", "apply_script")
    )
    armed_this_process: bool = Field(
        default=False, validation_alias=AliasChoices("armed_this_process", "armed", "armed_this_session")
    )
    authenticated_arming_receipt: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "authenticated_arming_receipt", "arming_receipt_available", "arming_receipt"
        ),
    )
    arming_receipt_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    target_selection_supported: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "target_selection_supported", "target_selection_capability"
        ),
    )
    persistence_receipt_supported: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "persistence_receipt_supported", "persistence_receipt_capability"
        ),
    )
    manual_action: ReadinessManualAction | None = None
    manual_actions: tuple[ReadinessManualAction, ...] = Field(default=(), max_length=MAX_ACTIONS)
    warnings: tuple[str, ...] = Field(default=(), max_length=MAX_ISSUES)

    @model_validator(mode="before")
    @classmethod
    def normalize_receipt_input(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        raw_receipt = data.get("arming_receipt")
        if isinstance(raw_receipt, str):
            data["arming_receipt_id"] = raw_receipt
            data["authenticated_arming_receipt"] = True
            data.pop("arming_receipt", None)
        elif isinstance(raw_receipt, dict):
            receipt_id = raw_receipt.get("receipt_id")
            if isinstance(receipt_id, str):
                data["arming_receipt_id"] = receipt_id
            data["authenticated_arming_receipt"] = bool(
                raw_receipt.get("authenticated", True)
            )
            data.pop("arming_receipt", None)
        return data

    @property
    def ready(self) -> bool:
        if not self.required:
            return True
        return (
            self.apply_script_present
            and self.armed_this_process
            and self.authenticated_arming_receipt
            and self.target_selection_supported
            and self.persistence_receipt_supported
        )

    @property
    def arming_receipt_available(self) -> bool:
        return self.authenticated_arming_receipt


class InstrumentTargetCoverage(CreationPipelineModel):
    """Read-only coverage for one loaded generator target."""

    target: PluginTarget | None = None
    target_fingerprint: Digest | None = None
    product_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    product_name: str = Field(min_length=1, max_length=MAX_SHORT_TEXT)
    current_preset: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    requested_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_ROLES)
    atlas_match: bool = Field(
        default=False, validation_alias=AliasChoices("atlas_match", "atlas_product_match")
    )
    preset_navigation_supported: bool = False
    preset_discovery_supported: bool = Field(
        default=False, validation_alias=AliasChoices("preset_discovery_supported", "preset_discovery_available")
    )
    usable_preset_candidate: bool = Field(
        default=False, validation_alias=AliasChoices("usable_preset_candidate", "usable_candidate")
    )
    preset_identity_verified: bool = False
    warnings: tuple[str, ...] = Field(default=(), max_length=MAX_ISSUES)


class InstrumentPoolCoverage(CreationPipelineModel):
    """Aggregate loaded-generator and requested-role coverage."""

    loaded_generators: tuple[InstrumentTargetCoverage, ...] = Field(
        default=(), max_length=MAX_TARGETS
    )
    requested_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_ROLES)
    required_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_ROLES)
    covered_roles: tuple[RoleIdentifier, ...] = Field(
        default=(), validation_alias=AliasChoices("covered_roles", "roles_covered"), max_length=MAX_ROLES
    )
    missing_roles: tuple[RoleIdentifier, ...] = Field(
        default=(), validation_alias=AliasChoices("missing_roles", "roles_missing"), max_length=MAX_ROLES
    )
    required_products: tuple[str, ...] = Field(default=(), max_length=MAX_ROLES)
    loaded_products: tuple[str, ...] = Field(default=(), max_length=MAX_ROLES)
    missing_products: tuple[str, ...] = Field(default=(), max_length=MAX_ROLES)
    preset_navigation_supported: bool = False
    usable_preset_discovery: bool = False
    warnings: tuple[str, ...] = Field(default=(), max_length=MAX_ISSUES)

    @model_validator(mode="after")
    def validate_roles(self) -> "InstrumentPoolCoverage":
        for label, values in (
            ("requested_roles", self.requested_roles),
            ("required_roles", self.required_roles),
            ("covered_roles", self.covered_roles),
            ("missing_roles", self.missing_roles),
            ("required_products", self.required_products),
            ("loaded_products", self.loaded_products),
            ("missing_products", self.missing_products),
        ):
            if len({item.casefold() for item in values}) != len(values):
                raise ValueError(f"{label} must not contain duplicates")
        return self

    @property
    def loaded_generator_count(self) -> int:
        return len(self.loaded_generators)

    @property
    def role_requirements(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.requested_roles, *self.required_roles)))

    @property
    def missing_required_roles(self) -> tuple[str, ...]:
        return self.missing_roles

    @property
    def roles_covered(self) -> tuple[str, ...]:
        return self.covered_roles

    @property
    def roles_missing(self) -> tuple[str, ...]:
        return self.missing_roles


class DrumCoverage(CreationPipelineModel):
    """Semantic drum-role coverage for the selected generator."""

    required: bool = False
    loaded_drum_generator: bool = Field(
        default=False,
        validation_alias=AliasChoices("loaded_drum_generator", "drum_generator_loaded"),
    )
    target_fingerprint: Digest | None = None
    target: PluginTarget | None = None
    product_name: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    exact_kit_preset_supported: bool = False
    drum_map: DrumPadMap | None = None
    pad_map_available: bool = Field(
        default=False, validation_alias=AliasChoices("pad_map_available", "reported_pad_map_available")
    )
    required_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_ROLES)
    mapped_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_ROLES)
    missing_roles: tuple[RoleIdentifier, ...] = Field(
        default=(), validation_alias=AliasChoices("missing_roles", "missing_required_roles"), max_length=MAX_ROLES
    )
    general_midi_fallback_available: bool = False
    warnings: tuple[str, ...] = Field(default=(), max_length=MAX_ISSUES)

class PatternIdentity(CreationPipelineModel):
    """Observation-scoped identity for a pattern target."""

    pattern_number: int = Field(ge=1, le=999)
    name: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    fingerprint: Digest | None = None
    contains_material: bool = False
    intended_section: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)


class PatternCoverage(CreationPipelineModel):
    """Pattern and Playlist delivery facts used by readiness."""

    existing_patterns: tuple[PatternIdentity, ...] = Field(
        default=(), validation_alias=AliasChoices("existing_patterns", "patterns"), max_length=MAX_PATTERNS
    )
    empty_pattern_numbers: tuple[int, ...] = Field(
        default=(), validation_alias=AliasChoices("empty_pattern_numbers", "empty_patterns"), max_length=MAX_PATTERNS
    )
    intended_pattern_numbers: tuple[int, ...] = Field(
        default=(), validation_alias=AliasChoices("intended_pattern_numbers", "intended_patterns"), max_length=MAX_PATTERNS
    )
    required_empty_patterns: tuple[int, ...] = Field(
        default=(), validation_alias=AliasChoices("required_empty_patterns", "required_patterns"), max_length=MAX_PATTERNS
    )
    empty_patterns_available: bool | None = None
    patterns_with_existing_material: tuple[int, ...] = Field(
        default=(),
        validation_alias=AliasChoices(
            "patterns_with_existing_material", "patterns_containing_material"
        ),
        max_length=MAX_PATTERNS,
    )
    required_sections: tuple[str, ...] = Field(default=(), max_length=MAX_PHASES)
    available_sections: tuple[str, ...] = Field(default=(), max_length=MAX_PHASES)
    missing_sections: tuple[str, ...] = Field(default=(), max_length=MAX_PHASES)
    playlist_clip_visibility_supported: bool = False
    playlist_placement_supported: bool = False
    manual_playlist_placement_required: bool = False
    expected_manual_playlist_actions: tuple[str, ...] = Field(
        default=(), validation_alias=AliasChoices("expected_manual_playlist_actions", "manual_playlist_actions"), max_length=MAX_ACTIONS
    )
    warnings: tuple[str, ...] = Field(default=(), max_length=MAX_ISSUES)

    @model_validator(mode="after")
    def validate_patterns(self) -> "PatternCoverage":
        for label, values in (
            ("empty_pattern_numbers", self.empty_pattern_numbers),
            ("intended_pattern_numbers", self.intended_pattern_numbers),
            ("required_empty_patterns", self.required_empty_patterns),
            ("patterns_with_existing_material", self.patterns_with_existing_material),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{label} must not contain duplicates")
        return self

    @property
    def existing_pattern_count(self) -> int:
        return len(self.existing_patterns)

    @property
    def empty_patterns(self) -> tuple[int, ...]:
        return self.empty_pattern_numbers

    @property
    def intended_patterns(self) -> tuple[int, ...]:
        return self.intended_pattern_numbers


class LoadedProcessingCapability(CreationPipelineModel):
    """One loaded effect with explicit Atlas and adapter evidence."""

    role_id: RoleIdentifier | None = None
    track_index: int | None = Field(default=None, ge=0)
    slot_index: int | None = Field(default=None, ge=0, le=9)
    product_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    product_name: str = Field(min_length=1, max_length=MAX_SHORT_TEXT)
    target_fingerprint: Digest | None = None
    atlas_match: bool = False
    atlas_product_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    adapter_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    semantic_controls: tuple[str, ...] = Field(default=(), max_length=MAX_TECHNIQUES)
    supported_techniques: tuple[str, ...] = Field(default=(), max_length=MAX_TECHNIQUES)
    unresolved_controls: tuple[str, ...] = Field(default=(), max_length=MAX_TECHNIQUES)
    controllable: bool = False


class MissingProcessingCapability(CreationPipelineModel):
    """One requested processing category without a usable loaded adapter."""

    role_id: RoleIdentifier | None = None
    category: str = Field(min_length=1, max_length=MAX_SHORT_TEXT)
    reason: str = Field(min_length=1, max_length=MAX_TEXT)
    required_for_completion: bool = Field(
        default=False, validation_alias=AliasChoices("required_for_completion", "required")
    )
    classification: IssueClassification = "limitation"


class RoleEffectCoverage(CreationPipelineModel):
    """Effect coverage for one musical role."""

    role_id: RoleIdentifier = Field(validation_alias=AliasChoices("role_id", "role"))
    loaded_effects: tuple[LoadedProcessingCapability, ...] = Field(
        default=(), max_length=MAX_EFFECTS
    )
    requested_techniques: tuple[str, ...] = Field(default=(), max_length=MAX_TECHNIQUES)
    supported_techniques: tuple[str, ...] = Field(default=(), max_length=MAX_TECHNIQUES)
    unresolved_controls: tuple[str, ...] = Field(default=(), max_length=MAX_TECHNIQUES)
    missing_capabilities: tuple[MissingProcessingCapability, ...] = Field(
        default=(), max_length=MAX_TECHNIQUES
    )
    state: EffectCoverageState = "not_applicable"
    dry_playback_allowed: bool = True
    processing_required_for_completion: bool = Field(
        default=False, validation_alias=AliasChoices("processing_required_for_completion", "processing_required")
    )
    warning: str | None = Field(default=None, max_length=MAX_TEXT)

    @property
    def effect_covered(self) -> bool:
        return self.state in {"effect_covered", "loaded_and_controllable"}


class EffectCoverageReport(CreationPipelineModel):
    """Project-level effect coverage, suitable for readiness and outcomes."""

    roles: tuple[RoleEffectCoverage, ...] = Field(
        default=(), validation_alias=AliasChoices("roles", "role_coverage"), max_length=MAX_ROLES
    )
    requested_categories: tuple[str, ...] = Field(default=(), max_length=MAX_TECHNIQUES)
    missing_capabilities: tuple[MissingProcessingCapability, ...] = Field(
        default=(), max_length=MAX_EFFECTS
    )
    loaded_capabilities: tuple[LoadedProcessingCapability, ...] = Field(
        default=(),
        validation_alias=AliasChoices("loaded_capabilities", "loaded_effect_chain"),
        max_length=MAX_EFFECTS,
    )
    completion_target: CompletionTarget = "playable_draft"
    processing_required_for_completion: bool = False
    can_produce_dry_draft: bool = Field(
        default=True, validation_alias=AliasChoices("can_produce_dry_draft", "dry_draft_possible")
    )
    dry_by_design: bool = False
    state: EffectCoverageState | None = None
    processing_state: str | None = Field(default=None, max_length=64)
    required_processing_missing: bool = False
    warnings: tuple[str, ...] = Field(default=(), max_length=MAX_ISSUES)

    @property
    def missing_required_capabilities(self) -> tuple[MissingProcessingCapability, ...]:
        return tuple(
            item for item in self.missing_capabilities if item.required_for_completion
        )

    @property
    def has_unresolved_controls(self) -> bool:
        return any(item.unresolved_controls for item in self.roles)

    @property
    def role_coverage(self) -> tuple[RoleEffectCoverage, ...]:
        return self.roles

    @property
    def loaded_effect_chain(self) -> tuple[LoadedProcessingCapability, ...]:
        return self.loaded_capabilities

    @property
    def missing_requested_categories(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.category for item in self.missing_capabilities))

    @property
    def dry_draft_possible(self) -> bool:
        return self.can_produce_dry_draft


class ScopeReadiness(CreationPipelineModel):
    """Mutation scope and known manual-handoff observations."""

    preserved_targets: tuple[Any, ...] = Field(default=(), max_length=MAX_TARGETS)
    allowed_mutation_categories: tuple[str, ...] = Field(default=(), max_length=MAX_ROLES)
    required_mutation_categories: tuple[str, ...] = Field(default=(), max_length=MAX_ROLES)
    unavailable_operations: tuple[str, ...] = Field(default=(), max_length=MAX_ACTIONS)
    expected_manual_playlist_actions: tuple[str, ...] = Field(default=(), max_length=MAX_ACTIONS)
    expected_export_action: str | None = Field(default=None, max_length=MAX_TEXT)
    warnings: tuple[str, ...] = Field(default=(), max_length=MAX_ISSUES)

class CreationReadinessInput(CreationPipelineModel):
    """Read-only facts consumed by :class:`CreationReadinessService`.

    The fields intentionally accept already-captured repository observations;
    the evaluator never invokes an inspector, bridge, or writer.  Integrators
    can progressively populate a dimension without changing the report
    contract.
    """

    observed_at: datetime = Field(default_factory=_now)
    connection: ConnectionReadiness = Field(default_factory=ConnectionReadiness)
    project: ProjectSummary | None = None
    piano_roll: PianoRollReadiness = Field(default_factory=PianoRollReadiness)
    instrument_pool: InstrumentPoolCoverage = Field(default_factory=InstrumentPoolCoverage)
    drum_coverage: DrumCoverage = Field(default_factory=DrumCoverage)
    patterns: PatternCoverage = Field(default_factory=PatternCoverage)
    effects: EffectCoverageReport = Field(default_factory=EffectCoverageReport)
    scope: ScopeReadiness = Field(default_factory=ScopeReadiness)
    requested_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_ROLES)
    required_drum_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_ROLES)
    completion_target: CompletionTarget = "playable_draft"
    processing_required: bool = False
    context_snapshot: SerializeAsAny[CreationPipelineModel] | None = None
    palette_inventory_digest: Digest | None = None
    preset_inventory_digest: Digest | None = None
    drum_map_digest: Digest | None = None
    effect_coverage_digest: Digest | None = None
    project_checkpoint_digest: Digest | None = None

    @model_validator(mode="after")
    def validate_role_lists(self) -> "CreationReadinessInput":
        for label, values in (
            ("requested_roles", self.requested_roles),
            ("required_drum_roles", self.required_drum_roles),
        ):
            if len({item.casefold() for item in values}) != len(values):
                raise ValueError(f"{label} must not contain duplicates")
        return self

    @model_validator(mode="before")
    @classmethod
    def normalize_connection_observation(cls, value: object) -> object:
        """Accept the repository ``ConnectionInfo`` as a convenient input.

        Existing inspectors return ``ConnectionInfo`` directly.  Keeping the
        process-local wrapper in the creation contract lets integrations pass
        that observation without writing an adapter or weakening strict field
        validation.
        """

        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "connection_info" in data and "connection" not in data:
            data["connection"] = data.pop("connection_info")
        connection = data.get("connection")
        if isinstance(connection, ConnectionInfo):
            data["connection"] = ConnectionReadiness(connection_info=connection)
        elif isinstance(connection, dict) and "connected" in connection:
            data["connection"] = ConnectionReadiness(
                connection_info=ConnectionInfo(**connection)
            )
        effects = data.get("effects")
        if not isinstance(effects, EffectCoverageReport) and isinstance(
            effects, BaseModel
        ):
            data["effects"] = _coerce_effect_coverage(effects.model_dump(mode="python"))
        elif isinstance(effects, dict) and (
            "processing_state" in effects or "required_processing_missing" in effects
        ):
            data["effects"] = _coerce_effect_coverage(effects)
        context = data.get("context_snapshot")
        if isinstance(context, dict):
            # Import lazily: ``context.py`` builds on these models, while a
            # serialized Production Run state may need to reconstruct the
            # richer snapshot after this module has already loaded.
            from .context import CreationRunContextSnapshot

            data["context_snapshot"] = CreationRunContextSnapshot.model_validate(
                context
            )
        return data


class CreationReadinessReport(CreationPipelineModel):
    """Complete aggregate preflight result; no mutation is implied."""

    schema_version: Literal["1.0"] = CREATION_PIPELINE_SCHEMA_VERSION
    observed_at: datetime
    overall_state: ReadinessState = Field(
        validation_alias=AliasChoices(
            "overall_state", "overall_status", "state", "status"
        )
    )
    score: float = Field(ge=0.0, le=100.0)
    dimensions: tuple[ReadinessDimension, ...] = Field(
        default=(), max_length=MAX_DIMENSIONS
    )
    blockers: tuple[ReadinessBlocker, ...] = Field(default=(), max_length=MAX_ISSUES)
    limitations: tuple[ReadinessLimitation, ...] = Field(default=(), max_length=MAX_ISSUES)
    manual_actions: tuple[ReadinessManualAction, ...] = Field(default=(), max_length=MAX_ACTIONS)
    optional_enhancements: tuple[ReadinessLimitation, ...] = Field(
        default=(), max_length=MAX_ACTIONS
    )
    context_snapshot: SerializeAsAny[CreationPipelineModel] | None = None
    mutations_performed: Literal[False] = False
    zero_mutations: Literal[True] = True
    warnings: tuple[str, ...] = Field(default=(), max_length=MAX_ISSUES)

    @model_validator(mode="before")
    @classmethod
    def normalize_context_snapshot(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        context = value.get("context_snapshot")
        if isinstance(context, dict):
            # See the matching input validator above.  Keeping the concrete
            # snapshot object through validation preserves strictness and
            # allows Production Run state to round-trip through model_dump.
            from .context import CreationRunContextSnapshot

            data = dict(value)
            data["context_snapshot"] = CreationRunContextSnapshot.model_validate(
                context
            )
            return data
        return value

    @property
    def state(self) -> ReadinessState:
        return self.overall_state

    @property
    def status(self) -> ReadinessState:
        return self.overall_state

    @property
    def is_ready(self) -> bool:
        return self.overall_state != "blocked"

    @property
    def blocking(self) -> bool:
        return bool(self.blockers) or self.overall_state == "blocked"

    @property
    def context(self) -> CreationPipelineModel | None:
        return self.context_snapshot

    @property
    def overall(self) -> ReadinessState:
        return self.overall_state

    @property
    def blocking_issues(self) -> tuple[ReadinessBlocker, ...]:
        return self.blockers

    @property
    def required_manual_actions(self) -> tuple[ReadinessManualAction, ...]:
        return tuple(item for item in self.manual_actions if item.required)

    @property
    def overall_status(self) -> ReadinessState:
        return self.overall_state

    @property
    def manual_handoff(self) -> tuple[ReadinessManualAction, ...]:
        return self.manual_actions

    @property
    def report_digest(self) -> str:
        """Stable digest excluding timestamps and the digest itself."""

        payload = self.model_dump(mode="json", exclude={"context_snapshot", "observed_at"})
        return _canonical_digest(payload)


def _canonical_digest(value: Any) -> str:
    """Return the same canonical SHA-256 shape used by Sound Selection."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=False)
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _coerce_effect_coverage(value: object) -> EffectCoverageReport:
    """Adapt the semantic-processing report to the readiness report shape.

    The processing subsystem owns the richer adapter-aware effect contract.
    Readiness only needs a bounded summary, so accepting that report here
    avoids a second scan and keeps the two layers independently deployable.
    """

    if not isinstance(value, dict):
        raise TypeError("effects must be an effect coverage mapping or report")
    data: dict[str, Any] = dict(value)

    def loaded(item: object) -> LoadedProcessingCapability:
        row: dict[str, Any] = item if isinstance(item, dict) else {}
        return LoadedProcessingCapability(
            role_id=row.get("role_id"),
            track_index=(
                None
                if not isinstance(row.get("target"), dict)
                else row["target"].get("track_index")
            ),
            slot_index=(
                None
                if not isinstance(row.get("target"), dict)
                else row["target"].get("slot_index")
            ),
            product_id=row.get("product_id"),
            product_name=row.get("product_name") or row.get("plugin_name") or "Unknown effect",
            target_fingerprint=row.get("target_fingerprint"),
            atlas_match=bool(row.get("atlas_match", False)),
            atlas_product_id=row.get("atlas_product_id"),
            adapter_id=row.get("adapter_id"),
            semantic_controls=tuple(row.get("controls", row.get("semantic_controls", ())) or ()),
            supported_techniques=tuple(row.get("supported_techniques", ()) or ()),
            unresolved_controls=tuple(row.get("unresolved_controls", ()) or ()),
            controllable=bool(row.get("adapter_available", row.get("controllable", False))),
        )

    def missing(item: object) -> MissingProcessingCapability:
        row: dict[str, Any] = item if isinstance(item, dict) else {}
        return MissingProcessingCapability(
            role_id=row.get("role_id") or row.get("role"),
            category=row.get("category") or "requested_effect",
            reason=row.get("reason") or "No compatible loaded effect was observed.",
            required_for_completion=bool(
                row.get("required_for_completion", row.get("required", False))
            ),
            classification=(
                "blocking"
                if row.get("classification") == "blocking"
                else "limitation"
            ),
        )

    def role(item: object) -> RoleEffectCoverage:
        row: dict[str, Any] = item if isinstance(item, dict) else {}
        raw_state = row.get("state", "not_applicable")
        state = {
            "unresolved_effect": "loaded_but_unresolved",
            "partially_covered": "loaded_but_unresolved",
        }.get(raw_state, raw_state)
        if state not in {
            "effect_covered",
            "dry_by_design",
            "missing_requested_effect",
            "loaded_but_unresolved",
            "loaded_and_controllable",
            "not_applicable",
        }:
            state = "not_applicable"
        state = cast(EffectCoverageState, state)
        return RoleEffectCoverage(
            role_id=row.get("role_id") or row.get("role") or "project",
            loaded_effects=tuple(loaded(item) for item in row.get("loaded_effects", ()) or ()),
            requested_techniques=tuple(row.get("requested_techniques", ()) or ()),
            supported_techniques=tuple(row.get("supported_techniques", ()) or ()),
            unresolved_controls=tuple(row.get("unresolved_controls", ()) or ()),
            missing_capabilities=tuple(
                missing(item) for item in row.get("missing_capabilities", ()) or ()
            ),
            state=state,
            dry_playback_allowed=bool(
                row.get("dry_playback_allowed", row.get("dry_playback_intentional", True))
            ),
            processing_required_for_completion=bool(
                row.get(
                    "processing_required_for_completion",
                    row.get("processing_required", False),
                )
            ),
            warning=(
                None
                if not row.get("limitations")
                else "; ".join(str(item) for item in row["limitations"])
            ),
        )

    loaded_rows = tuple(loaded(item) for item in data.get("loaded_capabilities", ()) or ())
    missing_rows = tuple(missing(item) for item in data.get("missing_capabilities", ()) or ())
    role_rows = tuple(role(item) for item in data.get("roles", ()) or ())
    raw_state_value = data.get("state")
    raw_state = raw_state_value if isinstance(raw_state_value, str) else None
    normalized_state = (
        None
        if raw_state is None
        else {
            "unresolved_effect": "loaded_but_unresolved",
            "partially_covered": "loaded_but_unresolved",
        }.get(raw_state, raw_state)
    )
    if normalized_state not in {
        "effect_covered",
        "dry_by_design",
        "missing_requested_effect",
        "loaded_but_unresolved",
        "loaded_and_controllable",
        "not_applicable",
        None,
    }:
        normalized_state = None
    else:
        normalized_state = cast(EffectCoverageState, normalized_state)
    target = data.get("completion_target", "playable_draft")
    if target not in {
        "composition_only",
        "playable_draft",
        "first_pass_production",
        "restrained_first_pass",
        "mix_ready",
        "polished_mix_ready",
        "custom",
    }:
        target = "custom"
    requested = tuple(
        dict.fromkeys(
            str(item)
            for row in role_rows
            for item in row.requested_techniques
        )
    )
    return EffectCoverageReport(
        roles=role_rows,
        requested_categories=tuple(data.get("requested_categories", requested) or requested),
        missing_capabilities=missing_rows,
        loaded_capabilities=loaded_rows,
        completion_target=target,
        processing_required_for_completion=bool(
            data.get(
                "processing_required_for_completion",
                data.get("required_processing_missing", False),
            )
        ),
        can_produce_dry_draft=True,
        dry_by_design=normalized_state == "dry_by_design",
        state=normalized_state,
        processing_state=data.get("processing_state"),
        required_processing_missing=bool(data.get("required_processing_missing", False)),
        warnings=tuple(data.get("warnings", ()) or ()),
    )


__all__ = [
    "CompletionTarget",
    "ConnectionReadiness",
    "CreationPipelineModel",
    "CreationReadinessInput",
    "CreationReadinessReport",
    "CREATION_PIPELINE_SCHEMA_VERSION",
    "DrumCoverage",
    "EffectCoverageReport",
    "EffectCoverageState",
    "InstrumentPoolCoverage",
    "InstrumentTargetCoverage",
    "LoadedProcessingCapability",
    "MissingProcessingCapability",
    "PatternCoverage",
    "PatternIdentity",
    "PianoRollReadiness",
    "ReadinessBlocker",
    "ReadinessDimension",
    "ReadinessDimensionName",
    "ReadinessEvidence",
    "ReadinessLimitation",
    "ReadinessManualAction",
    "ReadinessState",
    "RoleEffectCoverage",
    "ScopeReadiness",
    "_canonical_digest",
]
