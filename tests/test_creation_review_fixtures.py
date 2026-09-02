"""Integrity and regeneration checks for public Creation Review fixtures."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "creation_review"
GENERATOR = ROOT / "scripts" / "generate_creation_review_fixtures.py"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class CreationReviewFixtureTests(unittest.TestCase):
    def manifest(self) -> dict[str, object]:
        return json.loads((FIXTURES / "fixture_manifest.json").read_text("utf-8"))

    def test_audio_files_match_pinned_hashes_and_container_shape(self) -> None:
        manifest = self.manifest()
        files = manifest["files"]
        self.assertIsInstance(files, dict)
        assert isinstance(files, dict)
        self.assertEqual(len(files), 11)
        for name, expected in files.items():
            with self.subTest(fixture=name):
                self.assertIsInstance(name, str)
                self.assertIsInstance(expected, dict)
                assert isinstance(name, str) and isinstance(expected, dict)
                path = FIXTURES / name
                self.assertEqual(path.stat().st_size, expected["bytes"])
                self.assertEqual(_sha256(path), expected["sha256"])
                with wave.open(str(path), "rb") as source:
                    self.assertEqual(source.getnchannels(), 2)
                    self.assertEqual(source.getsampwidth(), 2)
                    self.assertEqual(source.getframerate(), manifest["sample_rate_hz"])

    def test_structural_fixture_is_synthetic_and_complete(self) -> None:
        manifest = self.manifest()
        descriptor = manifest["structural_fixture"]
        self.assertIsInstance(descriptor, dict)
        assert isinstance(descriptor, dict)
        path = FIXTURES / str(descriptor["name"])
        self.assertEqual(_sha256(path), descriptor["sha256"])
        payload = json.loads(path.read_text("utf-8"))
        self.assertEqual(payload["source_run"]["run_id"], "1" * 32)
        self.assertEqual(len(payload["sections"]), 4)
        self.assertEqual(
            {item["role_id"] for item in payload["generated_outputs"]},
            {"main_chords", "main_lead", "primary_bass", "sub_bass", "drums"},
        )
        self.assertEqual(payload["feedback"]["accepted"], ["main_chords"])
        self.assertEqual(payload["feedback"]["rejected"], ["main_lead"])
        self.assertEqual(len(payload["playlist_handoff"]), 4)

    def test_generator_recreates_the_exact_fixture_set(self) -> None:
        specification = importlib.util.spec_from_file_location(
            "postfader_creation_review_fixture_generator", GENERATOR
        )
        self.assertIsNotNone(specification)
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            module.OUTPUT = Path(directory)
            module.main()
            expected_names = {path.name for path in FIXTURES.iterdir()}
            actual_names = {path.name for path in Path(directory).iterdir()}
            self.assertEqual(actual_names, expected_names)
            for name in sorted(expected_names):
                with self.subTest(path=name):
                    self.assertEqual(
                        (Path(directory) / name).read_bytes(),
                        (FIXTURES / name).read_bytes(),
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
