#!/usr/bin/env python3
"""Generate deterministic, public-safe fixtures for Creation Review tests.

The audio is synthetic PCM assembled from sines, impulses, and deterministic
envelopes.  No recording, preset, project, or user path is embedded.  The
manifest records every hash and the deliberately introduced characteristics so
review tests can distinguish improvements from regressions without an FLP.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "creation_review"
SAMPLE_RATE = 16_000
BASE_SECONDS = 4
BASE_FRAMES = SAMPLE_RATE * BASE_SECONDS


def _pcm16(value: float) -> int:
    return max(-32768, min(32767, round(value * 32767.0)))


def _write(path: Path, frames: list[tuple[float, float]]) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        payload = bytearray()
        for left, right in frames:
            payload.extend(struct.pack("<hh", _pcm16(left), _pcm16(right)))
        output.writeframes(payload)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _song(
    gains: tuple[float, float, float, float],
    *,
    bright: float = 0.04,
    low_mid: float = 0.04,
    width: float = 0.12,
    clip_drive: float = 1.0,
) -> list[tuple[float, float]]:
    frames: list[tuple[float, float]] = []
    for index in range(BASE_FRAMES):
        time_s = index / SAMPLE_RATE
        section = min(3, index // SAMPLE_RATE)
        gain = gains[section]
        beat_age = index % (SAMPLE_RATE // 4)
        transient = 0.18 * max(0.0, 1.0 - beat_age / 48.0)
        body = (
            0.42 * math.sin(2.0 * math.pi * 110.0 * time_s)
            + low_mid * math.sin(2.0 * math.pi * 260.0 * time_s)
            + 0.09 * math.sin(2.0 * math.pi * 660.0 * time_s)
        )
        sheen_l = bright * math.sin(2.0 * math.pi * 5200.0 * time_s)
        sheen_r = bright * math.sin(2.0 * math.pi * 5200.0 * time_s + width)
        left = clip_drive * gain * (body + sheen_l + transient)
        right = clip_drive * gain * (body + sheen_r - transient * width)
        frames.append((max(-1.0, min(1.0, left)), max(-1.0, min(1.0, right))))
    return frames


def _stem(*, vocal: bool) -> list[tuple[float, float]]:
    frames: list[tuple[float, float]] = []
    frequency = 880.0 if vocal else 840.0
    for index in range(BASE_FRAMES):
        time_s = index / SAMPLE_RATE
        active = 1.0 if (index // (SAMPLE_RATE // 2)) % 2 == 0 else 0.35
        value = active * (
            0.24 * math.sin(2.0 * math.pi * frequency * time_s)
            + 0.12 * math.sin(2.0 * math.pi * 1760.0 * time_s)
        )
        if vocal:
            frames.append((value, value * 0.98))
        else:
            frames.append((value * 1.03, value))
    return frames


def _offset() -> list[tuple[float, float]]:
    delay = SAMPLE_RATE // 4
    baseline = _song((0.25, 0.32, 0.48, 0.44))
    return [(0.0, 0.0)] * delay + baseline[:-delay]


def _silence_tail() -> list[tuple[float, float]]:
    frames = _song((0.30, 0.38, 0.52, 0.0))
    tail_start = 3 * SAMPLE_RATE
    for index in range(tail_start, BASE_FRAMES):
        decay = math.exp(-6.0 * (index - tail_start) / SAMPLE_RATE)
        value = 0.18 * decay * math.sin(2.0 * math.pi * 440.0 * index / SAMPLE_RATE)
        frames[index] = (value, value * 0.8)
    return frames


def _structural_fixture() -> dict[str, object]:
    digest = "a" * 64
    return {
        "schema_version": "1.0",
        "source_run": {
            "run_id": "1" * 32,
            "status": "completed",
            "plan_digest": digest,
            "brief": "Create a synthetic four-section electronic draft.",
            "tempo_bpm": 120.0,
            "time_signature": [4, 4],
        },
        "sections": [
            {"section_id": "intro", "name": "Intro", "start_bar": 1, "end_bar": 9},
            {"section_id": "build", "name": "Build", "start_bar": 9, "end_bar": 17},
            {"section_id": "drop_a", "name": "Drop A", "start_bar": 17, "end_bar": 25},
            {"section_id": "drop_b", "name": "Drop B", "start_bar": 25, "end_bar": 33},
        ],
        "palette": {
            "palette_id": "synthetic-palette",
            "roles": ["main_chords", "main_lead", "primary_bass", "sub_bass", "drums"],
            "anchors": ["main_chords", "primary_bass", "sub_bass", "drums"],
        },
        "generated_outputs": [
            {"role_id": role, "sequence_digest": character * 64}
            for role, character in (
                ("main_chords", "b"),
                ("main_lead", "c"),
                ("primary_bass", "d"),
                ("sub_bass", "e"),
                ("drums", "f"),
            )
        ],
        "feedback": {
            "accepted": ["main_chords"],
            "rejected": ["main_lead"],
            "role_notes": {"primary_bass": "too soft"},
        },
        "revision": {
            "plan_id": "synthetic-revision-1",
            "operations": ["adjust_role_level", "change_sound_assignment"],
        },
        "comparison": {
            "improvements": ["build_to_drop_contrast", "primary_bass_level"],
            "regressions": ["stereo_correlation"],
        },
        "playlist_handoff": [
            {
                "pattern_number": index,
                "pattern_name": f"PF {name}",
                "start_bar": start,
                "end_bar": end,
                "playlist_track_number": index,
            }
            for index, name, start, end in (
                (1, "Intro", 1, 9),
                (2, "Build", 9, 17),
                (3, "Drop A", 17, 25),
                (4, "Drop B", 25, 33),
            )
        ],
    }


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    fixtures = {
        "clean_baseline.wav": _song((0.25, 0.34, 0.50, 0.46)),
        "clipped_candidate.wav": _song((0.45, 0.62, 0.88, 0.84), clip_drive=2.1),
        "overly_bright_candidate.wav": _song((0.25, 0.34, 0.50, 0.46), bright=0.32),
        "low_mid_heavy_candidate.wav": _song((0.25, 0.34, 0.50, 0.46), low_mid=0.48),
        "weak_drop_contrast.wav": _song((0.25, 0.31, 0.33, 0.32)),
        "improved_contrast_with_regression.wav": _song(
            (0.25, 0.32, 0.57, 0.53), width=2.9
        ),
        "masking_vocal_stem.wav": _stem(vocal=True),
        "masking_instrumental_stem.wav": _stem(vocal=False),
        "offset_candidate.wav": _offset(),
        "silence_tail.wav": _silence_tail(),
        "duration_mismatch.wav": _song((0.25, 0.34, 0.50, 0.46))[: 3 * SAMPLE_RATE],
    }
    paths: list[Path] = []
    for name, frames in fixtures.items():
        path = OUTPUT / name
        _write(path, frames)
        paths.append(path)

    structural_path = OUTPUT / "creation-review-structural-v1.json"
    structural_path.write_text(
        json.dumps(_structural_fixture(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "1.0",
        "generator": "scripts/generate_creation_review_fixtures.py",
        "sample_rate_hz": SAMPLE_RATE,
        "channels": 2,
        "sample_width_bytes": 2,
        "base_duration_seconds": BASE_SECONDS,
        "files": {
            path.name: {"bytes": path.stat().st_size, "sha256": _sha256(path)}
            for path in sorted(paths)
        },
        "structural_fixture": {
            "name": structural_path.name,
            "bytes": structural_path.stat().st_size,
            "sha256": _sha256(structural_path),
        },
        "known_properties": {
            "clipped_candidate.wav": ["sample_clipping"],
            "overly_bright_candidate.wav": ["increased_high_frequency_share"],
            "low_mid_heavy_candidate.wav": ["increased_low_mid_share"],
            "weak_drop_contrast.wav": ["weak_build_to_drop_energy_change"],
            "improved_contrast_with_regression.wav": [
                "stronger_build_to_drop_energy_change",
                "worse_stereo_correlation",
            ],
            "masking_vocal_stem.wav": ["synchronized_overlap_source"],
            "masking_instrumental_stem.wav": ["synchronized_overlap_source"],
            "offset_candidate.wav": ["quarter_second_lead_offset"],
            "silence_tail.wav": ["sustained_decay_tail"],
            "duration_mismatch.wav": ["one_second_shorter"],
        },
    }
    (OUTPUT / "fixture_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
