"""Immutable contracts for Sound Selection.

The Sound Selection package deliberately stops at planning data.  It does not
call FL Studio, discover files, or infer that an Atlas product is installed.
Runtime adapters can turn the records here into read-only observations or
verified mutations while preserving the same explicit Track B plug-in target
union.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..plugin_atlas.models import ProductKnowledge
from ..track_b_contracts import PluginTarget


SOUND_SELECTION_SCHEMA_VERSION = "1.0"
SOUND_SELECTION_DIGEST_ALGORITHM = "sha256-canonical-json-v1"
SHA256_PATTERN = r"^[0-9a-f]{64}$"

MAX_SOUND_ID = 128
MAX_SOUND_NAME = 256
MAX_SOUND_TEXT = 4096
MAX_ROLE_COUNT = 128
MAX_CANDIDATES = 512
MAX_PRESETS = 4096
MAX_REPORTED_PRESET_COUNT = 1_000_000
MAX_DESCRIPTORS = 64
MAX_HISTORY_STYLE_TAGS = 32
MAX_DRUM_PADS = 512
MAX_DRUM_ROLES = 32

SoundId = Annotated[
    str,
    Field(min_length=1, max_length=MAX_SOUND_ID, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
]
SoundText = Annotated[str, Field(min_length=1, max_length=MAX_SOUND_TEXT)]
ShortSoundText = Annotated[str, Field(min_length=1, max_length=MAX_SOUND_NAME)]
Digest = Annotated[str, Field(pattern=SHA256_PATTERN)]
RoleIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$"),
]
DescriptorIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$"),
]

SelectionPolicyMode = Literal["consistent", "balanced", "exploratory", "custom"]
SourceStrategy = Literal[
    "instrument_pool",
    "loop_starter",
    "existing_palette",
    "mixed",
]
PresetDescriptorProvenance = Literal[
    "user_explicit",
    "bundled_reviewed",
    "user_local_reviewed",
    "explicit_feedback",
    "atlas_product",
    "preset_name_token",
    "unknown",
]
HistoryVerdict = Literal["accepted", "rejected", "neutral"]

# A drum role must be safe to plan even when the caller does not know which
# pattern style will be rendered later.  These are the minimum roles used by
# the built-in pattern writer; optional roles (clap, open hat, crash, etc.)
# remain available through an explicit request or a style-specific validator.
_DRUM_ROLE_TYPES = frozenset(
    {"drums", "drum", "drum_kit", "kit", "percussion"}
)
DEFAULT_REQUIRED_DRUM_ROLES: tuple[str, ...] = (
    "kick",
    "snare",
    "closed_hat",
)


def _freeze_json_arrays(value: object) -> object:
    """Convert input arrays to tuples before Pydantic constructs a model.

    A frozen Pydantic model only protects attribute assignment.  Converting
    arrays at the boundary also prevents callers from mutating nested lists
    after validation.  Sound Selection models intentionally avoid mutable
    mapping fields; values that are mappings are copied recursively for
    validators and are represented by typed models before they leave here.
    """

    if isinstance(value, list):
        return tuple(_freeze_json_arrays(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_json_arrays(item) for item in value)
    if isinstance(value, dict):
        return {key: _freeze_json_arrays(item) for key, item in value.items()}
    return value


class SoundSelectionModel(BaseModel):
    """Strict, frozen, JSON-facing base for the Sound Selection contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _freeze_arrays(cls, value: object) -> object:
        return _freeze_json_arrays(value)

    @model_validator(mode="after")
    def _validate_tuple_text_bounds(self) -> "SoundSelectionModel":
        """Keep every public tuple-of-text field bounded per element.

        ``Field(max_length=...)`` on a tuple limits its item count, not the
        size of each string.  Most specialized fields add a tighter semantic
        bound through their annotated aliases; this common guard closes the
        remaining safety gap for free-form tuple text such as warnings,
        aliases, and style tags.
        """

        for field_name in type(self).model_fields:
            value = getattr(self, field_name, None)
            if not isinstance(value, tuple):
                continue
            for index, item in enumerate(value):
                if isinstance(item, str) and len(item) > MAX_SOUND_TEXT:
                    raise ValueError(
                        f"{field_name}[{index}] exceeds the {MAX_SOUND_TEXT}-character text bound"
                    )
        return self


class SoundCreativeDirection(SoundSelectionModel):
    """Structured creative hints supplied by the connected AI."""

    genre: str | None = Field(default=None, min_length=1, max_length=128)
    mood: tuple[str, ...] = Field(default=(), max_length=16)
    references: tuple[str, ...] = Field(default=(), max_length=8)
    style: tuple[str, ...] = Field(default=(), max_length=16)
    energy: str | None = Field(default=None, min_length=1, max_length=256)
    production_notes: str | None = Field(default=None, max_length=MAX_SOUND_TEXT)
    @model_validator(mode="after")
    def validate_text_lists(self) -> "SoundCreativeDirection":
        for label, values, maximum in (
            ("mood", self.mood, 128),
            ("references", self.references, 256),
            ("style", self.style, 128),
        ):
            if any(not item.strip() or len(item) > maximum for item in values):
                raise ValueError(f"creative_direction.{label} contains invalid text")
        return self


class SoundSelectionPolicy(SoundSelectionModel):
    """Bounded ranking policy; policy knobs never bypass hard constraints."""

    mode: SelectionPolicyMode = "balanced"
    continuity_weight: float = Field(default=0.80, ge=0.0, le=4.0)
    novelty_weight: float = Field(default=0.30, ge=0.0, le=4.0)
    palette_cohesion_weight: float = Field(default=0.70, ge=0.0, le=4.0)
    role_fit_weight: float = Field(default=1.00, ge=0.0, le=4.0)
    user_direction_weight: float = Field(default=2.50, ge=0.0, le=8.0)
    verification_weight: float = Field(default=0.35, ge=0.0, le=2.0)
    recent_use_window: int = Field(default=5, ge=1, le=64)
    exact_preset_repeat_penalty: float = Field(default=0.40, ge=0.0, le=2.0)
    same_product_repeat_penalty: float = Field(default=0.18, ge=0.0, le=2.0)
    rejected_choice_penalty: float = Field(default=0.75, ge=0.0, le=4.0)
    accepted_choice_bonus: float = Field(default=0.30, ge=0.0, le=2.0)

    @model_validator(mode="after")
    def validate_mode(self) -> "SoundSelectionPolicy":
        if self.mode == "consistent":
            # Consistent mode is still a ranking policy; these caps preserve a
            # strong role/user fit while making continuity the clear default.
            if self.novelty_weight > 2.0:
                raise ValueError("consistent policy cannot use novelty_weight above 2")
        elif self.mode == "exploratory" and self.novelty_weight == 0.0:
            raise ValueError("exploratory policy needs a positive novelty_weight")
        return self

    def effective_weights(self) -> tuple[float, float]:
        """Return mode-adjusted continuity and novelty weights."""

        if self.mode == "consistent":
            return self.continuity_weight * 1.5, self.novelty_weight * 0.25
        if self.mode == "exploratory":
            return self.continuity_weight * 0.60, self.novelty_weight * 1.50
        return self.continuity_weight, self.novelty_weight


RoleType = Annotated[str, Field(min_length=1, max_length=64)]
Register = Literal["low", "low_mid", "mid", "mid_high", "high", "full", "unknown"]


class SoundRoleRequest(SoundSelectionModel):
    """One requested musical function, including bounded custom roles."""

    role_id: RoleIdentifier
    display_name: str | None = Field(default=None, max_length=128)
    role_type: RoleType = "custom"
    section_scope: tuple[str, ...] = Field(default=(), max_length=32)
    required: bool = True
    target_candidates: tuple[PluginTarget, ...] = Field(default=(), max_length=64)
    preferred_products: tuple[str, ...] = Field(default=(), max_length=32)
    excluded_products: tuple[str, ...] = Field(default=(), max_length=32)
    preferred_presets: tuple[str, ...] = Field(default=(), max_length=32)
    excluded_presets: tuple[str, ...] = Field(default=(), max_length=32)
    desired_descriptors: tuple[DescriptorIdentifier, ...] = Field(
        default=(), max_length=MAX_DESCRIPTORS
    )
    undesired_descriptors: tuple[DescriptorIdentifier, ...] = Field(
        default=(), max_length=MAX_DESCRIPTORS
    )
    # ``register`` is reserved by Pydantic's BaseModel API.  Store it under a
    # private-looking field while retaining the public JSON/attribute contract.
    register_: Register | None = Field(
        default=None, validation_alias="register", serialization_alias="register"
    )
    articulation: str | None = Field(default=None, max_length=128)
    brightness: float | None = Field(default=None, ge=0.0, le=1.0)
    width: float | None = Field(default=None, ge=0.0, le=1.0)
    motion: float | None = Field(default=None, ge=0.0, le=1.0)
    aggression: float | None = Field(default=None, ge=0.0, le=1.0)
    softness: float | None = Field(default=None, ge=0.0, le=1.0)
    density: float | None = Field(default=None, ge=0.0, le=1.0)
    complexity: float | None = Field(default=None, ge=0.0, le=1.0)
    energy: float | None = Field(default=None, ge=0.0, le=1.0)
    source_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=16)
    technique_ids: tuple[str, ...] = Field(default=(), max_length=32)
    lock_existing: bool = False
    allow_layering: bool = False
    allow_section_variation: bool = True
    continuity_priority: float = Field(default=0.60, ge=0.0, le=1.0)
    novelty_priority: float = Field(default=0.35, ge=0.0, le=1.0)
    required_drum_roles: tuple[str, ...] = Field(default=(), max_length=MAX_DRUM_ROLES)

    @model_validator(mode="before")
    @classmethod
    def default_drum_roles(cls, value: object) -> object:
        """Require the minimum semantic map for every drum-role request.

        This is deliberately a ``before`` validator.  Returning a copied
        frozen model from an ``after`` validator is not supported by every
        Pydantic construction path (notably ``__init__``), which could leave a
        directly-created drum request with no required map roles.
        """

        if not isinstance(value, dict):
            return value
        data = dict(value)
        raw_role_type = data.get("role_type", "custom")
        raw_role_id = data.get("role_id", "")
        role_type = (
            raw_role_type.casefold().replace("-", "_").replace(" ", "_")
            if isinstance(raw_role_type, str)
            else ""
        )
        role_id = (
            raw_role_id.casefold().replace("-", "_")
            if isinstance(raw_role_id, str)
            else ""
        )
        is_drum_role = role_type in _DRUM_ROLE_TYPES or (
            role_type == "custom" and role_id in _DRUM_ROLE_TYPES
        )
        if is_drum_role and not data.get("required_drum_roles"):
            data["required_drum_roles"] = DEFAULT_REQUIRED_DRUM_ROLES
        return data

    @model_validator(mode="after")
    def validate_role_lists(self) -> "SoundRoleRequest":
        for label, values in (
            ("section_scope", self.section_scope),
            ("preferred_products", self.preferred_products),
            ("excluded_products", self.excluded_products),
            ("preferred_presets", self.preferred_presets),
            ("excluded_presets", self.excluded_presets),
            ("source_roles", self.source_roles),
            ("technique_ids", self.technique_ids),
            ("required_drum_roles", self.required_drum_roles),
        ):
            if any(not item.strip() for item in values):
                raise ValueError(f"{label} entries must contain non-empty text")
            if len(set(item.casefold() for item in values)) != len(values):
                raise ValueError(f"{label} entries must not contain duplicates")
        if set(item.casefold() for item in self.desired_descriptors).intersection(
            item.casefold() for item in self.undesired_descriptors
        ):
            raise ValueError("desired and undesired descriptors cannot overlap")
        return self

    @property
    def register(self) -> Register | None:
        return self.register_


class SoundSelectionRequest(SoundSelectionModel):
    """A structured sound-choice request interpreted by the connected AI."""

    schema_version: Literal["1.0"] = SOUND_SELECTION_SCHEMA_VERSION
    brief: SoundText
    creative_direction: SoundCreativeDirection | str | None = None
    roles: tuple[SoundRoleRequest, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    source_strategy: SourceStrategy = "instrument_pool"
    selection_policy: SoundSelectionPolicy = Field(default_factory=SoundSelectionPolicy)
    product_preferences: tuple[str, ...] = Field(default=(), max_length=64)
    product_exclusions: tuple[str, ...] = Field(default=(), max_length=64)
    preset_preferences: tuple[str, ...] = Field(default=(), max_length=64)
    preset_exclusions: tuple[str, ...] = Field(default=(), max_length=64)
    stock_only: bool = False
    third_party_allowed: bool = True
    preserve_existing_roles: bool = True
    allow_effect_presets: bool = False
    allow_drum_kit_change: bool = True
    seed: int = Field(default=0, ge=-(2**31), le=2**31 - 1)
    persist_history: bool = True
    project_key: str | None = Field(default=None, max_length=256)
    current_palette_id: str | None = Field(default=None, max_length=MAX_SOUND_ID)
    max_candidates_per_role: int = Field(default=8, ge=1, le=64)

    @model_validator(mode="after")
    def validate_request_lists(self) -> "SoundSelectionRequest":
        for label, values in (
            ("product_preferences", self.product_preferences),
            ("product_exclusions", self.product_exclusions),
            ("preset_preferences", self.preset_preferences),
            ("preset_exclusions", self.preset_exclusions),
        ):
            if any(not item.strip() for item in values):
                raise ValueError(f"{label} entries must contain non-empty text")
            if len(set(item.casefold() for item in values)) != len(values):
                raise ValueError(f"{label} entries must not contain duplicates")
        role_ids = [role.role_id.casefold() for role in self.roles]
        if len(set(role_ids)) != len(role_ids):
            raise ValueError("roles must not contain duplicate role_id values")
        if not self.third_party_allowed and self.stock_only is False:
            # ``third_party_allowed=false`` is an exclusion policy even when
            # callers do not use the shorter stock_only spelling.
            return self
        return self


class DescriptorEvidence(SoundSelectionModel):
    """One descriptor with source and confidence, never an audio claim."""

    descriptor: DescriptorIdentifier
    provenance: PresetDescriptorProvenance = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_id: str | None = Field(default=None, max_length=MAX_SOUND_ID)
    detail: str | None = Field(default=None, max_length=512)


class SoundScoreBreakdown(SoundSelectionModel):
    """Inspectable score components.  Values are metadata-level evidence."""

    hard_constraints: float = 0.0
    user_direction: float = 0.0
    role_fit: float = 0.0
    palette_cohesion: float = 0.0
    continuity: float = 0.0
    cross_project_diversity: float = 0.0
    feedback: float = 0.0
    verification: float = 0.0
    total: float = 0.0

    @property
    def component_total(self) -> float:
        return round(
            self.hard_constraints
            + self.user_direction
            + self.role_fit
            + self.palette_cohesion
            + self.continuity
            + self.cross_project_diversity
            + self.feedback
            + self.verification,
            8,
        )

    @property
    def totals_consistent(self) -> bool:
        return abs(self.component_total - self.total) <= 1e-7


class SoundCandidate(SoundSelectionModel):
    """One loaded target/preset candidate presented to the deterministic scorer."""

    candidate_id: SoundId | None = None
    target: PluginTarget | None = None
    target_fingerprint: Digest | None = None
    product_id: str | None = Field(default=None, max_length=MAX_SOUND_ID)
    product_name: ShortSoundText
    product_aliases: tuple[str, ...] = Field(default=(), max_length=32)
    product_origin: Literal["stock", "third_party", "unknown"] = "unknown"
    current_preset: str | None = Field(default=None, max_length=MAX_SOUND_NAME)
    candidate_preset: str | None = Field(default=None, max_length=MAX_SOUND_NAME)
    preset_index: int | None = Field(default=None, ge=0, le=MAX_REPORTED_PRESET_COUNT - 1)
    preset_identity_digest: Digest | None = None
    descriptors: tuple[DescriptorIdentifier, ...] = Field(
        default=(), max_length=MAX_DESCRIPTORS
    )
    descriptor_provenance: tuple[DescriptorEvidence, ...] = Field(
        default=(), max_length=MAX_DESCRIPTORS * 2
    )
    style_tags: tuple[str, ...] = Field(default=(), max_length=MAX_HISTORY_STYLE_TAGS)
    role_ids: tuple[str, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    registers: tuple[Register, ...] = Field(default=(), max_length=8)
    articulations: tuple[str, ...] = Field(default=(), max_length=32)
    atlas_product: ProductKnowledge | None = None
    atlas_product_id: str | None = Field(default=None, max_length=MAX_SOUND_ID)
    atlas_categories: tuple[str, ...] = Field(default=(), max_length=32)
    atlas_common_roles: tuple[str, ...] = Field(default=(), max_length=32)
    atlas_technique_ids: tuple[str, ...] = Field(default=(), max_length=32)
    control_adapter_id: str | None = Field(default=None, max_length=MAX_SOUND_ID)
    atlas_confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    adapter_available: bool = False
    preset_navigation_available: bool = False
    preset_readback_available: bool = False
    preset_identity_stable: bool = False
    pad_map_available: bool = False
    source_strategy: SourceStrategy = "instrument_pool"
    section_scope: tuple[str, ...] = Field(default=(), max_length=32)
    drum_map_id: str | None = Field(default=None, max_length=MAX_SOUND_ID)
    drum_missing_roles: tuple[str, ...] = Field(default=(), max_length=MAX_DRUM_ROLES)
    role_compatibility: float = Field(default=0.0, ge=0.0, le=1.0)
    brightness: float | None = Field(default=None, ge=0.0, le=1.0)
    width: float | None = Field(default=None, ge=0.0, le=1.0)
    motion: float | None = Field(default=None, ge=0.0, le=1.0)
    aggression: float | None = Field(default=None, ge=0.0, le=1.0)
    softness: float | None = Field(default=None, ge=0.0, le=1.0)
    density: float | None = Field(default=None, ge=0.0, le=1.0)
    complexity: float | None = Field(default=None, ge=0.0, le=1.0)
    energy: float | None = Field(default=None, ge=0.0, le=1.0)
    current_project_usage: int = Field(default=0, ge=0)
    cross_project_usage: int = Field(default=0, ge=0)
    accepted_count: int = Field(default=0, ge=0)
    rejected_count: int = Field(default=0, ge=0)
    score: float = 0.0
    score_breakdown: SoundScoreBreakdown = Field(default_factory=SoundScoreBreakdown)
    confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    warnings: tuple[str, ...] = Field(default=(), max_length=32)
    disqualification_reasons: tuple[str, ...] = Field(default=(), max_length=32)

    @property
    def selected_preset(self) -> str | None:
        return self.candidate_preset if self.candidate_preset is not None else self.current_preset

    @property
    def identity_digest(self) -> str:
        if self.preset_identity_digest is not None:
            return self.preset_identity_digest
        return preset_identity_digest(
            self.product_id or self.product_name,
            self.selected_preset,
            self.preset_index,
        )

    @property
    def target_kind(self) -> str | None:
        return None if self.target is None else self.target.kind

    @property
    def is_stock(self) -> bool:
        if self.product_origin == "stock":
            return True
        return self.atlas_product is not None and self.atlas_product.origin == "stock"

    @property
    def is_loop_starter(self) -> bool:
        return self.source_strategy == "loop_starter"

    @model_validator(mode="after")
    def validate_candidate_lists(self) -> "SoundCandidate":
        descriptor_keys = {
            item.casefold().replace("_", "-") for item in self.descriptors
        }
        if len(descriptor_keys) != len(self.descriptors):
            raise ValueError("candidate descriptors must not contain duplicates")
        return self


class DrumPad(SoundSelectionModel):
    """One generic plug-in pad observation."""

    pad_index: int = Field(ge=0, lt=MAX_DRUM_PADS)
    midi_note: int | None = Field(default=None, ge=0, le=127)
    color: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    empty: bool = False
    muted: bool = False
    semitone_name: str | None = Field(default=None, max_length=64)
    semantic_roles: tuple[str, ...] = Field(default=(), max_length=MAX_DRUM_ROLES)

    @model_validator(mode="after")
    def validate_pad_roles(self) -> "DrumPad":
        roles = [item.casefold() for item in self.semantic_roles]
        if len(set(roles)) != len(roles):
            raise ValueError("drum pad semantic roles must be unique")
        return self


DrumRole = Annotated[str, Field(min_length=1, max_length=64)]


class DrumRoleMapping(SoundSelectionModel):
    role: DrumRole
    pad_index: int = Field(ge=0, lt=MAX_DRUM_PADS)
    midi_note: int = Field(ge=0, le=127)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source: Literal["user_explicit", "reported_semitone", "reported_name", "unknown"] = "unknown"


class DrumPadMap(SoundSelectionModel):
    """A semantic drum map derived from plug-in-reported pad data."""

    map_id: SoundId | None = None
    target: PluginTarget | None = None
    pad_count: int = Field(ge=0, le=MAX_DRUM_PADS)
    pads: tuple[DrumPad, ...] = Field(default=(), max_length=MAX_DRUM_PADS)
    mappings: tuple[DrumRoleMapping, ...] = Field(default=(), max_length=MAX_DRUM_ROLES)
    missing_roles: tuple[str, ...] = Field(default=(), max_length=MAX_DRUM_ROLES)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_pad_map(self) -> "DrumPadMap":
        if self.pad_count != len(self.pads) and self.pads:
            raise ValueError("pad_count must equal the reported pad list length")
        pad_indices = {pad.pad_index for pad in self.pads}
        if len(pad_indices) != len(self.pads):
            raise ValueError("pad indices must be unique")
        mapping_roles = [mapping.role.casefold() for mapping in self.mappings]
        if len(set(mapping_roles)) != len(mapping_roles):
            raise ValueError("drum semantic roles must map at most once")
        missing_roles = [item.casefold() for item in self.missing_roles]
        if len(set(missing_roles)) != len(missing_roles):
            raise ValueError("missing drum roles must be unique")
        return self

    @property
    def mapped_notes(self) -> tuple[tuple[str, int], ...]:
        return tuple((mapping.role, mapping.midi_note) for mapping in self.mappings)

    def missing_required(self, roles: tuple[str, ...]) -> tuple[str, ...]:
        mapped = {item.role.casefold() for item in self.mappings}
        return tuple(role for role in roles if role.casefold() not in mapped)


class DrumKitCandidate(SoundSelectionModel):
    candidate_id: SoundId | None = None
    target: PluginTarget | None = None
    product_id: str | None = Field(default=None, max_length=MAX_SOUND_ID)
    product_name: ShortSoundText
    preset: str | None = Field(default=None, max_length=MAX_SOUND_NAME)
    preset_index: int | None = Field(default=None, ge=0, lt=MAX_REPORTED_PRESET_COUNT)
    drum_map: DrumPadMap | None = None
    required_roles: tuple[str, ...] = Field(default=(), max_length=MAX_DRUM_ROLES)
    missing_roles: tuple[str, ...] = Field(default=(), max_length=MAX_DRUM_ROLES)
    descriptors: tuple[DescriptorIdentifier, ...] = Field(default=(), max_length=MAX_DESCRIPTORS)
    score: float = 0.0
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: tuple[str, ...] = Field(default=(), max_length=32)


class SoundTargetInventory(SoundSelectionModel):
    """A loaded generator/effect and bounded preset/pad observations."""

    target: PluginTarget
    target_fingerprint: Digest | None = None
    product_id: str | None = Field(default=None, max_length=MAX_SOUND_ID)
    product_name: ShortSoundText
    product_aliases: tuple[str, ...] = Field(default=(), max_length=32)
    product_origin: Literal["stock", "third_party", "unknown"] = "unknown"
    current_preset: str | None = Field(default=None, max_length=MAX_SOUND_NAME)
    current_preset_index: int | None = Field(default=None, ge=0, lt=MAX_REPORTED_PRESET_COUNT)
    # FL may report a very large catalog.  ``preset_names`` remains bounded to
    # one page; this field preserves the reported count without enumerating it.
    preset_count: int | None = Field(default=None, ge=0, le=MAX_REPORTED_PRESET_COUNT)
    preset_names: tuple[str, ...] = Field(default=(), max_length=MAX_PRESETS)
    preset_indices: tuple[int, ...] = Field(default=(), max_length=MAX_PRESETS)
    descriptors: tuple[DescriptorIdentifier, ...] = Field(default=(), max_length=MAX_DESCRIPTORS)
    descriptor_provenance: tuple[DescriptorEvidence, ...] = Field(
        default=(), max_length=MAX_DESCRIPTORS * 2
    )
    style_tags: tuple[str, ...] = Field(default=(), max_length=MAX_HISTORY_STYLE_TAGS)
    role_ids: tuple[str, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    registers: tuple[Register, ...] = Field(default=(), max_length=8)
    articulations: tuple[str, ...] = Field(default=(), max_length=32)
    brightness: float | None = Field(default=None, ge=0.0, le=1.0)
    width: float | None = Field(default=None, ge=0.0, le=1.0)
    motion: float | None = Field(default=None, ge=0.0, le=1.0)
    aggression: float | None = Field(default=None, ge=0.0, le=1.0)
    softness: float | None = Field(default=None, ge=0.0, le=1.0)
    density: float | None = Field(default=None, ge=0.0, le=1.0)
    complexity: float | None = Field(default=None, ge=0.0, le=1.0)
    energy: float | None = Field(default=None, ge=0.0, le=1.0)
    atlas_product: ProductKnowledge | None = None
    atlas_product_id: str | None = Field(default=None, max_length=MAX_SOUND_ID)
    atlas_categories: tuple[str, ...] = Field(default=(), max_length=32)
    atlas_common_roles: tuple[str, ...] = Field(default=(), max_length=32)
    atlas_technique_ids: tuple[str, ...] = Field(default=(), max_length=32)
    control_adapter_id: str | None = Field(default=None, max_length=MAX_SOUND_ID)
    atlas_confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    adapter_available: bool = False
    preset_navigation_available: bool = False
    preset_readback_available: bool = False
    preset_identity_stable: bool = False
    pad_map: DrumPadMap | None = None
    source_strategy: SourceStrategy = "instrument_pool"
    section_scope: tuple[str, ...] = Field(default=(), max_length=32)
    warnings: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_inventory_page(self) -> "SoundTargetInventory":
        if len(set(self.preset_indices)) != len(self.preset_indices):
            raise ValueError("preset indices must be unique within an inventory page")
        if len(self.preset_indices) > len(self.preset_names):
            raise ValueError("preset indices cannot outnumber the observed preset names")
        if self.preset_names and self.preset_indices and len(self.preset_indices) != len(self.preset_names):
            raise ValueError("preset indices must align one-for-one with observed preset names")
        if len(set(name.casefold() for name in self.preset_names)) != len(self.preset_names):
            if len(self.preset_indices) != len(self.preset_names):
                raise ValueError("duplicate preset names require an index for exact identity")
        if self.preset_count is not None and self.preset_count < len(self.preset_names):
            raise ValueError("preset_count cannot be below the bounded preset page length")
        return self

    def candidates(self, *, max_presets: int = MAX_PRESETS) -> tuple[SoundCandidate, ...]:
        """Expand this target into a bounded deterministic candidate page."""

        if type(max_presets) is not int or not (1 <= max_presets <= MAX_PRESETS):
            raise ValueError("max_presets is outside Sound Selection bounds")
        # A blank catalog row is an observation about an unavailable/unnamed
        # slot, not an executable preset identity.  Filter it before creating
        # candidates, including when callers explicitly requested blank rows
        # from the live Track B page.  Keep the bound on executable candidates
        # rather than allowing leading blank rows to consume the whole page.
        observed_rows: list[tuple[str, int | None]] = []
        for position, name in enumerate(self.preset_names):
            if not isinstance(name, str) or not name.strip():
                continue
            index = (
                self.preset_indices[position]
                if position < len(self.preset_indices)
                else None
            )
            if (
                index is None
                and name == self.current_preset
                and self.current_preset_index is not None
            ):
                index = self.current_preset_index
            observed_rows.append((name, index))
            if len(observed_rows) >= max_presets:
                break
        if not observed_rows:
            current = self.current_preset
            if isinstance(current, str) and current.strip():
                observed_rows.append((current, self.current_preset_index))
        if not observed_rows:
            return ()
        descriptor_names = self.descriptors
        descriptor_evidence = self.descriptor_provenance
        classifier: Callable[[str], tuple[DescriptorEvidence, ...]] | None
        try:
            from .descriptors import classify_preset_name

            classifier = classify_preset_name
        except (ImportError, OSError, ValueError):
            # Descriptor metadata is a ranking aid.  A missing optional data
            # resource must not make a live runtime candidate disappear.
            classifier = None
        rows: list[SoundCandidate] = []
        for name, index in observed_rows:
            row_descriptors = descriptor_names
            row_evidence = descriptor_evidence
            if classifier is not None and name:
                inferred = classifier(name)
                row_evidence = tuple(
                    dict.fromkeys((*descriptor_evidence, *inferred))
                )
                row_descriptors = tuple(
                    sorted(set((*descriptor_names, *(item.descriptor for item in inferred))))
                )
            rows.append(
                SoundCandidate(
                    candidate_id=_candidate_id(self.target, self.product_id, name, index),
                    target=self.target,
                    target_fingerprint=self.target_fingerprint,
                    product_id=self.product_id,
                    product_name=self.product_name,
                    product_aliases=self.product_aliases,
                    product_origin=self.product_origin,
                    current_preset=self.current_preset,
                    candidate_preset=name,
                    preset_index=index,
                    preset_identity_digest=preset_identity_digest(
                        self.product_id or self.product_name, name, index
                    ),
                    descriptors=row_descriptors,
                    descriptor_provenance=row_evidence,
                    style_tags=self.style_tags,
                    role_ids=self.role_ids,
                    registers=self.registers,
                    articulations=self.articulations,
                    atlas_product=self.atlas_product,
                    atlas_product_id=self.atlas_product_id,
                    atlas_categories=self.atlas_categories,
                    atlas_common_roles=self.atlas_common_roles,
                    atlas_technique_ids=self.atlas_technique_ids,
                    control_adapter_id=self.control_adapter_id,
                    atlas_confidence=self.atlas_confidence,
                    adapter_available=self.adapter_available,
                    preset_navigation_available=self.preset_navigation_available,
                    preset_readback_available=self.preset_readback_available,
                    preset_identity_stable=self.preset_identity_stable,
                    pad_map_available=self.pad_map is not None,
                    source_strategy=self.source_strategy,
                    section_scope=self.section_scope,
                    drum_map_id=None if self.pad_map is None else self.pad_map.map_id,
                    drum_missing_roles=() if self.pad_map is None else self.pad_map.missing_roles,
                    brightness=self.brightness,
                    width=self.width,
                    motion=self.motion,
                    aggression=self.aggression,
                    softness=self.softness,
                    density=self.density,
                    complexity=self.complexity,
                    energy=self.energy,
                    warnings=self.warnings,
                )
            )
        return tuple(rows)


# Friendly aliases used by integration layers and callers that prefer shorter names.
LoadedSoundTarget = SoundTargetInventory
LoadedSound = SoundTargetInventory
SoundInventoryItem = SoundTargetInventory


class SoundInventory(SoundSelectionModel):
    """Compact loaded generator/effect inventory used by palette planning."""

    schema_version: Literal["1.0"] = SOUND_SELECTION_SCHEMA_VERSION
    observed_at: datetime | None = None
    session_fingerprint: str | None = Field(default=None, max_length=128)
    loaded_generators: tuple[SoundTargetInventory, ...] = Field(default=(), max_length=MAX_CANDIDATES)
    loaded_effects: tuple[SoundTargetInventory, ...] = Field(default=(), max_length=MAX_CANDIDATES)
    current_palette_id: str | None = Field(default=None, max_length=MAX_SOUND_ID)
    locked_roles: tuple[str, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    known_unloaded_products: tuple[str, ...] = Field(default=(), max_length=MAX_CANDIDATES)
    warnings: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_targets(self) -> "SoundInventory":
        if len(self.loaded_generators) + len(self.loaded_effects) > MAX_CANDIDATES:
            raise ValueError("loaded inventory target count exceeds the bounded maximum")
        targets = [target_identity_key(item.target) for item in self.loaded_targets]
        if len(set(targets)) != len(targets):
            raise ValueError("loaded inventory targets must be unique")
        return self

    @property
    def loaded_targets(self) -> tuple[SoundTargetInventory, ...]:
        return (*self.loaded_generators, *self.loaded_effects)

    def candidates(self, *, include_effects: bool = False, max_presets: int = MAX_PRESETS) -> tuple[SoundCandidate, ...]:
        targets = self.loaded_targets if include_effects else self.loaded_generators
        rows: list[SoundCandidate] = []
        for target in targets:
            rows.extend(target.candidates(max_presets=max_presets))
        return tuple(rows)


class SoundPaletteAssignment(SoundSelectionModel):
    """One role assignment in a planned palette."""

    role_id: RoleIdentifier
    target: PluginTarget | None = None
    target_fingerprint: Digest | None = None
    product_id: str | None = Field(default=None, max_length=MAX_SOUND_ID)
    product_name: str | None = Field(default=None, max_length=MAX_SOUND_NAME)
    selected_preset: str | None = Field(default=None, max_length=MAX_SOUND_NAME)
    selected_preset_index: int | None = Field(default=None, ge=0, lt=MAX_REPORTED_PRESET_COUNT)
    preset_identity_digest: Digest | None = None
    anchor: bool = False
    locked: bool = False
    section_scope: tuple[str, ...] = Field(default=(), max_length=32)
    parent_assignment_id: str | None = Field(default=None, max_length=MAX_SOUND_ID)
    selection_action: Literal["keep_current", "select_preset", "loop_starter_reroll"] = "select_preset"
    drum_map_id: str | None = Field(default=None, max_length=MAX_SOUND_ID)
    score: float = 0.0
    score_breakdown: SoundScoreBreakdown = Field(default_factory=SoundScoreBreakdown)
    selection_reason: str = Field(default="", max_length=1024)
    fallback_candidate_ids: tuple[str, ...] = Field(default=(), max_length=64)
    required_verification: bool = True
    confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    warnings: tuple[str, ...] = Field(default=(), max_length=32)

    @property
    def assignment_id(self) -> str:
        return "assignment-" + canonical_digest(
            {
                "role_id": self.role_id,
                "target": (
                    None
                    if self.target is None
                    else self.target.model_dump(mode="json", exclude_none=False)
                ),
                "product_id": self.product_id or self.product_name,
                "preset_name": self.selected_preset,
                "preset_index": self.selected_preset_index,
            }
        )[:24]


class SoundPaletteVariation(SoundSelectionModel):
    """A section-scoped assignment delta linked to a parent role assignment."""

    variation_id: SoundId
    section: str = Field(min_length=1, max_length=128)
    assignments: tuple[SoundPaletteAssignment, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    unchanged_role_ids: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    rationale: str = Field(default="", max_length=1024)
    blockers: tuple[str, ...] = Field(default=(), max_length=32)


class SoundPalettePlan(SoundSelectionModel):
    """Read-only output of deterministic palette planning."""

    schema_version: Literal["1.0"] = SOUND_SELECTION_SCHEMA_VERSION
    palette_id: SoundId
    request_digest: Digest
    inventory_session_fingerprint: str | None = Field(default=None, max_length=128)
    project_key: str | None = Field(default=None, max_length=256)
    policy: SoundSelectionPolicy
    assignments: tuple[SoundPaletteAssignment, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    anchor_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    flexible_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    section_variations: tuple[SoundPaletteVariation, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    drum_map: DrumPadMap | None = None
    unused_candidate_ids: tuple[str, ...] = Field(default=(), max_length=MAX_CANDIDATES)
    unused_candidate_targets: tuple[PluginTarget, ...] = Field(default=(), max_length=MAX_CANDIDATES)
    conflicts: tuple[str, ...] = Field(default=(), max_length=64)
    blockers: tuple[str, ...] = Field(default=(), max_length=64)
    warnings: tuple[str, ...] = Field(default=(), max_length=64)
    rationale: str = Field(default="", max_length=MAX_SOUND_TEXT)
    plan_digest: Digest | None = None
    mutations_applied: Literal[False] = False

    @model_validator(mode="after")
    def validate_assignments(self) -> "SoundPalettePlan":
        role_ids = [item.role_id.casefold() for item in self.assignments]
        if len(set(role_ids)) != len(role_ids):
            raise ValueError("palette assignments must contain unique role IDs")
        if set(item.casefold() for item in self.anchor_roles).intersection(
            item.casefold() for item in self.flexible_roles
        ):
            raise ValueError("anchor and flexible role lists cannot overlap")
        if len(set(item.casefold() for item in self.anchor_roles)) != len(self.anchor_roles):
            raise ValueError("anchor_roles must not contain duplicates")
        if len(set(item.casefold() for item in self.flexible_roles)) != len(self.flexible_roles):
            raise ValueError("flexible_roles must not contain duplicates")
        return self


class SoundPaletteVariationPlan(SoundSelectionModel):
    """Read-only delta plan for a section variation of an existing palette."""

    schema_version: Literal["1.0"] = SOUND_SELECTION_SCHEMA_VERSION
    variation_id: SoundId
    base_palette_id: SoundId
    request_digest: Digest
    section: str = Field(min_length=1, max_length=128)
    preserve_anchor_roles: bool = True
    assignments: tuple[SoundPaletteAssignment, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    unchanged_role_ids: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    conflicts: tuple[str, ...] = Field(default=(), max_length=64)
    blockers: tuple[str, ...] = Field(default=(), max_length=64)
    warnings: tuple[str, ...] = Field(default=(), max_length=64)
    rationale: str = Field(default="", max_length=MAX_SOUND_TEXT)
    plan_digest: Digest | None = None

    @model_validator(mode="after")
    def validate_variation_assignments(self) -> "SoundPaletteVariationPlan":
        role_ids = [item.role_id.casefold() for item in self.assignments]
        if len(set(role_ids)) != len(role_ids):
            raise ValueError("variation assignments must contain unique role IDs")
        if set(item.casefold() for item in self.unchanged_role_ids).intersection(role_ids):
            raise ValueError("a variation role cannot be both changed and unchanged")
        if len(set(item.casefold() for item in self.unchanged_role_ids)) != len(self.unchanged_role_ids):
            raise ValueError("unchanged_role_ids must not contain duplicates")
        return self


class PaletteApplyReceipt(SoundSelectionModel):
    """Truthful per-assignment result supplied by a verified execution layer."""

    assignment_id: str = Field(min_length=1, max_length=MAX_SOUND_ID)
    role_id: RoleIdentifier
    verified: bool
    verification_summary: str = Field(min_length=1, max_length=1024)
    selected_preset: str | None = Field(default=None, max_length=MAX_SOUND_NAME)
    warnings: tuple[str, ...] = Field(default=(), max_length=32)


class SoundPaletteState(SoundSelectionModel):
    """Immutable snapshot of a palette and its verified application receipts."""

    schema_version: Literal["1.0"] = SOUND_SELECTION_SCHEMA_VERSION
    palette_id: SoundId
    status: Literal["planned", "applying", "applied", "partially_applied", "failed", "superseded"] = "planned"
    created_at: datetime
    updated_at: datetime
    project_key: str | None = Field(default=None, max_length=256)
    session_identity: str | None = Field(default=None, max_length=128)
    assignments: tuple[SoundPaletteAssignment, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    locked_assignments: tuple[str, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    section_variations: tuple[SoundPaletteVariation, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    apply_receipts: tuple[PaletteApplyReceipt, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    accepted_feedback: tuple[str, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    rejected_feedback: tuple[str, ...] = Field(default=(), max_length=MAX_ROLE_COUNT)
    warnings: tuple[str, ...] = Field(default=(), max_length=64)
    blockers: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_state_assignments(self) -> "SoundPaletteState":
        role_ids = [item.role_id.casefold() for item in self.assignments]
        if len(set(role_ids)) != len(role_ids):
            raise ValueError("palette state assignments must contain unique role IDs")
        if self.updated_at < self.created_at:
            raise ValueError("palette state updated_at cannot be before created_at")
        if len(set(self.locked_assignments)) != len(self.locked_assignments):
            raise ValueError("locked assignment IDs must be unique")
        receipt_ids = [item.assignment_id for item in self.apply_receipts]
        if len(set(receipt_ids)) != len(receipt_ids):
            raise ValueError("palette apply receipts must be unique per assignment")
        if len(set(self.accepted_feedback)) != len(self.accepted_feedback):
            raise ValueError("accepted feedback IDs must be unique")
        if len(set(self.rejected_feedback)) != len(self.rejected_feedback):
            raise ValueError("rejected feedback IDs must be unique")
        return self


class SoundFeedbackRequest(SoundSelectionModel):
    """Explicit user feedback; silence is never inferred as acceptance."""

    palette_id: SoundId
    role_id: RoleIdentifier | None = None
    verdict: HistoryVerdict
    descriptors: tuple[DescriptorIdentifier, ...] = Field(default=(), max_length=MAX_DESCRIPTORS)
    note: str | None = Field(default=None, max_length=512)
    persist: bool = True

    @model_validator(mode="after")
    def validate_feedback(self) -> "SoundFeedbackRequest":
        descriptors = [item.casefold() for item in self.descriptors]
        if len(set(descriptors)) != len(descriptors):
            raise ValueError("feedback descriptors must not contain duplicates")
        if self.note is not None and not self.note.strip():
            raise ValueError("feedback note must contain text when supplied")
        return self


class SoundScoreResult(SoundSelectionModel):
    """Scored candidate plus hard-constraint outcome."""

    candidate: SoundCandidate
    role_id: RoleIdentifier
    eligible: bool
    score: float
    breakdown: SoundScoreBreakdown
    rationale: str = Field(default="", max_length=1024)
    disqualification_reasons: tuple[str, ...] = Field(default=(), max_length=32)


def canonical_digest(value: Any) -> str:
    """Return the repository's canonical SHA-256 JSON digest for a value."""

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


def preset_identity_digest(
    product_id: str,
    preset_name: str | None,
    preset_index: int | None = None,
) -> str:
    """Digest a preset identity without storing raw project or prompt data."""

    return canonical_digest(
        {
            "algorithm": SOUND_SELECTION_DIGEST_ALGORITHM,
            "product_id": product_id,
            "preset_index": preset_index,
            "preset_name": preset_name,
        }
    )


def target_identity_key(target: PluginTarget | None) -> str:
    """Return an explicit stable key for an existing Track B target."""

    if target is None:
        return "unresolved"
    return canonical_digest(target.model_dump(mode="json", exclude_none=False))


def _candidate_id(
    target: PluginTarget | None,
    product_id: str | None,
    preset_name: str | None,
    preset_index: int | None,
) -> str:
    digest = canonical_digest(
        {
            "target": None if target is None else target.model_dump(mode="json"),
            "product_id": product_id,
            "preset_name": preset_name,
            "preset_index": preset_index,
        }
    )
    return f"candidate-{digest[:24]}"


# Public vocabulary of role names.  Custom IDs remain valid through the
# bounded RoleIdentifier type.
DEFAULT_ROLE_IDS: tuple[str, ...] = (
    "main_chords",
    "main_lead",
    "primary_bass",
    "sub_bass",
    "vocal_chop",
    "drums",
    "texture",
    "countermelody",
    "fx",
)

DEFAULT_DRUM_ROLES: tuple[str, ...] = (
    "kick",
    "snare",
    "clap",
    "closed_hat",
    "open_hat",
    "crash",
    "ride",
    "tom",
    "percussion",
)


__all__ = [
    "DEFAULT_REQUIRED_DRUM_ROLES",
    "DEFAULT_DRUM_ROLES",
    "DEFAULT_ROLE_IDS",
    "MAX_CANDIDATES",
    "MAX_DESCRIPTORS",
    "MAX_DRUM_PADS",
    "MAX_DRUM_ROLES",
    "MAX_HISTORY_STYLE_TAGS",
    "MAX_PRESETS",
    "MAX_REPORTED_PRESET_COUNT",
    "MAX_ROLE_COUNT",
    "MAX_SOUND_ID",
    "MAX_SOUND_NAME",
    "MAX_SOUND_TEXT",
    "DescriptorEvidence",
    "DescriptorIdentifier",
    "Digest",
    "DrumKitCandidate",
    "DrumPad",
    "DrumPadMap",
    "DrumRole",
    "DrumRoleMapping",
    "HistoryVerdict",
    "LoadedSound",
    "LoadedSoundTarget",
    "PaletteApplyReceipt",
    "PresetDescriptorProvenance",
    "Register",
    "RoleIdentifier",
    "RoleType",
    "SelectionPolicyMode",
    "ShortSoundText",
    "SOUND_SELECTION_DIGEST_ALGORITHM",
    "SOUND_SELECTION_SCHEMA_VERSION",
    "SoundCandidate",
    "SoundCreativeDirection",
    "SoundFeedbackRequest",
    "SoundInventory",
    "SoundInventoryItem",
    "SoundPaletteAssignment",
    "SoundPalettePlan",
    "SoundPaletteState",
    "SoundPaletteVariation",
    "SoundPaletteVariationPlan",
    "SoundScoreBreakdown",
    "SoundScoreResult",
    "SoundSelectionModel",
    "SoundSelectionPolicy",
    "SoundSelectionRequest",
    "SoundRoleRequest",
    "SoundTargetInventory",
    "SourceStrategy",
    "canonical_digest",
    "preset_identity_digest",
    "target_identity_key",
]
