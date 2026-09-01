"""Truthful, provenance-carrying descriptions of selected sounds.

The creation pipeline may know useful things about a selected patch without
having heard it.  These contracts keep that distinction explicit: every value
has a confidence and provenance, and preset-name inference is prevented from
claiming high confidence.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from ..sound_selection.models import (
    CandidateMetadataProvenance,
    ConfidenceLevel,
    DescriptorEvidence,
    PresetDescriptorProvenance,
    SoundPaletteAssignment,
)


MAX_CHARACTERISTICS = 32
MAX_CHARACTERISTIC_EVIDENCE = 8
MAX_CHARACTERISTIC_TEXT = 512

CharacteristicName: TypeAlias = Literal[
    "attack_speed",
    "release_length",
    "sustain_behavior",
    "articulation",
    "plucked",
    "monophonic",
    "practical_polyphony",
    "preferred_register_low",
    "preferred_register_high",
    "usable_pitch_low",
    "usable_pitch_high",
    "bass_suitability",
    "sub_suitability",
    "transient_intensity",
    "timbral_density",
    "motion",
    "brightness",
    "width",
    "aggressiveness",
    "softness",
    "complexity",
    "tonal",
    "rhythmic_function",
]
CharacteristicConfidence: TypeAlias = Literal[
    "high", "medium", "low", "unknown"
]
CharacteristicProvenance: TypeAlias = Literal[
    "user_explicit",
    "reviewed_bundled_preset_metadata",
    "reviewed_user_local_metadata",
    "explicit_feedback",
    "plugin_atlas_product_knowledge",
    "normalized_preset_name_inference",
    "server_derived_default",
    "unknown",
]
CharacteristicValue: TypeAlias = str | float | int | StrictBool


class SoundCharacteristicModel(BaseModel):
    """Strict immutable base shared by sound-characteristic contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


class SoundCharacteristicEvidence(SoundCharacteristicModel):
    provenance: CharacteristicProvenance
    detail: str = Field(min_length=1, max_length=MAX_CHARACTERISTIC_TEXT)
    source_id: str | None = Field(default=None, min_length=1, max_length=256)


class SoundCharacteristic(SoundCharacteristicModel):
    name: CharacteristicName
    value: CharacteristicValue
    confidence: CharacteristicConfidence
    provenance: CharacteristicProvenance
    evidence: tuple[SoundCharacteristicEvidence, ...] = Field(
        default=(), max_length=MAX_CHARACTERISTIC_EVIDENCE
    )

    @model_validator(mode="after")
    def prevent_name_only_overclaim(self) -> "SoundCharacteristic":
        if (
            self.provenance == "normalized_preset_name_inference"
            and self.confidence == "high"
        ):
            raise ValueError(
                "preset-name inference cannot carry high characteristic confidence"
            )
        if self.provenance == "unknown" and self.confidence != "unknown":
            raise ValueError("unknown provenance requires unknown confidence")
        return self


class SelectedSoundCharacteristics(SoundCharacteristicModel):
    """Characteristics attached to one exact palette assignment."""

    schema_version: Literal["1.0"] = "1.0"
    role_id: str = Field(min_length=1, max_length=64)
    assignment_id: str | None = Field(default=None, min_length=1, max_length=128)
    product_id: str | None = Field(default=None, min_length=1, max_length=128)
    preset_name: str | None = Field(default=None, min_length=1, max_length=256)
    preset_index: int | None = Field(default=None, ge=0, le=999_999)
    preset_identity_verified: StrictBool = False
    characteristics: tuple[SoundCharacteristic, ...] = Field(
        default=(), max_length=MAX_CHARACTERISTICS
    )
    coverage: Literal["reviewed_exact", "family", "name_inferred", "unknown"] = (
        "unknown"
    )
    warnings: tuple[str, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_characteristics(self) -> "SelectedSoundCharacteristics":
        names = [item.name for item in self.characteristics]
        if len(names) != len(set(names)):
            raise ValueError("sound characteristic names must be unique")
        if self.preset_identity_verified and self.preset_name is None:
            raise ValueError("verified preset identity requires a preset name")
        if self.coverage == "reviewed_exact" and not self.characteristics:
            raise ValueError("reviewed exact coverage requires characteristics")
        return self

    def get(self, name: CharacteristicName) -> SoundCharacteristic | None:
        return next(
            (item for item in self.characteristics if item.name == name),
            None,
        )


def characteristics_from_palette_assignment(
    assignment: SoundPaletteAssignment,
) -> SelectedSoundCharacteristics:
    """Project one palette assignment's evidence into composition constraints.

    The assignment is already a bounded snapshot of the selected candidate.
    This adapter never reopens the inventory and never infers characteristics
    from the preset display name.  Family metadata remains family confidence;
    only an explicit exact metadata record may claim reviewed-exact coverage.
    """

    provenance_map: dict[CandidateMetadataProvenance, CharacteristicProvenance] = {
        "bundled_reviewed": "reviewed_bundled_preset_metadata",
        "user_local_reviewed": "reviewed_user_local_metadata",
        "name_inferred": "normalized_preset_name_inference",
        "atlas_product": "plugin_atlas_product_knowledge",
        "unknown": "unknown",
    }
    confidence_map: dict[ConfidenceLevel, CharacteristicConfidence] = {
        "high": "high",
        "medium": "medium",
        "low": "low",
        "metadata_insufficient": "low",
        "unknown": "unknown",
    }
    descriptor_provenance_map: dict[
        PresetDescriptorProvenance, CharacteristicProvenance
    ] = {
        "user_explicit": "user_explicit",
        "bundled_reviewed": "reviewed_bundled_preset_metadata",
        "user_local_reviewed": "reviewed_user_local_metadata",
        "explicit_feedback": "explicit_feedback",
        "atlas_product": "plugin_atlas_product_knowledge",
        "preset_name_token": "normalized_preset_name_inference",
        "unknown": "unknown",
    }

    def evidence_provenance(
        descriptor: DescriptorEvidence | None = None,
    ) -> CharacteristicProvenance:
        if descriptor is not None:
            return descriptor_provenance_map[descriptor.provenance]
        return provenance_map[assignment.characteristic_provenance]

    def evidence_confidence(
        provenance: CharacteristicProvenance,
        descriptor: DescriptorEvidence | None = None,
    ) -> CharacteristicConfidence:
        if provenance == "unknown":
            return "unknown"
        if descriptor is not None:
            confidence: CharacteristicConfidence = (
                "high"
                if descriptor.confidence >= 0.8
                else "medium"
                if descriptor.confidence >= 0.5
                else "low"
            )
        else:
            confidence = confidence_map[assignment.metadata_confidence]
        if provenance == "normalized_preset_name_inference" and confidence == "high":
            return "low"
        return confidence

    values: dict[CharacteristicName, SoundCharacteristic] = {}

    def add(
        name: CharacteristicName,
        value: CharacteristicValue,
        *,
        descriptor: DescriptorEvidence | None = None,
    ) -> None:
        provenance = evidence_provenance(descriptor)
        detail = (
            descriptor.detail
            if descriptor is not None and descriptor.detail
            else "Selected palette assignment metadata."
        )
        values[name] = SoundCharacteristic(
            name=name,
            value=value,
            confidence=evidence_confidence(provenance, descriptor),
            provenance=provenance,
            evidence=(
                SoundCharacteristicEvidence(
                    provenance=provenance,
                    detail=detail,
                    source_id=(
                        descriptor.source_id
                        if descriptor is not None
                        else assignment.metadata_source_id
                    ),
                ),
            ),
        )

    descriptor_by_name = {
        item.descriptor: item for item in assignment.descriptor_provenance
    }
    descriptors = set(assignment.descriptors)
    for descriptor, characteristic_name, value in (
        ("plucked", "plucked", True),
        ("mono", "monophonic", True),
        ("sub-heavy", "sub_suitability", "preferred"),
        ("tonal", "tonal", True),
        ("atonal", "tonal", False),
        ("percussive", "rhythmic_function", "percussive"),
        ("sustained", "sustain_behavior", "sustained"),
    ):
        if descriptor in descriptors:
            add(
                characteristic_name,  # type: ignore[arg-type]
                value,
                descriptor=descriptor_by_name.get(descriptor),
            )
    if assignment.mono_poly == "mono":
        add("monophonic", True)
        add("practical_polyphony", 1)
    if assignment.articulations:
        add("articulation", ", ".join(assignment.articulations))
    if assignment.envelope_behavior:
        add("sustain_behavior", ", ".join(assignment.envelope_behavior))

    register_bounds = {
        "low": (24, 48),
        "low_mid": (36, 60),
        "mid": (48, 72),
        "mid_high": (60, 84),
        "high": (72, 96),
    }
    bounded_registers = [
        register_bounds[item]
        for item in assignment.registers
        if item in register_bounds
    ]
    if bounded_registers:
        add("preferred_register_low", min(item[0] for item in bounded_registers))
        add("preferred_register_high", max(item[1] for item in bounded_registers))

    for value, name in (
        (assignment.brightness, "brightness"),
        (assignment.width, "width"),
        (assignment.motion, "motion"),
        (assignment.aggression, "aggressiveness"),
        (assignment.softness, "softness"),
        (assignment.density, "timbral_density"),
        (assignment.complexity, "complexity"),
    ):
        if value is not None:
            add(name, value)  # type: ignore[arg-type]

    provenance = provenance_map[assignment.metadata_provenance]
    if assignment.metadata_family_id is not None:
        coverage = "family"
    elif provenance in {
        "reviewed_bundled_preset_metadata",
        "reviewed_user_local_metadata",
    } and values:
        coverage = "reviewed_exact"
    elif provenance == "normalized_preset_name_inference":
        coverage = "name_inferred"
    else:
        coverage = "unknown"
    warnings = tuple(
        dict.fromkeys(
            (
                *assignment.known_limitations,
                *assignment.warnings,
                *(
                    ("Selected-sound metadata is limited; conservative defaults apply.",)
                    if not values
                    else ()
                ),
            )
        )
    )[:16]
    return SelectedSoundCharacteristics(
        role_id=assignment.role_id,
        assignment_id=assignment.assignment_id,
        product_id=assignment.product_id,
        preset_name=assignment.selected_preset,
        preset_index=assignment.selected_preset_index,
        preset_identity_verified=False,
        characteristics=tuple(values.values()),
        coverage=coverage,
        warnings=warnings,
    )


PitchBound = Annotated[int, Field(ge=0, le=131)]


class CompositionConstraintDecision(SoundCharacteristicModel):
    parameter: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=128)
    source: Literal[
        "connected_ai",
        "selected_sound_characteristic",
        "server_derived_default",
    ]
    confidence: CharacteristicConfidence
    reason: str = Field(min_length=1, max_length=MAX_CHARACTERISTIC_TEXT)


class CompositionRoleConstraints(SoundCharacteristicModel):
    """Bounded generation constraints derived for one musical role."""

    role_kind: Literal[
        "intro",
        "chords",
        "lead",
        "primary_bass",
        "sub_bass",
        "drums",
        "texture",
        "countermelody",
        "generic",
    ] = "generic"
    register_low: PitchBound = 0
    register_high: PitchBound = 131
    monophonic: StrictBool = False
    practical_polyphony: int | None = Field(default=None, ge=1, le=32)
    note_duration_scale: float = Field(default=1.0, ge=0.1, le=4.0)
    density_scale: float = Field(default=1.0, ge=0.1, le=1.0)
    overlap_ratio: float = Field(default=0.92, ge=0.05, le=1.5)
    decisions: tuple[CompositionConstraintDecision, ...] = Field(
        default=(), max_length=24
    )

    @model_validator(mode="after")
    def validate_range(self) -> "CompositionRoleConstraints":
        if self.register_low > self.register_high:
            raise ValueError("composition register_low cannot exceed register_high")
        if self.monophonic and self.practical_polyphony not in (None, 1):
            raise ValueError("monophonic constraints cannot declare polyphony above one")
        return self


class SoundAwareCompositionProfile(SoundCharacteristicModel):
    schema_version: Literal["1.0"] = "1.0"
    selected_sound: SelectedSoundCharacteristics
    constraints: CompositionRoleConstraints
    metadata_confidence: CharacteristicConfidence
    server_defaults_used: StrictBool
    warnings: tuple[str, ...] = Field(default=(), max_length=16)
