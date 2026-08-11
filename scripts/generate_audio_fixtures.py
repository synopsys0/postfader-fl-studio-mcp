#!/usr/bin/env python3
"""Generate deterministic PCM WAV fixtures used by render/analysis tests.

The fixtures contain no random data and are safe to regenerate.  Keeping the
generator beside the binaries makes every expected delay, gain, sample rate,
and boundary impulse auditable.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "audio"
SAMPLE_RATE = 48_000
FRAMES = SAMPLE_RATE * 4
DELAY_SAMPLES = 480
CANDIDATE_GAIN_DB = -6.0
CANDIDATE_GAIN = 10.0 ** (CANDIDATE_GAIN_DB / 20.0)


def _clamp_pcm16(value: float) -> int:
    return max(-32768, min(32767, round(value * 32767.0)))


def _write_stereo(path: Path, frames: list[tuple[float, float]]) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        payload = bytearray()
        for left, right in frames:
            payload.extend(struct.pack("<hh", _clamp_pcm16(left), _clamp_pcm16(right)))
        output.writeframes(payload)


def _reference_frames() -> list[tuple[float, float]]:
    frames: list[tuple[float, float]] = []
    transient_period = SAMPLE_RATE // 2
    transient_decay = max(1, SAMPLE_RATE // 500)
    for index in range(FRAMES):
        time_s = index / SAMPLE_RATE
        section_gain = 0.65 if time_s < 2.0 else 0.9
        transient_age = index % transient_period
        transient = 0.18 * max(0.0, 1.0 - transient_age / transient_decay)
        left = section_gain * (
            0.28 * math.sin(2.0 * math.pi * 220.0 * time_s)
            + 0.10 * math.sin(2.0 * math.pi * 880.0 * time_s)
        ) + transient
        right = section_gain * (
            0.26 * math.sin(2.0 * math.pi * 220.0 * time_s + 0.25)
            + 0.09 * math.sin(2.0 * math.pi * 1320.0 * time_s)
        ) - transient * 0.4
        frames.append((left, right))
    return frames


def _boundary_frames() -> list[tuple[float, float]]:
    frames = [(0.0, 0.0) for _ in range(FRAMES)]
    for index, amplitude in (
        (0, 0.75),
        (SAMPLE_RATE, 0.65),
        (2 * SAMPLE_RATE, 0.55),
        (3 * SAMPLE_RATE, 0.45),
        (FRAMES - 1, 0.35),
    ):
        frames[index] = (amplitude, -amplitude)
    return frames


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    reference = _reference_frames()
    delayed = [(0.0, 0.0)] * DELAY_SAMPLES + [
        (left * CANDIDATE_GAIN, right * CANDIDATE_GAIN)
        for left, right in reference[:-DELAY_SAMPLES]
    ]

    reference_path = OUTPUT / "reference_mix.wav"
    candidate_path = OUTPUT / "candidate_delayed_minus6db.wav"
    boundary_path = OUTPUT / "boundary_impulses.wav"
    _write_stereo(reference_path, reference)
    _write_stereo(candidate_path, delayed)
    _write_stereo(boundary_path, _boundary_frames())

    manifest = {
        "schema_version": "1.0",
        "generator": "scripts/generate_audio_fixtures.py",
        "sample_rate_hz": SAMPLE_RATE,
        "channels": 2,
        "sample_width_bytes": 2,
        "frame_count": FRAMES,
        "duration_seconds": FRAMES / SAMPLE_RATE,
        "candidate_expected_delay_samples": DELAY_SAMPLES,
        "candidate_expected_gain_db": CANDIDATE_GAIN_DB,
        "files": {
            path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
            for path in (reference_path, candidate_path, boundary_path)
        },
        "boundary_impulse_sample_indices": [
            0,
            SAMPLE_RATE,
            2 * SAMPLE_RATE,
            3 * SAMPLE_RATE,
            FRAMES - 1,
        ],
    }
    (OUTPUT / "fixture_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
