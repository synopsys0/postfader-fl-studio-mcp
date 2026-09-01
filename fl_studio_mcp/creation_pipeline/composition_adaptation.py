"""Deterministic note adaptation for selected instrument characteristics."""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import Field

from ..creative import CreativeNote, NoteSequence, make_sequence
from .sound_characteristics import (
    CharacteristicConfidence,
    CompositionConstraintDecision,
    CompositionRoleConstraints,
    SelectedSoundCharacteristics,
    SoundAwareCompositionProfile,
    SoundCharacteristicModel,
)


MAX_ADAPTATION_CHANGES = 32


class CompositionAdaptation(SoundCharacteristicModel):
    parameter: str = Field(min_length=1, max_length=64)
    before: str = Field(min_length=1, max_length=128)
    after: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=512)
    confidence: CharacteristicConfidence
    source: Literal[
        "connected_ai",
        "selected_sound_characteristic",
        "server_derived_default",
    ]


class CompositionAdaptationReport(SoundCharacteristicModel):
    schema_version: Literal["1.0"] = "1.0"
    role_id: str = Field(min_length=1, max_length=64)
    source_note_count: int = Field(ge=0, le=8192)
    adapted_note_count: int = Field(ge=0, le=8192)
    changes: tuple[CompositionAdaptation, ...] = Field(
        default=(), max_length=MAX_ADAPTATION_CHANGES
    )
    metadata_confidence: CharacteristicConfidence
    connected_ai_choices: tuple[str, ...] = Field(default=(), max_length=16)
    server_derived_choices: tuple[str, ...] = Field(default=(), max_length=16)
    identity_preserved: bool = True
    warnings: tuple[str, ...] = Field(default=(), max_length=16)


class SoundAwareCompositionResult(SoundCharacteristicModel):
    sequence: NoteSequence
    profile: SoundAwareCompositionProfile
    adaptation: CompositionAdaptationReport


_CONFIDENCE_ORDER = {"unknown": 0, "low": 1, "medium": 2, "high": 3}


def _text(characteristics: SelectedSoundCharacteristics, name: str) -> str | None:
    item = next((value for value in characteristics.characteristics if value.name == name), None)
    if item is None:
        return None
    return str(item.value).strip().casefold()


def _boolean(
    characteristics: SelectedSoundCharacteristics, name: str
) -> bool | None:
    item = next((value for value in characteristics.characteristics if value.name == name), None)
    if item is None or type(item.value) is not bool:
        return None
    return item.value


def _integer(
    characteristics: SelectedSoundCharacteristics, name: str
) -> int | None:
    item = next((value for value in characteristics.characteristics if value.name == name), None)
    if item is None or type(item.value) is not int:
        return None
    return item.value


def _confidence(characteristics: SelectedSoundCharacteristics) -> CharacteristicConfidence:
    if not characteristics.characteristics:
        return "unknown"
    return min(
        (item.confidence for item in characteristics.characteristics),
        key=_CONFIDENCE_ORDER.__getitem__,
    )


def derive_composition_profile(
    characteristics: SelectedSoundCharacteristics,
    *,
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
    ] = "generic",
    connected_ai_register: tuple[int, int] | None = None,
    connected_ai_polyphony: int | None = None,
) -> SoundAwareCompositionProfile:
    """Derive conservative constraints without treating names as audio proof."""

    low, high = {
        "sub_bass": (24, 48),
        "primary_bass": (28, 60),
        "chords": (43, 91),
        "lead": (55, 96),
        "countermelody": (55, 100),
        "intro": (43, 96),
        "texture": (36, 108),
    }.get(role_kind, (0, 131))
    decisions: list[CompositionConstraintDecision] = []
    defaults_used = True
    if role_kind in {"sub_bass", "primary_bass", "chords", "lead", "countermelody", "intro", "texture"}:
        decisions.append(
            CompositionConstraintDecision(
                parameter="register",
                value=f"{low}..{high}",
                source="server_derived_default",
                confidence="medium",
                reason=f"Conservative {role_kind.replace('_', ' ')} register default.",
            )
        )

    metadata_low = _integer(characteristics, "usable_pitch_low")
    metadata_high = _integer(characteristics, "usable_pitch_high")
    preferred_low = _integer(characteristics, "preferred_register_low")
    preferred_high = _integer(characteristics, "preferred_register_high")
    if metadata_low is not None:
        low = max(low, metadata_low)
    if metadata_high is not None:
        high = min(high, metadata_high)
    if preferred_low is not None:
        low = max(low, preferred_low)
    if preferred_high is not None:
        high = min(high, preferred_high)
    if any(value is not None for value in (metadata_low, metadata_high, preferred_low, preferred_high)):
        defaults_used = False
        decisions.append(
            CompositionConstraintDecision(
                parameter="register",
                value=f"{low}..{high}",
                source="selected_sound_characteristic",
                confidence=_confidence(characteristics),
                reason="Selected-sound register evidence narrowed the role range.",
            )
        )
    if connected_ai_register is not None:
        requested_low, requested_high = connected_ai_register
        if not 0 <= requested_low <= requested_high <= 131:
            raise ValueError("connected_ai_register must be within 0..131")
        low = max(low, requested_low)
        high = min(high, requested_high)
        decisions.append(
            CompositionConstraintDecision(
                parameter="register",
                value=f"{low}..{high}",
                source="connected_ai",
                confidence="high",
                reason="The connected AI supplied an explicit bounded role register.",
            )
        )
    if low > high:
        raise ValueError("selected sound and requested role have no overlapping pitch range")

    monophonic = role_kind in {"lead", "primary_bass", "sub_bass"}
    declared_mono = _boolean(characteristics, "monophonic")
    if declared_mono is not None:
        monophonic = declared_mono
        defaults_used = False
        decisions.append(
            CompositionConstraintDecision(
                parameter="monophonic",
                value=str(monophonic).lower(),
                source="selected_sound_characteristic",
                confidence=_confidence(characteristics),
                reason="The selected sound declares mono/poly behavior.",
            )
        )

    polyphony = 1 if monophonic else _integer(characteristics, "practical_polyphony")
    if connected_ai_polyphony is not None:
        if not 1 <= connected_ai_polyphony <= 32:
            raise ValueError("connected_ai_polyphony must be within 1..32")
        polyphony = 1 if monophonic else connected_ai_polyphony
        decisions.append(
            CompositionConstraintDecision(
                parameter="practical_polyphony",
                value=str(polyphony),
                source="connected_ai",
                confidence="high",
                reason="The connected AI supplied a bounded voice limit.",
            )
        )

    articulation = _text(characteristics, "articulation")
    plucked = _boolean(characteristics, "plucked")
    attack = _text(characteristics, "attack_speed")
    release = _text(characteristics, "release_length")
    density = _text(characteristics, "timbral_density") or _text(characteristics, "complexity")
    duration_scale = 1.0
    density_scale = 1.0
    overlap_ratio = 0.92
    if plucked or articulation in {"pluck", "plucked", "staccato"} or attack in {"fast", "hard"}:
        duration_scale = 0.55
        overlap_ratio = 0.72
    elif articulation in {"pad", "sustained", "legato"} or attack in {"slow", "soft"}:
        duration_scale = 1.45
        density_scale = 0.65
        overlap_ratio = 1.0
    if release in {"long", "very_long"}:
        density_scale = min(density_scale, 0.7)
        overlap_ratio = min(overlap_ratio, 0.9)
    elif release in {"short", "very_short"}:
        overlap_ratio = min(overlap_ratio, 0.75)
    if density in {"dense", "complex", "high"}:
        density_scale = min(density_scale, 0.6)
    if (duration_scale, density_scale, overlap_ratio) != (1.0, 1.0, 0.92):
        defaults_used = False
        decisions.append(
            CompositionConstraintDecision(
                parameter="articulation",
                value=(
                    f"duration={duration_scale:.2f},density={density_scale:.2f},"
                    f"overlap={overlap_ratio:.2f}"
                ),
                source="selected_sound_characteristic",
                confidence=_confidence(characteristics),
                reason="Envelope, articulation, or density evidence changed note shaping.",
            )
        )

    warnings: list[str] = []
    confidence = _confidence(characteristics)
    if confidence in {"low", "unknown"}:
        warnings.append(
            "Sound-aware adaptation uses weak or missing metadata; identity verification does not prove suitability."
        )
    return SoundAwareCompositionProfile(
        selected_sound=characteristics,
        constraints=CompositionRoleConstraints(
            role_kind=role_kind,
            register_low=low,
            register_high=high,
            monophonic=monophonic,
            practical_polyphony=polyphony,
            note_duration_scale=duration_scale,
            density_scale=density_scale,
            overlap_ratio=overlap_ratio,
            decisions=tuple(decisions),
        ),
        metadata_confidence=confidence,
        server_defaults_used=defaults_used,
        warnings=tuple(warnings),
    )


def _pitch_in_range(pitch: int, low: int, high: int) -> int:
    candidates = [value for value in range(low, high + 1) if value % 12 == pitch % 12]
    if candidates:
        return min(candidates, key=lambda value: (abs(value - pitch), value))
    return max(low, min(high, pitch))


def adapt_note_sequence(
    sequence: NoteSequence,
    profile: SoundAwareCompositionProfile,
) -> SoundAwareCompositionResult:
    """Apply only deterministic, explainable pitch/density/articulation rules."""

    constraints = profile.constraints
    source = sorted(
        sequence.notes,
        key=lambda note: (note.start_beats, note.pitch, note.duration_beats),
    )
    notes = [
        note.model_copy(
            update={
                "pitch": _pitch_in_range(
                    note.pitch, constraints.register_low, constraints.register_high
                )
            }
        )
        for note in source
    ]
    changes: list[CompositionAdaptation] = []
    if any(left.pitch != right.pitch for left, right in zip(source, notes)):
        changes.append(
            CompositionAdaptation(
                parameter="register",
                before="source range",
                after=f"{constraints.register_low}..{constraints.register_high}",
                reason="Notes were octave-folded into the selected sound's usable role register.",
                confidence=profile.metadata_confidence,
                source="selected_sound_characteristic",
            )
        )

    if constraints.density_scale < 0.999 and len(notes) > 1:
        target_count = max(1, round(len(notes) * constraints.density_scale))
        selected_indices = {
            round(index * (len(notes) - 1) / max(1, target_count - 1))
            for index in range(target_count)
        }
        notes = [note for index, note in enumerate(notes) if index in selected_indices]
        changes.append(
            CompositionAdaptation(
                parameter="density",
                before=str(len(source)),
                after=str(len(notes)),
                reason="Dense, sustained, or slow material uses fewer retriggers.",
                confidence=profile.metadata_confidence,
                source="selected_sound_characteristic",
            )
        )

    if constraints.practical_polyphony is not None:
        grouped: dict[float, list[CreativeNote]] = defaultdict(list)
        for note in notes:
            grouped[note.start_beats].append(note)
        bounded: list[CreativeNote] = []
        center = (constraints.register_low + constraints.register_high) / 2
        for start in sorted(grouped):
            chord = sorted(
                grouped[start], key=lambda note: (abs(note.pitch - center), note.pitch)
            )[: constraints.practical_polyphony]
            bounded.extend(sorted(chord, key=lambda note: note.pitch))
        if len(bounded) != len(notes):
            changes.append(
                CompositionAdaptation(
                    parameter="polyphony",
                    before="unbounded",
                    after=str(constraints.practical_polyphony),
                    reason="Simultaneous notes were bounded to the declared practical voice count.",
                    confidence=profile.metadata_confidence,
                    source="selected_sound_characteristic",
                )
            )
        notes = sorted(bounded, key=lambda note: (note.start_beats, note.pitch))

    if constraints.monophonic and notes:
        mono: list[CreativeNote] = []
        for note in notes:
            if mono and abs(note.start_beats - mono[-1].start_beats) <= 1e-9:
                continue
            if mono:
                previous = mono[-1]
                available = max(0.01, note.start_beats - previous.start_beats)
                if previous.duration_beats > available:
                    mono[-1] = previous.model_copy(
                        update={"duration_beats": round(available * 0.98, 6)}
                    )
            mono.append(note)
        if len(mono) != len(notes) or any(
            left.duration_beats != right.duration_beats
            for left, right in zip(notes, mono)
        ):
            changes.append(
                CompositionAdaptation(
                    parameter="monophony",
                    before="polyphonic or overlapping",
                    after="single non-overlapping line",
                    reason="The selected sound or musical role is monophonic.",
                    confidence=profile.metadata_confidence,
                    source="selected_sound_characteristic",
                )
            )
        notes = mono

    scaled: list[CreativeNote] = []
    for index, note in enumerate(notes):
        duration = note.duration_beats * constraints.note_duration_scale
        if index + 1 < len(notes):
            next_start = notes[index + 1].start_beats
            if next_start > note.start_beats:
                duration = min(
                    duration,
                    (next_start - note.start_beats) * constraints.overlap_ratio,
                )
        duration = max(0.01, min(256.0, duration))
        scaled.append(note.model_copy(update={"duration_beats": round(duration, 6)}))
    if any(left.duration_beats != right.duration_beats for left, right in zip(notes, scaled)):
        changes.append(
            CompositionAdaptation(
                parameter="articulation",
                before="source durations",
                after=f"scale {constraints.note_duration_scale:.2f}",
                reason="Note lengths and overlap were adapted to attack, release, and articulation evidence.",
                confidence=profile.metadata_confidence,
                source="selected_sound_characteristic",
            )
        )
    notes = scaled

    adapted = make_sequence(
        name=sequence.name,
        generator=sequence.generator,
        notes=notes,
        tempo_bpm=sequence.tempo_bpm,
        numerator=sequence.time_signature_numerator,
        denominator=sequence.time_signature_denominator,
        seed=sequence.seed,
        pitch_collection=sequence.pitch_collection,
        warnings=[*sequence.warnings, *profile.warnings],
    )
    connected = tuple(
        decision.parameter
        for decision in constraints.decisions
        if decision.source == "connected_ai"
    )
    server = tuple(
        decision.parameter
        for decision in constraints.decisions
        if decision.source == "server_derived_default"
    )
    return SoundAwareCompositionResult(
        sequence=adapted,
        profile=profile,
        adaptation=CompositionAdaptationReport(
            role_id=profile.selected_sound.role_id,
            source_note_count=len(source),
            adapted_note_count=len(notes),
            changes=tuple(changes),
            metadata_confidence=profile.metadata_confidence,
            connected_ai_choices=tuple(dict.fromkeys(connected)),
            server_derived_choices=tuple(dict.fromkeys(server)),
            warnings=profile.warnings,
        ),
    )


def develop_section_variation(
    sequence: NoteSequence,
    *,
    density_scale: float = 1.0,
    register_shift: int = 0,
    articulation_scale: float = 1.0,
) -> NoteSequence:
    """Create a bounded identity-preserving section development.

    Existing starts and pitch classes are retained; only allowed density,
    register, and articulation dimensions change.
    """

    if not 0.25 <= density_scale <= 2.0:
        raise ValueError("density_scale must be within 0.25..2")
    if not -24 <= register_shift <= 24:
        raise ValueError("register_shift must be within -24..24 semitones")
    if not 0.25 <= articulation_scale <= 2.0:
        raise ValueError("articulation_scale must be within 0.25..2")
    notes = list(sequence.notes)
    if density_scale < 1.0 and notes:
        target = max(1, round(len(notes) * density_scale))
        keep = {
            round(index * (len(notes) - 1) / max(1, target - 1))
            for index in range(target)
        }
        notes = [note for index, note in enumerate(notes) if index in keep]
    varied = [
        note.model_copy(
            update={
                "pitch": max(0, min(131, note.pitch + register_shift)),
                "duration_beats": max(
                    0.01, min(256.0, note.duration_beats * articulation_scale)
                ),
            }
        )
        for note in notes
    ]
    return make_sequence(
        name=f"{sequence.name} variation",
        generator=sequence.generator,
        notes=varied,
        tempo_bpm=sequence.tempo_bpm,
        numerator=sequence.time_signature_numerator,
        denominator=sequence.time_signature_denominator,
        seed=sequence.seed,
        pitch_collection=sequence.pitch_collection,
        warnings=[
            *sequence.warnings,
            "Section variation preserves source starts and pitch-class identity.",
        ],
    )
