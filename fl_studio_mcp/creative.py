"""Creative workflows for notes, composition, Piano Roll, and MIDI files.

FL exposes Piano Roll notes only inside its separate ``.pyscript`` runtime.
The controller-script API can select the target channel/pattern and focus the
Piano Roll, but it cannot call a Piano Roll script or read the resulting score.
This module keeps that boundary explicit: it generates a bounded script from
typed data, targets FL through the live bridge, and sends FL's documented
run-last-script shortcut. Note writes are verified only when an authenticated
script-runtime apply receipt and a second read-only persisted-score receipt
agree; those receipts are never described as controller-API note access. Pure
composition and MIDI export remain deterministic and verifiable on the host.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import platform
import random
import struct
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import ConfigDict, Field, model_validator

from .bridge_client import get_client
from .contracts import ContractModel, SCHEMA_VERSION
from .host_config import fl_studio_user_data_dir
from .readonly_inspector import IncompatibleFLStudio, connection_from_ping
from .performance import (
    TrackBController,
    TrackBInspector,
    TrackBMutationGateway,
    TrackBReadGateway,
)
from .sound_selection.models import DrumPadMap
from .track_b_contracts import (
    SHA256_PATTERN,
    VerifiedPatternIdentityWrite,
    VerifiedPatternLengthWrite,
    VerifiedPatternSelectionWrite,
)
from .verified_writer import (
    PROVENANCE_REFUSAL,
    WRITES_DISABLED_HELP,
    VerifiedWritesUnavailable,
)


MAX_CREATIVE_NOTES = 4096
MAX_PIANO_ROLL_NOTES = 2048
MAX_MIDI_TRACKS = 32
MAX_SEQUENCE_BEATS = 4096.0
PIANO_ROLL_SCRIPT_NAME = "Postfader_Apply.pyscript"
PIANO_ROLL_RECEIPT_NAME = "Postfader_Apply.receipt.json"
PIANO_ROLL_SCRIPTS_DIR_ENV = "POSTFADER_PIANO_ROLL_SCRIPTS_DIR"
CREATIVE_ENGINE_VERSION = "postfader-creative-1"
MAX_PIANO_ROLL_RECEIPT_BYTES = 8192
MAX_PIANO_ROLL_SCORE_EVIDENCE_NOTES = 16384
PIANO_ROLL_RECEIPT_WAIT_SECONDS = 2.0
# Piano Roll control fields are persisted by FL on a 1/128 grid even though
# the scripting API accepts arbitrary floats.  Runtime digests must identify
# the persisted value, while note requests and receipt payloads retain their
# original values and claims.  ``selected`` is intentionally not part of a
# runtime signature: FL may clear that transient editor selection after the
# script finishes without changing the musical note.
PIANO_ROLL_CONTROL_RESOLUTION = 128

# A single MCP process can serve concurrent creative requests. Keep the
# destination check, atomic commit, and verification together so two
# create-only exports cannot both observe an absent path and then replace one
# another's file. The lock is intentionally process-local: the MCP server's
# export calls share this module, and the atomic temporary-file commit remains
# safe for readers on every supported platform.
_MIDI_EXPORT_LOCK = threading.Lock()
# Serialize the focus-sensitive target/script/hotkey sequence with setup calls
# that replace the fixed script or mutate its armed state. Status-only reads
# retain their own short-lived registry lock.
_PIANO_ROLL_DISPATCH_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session_precondition(value: str | None) -> str | None:
    """Validate an optional bridge-lifetime guard before any side effect."""

    if value is None:
        return None
    if (
        type(value) is not str
        or len(value) != 32
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(
            "session_fingerprint must be exactly 32 lowercase hexadecimal characters"
        )
    return value


class CreativeModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CreativeNote(CreativeModel):
    """One note in quarter-note beat units.

    FL's Piano Roll supports note numbers 0..131. Standard MIDI export is
    limited to 0..127 and rejects the four FL-only values at export time.
    """

    pitch: int = Field(ge=0, le=131)
    start_beats: float = Field(ge=0.0, le=MAX_SEQUENCE_BEATS)
    duration_beats: float = Field(gt=0.0, le=256.0)
    velocity: float = Field(default=100.0 / 127.0, ge=0.0, le=1.0)
    pan: float = Field(default=0.5, ge=0.0, le=1.0)
    release: float = Field(default=0.5, ge=0.0, le=1.0)
    color: int = Field(default=0, ge=0, le=15)
    pitch_offset_tenths: int = Field(default=0, ge=-120, le=120)
    slide: bool = False
    portamento: bool = False
    muted: bool = False
    selected: bool = True


def note_digest(notes: Sequence[CreativeNote]) -> str:
    payload = [
        note.model_dump(mode="json", exclude_none=False)
        for note in sorted(
            notes,
            key=lambda item: (
                item.start_beats,
                item.pitch,
                item.duration_beats,
                item.velocity,
                item.color,
            ),
        )
    ]
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class NoteSequence(CreativeModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    name: str = Field(min_length=1, max_length=128)
    generator: str = Field(min_length=1, max_length=64)
    engine_version: Literal["postfader-creative-1"] = CREATIVE_ENGINE_VERSION
    tempo_bpm: float = Field(default=120.0, ge=10.0, le=522.0)
    time_signature_numerator: int = Field(default=4, ge=1, le=32)
    time_signature_denominator: Literal[1, 2, 4, 8, 16, 32] = 4
    notes: list[CreativeNote] = Field(max_length=MAX_CREATIVE_NOTES)
    note_count: int = Field(ge=0, le=MAX_CREATIVE_NOTES)
    duration_beats: float = Field(ge=0.0, le=MAX_SEQUENCE_BEATS + 256.0)
    note_digest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: int | None = None
    pitch_collection: list[int] = Field(default_factory=list, max_length=12)
    mutations_applied: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_summary(self) -> "NoteSequence":
        if self.note_count != len(self.notes):
            raise ValueError("note_count must equal notes length")
        if self.note_digest_sha256 != note_digest(self.notes):
            raise ValueError("note digest does not match notes")
        expected_duration = max(
            (note.start_beats + note.duration_beats for note in self.notes),
            default=0.0,
        )
        if abs(self.duration_beats - expected_duration) > 1e-6:
            raise ValueError("duration_beats must equal the final note end")
        return self


def make_sequence(
    *,
    name: str,
    generator: str,
    notes: Sequence[CreativeNote],
    tempo_bpm: float = 120.0,
    numerator: int = 4,
    denominator: Literal[1, 2, 4, 8, 16, 32] = 4,
    seed: int | None = None,
    pitch_collection: Sequence[int] = (),
    warnings: Sequence[str] = (),
) -> NoteSequence:
    ordered = sorted(
        notes, key=lambda item: (item.start_beats, item.pitch, item.duration_beats)
    )
    if len(ordered) > MAX_CREATIVE_NOTES:
        raise ValueError(f"a sequence may contain at most {MAX_CREATIVE_NOTES} notes")
    duration = max(
        (note.start_beats + note.duration_beats for note in ordered), default=0.0
    )
    return NoteSequence(
        name=name,
        generator=generator,
        tempo_bpm=tempo_bpm,
        time_signature_numerator=numerator,
        time_signature_denominator=denominator,
        notes=ordered,
        note_count=len(ordered),
        duration_beats=round(duration, 6),
        note_digest_sha256=note_digest(ordered),
        seed=seed,
        pitch_collection=list(pitch_collection),
        warnings=list(warnings),
    )


# ---------------------------------------------------------------------------
# Pitch collections and deterministic composition
# ---------------------------------------------------------------------------


PITCH_COLLECTIONS: dict[str, tuple[int, ...]] = {
    "chromatic": tuple(range(12)),
    "major": (0, 2, 4, 5, 7, 9, 11),
    "ionian": (0, 2, 4, 5, 7, 9, 11),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
    "phrygian": (0, 1, 3, 5, 7, 8, 10),
    "lydian": (0, 2, 4, 6, 7, 9, 11),
    "mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "natural_minor": (0, 2, 3, 5, 7, 8, 10),
    "aeolian": (0, 2, 3, 5, 7, 8, 10),
    "locrian": (0, 1, 3, 5, 6, 8, 10),
    "harmonic_minor": (0, 2, 3, 5, 7, 8, 11),
    "melodic_minor": (0, 2, 3, 5, 7, 9, 11),
    "major_pentatonic": (0, 2, 4, 7, 9),
    "minor_pentatonic": (0, 3, 5, 7, 10),
    "blues": (0, 3, 5, 6, 7, 10),
    "raga_mohanam": (0, 2, 4, 7, 9),
    "raga_kalyani": (0, 2, 4, 6, 7, 9, 11),
    "raga_mayamalavagowla": (0, 1, 4, 5, 7, 8, 11),
    "raga_hamsadhwani": (0, 2, 4, 7, 11),
}

_ROOT_NAMES = {
    "C": 0,
    "B#": 0,
    "C#": 1,
    "DB": 1,
    "D": 2,
    "D#": 3,
    "EB": 3,
    "E": 4,
    "FB": 4,
    "E#": 5,
    "F": 5,
    "F#": 6,
    "GB": 6,
    "G": 7,
    "G#": 8,
    "AB": 8,
    "A": 9,
    "A#": 10,
    "BB": 10,
    "B": 11,
    "CB": 11,
}

_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7}


def resolve_root(root: str | int) -> int:
    if type(root) is int:
        if 0 <= root <= 11:
            return root
        raise ValueError("integer root must be a pitch class within 0..11")
    if not isinstance(root, str):
        raise ValueError("root must be a note name or pitch class")
    spelling = root.strip().upper().replace("♯", "#").replace("♭", "B")
    if spelling not in _ROOT_NAMES:
        raise ValueError(
            "root must be C, C#/Db, D, D#/Eb, E, F, F#/Gb, G, G#/Ab, A, A#/Bb, or B"
        )
    return _ROOT_NAMES[spelling]


def resolve_pitch_collection(
    name: str, custom_intervals: Sequence[int] | None = None
) -> tuple[int, ...]:
    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    if key == "custom":
        if not custom_intervals:
            raise ValueError("custom pitch collection needs custom_intervals")
        intervals = tuple(sorted(set(custom_intervals)))
        if any(
            type(value) is not int or value < 0 or value > 11 for value in intervals
        ):
            raise ValueError(
                "custom intervals must be unique pitch classes within 0..11"
            )
        if 0 not in intervals:
            raise ValueError("custom intervals must include root interval 0")
        return intervals
    if custom_intervals is not None:
        raise ValueError("custom_intervals is valid only when collection='custom'")
    try:
        return PITCH_COLLECTIONS[key]
    except KeyError:
        raise ValueError(
            "unknown pitch collection; choose one of: "
            + ", ".join(sorted(PITCH_COLLECTIONS))
            + ", custom"
        ) from None


def _roman_degree(token: str) -> tuple[int, bool]:
    if not isinstance(token, str) or not token.strip():
        raise ValueError("progression entries must be non-empty Roman numerals")
    cleaned = token.strip().replace("°", "dim")
    roman = ""
    for character in cleaned:
        if character.upper() in "IVX":
            roman += character
        else:
            break
    degree = _ROMAN.get(roman.upper())
    if degree is None:
        raise ValueError(f"unsupported Roman chord {token!r}; use I through vii")
    return degree, "7" in cleaned


def _scale_pitch(root: int, intervals: Sequence[int], degree_index: int) -> int:
    octave, index = divmod(degree_index, len(intervals))
    return root + intervals[index] + 12 * octave


def _voice_chord(
    pitches: list[int], previous: Sequence[int] | None, low: int, high: int
) -> list[int]:
    candidates: list[list[int]] = []
    for inversion in range(len(pitches)):
        inverted = pitches[inversion:] + [value + 12 for value in pitches[:inversion]]
        for shift in (-24, -12, 0, 12, 24):
            candidate = sorted(value + shift for value in inverted)
            if candidate[0] >= low and candidate[-1] <= high:
                candidates.append(candidate)
    if not candidates:
        raise ValueError("requested chord voicing does not fit the pitch range")
    if previous is None:
        center = (low + high) / 2.0
        return min(candidates, key=lambda chord: abs(sum(chord) / len(chord) - center))
    return min(
        candidates,
        key=lambda chord: sum(
            abs(chord[i] - previous[min(i, len(previous) - 1)])
            for i in range(len(chord))
        ),
    )


def compose_chord_progression(
    progression: Sequence[str],
    *,
    root: str | int = "C",
    collection: str = "major",
    custom_intervals: Sequence[int] | None = None,
    beats_per_chord: float = 4.0,
    octave: int = 4,
    voicing: Literal["close", "open", "drop2"] = "close",
    velocity: float = 0.78,
    tempo_bpm: float = 120.0,
) -> NoteSequence:
    if not 1 <= len(progression) <= 64:
        raise ValueError("progression must contain 1..64 chords")
    if not 0.125 <= beats_per_chord <= 32.0:
        raise ValueError("beats_per_chord must be within 0.125..32")
    if octave < 0 or octave > 8:
        raise ValueError("octave must be within 0..8")
    if not 0.0 <= velocity <= 1.0:
        raise ValueError("velocity must be within 0..1")
    root_pc = resolve_root(root)
    intervals = resolve_pitch_collection(collection, custom_intervals)
    if len(intervals) < 5:
        raise ValueError(
            "chord generation needs at least five pitch-collection degrees"
        )
    anchor = (octave + 1) * 12 + root_pc
    notes: list[CreativeNote] = []
    previous: list[int] | None = None
    for chord_index, token in enumerate(progression):
        degree, seventh = _roman_degree(token)
        degrees = [degree - 1, degree + 1, degree + 3]
        if seventh:
            degrees.append(degree + 5)
        pitches = [
            anchor + (_scale_pitch(0, intervals, item) - intervals[0])
            for item in degrees
        ]
        if voicing == "open" and len(pitches) >= 3:
            pitches[1] += 12
        voiced = _voice_chord(
            pitches, previous, max(0, anchor - 24), min(131, anchor + 31)
        )
        if voicing == "drop2" and len(voiced) >= 4:
            voiced[-2] -= 12
            voiced.sort()
        previous = voiced
        start = chord_index * beats_per_chord
        for pitch in voiced:
            notes.append(
                CreativeNote(
                    pitch=pitch,
                    start_beats=start,
                    duration_beats=beats_per_chord,
                    velocity=velocity,
                )
            )
    return make_sequence(
        name="Chord progression " + "-".join(progression),
        generator="chord_progression",
        notes=notes,
        tempo_bpm=tempo_bpm,
        pitch_collection=[(root_pc + value) % 12 for value in intervals],
    )


def compose_melody(
    *,
    root: str | int = "C",
    collection: str = "major",
    custom_intervals: Sequence[int] | None = None,
    bars: int = 4,
    beats_per_bar: int = 4,
    density: float = 0.65,
    register_low: int = 60,
    register_high: int = 84,
    contour: Literal["balanced", "rising", "falling", "arch", "wave"] = "balanced",
    seed: int = 0,
    tempo_bpm: float = 120.0,
) -> NoteSequence:
    if not 1 <= bars <= 64 or not 1 <= beats_per_bar <= 16:
        raise ValueError("bars and beats_per_bar must be within 1..64 and 1..16")
    if not 0.05 <= density <= 1.0:
        raise ValueError("density must be within 0.05..1")
    if not 0 <= register_low < register_high <= 131:
        raise ValueError("register must be an increasing range within 0..131")
    root_pc = resolve_root(root)
    intervals = resolve_pitch_collection(collection, custom_intervals)
    available = [
        pitch
        for pitch in range(register_low, register_high + 1)
        if (pitch - root_pc) % 12 in intervals
    ]
    if len(available) < 3:
        raise ValueError("register contains too few pitches from the collection")
    rng = random.Random(seed)
    total_beats = bars * beats_per_bar
    grid = 0.5 if density < 0.8 else 0.25
    slots = int(round(total_beats / grid))
    index = len(available) // 2
    notes: list[CreativeNote] = []
    last_end = 0.0
    for slot in range(slots):
        progress = slot / max(1, slots - 1)
        if contour == "rising":
            bias = 0.30
        elif contour == "falling":
            bias = -0.30
        elif contour == "arch":
            bias = 0.35 if progress < 0.5 else -0.35
        elif contour == "wave":
            bias = 0.32 * math.sin(progress * math.tau * 2.0)
        else:
            bias = 0.0
        if rng.random() > density:
            continue
        movement = rng.choices(
            [-2, -1, 0, 1, 2],
            weights=[0.10, 0.27 - bias / 4, 0.26, 0.27 + bias / 4, 0.10],
            k=1,
        )[0]
        index = max(0, min(len(available) - 1, index + movement))
        start = slot * grid
        if start < last_end - 1e-9:
            continue
        duration = grid * rng.choice((1, 1, 1, 2, 2, 3))
        duration = min(duration, total_beats - start)
        if duration <= 0:
            continue
        phrase_accent = 0.08 if slot % int(round(beats_per_bar / grid)) == 0 else 0.0
        velocity = max(0.35, min(1.0, 0.68 + phrase_accent + rng.uniform(-0.08, 0.08)))
        notes.append(
            CreativeNote(
                pitch=available[index],
                start_beats=round(start, 6),
                duration_beats=round(duration * 0.92, 6),
                velocity=round(velocity, 4),
            )
        )
        last_end = start + duration
    return make_sequence(
        name=f"{collection} melody",
        generator="melody",
        notes=notes,
        tempo_bpm=tempo_bpm,
        numerator=beats_per_bar,
        seed=seed,
        pitch_collection=[(root_pc + value) % 12 for value in intervals],
    )


def compose_bassline(
    progression: Sequence[str],
    *,
    root: str | int = "C",
    collection: str = "major",
    custom_intervals: Sequence[int] | None = None,
    beats_per_chord: float = 4.0,
    octave: int = 2,
    style: Literal["roots", "eighths", "octaves", "walking"] = "roots",
    seed: int = 0,
    tempo_bpm: float = 120.0,
) -> NoteSequence:
    if not 1 <= len(progression) <= 64:
        raise ValueError("progression must contain 1..64 chords")
    if not 0.5 <= beats_per_chord <= 32.0:
        raise ValueError("beats_per_chord must be within 0.5..32")
    if not 0 <= octave <= 7:
        raise ValueError("octave must be within 0..7")
    root_pc = resolve_root(root)
    intervals = resolve_pitch_collection(collection, custom_intervals)
    rng = random.Random(seed)
    anchor = (octave + 1) * 12 + root_pc
    notes: list[CreativeNote] = []
    for chord_index, token in enumerate(progression):
        degree, _seventh = _roman_degree(token)
        chord_root = anchor + _scale_pitch(0, intervals, degree - 1)
        start = chord_index * beats_per_chord
        if style == "roots":
            events = [(0.0, beats_per_chord * 0.92, chord_root)]
        elif style == "eighths":
            events = [
                (offset * 0.5, 0.43, chord_root)
                for offset in range(int(round(beats_per_chord * 2)))
            ]
        elif style == "octaves":
            events = [
                (offset * 0.5, 0.43, chord_root + (12 if offset % 2 else 0))
                for offset in range(int(round(beats_per_chord * 2)))
            ]
        else:
            next_degree, _ = _roman_degree(
                progression[(chord_index + 1) % len(progression)]
            )
            next_root = anchor + _scale_pitch(0, intervals, next_degree - 1)
            steps = max(1, int(round(beats_per_chord)))
            events = []
            for offset in range(steps):
                blend = offset / max(1, steps)
                target = round(chord_root + (next_root - chord_root) * blend)
                candidates = [
                    pitch
                    for pitch in range(target - 3, target + 4)
                    if (pitch - root_pc) % 12 in intervals
                ]
                pitch = (
                    min(candidates, key=lambda item: abs(item - target))
                    if candidates
                    else chord_root
                )
                if offset not in (0, steps - 1) and rng.random() < 0.2:
                    pitch += rng.choice((-12, 12))
                events.append((float(offset), 0.88, pitch))
        for offset, duration, pitch in events:
            if 0 <= pitch <= 131:
                notes.append(
                    CreativeNote(
                        pitch=pitch,
                        start_beats=round(start + offset, 6),
                        duration_beats=min(duration, beats_per_chord - offset),
                        velocity=round(0.72 + rng.uniform(-0.06, 0.06), 4),
                    )
                )
    return make_sequence(
        name=f"{style} bassline",
        generator="bassline",
        notes=notes,
        tempo_bpm=tempo_bpm,
        seed=seed,
        pitch_collection=[(root_pc + value) % 12 for value in intervals],
    )


def compose_drums(
    *,
    style: Literal["house", "hiphop", "trap", "pop", "dnb"] = "house",
    bars: int = 4,
    beats_per_bar: int = 4,
    seed: int = 0,
    swing: float = 0.0,
    tempo_bpm: float = 120.0,
    drum_map: DrumPadMap | None = None,
) -> NoteSequence:
    if not 1 <= bars <= 64 or not 1 <= beats_per_bar <= 16:
        raise ValueError("bars and beats_per_bar must be within 1..64 and 1..16")
    if not 0.0 <= swing <= 0.49:
        raise ValueError("swing must be within 0..0.49 beats")

    def normalise_role(role: str) -> str:
        return "_".join(role.strip().casefold().replace("-", "_").split())

    # General MIDI remains an explicit, backwards-compatible fallback for
    # callers that do not have a loaded instrument map. Once a map is supplied,
    # however, every role used by this style must be mapped: falling back to a
    # GM pitch would make a successful-looking sequence address the wrong pad.
    required_roles = ["kick", "snare", "closed_hat"]
    if style == "house":
        required_roles.append("open_hat")
    if drum_map is not None:
        if not isinstance(drum_map, DrumPadMap):
            raise TypeError("drum_map must be a DrumPadMap or None")
        mappings = {
            normalise_role(mapping.role): mapping
            for mapping in drum_map.mappings
        }
        declared_missing = {
            normalise_role(role) for role in drum_map.missing_roles
        }
        missing_roles = tuple(
            role
            for role in required_roles
            if normalise_role(role) in declared_missing
            or normalise_role(role) not in mappings
        )
        if missing_roles:
            raise ValueError(
                "supplied drum_map is missing required semantic role(s): "
                + ", ".join(missing_roles)
            )
        role_pitches = {
            role: mappings[normalise_role(role)].midi_note
            for role in required_roles
        }
    else:
        role_pitches = {
            "kick": 36,
            "snare": 38 if style != "trap" else 39,
            "closed_hat": 42,
            "open_hat": 46,
        }

    rng = random.Random(seed)
    notes: list[CreativeNote] = []

    def hit(pitch: int, beat: float, velocity: float, length: float = 0.12) -> None:
        shifted = beat + (swing if int(round(beat * 2)) % 2 else 0.0)
        if shifted < bars * beats_per_bar:
            notes.append(
                CreativeNote(
                    pitch=pitch,
                    start_beats=round(shifted, 6),
                    duration_beats=length,
                    velocity=max(0.05, min(1.0, round(velocity, 4))),
                    color=9,
                )
            )

    for bar in range(bars):
        base = bar * beats_per_bar
        if style in {"house", "pop"}:
            for beat in range(beats_per_bar):
                hit(role_pitches["kick"], base + beat, 0.92 + rng.uniform(-0.04, 0.04))
            for beat in (1, 3):
                if beat < beats_per_bar:
                    hit(role_pitches["snare"], base + beat, 0.84 + rng.uniform(-0.04, 0.04))
            for step in range(beats_per_bar * 2):
                hit(
                    role_pitches["closed_hat"],
                    base + step * 0.5,
                    0.58 + (0.10 if step % 2 else 0.0),
                )
            if style == "house":
                for beat in range(beats_per_bar):
                    hit(role_pitches["open_hat"], base + beat + 0.5, 0.62)
        elif style in {"hiphop", "trap"}:
            for beat in (0.0, 2.5 if style == "hiphop" else 2.75):
                if beat < beats_per_bar:
                    hit(role_pitches["kick"], base + beat, 0.94)
            for beat in (1.0, 3.0):
                if beat < beats_per_bar:
                    hit(role_pitches["snare"], base + beat, 0.86)
            division = 0.5 if style == "hiphop" else 0.25
            for step in range(int(beats_per_bar / division)):
                if style == "trap" and rng.random() < 0.12:
                    continue
                hit(
                    role_pitches["closed_hat"],
                    base + step * division,
                    0.48 + rng.uniform(-0.06, 0.12),
                    0.08,
                )
            if style == "trap" and rng.random() < 0.65:
                for offset in (3.5, 3.75, 3.875):
                    if offset < beats_per_bar:
                        hit(role_pitches["closed_hat"], base + offset, 0.56, 0.05)
        else:  # dnb
            for beat in (0.0, 2.75):
                if beat < beats_per_bar:
                    hit(role_pitches["kick"], base + beat, 0.96)
            for beat in (1.0, 3.0):
                if beat < beats_per_bar:
                    hit(role_pitches["snare"], base + beat, 0.90)
            for step in range(beats_per_bar * 4):
                if rng.random() > 0.12:
                    hit(
                        role_pitches["closed_hat"],
                        base + step * 0.25,
                        0.45 + rng.uniform(-0.08, 0.12),
                        0.07,
                    )
    warnings = () if drum_map is not None else (
        "Uses General MIDI drum note numbers; map them to the loaded drum instrument as needed.",
    )
    return make_sequence(
        name=f"{style} drums",
        generator="drums",
        notes=notes,
        tempo_bpm=tempo_bpm,
        numerator=beats_per_bar,
        seed=seed,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Type-1 Standard MIDI File export with byte-for-byte readback
# ---------------------------------------------------------------------------


class MidiTrackSpec(CreativeModel):
    name: str = Field(min_length=1, max_length=80)
    channel: int = Field(default=0, ge=0, le=15)
    notes: list[CreativeNote] = Field(min_length=1, max_length=MAX_CREATIVE_NOTES)


class MidiExportReceipt(CreativeModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    exported_at: datetime
    path: str
    midi_format: Literal[1] = 1
    ppq: int = Field(ge=24, le=9600)
    tempo_bpm: float = Field(ge=10.0, le=522.0)
    track_count: int = Field(ge=1, le=MAX_MIDI_TRACKS + 1)
    musical_track_count: int = Field(ge=1, le=MAX_MIDI_TRACKS)
    note_count: int = Field(ge=1)
    byte_count: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    readback_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    header_verified: bool
    note_events_verified: bool
    verified: bool
    overwritten_existing_file: bool
    atomic_replace_used: Literal[True] = True


def _vlq(value: int) -> bytes:
    if value < 0 or value > 0x0FFFFFFF:
        raise ValueError("MIDI variable-length quantity is outside its four-byte range")
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(out))


def _meta(kind: int, payload: bytes) -> bytes:
    return bytes((0xFF, kind)) + _vlq(len(payload)) + payload


def _track_chunk(events: Sequence[tuple[int, int, bytes]]) -> bytes:
    body = bytearray()
    previous = 0
    for absolute, order, event in sorted(
        events, key=lambda item: (item[0], item[1], item[2])
    ):
        if absolute < previous:
            raise ValueError("MIDI events are not monotonic")
        body.extend(_vlq(absolute - previous))
        body.extend(event)
        previous = absolute
    body.extend(b"\x00\xff\x2f\x00")
    return b"MTrk" + struct.pack(">I", len(body)) + bytes(body)


def _midi_bytes(
    tracks: Sequence[MidiTrackSpec],
    *,
    tempo_bpm: float,
    ppq: int,
    numerator: int,
    denominator: int,
) -> tuple[bytes, int]:
    if not 1 <= len(tracks) <= MAX_MIDI_TRACKS:
        raise ValueError(f"tracks must contain 1..{MAX_MIDI_TRACKS} musical tracks")
    if not 24 <= ppq <= 9600:
        raise ValueError("ppq must be within 24..9600")
    if not 10.0 <= tempo_bpm <= 522.0:
        raise ValueError("tempo_bpm must be within 10..522")
    if not 1 <= numerator <= 32 or denominator not in {1, 2, 4, 8, 16, 32}:
        raise ValueError("invalid MIDI time signature")
    micros = int(round(60_000_000.0 / tempo_bpm))
    if not 1 <= micros <= 0xFFFFFF:
        raise ValueError("tempo cannot be represented by a MIDI set-tempo event")
    conductor_events = [
        (0, 0, _meta(0x03, b"Postfader Conductor")),
        (0, 1, _meta(0x51, micros.to_bytes(3, "big"))),
        (0, 2, _meta(0x58, bytes((numerator, int(math.log2(denominator)), 24, 8)))),
    ]
    chunks = [_track_chunk(conductor_events)]
    note_count = 0
    for spec in tracks:
        name_bytes = spec.name.encode("utf-8")
        events: list[tuple[int, int, bytes]] = [(0, 0, _meta(0x03, name_bytes))]
        for note in spec.notes:
            if note.pitch > 127:
                raise ValueError(
                    f"track {spec.name!r} contains FL-only pitch {note.pitch}; MIDI permits 0..127"
                )
            start = int(round(note.start_beats * ppq))
            duration = max(1, int(round(note.duration_beats * ppq)))
            end = start + duration
            velocity = max(1, min(127, int(round(note.velocity * 127.0))))
            events.append(
                (start, 2, bytes((0x90 | spec.channel, note.pitch, velocity)))
            )
            events.append((end, 1, bytes((0x80 | spec.channel, note.pitch, 0))))
            note_count += 1
        chunks.append(_track_chunk(events))
    header = b"MThd" + struct.pack(">IHHH", 6, 1, len(chunks), ppq)
    return header + b"".join(chunks), note_count


def _read_vlq(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if offset >= len(data):
            raise ValueError("truncated MIDI variable-length value")
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, offset
    raise ValueError("MIDI variable-length value exceeds four bytes")


def _inspect_midi(data: bytes) -> tuple[int, int, int, int]:
    if len(data) < 14 or data[:4] != b"MThd" or struct.unpack(">I", data[4:8])[0] != 6:
        raise ValueError("written file lacks a valid MIDI header")
    midi_format, track_count, ppq = struct.unpack(">HHH", data[8:14])
    offset = 14
    note_ons = 0
    parsed_tracks = 0
    for _ in range(track_count):
        if data[offset : offset + 4] != b"MTrk" or offset + 8 > len(data):
            raise ValueError("written file has a malformed MIDI track chunk")
        length = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
        pos = offset + 8
        end = pos + length
        if end > len(data):
            raise ValueError("written file has a truncated MIDI track chunk")
        running: int | None = None
        while pos < end:
            _delta, pos = _read_vlq(data, pos)
            status = data[pos]
            if status < 0x80:
                if running is None:
                    raise ValueError("invalid MIDI running status")
                status = running
            else:
                pos += 1
                if status < 0xF0:
                    running = status
            if status == 0xFF:
                running = None
                if pos >= end:
                    raise ValueError("truncated MIDI meta event")
                pos += 1
                size, pos = _read_vlq(data, pos)
                pos += size
            elif status in (0xF0, 0xF7):
                running = None
                size, pos = _read_vlq(data, pos)
                pos += size
            else:
                kind = status & 0xF0
                width = 1 if kind in (0xC0, 0xD0) else 2
                if pos + width > end:
                    raise ValueError("truncated MIDI channel event")
                if kind == 0x90 and data[pos + 1] > 0:
                    note_ons += 1
                pos += width
        if pos != end:
            raise ValueError("MIDI parser did not end on the track boundary")
        offset = end
        parsed_tracks += 1
    if offset != len(data):
        raise ValueError("written MIDI file has trailing bytes")
    return midi_format, parsed_tracks, ppq, note_ons


def export_type1_midi(
    path: str,
    tracks: Sequence[MidiTrackSpec],
    *,
    tempo_bpm: float = 120.0,
    ppq: int = 480,
    numerator: int = 4,
    denominator: int = 4,
    overwrite: bool = False,
) -> MidiExportReceipt:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty absolute .mid path")
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        raise ValueError("MIDI output path must be absolute")
    target = Path(os.path.realpath(expanded))
    if target.suffix.lower() not in {".mid", ".midi"}:
        raise ValueError("MIDI output path must end in .mid or .midi")
    if not target.parent.is_dir():
        raise ValueError("MIDI output parent directory does not exist")
    initially_existed = target.exists()
    if initially_existed and not overwrite:
        raise FileExistsError(
            "MIDI output already exists; pass overwrite=true explicitly"
        )
    if initially_existed and not target.is_file():
        raise ValueError("MIDI output target exists but is not a regular file")
    data, expected_notes = _midi_bytes(
        tracks,
        tempo_bpm=tempo_bpm,
        ppq=ppq,
        numerator=numerator,
        denominator=denominator,
    )
    digest = hashlib.sha256(data).hexdigest()
    with _MIDI_EXPORT_LOCK:
        # Re-check after rendering: another request may have created the
        # destination while this request was building its bytes.
        existed = target.exists()
        if existed and not overwrite:
            raise FileExistsError(
                "MIDI output already exists; pass overwrite=true explicitly"
            )
        if existed and not target.is_file():
            raise ValueError("MIDI output target exists but is not a regular file")

        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".postfader-midi-",
                suffix=".tmp",
                dir=target.parent,
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if overwrite:
                os.replace(temporary_name, target)
            else:
                # A hard-link creation is an atomic no-replace commit on both
                # POSIX and Windows filesystems supported by Python. Unlike a
                # check followed by os.replace, it cannot clobber a target
                # created by another process after the re-check above.
                try:
                    os.link(temporary_name, target)
                except FileExistsError as exc:
                    raise FileExistsError(
                        "MIDI output already exists; pass overwrite=true explicitly"
                    ) from exc
                except OSError as exc:
                    raise OSError(
                        "could not atomically create the MIDI output; the target "
                        "filesystem must support same-directory hard links"
                    ) from exc
                os.unlink(temporary_name)
            temporary_name = None
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
        readback = target.read_bytes()
    readback_digest = hashlib.sha256(readback).hexdigest()
    midi_format, track_count, observed_ppq, observed_notes = _inspect_midi(readback)
    header_verified = (
        midi_format == 1 and track_count == len(tracks) + 1 and observed_ppq == ppq
    )
    note_events_verified = observed_notes == expected_notes
    return MidiExportReceipt(
        exported_at=_now(),
        path=str(target),
        ppq=ppq,
        tempo_bpm=tempo_bpm,
        track_count=track_count,
        musical_track_count=len(tracks),
        note_count=expected_notes,
        byte_count=len(readback),
        sha256=digest,
        readback_sha256=readback_digest,
        header_verified=header_verified,
        note_events_verified=note_events_verified,
        verified=(
            digest == readback_digest and header_verified and note_events_verified
        ),
        overwritten_existing_file=existed,
    )


# ---------------------------------------------------------------------------
# Piano Roll generated-script bridge
# ---------------------------------------------------------------------------


class PianoRollBridgeStatus(CreativeModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    platform: Literal["macos", "windows", "unsupported"]
    scripts_directory: str
    script_path: str
    script_exists: bool
    shortcut: str
    automatic_trigger_supported: bool
    prepared_this_session: bool
    armed_this_session: bool
    last_request_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    last_requested_note_count: int | None = Field(default=None, ge=0)
    last_requested_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    last_operation: str | None = None
    authoritative_fl_note_readback_available: Literal[False] = False
    setup_instruction: str


class PianoRollTargetReceipt(CreativeModel):
    command: Literal["creative.prepare_piano_roll"]
    channel_index: int = Field(ge=0)
    target_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    pattern_number: int = Field(ge=1, le=999)
    before_channel_indices: list[int]
    after_channel_indices: list[int]
    before_pattern_number: int = Field(ge=1)
    after_pattern_number: int = Field(ge=1)
    piano_roll_visible_before: bool | None = None
    piano_roll_visible_after: bool | None = None
    selected_target_verified: bool
    piano_roll_visibility_verified: bool | None = None
    session_fingerprint: str = Field(pattern=r"^[0-9a-f]{32}$")
    session_precondition_applied: Literal[True] = True
    project_saved: Literal[False] = False


class HotkeyDispatch(CreativeModel):
    platform: Literal["macos", "windows", "unsupported"]
    shortcut: str
    fl_window_found: bool
    fl_window_focused: bool
    hotkey_dispatched: bool
    error: str | None = Field(default=None, max_length=512)


class PianoRollScriptRuntimeEvidence(CreativeModel):
    """Bounded evidence emitted from FL's isolated Piano Roll script runtime."""

    evidence_scope: Literal["fl_piano_roll_script_runtime"] = (
        "fl_piano_roll_script_runtime"
    )
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    operation: Literal["write_notes"]
    mode: Literal["append", "replace"]
    requested_note_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_added_note_count: int = Field(ge=1, le=MAX_PIANO_ROLL_NOTES)
    ppq: int = Field(ge=1, le=1_000_000)
    before_note_count: int | None = Field(default=None, ge=0, le=1_000_000)
    score_note_count: int = Field(ge=0, le=1_000_000)
    added_note_digest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    score_digest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    script_completed: bool
    postcondition_verified: bool
    error: str | None = Field(default=None, max_length=512)
    receipt_path: str
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_authentication_verified: Literal[True] = True
    persistence_check_completed: bool = False
    persistence_check_verified: bool = False
    verification_receipt_path: str | None = None
    verification_receipt_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def validate_persistence_evidence(self) -> "PianoRollScriptRuntimeEvidence":
        if self.persistence_check_verified and not self.persistence_check_completed:
            raise ValueError(
                "a verified persistence check must also be marked completed"
            )
        if bool(self.verification_receipt_path) != bool(
            self.verification_receipt_sha256
        ):
            raise ValueError(
                "persistence receipt path and digest must appear together"
            )
        return self


class _PianoRollPersistenceReceipt(CreativeModel):
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    operation: Literal["verify_write_notes"]
    ppq: int = Field(ge=1, le=1_000_000)
    expected_score_note_count: int = Field(ge=0, le=MAX_PIANO_ROLL_SCORE_EVIDENCE_NOTES)
    observed_score_note_count: int = Field(ge=0, le=1_000_000)
    expected_score_digest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_score_digest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    persistence_check_completed: bool
    persistence_check_verified: bool
    error: str | None = Field(default=None, max_length=512)
    receipt_path: str
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_authentication_verified: Literal[True] = True


class PianoRollDispatch(CreativeModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    requested_at: datetime
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    operation: str
    mode: str | None = None
    script_path: str
    script_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_note_count: int | None = Field(
        default=None, ge=0, le=MAX_PIANO_ROLL_NOTES
    )
    requested_note_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    target: PianoRollTargetReceipt | None = None
    trigger: HotkeyDispatch | None = None
    verification_target: PianoRollTargetReceipt | None = None
    verification_trigger: HotkeyDispatch | None = None
    verification_script_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    status: Literal[
        "prepared_for_manual_run",
        "hotkey_dispatched_unverified",
        "hotkey_not_dispatched",
        "script_runtime_verified",
    ]
    application_verified: bool = False
    script_runtime_evidence: PianoRollScriptRuntimeEvidence | None = None
    authoritative_note_readback_available: Literal[False] = False
    project_saved: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_runtime_verification(self) -> "PianoRollDispatch":
        evidence = self.script_runtime_evidence
        if evidence is not None and (
            evidence.request_id != self.request_id
            or evidence.operation != self.operation
            or evidence.mode != self.mode
            or evidence.requested_note_digest != self.requested_note_digest
            or evidence.expected_added_note_count != self.requested_note_count
        ):
            raise ValueError(
                "Piano Roll script-runtime evidence must match this dispatch"
            )
        verified = (
            self.status == "script_runtime_verified"
            and evidence is not None
            and evidence.script_completed
            and evidence.postcondition_verified
            and evidence.persistence_check_completed
            and evidence.persistence_check_verified
        )
        if self.application_verified != verified:
            raise ValueError(
                "application_verified must match authenticated Piano Roll "
                "script-runtime evidence"
            )
        return self


class PianoRollTransform(CreativeModel):
    operation: Literal[
        "quantize", "transpose", "humanize", "duplicate", "delete", "clear"
    ]
    scope: Literal["selected", "all"] = "selected"
    grid_beats: float | None = Field(default=None, gt=0.0, le=16.0)
    quantize_lengths: bool = False
    semitones: int | None = Field(default=None, ge=-96, le=96)
    timing_beats: float | None = Field(default=None, ge=0.0, le=2.0)
    velocity_amount: float | None = Field(default=None, ge=0.0, le=1.0)
    seed: int = 0
    offset_beats: float | None = Field(default=None, gt=0.0, le=MAX_SEQUENCE_BEATS)
    repeats: int = Field(default=1, ge=1, le=32)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> "PianoRollTransform":
        if self.operation == "quantize" and self.grid_beats is None:
            raise ValueError("quantize needs grid_beats")
        if self.operation == "transpose" and self.semitones is None:
            raise ValueError("transpose needs semitones")
        if (
            self.operation == "humanize"
            and self.timing_beats is None
            and self.velocity_amount is None
        ):
            raise ValueError("humanize needs timing_beats and/or velocity_amount")
        if self.operation == "duplicate" and self.offset_beats is None:
            raise ValueError("duplicate needs offset_beats")
        return self


def _platform_label() -> Literal["macos", "windows", "unsupported"]:
    if sys_platform := platform.system():
        if sys_platform == "Darwin":
            return "macos"
        if sys_platform == "Windows":
            return "windows"
    return "unsupported"


def piano_roll_scripts_directory() -> Path:
    override = os.environ.get(PIANO_ROLL_SCRIPTS_DIR_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError(
                f"{PIANO_ROLL_SCRIPTS_DIR_ENV} must be an absolute directory"
            )
        return path.resolve()
    return (
        fl_studio_user_data_dir() / "Settings" / "Piano roll scripts" / "Postfader"
    ).resolve()


def _atomic_text(path: Path, content: str) -> str:
    encoded = content.encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".postfader-pyscript-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
    return hashlib.sha256(encoded).hexdigest()


def _bootstrap_script(
    *, request_id: str, receipt_path: Path, receipt_secret: str
) -> str:
    return f"""# Script.Name = "Postfader Apply"
# Script.Category = "Postfader"
# AUTO-GENERATED. Run once from the Piano Roll Scripts menu, then use the
# run-last-script shortcut for subsequent PostFader requests.
import hashlib
import hmac
import json
import os
POSTFADER_BOOTSTRAP = True
REQUEST_ID = {request_id!r}
RECEIPT_PATH = {ascii(os.fspath(receipt_path))}
RECEIPT_SECRET = {receipt_secret!r}
payload = {{
    "request_id": REQUEST_ID,
    "operation": "arm_piano_roll_bridge",
    "script_loaded": True,
}}
canonical = json.dumps(
    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
).encode("ascii")
signature = hmac.new(
    bytes.fromhex(RECEIPT_SECRET), canonical, hashlib.sha256
).hexdigest()
encoded = json.dumps(
    {{"payload": payload, "hmac_sha256": signature}},
    sort_keys=True, separators=(",", ":"), ensure_ascii=True,
)
with open(RECEIPT_PATH, "xb") as handle:
    handle.write(encoded.encode("ascii"))
    handle.flush()
    os.fsync(handle.fileno())
"""


def _notes_script(
    notes: Sequence[CreativeNote],
    mode: Literal["append", "replace"],
    request_id: str,
    *,
    requested_note_digest: str,
    receipt_path: Path,
    receipt_secret: str,
) -> str:
    rows = [
        [
            note.pitch,
            note.start_beats,
            note.duration_beats,
            note.velocity,
            note.pan,
            note.release,
            note.color,
            note.pitch_offset_tenths,
            note.slide,
            note.portamento,
            note.muted,
            note.selected,
        ]
        for note in notes
    ]
    # This file is executed by FL's Python-based Piano Roll runtime.  JSON is
    # close to Python syntax, but its ``true``/``false``/``null`` literals are
    # invalid Python and fail before the script can dispatch any notes.
    literal = repr(rows)
    return f"""# Script.Name = "Postfader Apply"
# Script.Category = "Postfader"
# AUTO-GENERATED by PostFader. Request {request_id}.
import hashlib
import hmac
import json
import os
import flpianoroll as flp
MODE = {mode!r}
NOTES = {literal}
REQUEST_ID = {request_id!r}
REQUESTED_NOTE_DIGEST = {requested_note_digest!r}
RECEIPT_PATH = {ascii(os.fspath(receipt_path))}
RECEIPT_SECRET = {receipt_secret!r}
MAX_SCORE_NOTES = {MAX_PIANO_ROLL_SCORE_EVIDENCE_NOTES}
PIANO_ROLL_CONTROL_RESOLUTION = {PIANO_ROLL_CONTROL_RESOLUTION}

def _canonical_control(value):
    return round(float(value) * PIANO_ROLL_CONTROL_RESOLUTION) / PIANO_ROLL_CONTROL_RESOLUTION

def _signature(note):
    return (
        int(note.number), int(note.time), int(note.length),
        _canonical_control(note.velocity), _canonical_control(note.pan),
        _canonical_control(note.release), int(note.color), int(note.pitchofs),
        bool(note.slide), bool(note.porta), bool(note.muted),
    )

def _digest(signatures):
    encoded = json.dumps(
        sorted(signatures), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()

def _emit(payload):
    encoded_payload = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    signature = hmac.new(
        bytes.fromhex(RECEIPT_SECRET), encoded_payload, hashlib.sha256
    ).hexdigest()
    envelope = {{"payload": payload, "hmac_sha256": signature}}
    encoded = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    with open(RECEIPT_PATH, "xb") as handle:
        handle.write(encoded.encode("ascii"))
        handle.flush()
        os.fsync(handle.fileno())

def _run():
    score = flp.score
    before = []
    if MODE == "append":
        if score.noteCount > MAX_SCORE_NOTES - len(NOTES):
            _emit({{
                "request_id": REQUEST_ID,
                "operation": "write_notes",
                "mode": MODE,
                "requested_note_digest": REQUESTED_NOTE_DIGEST,
                "expected_added_note_count": len(NOTES),
                "ppq": int(score.PPQ),
                "before_note_count": int(score.noteCount),
                "score_note_count": int(score.noteCount),
                "added_note_digest_sha256": None,
                "score_digest_sha256": None,
                "script_completed": False,
                "postcondition_verified": False,
                "error": "The target score is too large for bounded append verification.",
            }})
            return
        before = [_signature(score.getNote(index)) for index in range(score.noteCount)]
    if MODE == "replace":
        try:
            score.clearNotes(True)
        except TypeError:
            score.clearNotes()
    added = []
    for row in NOTES:
        note = flp.Note()
        note.number = int(row[0])
        note.time = max(0, int(round(float(row[1]) * score.PPQ)))
        note.length = max(1, int(round(float(row[2]) * score.PPQ)))
        note.velocity = float(row[3])
        note.pan = float(row[4])
        note.release = float(row[5])
        note.color = int(row[6])
        note.pitchofs = int(row[7])
        note.slide = bool(row[8])
        note.porta = bool(row[9])
        note.muted = bool(row[10])
        note.selected = bool(row[11])
        added.append(_signature(note))
        score.addNote(note)
    after = [_signature(score.getNote(index)) for index in range(score.noteCount)]
    expected = added if MODE == "replace" else before + added
    verified = len(after) == len(expected) and sorted(after) == sorted(expected)
    _emit({{
        "request_id": REQUEST_ID,
        "operation": "write_notes",
        "mode": MODE,
        "requested_note_digest": REQUESTED_NOTE_DIGEST,
        "expected_added_note_count": len(NOTES),
        "ppq": int(score.PPQ),
        "before_note_count": len(before) if MODE == "append" else None,
        "score_note_count": len(after),
        "added_note_digest_sha256": _digest(added),
        "score_digest_sha256": _digest(after),
        "script_completed": True,
        "postcondition_verified": verified,
        "error": None if verified else "The post-apply score did not match the requested mutation.",
    }})

try:
    _run()
except Exception as error:
    try:
        count = int(flp.score.noteCount)
        _emit({{
            "request_id": REQUEST_ID,
            "operation": "write_notes",
            "mode": MODE,
            "requested_note_digest": REQUESTED_NOTE_DIGEST,
            "expected_added_note_count": len(NOTES),
            "ppq": int(flp.score.PPQ),
            "before_note_count": None,
            "score_note_count": count,
            "added_note_digest_sha256": None,
            "score_digest_sha256": None,
            "script_completed": False,
            "postcondition_verified": False,
            "error": (type(error).__name__ + ": " + str(error))[:512],
        }})
    finally:
        raise
"""


def _persistence_script(
    *,
    request_id: str,
    expected_score_note_count: int,
    expected_score_digest: str,
    receipt_path: Path,
    receipt_secret: str,
) -> str:
    return f"""# Script.Name = "Postfader Apply"
# Script.Category = "Postfader"
# AUTO-GENERATED read-only verifier for PostFader request {request_id}.
import hashlib
import hmac
import json
import os
import flpianoroll as flp
REQUEST_ID = {request_id!r}
EXPECTED_SCORE_NOTE_COUNT = {expected_score_note_count}
EXPECTED_SCORE_DIGEST = {expected_score_digest!r}
RECEIPT_PATH = {ascii(os.fspath(receipt_path))}
RECEIPT_SECRET = {receipt_secret!r}
MAX_SCORE_NOTES = {MAX_PIANO_ROLL_SCORE_EVIDENCE_NOTES}
PIANO_ROLL_CONTROL_RESOLUTION = {PIANO_ROLL_CONTROL_RESOLUTION}

def _canonical_control(value):
    return round(float(value) * PIANO_ROLL_CONTROL_RESOLUTION) / PIANO_ROLL_CONTROL_RESOLUTION

def _signature(note):
    return (
        int(note.number), int(note.time), int(note.length),
        _canonical_control(note.velocity), _canonical_control(note.pan),
        _canonical_control(note.release), int(note.color), int(note.pitchofs),
        bool(note.slide), bool(note.porta), bool(note.muted),
    )

def _digest(signatures):
    encoded = json.dumps(
        sorted(signatures), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()

def _emit(payload):
    encoded_payload = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    signature = hmac.new(
        bytes.fromhex(RECEIPT_SECRET), encoded_payload, hashlib.sha256
    ).hexdigest()
    envelope = {{"payload": payload, "hmac_sha256": signature}}
    encoded = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    with open(RECEIPT_PATH, "xb") as handle:
        handle.write(encoded.encode("ascii"))
        handle.flush()
        os.fsync(handle.fileno())

def _run():
    score = flp.score
    count = int(score.noteCount)
    if count > MAX_SCORE_NOTES:
        _emit({{
            "request_id": REQUEST_ID,
            "operation": "verify_write_notes",
            "ppq": int(score.PPQ),
            "expected_score_note_count": EXPECTED_SCORE_NOTE_COUNT,
            "observed_score_note_count": count,
            "expected_score_digest_sha256": EXPECTED_SCORE_DIGEST,
            "observed_score_digest_sha256": None,
            "persistence_check_completed": False,
            "persistence_check_verified": False,
            "error": "The target score is too large for bounded persistence verification.",
        }})
        return
    observed = [_signature(score.getNote(index)) for index in range(count)]
    observed_digest = _digest(observed)
    verified = (
        count == EXPECTED_SCORE_NOTE_COUNT
        and observed_digest == EXPECTED_SCORE_DIGEST
    )
    _emit({{
        "request_id": REQUEST_ID,
        "operation": "verify_write_notes",
        "ppq": int(score.PPQ),
        "expected_score_note_count": EXPECTED_SCORE_NOTE_COUNT,
        "observed_score_note_count": count,
        "expected_score_digest_sha256": EXPECTED_SCORE_DIGEST,
        "observed_score_digest_sha256": observed_digest,
        "persistence_check_completed": True,
        "persistence_check_verified": verified,
        "error": None if verified else "The persisted score does not match the apply receipt.",
    }})

try:
    _run()
except Exception as error:
    try:
        _emit({{
            "request_id": REQUEST_ID,
            "operation": "verify_write_notes",
            "ppq": int(flp.score.PPQ),
            "expected_score_note_count": EXPECTED_SCORE_NOTE_COUNT,
            "observed_score_note_count": int(flp.score.noteCount),
            "expected_score_digest_sha256": EXPECTED_SCORE_DIGEST,
            "observed_score_digest_sha256": None,
            "persistence_check_completed": False,
            "persistence_check_verified": False,
            "error": (type(error).__name__ + ": " + str(error))[:512],
        }})
    finally:
        raise
"""


def _piano_roll_receipt_path(
    script_path: Path,
    request_id: str,
    *,
    phase: Literal["arm", "apply", "verify"] = "apply",
) -> Path:
    directory = script_path.parent / ".postfader-acks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    receipts = sorted(
        (
            item
            for item in directory.glob("*.json")
            if len(item.name) >= 32
            and all(
                character in "0123456789abcdef" for character in item.name[:32]
            )
        ),
        key=lambda item: (item.stat().st_mtime_ns, item.name),
    )
    for stale in receipts[:-31]:
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    path = directory / f"{request_id}-{phase}.json"
    for candidate in (path, Path(os.fspath(path) + ".tmp")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
    return path


def _canonical_piano_roll_control(value: float) -> float:
    """Canonicalize a Piano Roll control to FL's persisted 1/128 grid."""

    return round(float(value) * PIANO_ROLL_CONTROL_RESOLUTION) / PIANO_ROLL_CONTROL_RESOLUTION


def _runtime_note_digest(
    notes: Sequence[CreativeNote], *, ppq: int
) -> str:
    signatures = [
        (
            int(note.pitch),
            max(0, int(round(float(note.start_beats) * ppq))),
            max(1, int(round(float(note.duration_beats) * ppq))),
            _canonical_piano_roll_control(note.velocity),
            _canonical_piano_roll_control(note.pan),
            _canonical_piano_roll_control(note.release),
            int(note.color),
            int(note.pitch_offset_tenths),
            bool(note.slide),
            bool(note.portamento),
            bool(note.muted),
        )
        for note in notes
    ]
    encoded = json.dumps(
        sorted(signatures),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _read_arm_receipt(
    path: Path, *, receipt_secret: str, request_id: str
) -> tuple[bool, str | None]:
    try:
        size = path.stat().st_size
        if size < 2 or size > MAX_PIANO_ROLL_RECEIPT_BYTES:
            raise ValueError("arming receipt size is outside its bounded range")
        encoded = path.read_bytes()
        if len(encoded) != size:
            raise ValueError("arming receipt changed while it was read")
        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate arming receipt key {key!r}")
                result[key] = value
            return result

        envelope = json.loads(
            encoded.decode("ascii"), object_pairs_hook=reject_duplicate_keys
        )
        if not isinstance(envelope, dict) or set(envelope) != {
            "payload",
            "hmac_sha256",
        }:
            raise ValueError("arming receipt envelope fields are invalid")
        payload = envelope["payload"]
        signature = envelope["hmac_sha256"]
        if not isinstance(payload, dict) or set(payload) != {
            "request_id",
            "operation",
            "script_loaded",
        }:
            raise ValueError("arming receipt payload fields are invalid")
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        expected_signature = hmac.new(
            bytes.fromhex(receipt_secret), canonical, hashlib.sha256
        ).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(
            signature, expected_signature
        ):
            raise ValueError("arming receipt authentication failed")
        if payload != {
            "request_id": request_id,
            "operation": "arm_piano_roll_bridge",
            "script_loaded": True,
        }:
            raise ValueError("arming receipt does not match this setup request")
        return True, None
    except Exception as exc:
        return False, f"Piano Roll arming proof was rejected: {exc}"[:512]


def _await_arm_receipt(
    path: Path, *, receipt_secret: str, request_id: str
) -> tuple[bool, str | None]:
    deadline = time.monotonic() + PIANO_ROLL_RECEIPT_WAIT_SECONDS
    last_error: str | None = None
    while True:
        if path.is_file():
            verified, error = _read_arm_receipt(
                path, receipt_secret=receipt_secret, request_id=request_id
            )
            if verified:
                return True, None
            if error is not None:
                last_error = error
        if time.monotonic() >= deadline:
            return (
                False,
                last_error or "FL did not emit Piano Roll script arming proof.",
            )
        time.sleep(0.025)


def _read_piano_roll_receipt(
    path: Path,
    *,
    receipt_secret: str,
    request_id: str,
    requested_note_digest: str,
    notes: Sequence[CreativeNote],
    mode: Literal["append", "replace"],
) -> tuple[PianoRollScriptRuntimeEvidence | None, str | None]:
    try:
        size = path.stat().st_size
        if size < 2 or size > MAX_PIANO_ROLL_RECEIPT_BYTES:
            raise ValueError("receipt size is outside its bounded range")
        encoded = path.read_bytes()
        if len(encoded) != size:
            raise ValueError("receipt changed while it was read")

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate receipt key {key!r}")
                result[key] = value
            return result

        envelope = json.loads(
            encoded.decode("ascii"), object_pairs_hook=reject_duplicate_keys
        )
        if not isinstance(envelope, dict) or set(envelope) != {
            "payload",
            "hmac_sha256",
        }:
            raise ValueError("receipt envelope fields are invalid")
        payload = envelope["payload"]
        signature = envelope["hmac_sha256"]
        if not isinstance(payload, dict) or not isinstance(signature, str):
            raise ValueError("receipt envelope types are invalid")
        expected_payload_fields = {
            "request_id",
            "operation",
            "mode",
            "requested_note_digest",
            "expected_added_note_count",
            "ppq",
            "before_note_count",
            "score_note_count",
            "added_note_digest_sha256",
            "score_digest_sha256",
            "script_completed",
            "postcondition_verified",
            "error",
        }
        if set(payload) != expected_payload_fields:
            raise ValueError("receipt payload fields are invalid")
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        expected_signature = hmac.new(
            bytes.fromhex(receipt_secret), canonical, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError("receipt authentication failed")
        if payload["request_id"] != request_id:
            raise ValueError("receipt request id does not match")
        if payload["operation"] != "write_notes" or payload["mode"] != mode:
            raise ValueError("receipt operation does not match")
        if payload["requested_note_digest"] != requested_note_digest:
            raise ValueError("receipt request digest does not match")
        if payload["expected_added_note_count"] != len(notes):
            raise ValueError("receipt requested note count does not match")
        ppq = payload["ppq"]
        if type(ppq) is not int or not 1 <= ppq <= 1_000_000:
            raise ValueError("receipt PPQ is outside its bounded range")
        runtime_digest = _runtime_note_digest(notes, ppq=ppq)
        if (
            payload["script_completed"]
            and payload["added_note_digest_sha256"] != runtime_digest
        ):
            raise ValueError("receipt applied-note rows do not match the request")
        if payload["postcondition_verified"]:
            before_count = payload["before_note_count"]
            score_count = payload["score_note_count"]
            if mode == "replace":
                counts_match = before_count is None and score_count == len(notes)
            else:
                counts_match = (
                    type(before_count) is int
                    and score_count == before_count + len(notes)
                )
            if not counts_match:
                raise ValueError("receipt score counts do not match the operation")
            if payload["error"] is not None:
                raise ValueError("verified receipt contains an error")
        evidence = PianoRollScriptRuntimeEvidence.model_validate(
            {
                **payload,
                "receipt_path": os.fspath(path),
                "receipt_sha256": hashlib.sha256(encoded).hexdigest(),
                "receipt_authentication_verified": True,
            }
        )
        return evidence, None
    except Exception as exc:
        return None, f"Piano Roll script receipt was rejected: {exc}"[:512]


def _await_piano_roll_receipt(
    path: Path,
    *,
    receipt_secret: str,
    request_id: str,
    requested_note_digest: str,
    notes: Sequence[CreativeNote],
    mode: Literal["append", "replace"],
) -> tuple[PianoRollScriptRuntimeEvidence | None, str | None]:
    deadline = time.monotonic() + PIANO_ROLL_RECEIPT_WAIT_SECONDS
    last_error: str | None = None
    while True:
        if path.is_file():
            evidence, error = _read_piano_roll_receipt(
                path,
                receipt_secret=receipt_secret,
                request_id=request_id,
                requested_note_digest=requested_note_digest,
                notes=notes,
                mode=mode,
            )
            if evidence is not None:
                return evidence, None
            if error is not None:
                last_error = error
        if time.monotonic() >= deadline:
            return (
                None,
                last_error
                or "FL did not emit a Piano Roll script-runtime receipt.",
            )
        time.sleep(0.025)


def _read_persistence_receipt(
    path: Path,
    *,
    receipt_secret: str,
    request_id: str,
    expected_score_note_count: int,
    expected_score_digest: str,
    expected_ppq: int,
) -> tuple[_PianoRollPersistenceReceipt | None, str | None]:
    try:
        size = path.stat().st_size
        if size < 2 or size > MAX_PIANO_ROLL_RECEIPT_BYTES:
            raise ValueError("receipt size is outside its bounded range")
        encoded = path.read_bytes()
        if len(encoded) != size:
            raise ValueError("receipt changed while it was read")

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate receipt key {key!r}")
                result[key] = value
            return result

        envelope = json.loads(
            encoded.decode("ascii"), object_pairs_hook=reject_duplicate_keys
        )
        if not isinstance(envelope, dict) or set(envelope) != {
            "payload",
            "hmac_sha256",
        }:
            raise ValueError("receipt envelope fields are invalid")
        payload = envelope["payload"]
        signature = envelope["hmac_sha256"]
        if not isinstance(payload, dict) or not isinstance(signature, str):
            raise ValueError("receipt envelope types are invalid")
        if set(payload) != {
            "request_id",
            "operation",
            "ppq",
            "expected_score_note_count",
            "observed_score_note_count",
            "expected_score_digest_sha256",
            "observed_score_digest_sha256",
            "persistence_check_completed",
            "persistence_check_verified",
            "error",
        }:
            raise ValueError("receipt payload fields are invalid")
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        expected_signature = hmac.new(
            bytes.fromhex(receipt_secret), canonical, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError("receipt authentication failed")
        if (
            payload["request_id"] != request_id
            or payload["operation"] != "verify_write_notes"
        ):
            raise ValueError("receipt request identity does not match")
        if (
            payload["expected_score_note_count"] != expected_score_note_count
            or payload["expected_score_digest_sha256"] != expected_score_digest
            or payload["ppq"] != expected_ppq
        ):
            raise ValueError("receipt persistence precondition does not match")
        if payload["persistence_check_verified"]:
            if (
                not payload["persistence_check_completed"]
                or payload["observed_score_note_count"]
                != expected_score_note_count
                or payload["observed_score_digest_sha256"]
                != expected_score_digest
                or payload["error"] is not None
            ):
                raise ValueError("verified persistence receipt is inconsistent")
        receipt = _PianoRollPersistenceReceipt.model_validate(
            {
                **payload,
                "receipt_path": os.fspath(path),
                "receipt_sha256": hashlib.sha256(encoded).hexdigest(),
                "receipt_authentication_verified": True,
            }
        )
        return receipt, None
    except Exception as exc:
        return None, f"Piano Roll persistence receipt was rejected: {exc}"[:512]


def _await_persistence_receipt(
    path: Path,
    *,
    receipt_secret: str,
    request_id: str,
    expected_score_note_count: int,
    expected_score_digest: str,
    expected_ppq: int,
) -> tuple[_PianoRollPersistenceReceipt | None, str | None]:
    deadline = time.monotonic() + PIANO_ROLL_RECEIPT_WAIT_SECONDS
    last_error: str | None = None
    while True:
        if path.is_file():
            receipt, error = _read_persistence_receipt(
                path,
                receipt_secret=receipt_secret,
                request_id=request_id,
                expected_score_note_count=expected_score_note_count,
                expected_score_digest=expected_score_digest,
                expected_ppq=expected_ppq,
            )
            if receipt is not None:
                return receipt, None
            if error is not None:
                last_error = error
        if time.monotonic() >= deadline:
            return (
                None,
                last_error or "FL did not emit a Piano Roll persistence receipt.",
            )
        time.sleep(0.025)


def _transform_script(request: PianoRollTransform, request_id: str) -> str:
    payload = repr(request.model_dump(mode="python"))
    return f"""# Script.Name = "Postfader Apply"
# Script.Category = "Postfader"
# AUTO-GENERATED by PostFader. Request {request_id}.
import flpianoroll as flp
REQUEST = {payload}

def _selected(note):
    return REQUEST["scope"] == "all" or bool(note.selected)

def _rand(state):
    state[0] = (1103515245 * state[0] + 12345) & 0x7fffffff
    return state[0] / 2147483647.0

def _copy(source):
    target = flp.Note()
    for field in ("number", "time", "length", "group", "pan", "velocity", "release", "color", "fcut", "fres", "pitchofs", "slide", "porta", "muted", "selected"):
        try:
            setattr(target, field, getattr(source, field))
        except Exception:
            pass
    return target

def _run():
    score = flp.score
    operation = REQUEST["operation"]
    if operation == "clear":
        if REQUEST["scope"] == "all":
            try:
                score.clearNotes(True)
            except TypeError:
                score.clearNotes()
        else:
            for index in range(score.noteCount - 1, -1, -1):
                if bool(score.getNote(index).selected):
                    score.deleteNote(index)
        return
    if operation == "delete":
        for index in range(score.noteCount - 1, -1, -1):
            note = score.getNote(index)
            if _selected(note):
                score.deleteNote(index)
        return
    if operation == "duplicate":
        originals = []
        for index in range(score.noteCount):
            note = score.getNote(index)
            if _selected(note):
                originals.append(_copy(note))
        offset = max(1, int(round(float(REQUEST["offset_beats"]) * score.PPQ)))
        for repeat in range(1, int(REQUEST["repeats"]) + 1):
            for source in originals:
                clone = _copy(source)
                clone.time = source.time + offset * repeat
                score.addNote(clone)
        return
    state = [int(REQUEST["seed"]) & 0x7fffffff]
    delete_indices = []
    for index in range(score.noteCount):
        note = score.getNote(index)
        if not _selected(note):
            continue
        if operation == "quantize":
            grid = max(1, int(round(float(REQUEST["grid_beats"]) * score.PPQ)))
            note.time = max(0, int(round(note.time / grid) * grid))
            if bool(REQUEST["quantize_lengths"]):
                note.length = max(grid, int(round(note.length / grid) * grid))
        elif operation == "transpose":
            note.number += int(REQUEST["semitones"])
            if note.number < 0 or note.number > 131:
                delete_indices.append(index)
        elif operation == "humanize":
            timing = REQUEST.get("timing_beats")
            velocity = REQUEST.get("velocity_amount")
            if timing is not None:
                delta = int(round((_rand(state) * 2.0 - 1.0) * float(timing) * score.PPQ))
                note.time = max(0, note.time + delta)
            if velocity is not None:
                note.velocity = max(0.0, min(1.0, note.velocity + (_rand(state) * 2.0 - 1.0) * float(velocity)))
    for index in reversed(delete_indices):
        score.deleteNote(index)

_run()
"""


class _PianoRollRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._prepared = False
        self._armed = False
        self._last_request_id: str | None = None
        self._last_count: int | None = None
        self._last_digest: str | None = None
        self._last_operation: str | None = None
        self._arm_request_id: str | None = None
        self._arm_receipt_secret: str | None = None
        self._arm_receipt_path: Path | None = None

    def status(self) -> PianoRollBridgeStatus:
        path = piano_roll_scripts_directory() / PIANO_ROLL_SCRIPT_NAME
        kind = _platform_label()
        with self._lock:
            return PianoRollBridgeStatus(
                observed_at=_now(),
                platform=kind,
                scripts_directory=str(path.parent),
                script_path=str(path),
                script_exists=path.is_file(),
                shortcut="Cmd+Opt+Y" if kind == "macos" else "Ctrl+Alt+Y",
                automatic_trigger_supported=kind in {"macos", "windows"},
                prepared_this_session=self._prepared,
                armed_this_session=self._armed,
                last_request_id=self._last_request_id,
                last_requested_note_count=self._last_count,
                last_requested_digest=self._last_digest,
                last_operation=self._last_operation,
                setup_instruction=(
                    "After prepare, open any Piano Roll and run Scripts > Postfader > "
                    "Postfader Apply once. Then call this tool with action='confirm' "
                    "and confirm_user_ran_script=true. Arming lasts for this MCP process."
                ),
            )

    def bridge_action(
        self,
        action: Literal["status", "prepare", "confirm"],
        *,
        confirm_user_ran_script: bool = False,
    ) -> PianoRollBridgeStatus:
        if action not in {"status", "prepare", "confirm"}:
            raise ValueError("action must be status, prepare, or confirm")
        if action == "status":
            if confirm_user_ran_script:
                raise ValueError(
                    "confirm_user_ran_script is valid only for action='confirm'"
                )
            return self.status()
        # Setup mutates the same fixed script/armed state used by dispatch.
        # Always acquire the dispatch lock first, matching note/transform lock
        # order, so prepare cannot replace a committed script before its hotkey.
        with _PIANO_ROLL_DISPATCH_LOCK:
            with self._lock:
                if action == "prepare":
                    path = piano_roll_scripts_directory() / PIANO_ROLL_SCRIPT_NAME
                    request_id = os.urandom(16).hex()
                    receipt_secret = os.urandom(32).hex()
                    receipt_path = _piano_roll_receipt_path(
                        path, request_id, phase="arm"
                    )
                    script = _bootstrap_script(
                        request_id=request_id,
                        receipt_path=receipt_path,
                        receipt_secret=receipt_secret,
                    )
                    _atomic_text(path, script)
                    self._prepared = True
                    self._armed = False
                    self._arm_request_id = request_id
                    self._arm_receipt_secret = receipt_secret
                    self._arm_receipt_path = receipt_path
                else:
                    if not confirm_user_ran_script:
                        raise ValueError(
                            "confirm requires confirm_user_ran_script=true"
                        )
                    path = piano_roll_scripts_directory() / PIANO_ROLL_SCRIPT_NAME
                    if not self._prepared or not path.is_file():
                        raise ValueError(
                            "prepare the PostFader Piano Roll script before confirming it"
                        )
                    if (
                        self._arm_request_id is None
                        or self._arm_receipt_secret is None
                        or self._arm_receipt_path is None
                    ):
                        raise ValueError(
                            "prepare a fresh PostFader Piano Roll arming request"
                        )
                    verified, error = _await_arm_receipt(
                        self._arm_receipt_path,
                        receipt_secret=self._arm_receipt_secret,
                        request_id=self._arm_request_id,
                    )
                    if not verified:
                        raise ValueError(
                            error
                            or "FL did not return Piano Roll script arming proof"
                        )
                    self._armed = True
            return self.status()

    def require_armed(self) -> None:
        with self._lock:
            if not self._armed:
                raise ValueError(
                    "Piano Roll auto-trigger is not armed for this MCP session; call "
                    "piano_roll_bridge(action='prepare'), run the script once in FL, "
                    "then call piano_roll_bridge(action='confirm', confirm_user_ran_script=true)"
                )

    def record(
        self,
        *,
        request_id: str,
        operation: str,
        count: int | None,
        digest: str | None,
    ) -> None:
        with self._lock:
            self._last_request_id = request_id
            self._last_operation = operation
            self._last_count = count
            self._last_digest = digest


PIANO_ROLL = _PianoRollRegistry()


def _target_fingerprint_precondition(value: str | None) -> str | None:
    """Validate an optional target identity guard before any bridge call."""

    if value is None:
        return None
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(
            "target_fingerprint must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _target_piano_roll(
    channel_index: int,
    pattern_number: int,
    *,
    session_fingerprint: str | None = None,
    target_fingerprint: str | None = None,
) -> PianoRollTargetReceipt:
    if type(channel_index) is not int or channel_index < 0:
        raise ValueError("channel_index must be a non-negative global index")
    if type(pattern_number) is not int or not 1 <= pattern_number <= 999:
        raise ValueError("pattern_number must be within 1..999")
    expected_target = _target_fingerprint_precondition(target_fingerprint)
    client, _ping, session = _writable_preflight(
        session_fingerprint=session_fingerprint
    )
    arguments: dict[str, Any] = {
        "channel": channel_index,
        "pattern": pattern_number,
        "index_scope": "global",
        "session_fingerprint": session,
    }
    if expected_target is not None:
        arguments["target_fingerprint"] = expected_target
    raw = client.call("creative.prepare_piano_roll", **arguments)
    receipt = PianoRollTargetReceipt.model_validate(raw)
    if (
        expected_target is not None
        and receipt.target_fingerprint != expected_target
    ):
        raise ValueError(
            "Piano Roll target fingerprint proof did not match the requested target"
        )
    return receipt


def _trigger_piano_roll_shortcut() -> HotkeyDispatch:
    kind = _platform_label()
    if kind == "macos":
        script = """tell application "System Events"
  tell process "FL Studio"
    set frontmost to true
    delay 0.4
    keystroke "y" using {command down, option down}
  end tell
end tell"""
        try:
            completed = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception as exc:
            return HotkeyDispatch(
                platform=kind,
                shortcut="Cmd+Opt+Y",
                fl_window_found=False,
                fl_window_focused=False,
                hotkey_dispatched=False,
                error=f"{type(exc).__name__}: {exc}"[:512],
            )
        error = (completed.stderr or "").strip()[:512] or None
        return HotkeyDispatch(
            platform=kind,
            shortcut="Cmd+Opt+Y",
            fl_window_found=completed.returncode == 0,
            fl_window_focused=completed.returncode == 0,
            hotkey_dispatched=completed.returncode == 0,
            error=error if completed.returncode else None,
        )
    if kind == "windows":
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            found = {"window": 0}
            callback_type = ctypes.WINFUNCTYPE(
                ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
            )

            def inspect_window(window: int, _parameter: int) -> bool:
                if not user32.IsWindowVisible(window):
                    return True
                length = user32.GetWindowTextLengthW(window)
                if length <= 0:
                    return True
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(window, title, length + 1)
                if "FL Studio" in title.value:
                    found["window"] = window
                    return False
                return True

            callback = callback_type(inspect_window)
            user32.EnumWindows(callback, 0)
            window = found["window"]
            if not window:
                return HotkeyDispatch(
                    platform=kind,
                    shortcut="Ctrl+Alt+Y",
                    fl_window_found=False,
                    fl_window_focused=False,
                    hotkey_dispatched=False,
                    error="FL Studio main window was not found",
                )
            user32.ShowWindow(window, 9)
            user32.keybd_event(0x12, 0, 0, 0)
            user32.SetForegroundWindow(window)
            user32.keybd_event(0x12, 0, 0x0002, 0)
            time.sleep(0.3)
            focused = user32.GetForegroundWindow() == window
            if not focused:
                return HotkeyDispatch(
                    platform=kind,
                    shortcut="Ctrl+Alt+Y",
                    fl_window_found=True,
                    fl_window_focused=False,
                    hotkey_dispatched=False,
                    error="Windows did not grant FL Studio foreground focus",
                )
            for virtual_key in (0x11, 0x12, 0x59):
                user32.keybd_event(virtual_key, 0, 0, 0)
            for virtual_key in (0x59, 0x12, 0x11):
                user32.keybd_event(virtual_key, 0, 0x0002, 0)
            return HotkeyDispatch(
                platform=kind,
                shortcut="Ctrl+Alt+Y",
                fl_window_found=True,
                fl_window_focused=True,
                hotkey_dispatched=True,
            )
        except Exception as exc:
            return HotkeyDispatch(
                platform=kind,
                shortcut="Ctrl+Alt+Y",
                fl_window_found=False,
                fl_window_focused=False,
                hotkey_dispatched=False,
                error=f"{type(exc).__name__}: {exc}"[:512],
            )
    return HotkeyDispatch(
        platform="unsupported",
        shortcut="Ctrl+Alt+Y",
        fl_window_found=False,
        fl_window_focused=False,
        hotkey_dispatched=False,
        error="automatic Piano Roll triggering supports only macOS and Windows",
    )


def write_piano_roll_notes(
    notes: Sequence[CreativeNote],
    *,
    channel_index: int,
    pattern_number: int,
    mode: Literal["append", "replace"] = "append",
    auto_trigger: bool = True,
    session_fingerprint: str | None = None,
    target_fingerprint: str | None = None,
) -> PianoRollDispatch:
    if not 1 <= len(notes) <= MAX_PIANO_ROLL_NOTES:
        raise ValueError(f"notes must contain 1..{MAX_PIANO_ROLL_NOTES} entries")
    if mode not in {"append", "replace"}:
        raise ValueError("mode must be append or replace")
    expected_target = _target_fingerprint_precondition(target_fingerprint)
    request_id = os.urandom(16).hex()
    digest = note_digest(notes)
    receipt_secret = os.urandom(32).hex()
    with _PIANO_ROLL_DISPATCH_LOCK:
        path = piano_roll_scripts_directory() / PIANO_ROLL_SCRIPT_NAME
        receipt_path = _piano_roll_receipt_path(path, request_id)
        script = _notes_script(
            notes,
            mode,
            request_id,
            requested_note_digest=digest,
            receipt_path=receipt_path,
            receipt_secret=receipt_secret,
        )
        target = None
        if auto_trigger:
            # Refuse before touching the generated integration file or FL
            # whenever the one-time setup handshake or live write preflight is
            # incomplete. Keep the target, script, registry, and hotkey as one
            # serialized operation so a later request cannot retarget FL while
            # this request's fixed script is being written or dispatched.
            PIANO_ROLL.require_armed()
            if session_fingerprint is None:
                if expected_target is None:
                    target = _target_piano_roll(channel_index, pattern_number)
                else:
                    target = _target_piano_roll(
                        channel_index,
                        pattern_number,
                        target_fingerprint=expected_target,
                    )
            else:
                if expected_target is None:
                    target = _target_piano_roll(
                        channel_index,
                        pattern_number,
                        session_fingerprint=session_fingerprint,
                    )
                else:
                    target = _target_piano_roll(
                        channel_index,
                        pattern_number,
                        session_fingerprint=session_fingerprint,
                        target_fingerprint=expected_target,
                    )
        elif session_fingerprint is not None:
            # Manual preparation still writes the integration script. When a
            # caller supplies a session guard, prove it before that file write
            # so a stale request cannot leave behind a script for another FL
            # session.
            _writable_preflight(session_fingerprint=session_fingerprint)
        script_digest = _atomic_text(path, script)
        PIANO_ROLL.record(
            request_id=request_id,
            operation="write_notes",
            count=len(notes),
            digest=digest,
        )
        if not auto_trigger:
            return PianoRollDispatch(
                requested_at=_now(),
                request_id=request_id,
                operation="write_notes",
                mode=mode,
                script_path=str(path),
                script_sha256=script_digest,
                requested_note_count=len(notes),
                requested_note_digest=digest,
                status="prepared_for_manual_run",
                warnings=[
                    "The generated script has not run. Open the intended Piano Roll and run Postfader Apply manually.",
                    "FL exposes no controller-API note readback; successful script application cannot be asserted here.",
                ],
            )
        trigger = _trigger_piano_roll_shortcut()
        evidence = None
        receipt_error = None
        verification_target = None
        verification_trigger = None
        verification_script_digest = None
        if trigger.hotkey_dispatched and path.is_file():
            evidence, receipt_error = _await_piano_roll_receipt(
                receipt_path,
                receipt_secret=receipt_secret,
                request_id=request_id,
                requested_note_digest=digest,
                notes=notes,
                mode=mode,
            )
        elif trigger.hotkey_dispatched:
            receipt_error = "The generated Piano Roll script file is unavailable."
        apply_verified = bool(
            evidence is not None
            and evidence.script_completed
            and evidence.postcondition_verified
        )
        if apply_verified and evidence is not None:
            try:
                expected_score_digest = evidence.score_digest_sha256
                if expected_score_digest is None:
                    raise ValueError("apply receipt omitted the score digest")
                verification_receipt_path = _piano_roll_receipt_path(
                    path, request_id, phase="verify"
                )
                verification_script = _persistence_script(
                    request_id=request_id,
                    expected_score_note_count=evidence.score_note_count,
                    expected_score_digest=expected_score_digest,
                    receipt_path=verification_receipt_path,
                    receipt_secret=receipt_secret,
                )
                verification_script_digest = _atomic_text(
                    path, verification_script
                )
                if session_fingerprint is None:
                    if expected_target is None:
                        verification_target = _target_piano_roll(
                            channel_index, pattern_number
                        )
                    else:
                        verification_target = _target_piano_roll(
                            channel_index,
                            pattern_number,
                            target_fingerprint=expected_target,
                        )
                elif expected_target is None:
                    verification_target = _target_piano_roll(
                        channel_index,
                        pattern_number,
                        session_fingerprint=session_fingerprint,
                    )
                else:
                    verification_target = _target_piano_roll(
                        channel_index,
                        pattern_number,
                        session_fingerprint=session_fingerprint,
                        target_fingerprint=expected_target,
                    )
                verification_trigger = _trigger_piano_roll_shortcut()
                if verification_trigger.hotkey_dispatched and path.is_file():
                    persistence, persistence_error = (
                        _await_persistence_receipt(
                            verification_receipt_path,
                            receipt_secret=receipt_secret,
                            request_id=request_id,
                            expected_score_note_count=evidence.score_note_count,
                            expected_score_digest=expected_score_digest,
                            expected_ppq=evidence.ppq,
                        )
                    )
                    if persistence is not None:
                        evidence = evidence.model_copy(
                            update={
                                "persistence_check_completed": (
                                    persistence.persistence_check_completed
                                ),
                                "persistence_check_verified": (
                                    persistence.persistence_check_verified
                                ),
                                "verification_receipt_path": (
                                    persistence.receipt_path
                                ),
                                "verification_receipt_sha256": (
                                    persistence.receipt_sha256
                                ),
                            }
                        )
                    receipt_error = persistence_error or (
                        persistence.error if persistence is not None else None
                    )
                else:
                    receipt_error = (
                        "The read-only Piano Roll persistence check was not dispatched."
                    )
            except Exception as exc:
                receipt_error = (
                    "The read-only Piano Roll persistence check failed: "
                    f"{type(exc).__name__}: {exc}"
                )[:512]
        verified = bool(
            evidence is not None
            and evidence.script_completed
            and evidence.postcondition_verified
            and evidence.persistence_check_completed
            and evidence.persistence_check_verified
        )
        if verified:
            status = "script_runtime_verified"
            warnings = [
                "FL's Piano Roll script runtime reported an authenticated exact score postcondition, then a second read-only invocation matched the persisted score.",
                "This is Piano Roll script-runtime readback, not controller-API note access.",
            ]
        else:
            status = (
                "hotkey_dispatched_unverified"
                if trigger.hotkey_dispatched
                else "hotkey_not_dispatched"
            )
            warnings = [
                "A dispatched shortcut alone proves only focus and key delivery, not note application.",
                receipt_error
                or (
                    evidence.error
                    if evidence is not None and evidence.error
                    else "The Piano Roll script-runtime postcondition was not verified."
                ),
                "Do not issue another Piano Roll mutation after an unverified result.",
            ]
        return PianoRollDispatch(
            requested_at=_now(),
            request_id=request_id,
            operation="write_notes",
            mode=mode,
            script_path=str(path),
            script_sha256=script_digest,
            requested_note_count=len(notes),
            requested_note_digest=digest,
            target=target,
            trigger=trigger,
            verification_target=verification_target,
            verification_trigger=verification_trigger,
            verification_script_sha256=verification_script_digest,
            status=status,
            application_verified=verified,
            script_runtime_evidence=evidence,
            warnings=warnings,
        )


def transform_piano_roll(
    request: PianoRollTransform,
    *,
    channel_index: int,
    pattern_number: int,
    auto_trigger: bool = True,
    session_fingerprint: str | None = None,
) -> PianoRollDispatch:
    request_id = os.urandom(16).hex()
    script = _transform_script(request, request_id)
    with _PIANO_ROLL_DISPATCH_LOCK:
        path = piano_roll_scripts_directory() / PIANO_ROLL_SCRIPT_NAME
        target = None
        if auto_trigger:
            # Keep a rejected live write side-effect free. Manual preparation is
            # intentionally file-mutating and is separately reported as such.
            # The target, fixed script, registry record, and hotkey remain one
            # serialized operation for the same reason as note writes above.
            PIANO_ROLL.require_armed()
            if session_fingerprint is None:
                target = _target_piano_roll(channel_index, pattern_number)
            else:
                target = _target_piano_roll(
                    channel_index,
                    pattern_number,
                    session_fingerprint=session_fingerprint,
                )
        elif session_fingerprint is not None:
            _writable_preflight(session_fingerprint=session_fingerprint)
        script_digest = _atomic_text(path, script)
        PIANO_ROLL.record(
            request_id=request_id,
            operation=request.operation,
            count=None,
            digest=None,
        )
        if not auto_trigger:
            return PianoRollDispatch(
                requested_at=_now(),
                request_id=request_id,
                operation=request.operation,
                mode=request.scope,
                script_path=str(path),
                script_sha256=script_digest,
                status="prepared_for_manual_run",
                warnings=[
                    "The transform script has not run; execute Postfader Apply in the intended Piano Roll.",
                    "The host cannot know the transformed note count because the controller API cannot read Piano Roll notes.",
                ],
            )
        trigger = _trigger_piano_roll_shortcut()
        return PianoRollDispatch(
            requested_at=_now(),
            request_id=request_id,
            operation=request.operation,
            mode=request.scope,
            script_path=str(path),
            script_sha256=script_digest,
            target=target,
            trigger=trigger,
            status="hotkey_dispatched_unverified"
            if trigger.hotkey_dispatched
            else "hotkey_not_dispatched",
            warnings=[
                "Transform execution is focus-sensitive and has no controller-API score readback.",
                "The generated script uses FL's live score object; inspect the Piano Roll before another mutation.",
            ],
        )


# ---------------------------------------------------------------------------
# Native arrangement and public REC-event automation helpers
# ---------------------------------------------------------------------------


class SectionMarker(CreativeModel):
    name: str = Field(min_length=1, max_length=64)
    bar_number: int = Field(ge=1, le=100000)
    beat_offset: float = Field(default=0.0, ge=0.0, le=31.999999)


class MarkerRequestEcho(CreativeModel):
    time_ticks: int = Field(ge=0, le=0x7FFFFFFF)
    name: str = Field(min_length=1, max_length=64)
    time_hint: str = Field(max_length=256)


class ArrangementMarkerReceipt(CreativeModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    applied_at: datetime
    command: Literal["arrangement.add_markers"]
    requested: list[MarkerRequestEcho] = Field(min_length=1, max_length=32)
    before_marker_names: list[str] = Field(max_length=512)
    after_marker_names: list[str] = Field(max_length=512)
    names_verified: bool
    times_verified: Literal[False] = False
    verification_status: Literal[
        "name_count_verified_time_unobservable",
        "marker_name_readback_unverified",
    ]
    verified: Literal[False] = False
    undo_point_created: bool | None = None
    ppq: int = Field(ge=1)
    pulses_per_bar: int = Field(ge=1)
    time_signature_numerator: int = Field(ge=1, le=32)
    session_fingerprint: str = Field(pattern=r"^[0-9a-f]{32}$")
    session_precondition_applied: Literal[True] = True
    project_saved: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


class AutomationRecordReceipt(CreativeModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    applied_at: datetime
    command: Literal["automation.record_value"]
    target_kind: Literal["mixer", "channel"]
    target_index: int = Field(ge=0)
    property: Literal["volume", "pan", "stereo_separation"]
    event_id: int
    controller_value: int = Field(ge=0)
    process_rec_event_result: int | None = None
    requested_normalized: float = Field(ge=0.0, le=1.0)
    before_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    after_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    control_value_verified: bool
    capture_conditions_held: bool
    song_position_before_ticks: int | None = None
    song_position_after_ticks: int | None = None
    automation_event_recorded: None = None
    automation_event_verification: Literal["unavailable_no_public_point_getter"] = (
        "unavailable_no_public_point_getter"
    )
    verified: Literal[False] = False
    undo_point_created: bool | None = None
    session_fingerprint: str = Field(pattern=r"^[0-9a-f]{32}$")
    session_precondition_applied: Literal[True] = True
    expected_before_applied: bool
    project_saved: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


class PatternPreparation(CreativeModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    applied_at: datetime
    pattern_number: int = Field(ge=1, le=999)
    pattern_was_reported_empty: Literal[True] = True
    selection: VerifiedPatternSelectionWrite
    identity: VerifiedPatternIdentityWrite | None = None
    length: VerifiedPatternLengthWrite | None = None
    outcome: Literal[
        "complete_verified",
        "selection_unverified",
        "identity_unverified",
        "length_unverified",
    ]
    verified: bool
    one_session_target: str = Field(pattern=r"^[0-9a-f]{32}$")
    automatic_replay_attempted: Literal[False] = False
    rollback_attempted: Literal[False] = False
    project_saved: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ordered_outcome(self) -> "PatternPreparation":
        if self.outcome == "selection_unverified":
            valid = (
                not self.selection.verified
                and self.identity is None
                and self.length is None
            )
        elif self.outcome == "identity_unverified":
            valid = (
                self.selection.verified
                and self.identity is not None
                and not self.identity.verified
                and self.length is None
            )
        elif self.outcome == "length_unverified":
            valid = (
                self.selection.verified
                and self.identity is not None
                and self.identity.verified
                and self.length is not None
                and not self.length.verified
            )
        else:
            valid = (
                self.selection.verified
                and self.identity is not None
                and self.identity.verified
                and self.length is not None
                and self.length.verified
            )
        if not valid:
            raise ValueError("outcome does not match the ordered write receipts")
        if self.verified != (self.outcome == "complete_verified"):
            raise ValueError("verified must be true only for complete_verified")
        return self


class _PinnedClient:
    """Reuse one preflight handshake without replaying any command."""

    def __init__(self, client: Any, ping: dict[str, Any]):
        self._client = client
        self._ping = dict(ping)

    @property
    def transport(self) -> str:
        return str(getattr(self._client, "transport", "unknown"))

    def ping(self) -> dict[str, Any]:
        return dict(self._ping)

    def call(self, command: str, **arguments: Any) -> dict[str, Any]:
        result = self._client.call(command, **arguments)
        if not isinstance(result, dict):
            raise ValueError(f"FL bridge returned a malformed {command} reply")
        return result


def _writable_preflight(
    *, session_fingerprint: str | None = None
) -> tuple[Any, dict[str, Any], str]:
    expected_session = _session_precondition(session_fingerprint)
    client = get_client()
    ping = client.ping()
    if not isinstance(ping, dict):
        raise ValueError("FL bridge returned a malformed creative-workflow handshake")
    connection = connection_from_ping(ping, getattr(client, "transport", "unknown"))
    if not connection.connected or not connection.compatible:
        raise IncompatibleFLStudio(connection.error or connection.compatibility_reason)
    if not connection.verified_writes_enabled:
        raise VerifiedWritesUnavailable(
            WRITES_DISABLED_HELP.format(
                mode=connection.bridge_mode,
                enabled=connection.verified_writes_enabled,
            )
        )
    if not connection.bridge_provenance_verified:
        raise VerifiedWritesUnavailable(
            PROVENANCE_REFUSAL.format(status=connection.bridge_provenance)
        )
    session = connection.session_fingerprint
    if session is None:
        raise VerifiedWritesUnavailable(
            "creative workflow requires a bridge session fingerprint"
        )
    if expected_session is not None and expected_session != session:
        raise VerifiedWritesUnavailable(
            "creative workflow session precondition failed before mutation; "
            "FL Studio reloaded the bridge or the project session changed"
        )
    return client, ping, session


def prepare_empty_pattern(
    *,
    name: str,
    length_beats: int = 16,
    color: int | None = None,
    start_pattern_number: int = 1,
    expected_pattern_number: int | None = None,
    session_fingerprint: str | None = None,
) -> PatternPreparation:
    if not isinstance(name, str) or not name or len(name) > 64:
        raise ValueError("name must contain 1..64 characters")
    if type(length_beats) is not int or not 1 <= length_beats <= 4096:
        raise ValueError("length_beats must be within 1..4096")
    if type(start_pattern_number) is not int or not 1 <= start_pattern_number <= 999:
        raise ValueError("start_pattern_number must be within 1..999")
    if expected_pattern_number is not None and (
        type(expected_pattern_number) is not int
        or not start_pattern_number <= expected_pattern_number <= 999
    ):
        raise ValueError(
            "expected_pattern_number must be within start_pattern_number..999"
        )
    if color is not None and (
        type(color) is not int or color < 0 or color > 0xFFFFFFFF
    ):
        raise ValueError("color must be an unsigned 32-bit FL color")
    client, ping, session = _writable_preflight(session_fingerprint=session_fingerprint)
    pinned = _PinnedClient(client, ping)
    inspector = TrackBInspector(TrackBReadGateway(pinned))
    found = inspector.find_empty_pattern(start_pattern_number=start_pattern_number)
    pattern = found.empty_pattern_number
    if pattern is None:
        raise ValueError("FL reported no empty pattern in the bounded search range")
    if expected_pattern_number is not None and pattern != expected_pattern_number:
        raise ValueError(
            f"pattern {expected_pattern_number} is no longer the first empty pattern "
            f"at or after {start_pattern_number}; FL reported pattern {pattern}"
        )
    controller = TrackBController(TrackBMutationGateway(pinned))
    selection = controller.select_pattern(
        pattern_number=pattern,
        session_fingerprint=session,
    )
    if not selection.verified:
        return PatternPreparation(
            applied_at=_now(),
            pattern_number=pattern,
            selection=selection,
            outcome="selection_unverified",
            verified=False,
            one_session_target=session,
            warnings=[
                "FL did not verify empty-pattern selection; identity and length were not attempted.",
                "The workflow is ordered and non-atomic; no rollback, replay, or save was attempted.",
            ],
        )
    identity = controller.set_pattern_identity(
        pattern_number=pattern,
        name=name,
        color=color,
        session_fingerprint=session,
    )
    if not identity.verified:
        return PatternPreparation(
            applied_at=_now(),
            pattern_number=pattern,
            selection=selection,
            identity=identity,
            outcome="identity_unverified",
            verified=False,
            one_session_target=session,
            warnings=[
                "FL verified selection but not pattern identity; length was not attempted.",
                "The workflow is ordered and non-atomic; no rollback, replay, or save was attempted.",
            ],
        )
    length = controller.set_pattern_length(
        pattern_number=pattern,
        length_beats=length_beats,
        session_fingerprint=session,
    )
    verified = length.verified
    return PatternPreparation(
        applied_at=_now(),
        pattern_number=pattern,
        selection=selection,
        identity=identity,
        length=length,
        outcome="complete_verified" if verified else "length_unverified",
        verified=verified,
        one_session_target=session,
        warnings=[
            "This workflow reuses an existing pattern FL reported as default/empty; it does not claim playlist clip placement.",
            "The three mutations are ordered, individually verified, and non-atomic; no rollback or save was attempted.",
        ],
    )


def add_section_markers(
    markers: Sequence[SectionMarker],
    *,
    session_fingerprint: str | None = None,
) -> ArrangementMarkerReceipt:
    if not 1 <= len(markers) <= 32:
        raise ValueError("markers must contain 1..32 section markers")
    client, _ping, session = _writable_preflight(
        session_fingerprint=session_fingerprint
    )
    project = client.call("project.info")
    if not isinstance(project, dict):
        raise ValueError("FL bridge returned a malformed project observation")
    ppq = project.get("ppq")
    pulses_per_bar = project.get("pulses_per_bar")
    numerator = project.get("time_signature_numerator")
    if (
        type(ppq) is not int
        or ppq < 1
        or type(pulses_per_bar) is not int
        or pulses_per_bar < 1
        or type(numerator) is not int
        or numerator < 1
        or numerator > 32
    ):
        raise ValueError("FL did not report a usable PPQ/pulses-per-bar snapshot")
    requests = []
    for marker in markers:
        if marker.beat_offset >= numerator:
            raise ValueError(
                f"marker {marker.name!r} beat_offset must be below the live {numerator}-beat bar"
            )
        ticks = int(
            round((marker.bar_number - 1) * pulses_per_bar + marker.beat_offset * ppq)
        )
        if ticks > 0x7FFFFFFF:
            raise ValueError(
                "section marker time exceeds FL's bounded integer timeline"
            )
        requests.append({"time_ticks": ticks, "name": marker.name})
    raw = client.call(
        "arrangement.add_markers",
        markers=requests,
        session_fingerprint=session,
    )
    if not isinstance(raw, dict):
        raise ValueError("FL bridge returned a malformed arrangement marker receipt")
    if raw.get("session_fingerprint") != session:
        raise ValueError("FL bridge session changed during arrangement marker write")
    return ArrangementMarkerReceipt.model_validate(
        {
            **raw,
            "applied_at": _now(),
            "ppq": ppq,
            "pulses_per_bar": pulses_per_bar,
            "time_signature_numerator": numerator,
            "warnings": [
                "Marker names are observed on a later idle tick, but Image-Line exposes no marker-time getter; requested times remain unverified.",
                "No automatic replay, rollback, or project save was attempted.",
            ],
        }
    )


def record_automation_value(
    *,
    target_kind: Literal["mixer", "channel"],
    target_index: int,
    property: Literal["volume", "pan", "stereo_separation"],
    value_normalized: float,
    allow_master: bool = False,
    expected_before: float | None = None,
    session_fingerprint: str | None = None,
) -> AutomationRecordReceipt:
    if target_kind not in {"mixer", "channel"}:
        raise ValueError("target_kind must be mixer or channel")
    if type(target_index) is not int or target_index < 0:
        raise ValueError("target_index must be a non-negative integer")
    if target_kind == "channel" and property == "stereo_separation":
        raise ValueError("channel automation supports volume and pan only")
    if property not in {"volume", "pan", "stereo_separation"}:
        raise ValueError("unsupported automation property")
    if isinstance(value_normalized, bool) or not isinstance(
        value_normalized, (int, float)
    ):
        raise ValueError("value_normalized must be numeric")
    value = float(value_normalized)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("value_normalized must be finite and within 0..1")
    if expected_before is not None and (
        isinstance(expected_before, bool)
        or not isinstance(expected_before, (int, float))
        or not math.isfinite(float(expected_before))
        or not 0.0 <= float(expected_before) <= 1.0
    ):
        raise ValueError(
            "expected_before must be null or a normalized number within 0..1"
        )
    client, _ping, session = _writable_preflight(
        session_fingerprint=session_fingerprint
    )
    arguments: dict[str, Any] = {
        "target_kind": target_kind,
        "target_index": target_index,
        "property": property,
        "value_normalized": value,
        "allow_master": allow_master,
        "session_fingerprint": session,
    }
    if target_kind == "channel":
        arguments["index_scope"] = "global"
    if expected_before is not None:
        arguments["expected_before"] = float(expected_before)
    raw = client.call("automation.record_value", **arguments)
    if not isinstance(raw, dict):
        raise ValueError("FL bridge returned a malformed automation receipt")
    if raw.get("session_fingerprint") != session:
        raise ValueError("FL bridge session changed during automation dispatch")
    return AutomationRecordReceipt.model_validate(
        {
            **raw,
            "applied_at": _now(),
            "warnings": [
                "The controlled value is later-tick verified, but FL exposes no public automation-point getter; event capture remains unverified.",
                "This command dispatches exactly once and requires playback plus recording to already be active.",
            ],
        }
    )
