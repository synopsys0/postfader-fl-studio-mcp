#!/usr/bin/env python3
"""Integrity checks for the deterministic synthetic audio fixtures."""

from __future__ import annotations

import hashlib
import json
import unittest
import wave
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
AUDIO_FIXTURES = ROOT / "tests" / "fixtures" / "audio"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_pcm16(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2:
            raise AssertionError("fixture is not 16-bit PCM")
        channels = source.getnchannels()
        rate = source.getframerate()
        frames = source.getnframes()
        data = np.frombuffer(source.readframes(frames), dtype="<i2")
    return data.reshape(-1, channels).astype(np.int32), rate


class FixtureTests(unittest.TestCase):
    def manifest(self) -> dict:
        return json.loads(
            (AUDIO_FIXTURES / "fixture_manifest.json").read_text(encoding="utf-8")
        )

    def test_audio_hashes_and_container_metadata(self) -> None:
        manifest = self.manifest()
        self.assertEqual(manifest["generator"], "scripts/generate_audio_fixtures.py")
        self.assertEqual(
            {path.name for path in AUDIO_FIXTURES.iterdir()},
            {*manifest["files"], "fixture_manifest.json"},
        )
        for name, expected in manifest["files"].items():
            with self.subTest(fixture=name):
                path = AUDIO_FIXTURES / name
                self.assertEqual(sha256(path), expected["sha256"])
                self.assertEqual(path.stat().st_size, expected["bytes"])
                samples, rate = read_pcm16(path)
                self.assertEqual(rate, manifest["sample_rate_hz"])
                self.assertEqual(samples.shape, (manifest["frame_count"], 2))

    def test_candidate_has_declared_delay_and_gain(self) -> None:
        manifest = self.manifest()
        reference, _ = read_pcm16(AUDIO_FIXTURES / "reference_mix.wav")
        candidate, _ = read_pcm16(
            AUDIO_FIXTURES / "candidate_delayed_minus6db.wav"
        )
        delay = manifest["candidate_expected_delay_samples"]
        gain = 10.0 ** (manifest["candidate_expected_gain_db"] / 20.0)

        self.assertTrue(np.all(candidate[:delay] == 0))
        expected = np.rint(reference[:-delay] * gain).astype(np.int32)
        error = np.abs(candidate[delay:] - expected)
        self.assertLessEqual(int(np.max(error)), 1)

    def test_boundary_impulses_are_sample_exact(self) -> None:
        manifest = self.manifest()
        samples, _ = read_pcm16(AUDIO_FIXTURES / "boundary_impulses.wav")
        nonzero = np.flatnonzero(np.any(samples != 0, axis=1)).tolist()
        self.assertEqual(nonzero, manifest["boundary_impulse_sample_indices"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
