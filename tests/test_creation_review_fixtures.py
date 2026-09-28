"""Integrity and regeneration checks for public Creation Review fixtures."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
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
