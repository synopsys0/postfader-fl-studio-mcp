"""Tests for the release CycloneDX SBOM generator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_generator():
    path = ROOT / "scripts" / "generate_sbom.py"
    spec = importlib.util.spec_from_file_location("postfader_generate_sbom", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load SBOM generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SbomTests(unittest.TestCase):
    def test_runtime_closure_excludes_unrelated_build_tools(self) -> None:
        generator = load_generator()
        with tempfile.TemporaryDirectory(prefix="postfader-sbom-") as raw:
            root = Path(raw)
            artifact = root / "PostFader-v0.20.0-Windows.zip"
            artifact.write_bytes(b"release artifact")
            inspection = root / "pip-inspect.json"
            inspection.write_text(
                json.dumps(
                    {
                        "installed": [
                            {
                                "metadata": {
                                    "name": "postfader-fl-studio-mcp",
                                    "version": "0.20.0",
                                    "license_expression": "Apache-2.0",
                                    "requires_dist": [
                                        "anyio>=4",
                                        "optional-tool>=1; extra == 'dev'",
                                    ],
                                },
                                "requested": True,
                            },
                            {
                                "metadata": {
                                    "name": "anyio",
                                    "version": "4.9.0",
                                    "requires_dist": ["sniffio>=1"],
                                },
                                "requested": False,
                            },
                            {
                                "metadata": {
                                    "name": "sniffio",
                                    "version": "1.3.1",
                                    "requires_dist": [],
                                },
                                "requested": False,
                            },
                            {
                                "metadata": {
                                    "name": "pip-audit",
                                    "version": "2.10.1",
                                    "requires_dist": [],
                                },
                                "requested": True,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            output = root / "release.cdx.json"
            bom = generator.generate_sbom(
                inspection,
                output,
                project_name="postfader-fl-studio-mcp",
                project_version="0.20.0",
                artifacts=[artifact],
                root=root,
            )

            names = {component["name"] for component in bom["components"]}
            self.assertEqual(
                names,
                {"anyio", "sniffio", "PostFader-v0.20.0-Windows.zip"},
            )
            self.assertNotIn("pip-audit", names)
            self.assertEqual(bom["metadata"]["component"]["name"], "postfader-fl-studio-mcp")
            self.assertEqual(
                bom["metadata"]["component"]["licenses"],
                [{"expression": "Apache-2.0"}],
            )
            properties = bom["metadata"]["properties"]
            excluded = {item["value"] for item in properties if item["name"] == "postfader:excluded-component"}
            self.assertIn("FL Studio", excluded)

            artifact_component = next(
                component
                for component in bom["components"]
                if component["name"] == artifact.name
            )
            expected_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
            self.assertEqual(
                artifact_component["hashes"],
                [{"alg": "SHA-256", "content": expected_hash}],
            )
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), bom)

    def test_missing_project_is_rejected(self) -> None:
        generator = load_generator()
        with tempfile.TemporaryDirectory(prefix="postfader-sbom-") as raw:
            root = Path(raw)
            inspection = root / "pip-inspect.json"
            inspection.write_text(json.dumps({"installed": []}), encoding="utf-8")
            artifact = root / "artifact.zip"
            artifact.write_bytes(b"artifact")
            with self.assertRaises(ValueError):
                generator.generate_sbom(
                    inspection,
                    root / "release.cdx.json",
                    project_name="postfader-fl-studio-mcp",
                    project_version="0.20.0",
                    artifacts=[artifact],
                    root=root,
                )


if __name__ == "__main__":
    unittest.main()
