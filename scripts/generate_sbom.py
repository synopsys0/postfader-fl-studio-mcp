#!/usr/bin/env python3
"""Generate a small CycloneDX SBOM for a PostFader release build.

The release workflow resolves the wheel in a clean temporary environment and
captures ``pip inspect --local`` there.  This script follows the installed
project's runtime dependency graph, deliberately leaving build and audit tools
out of the release SBOM.  Release files are represented as file components with
their SHA-256 digests so the SBOM also records the exact package bytes that are
about to be published.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
CYCLONEDX_SCHEMA = "https://cyclonedx.org/schema/bom-1.6.schema.json"
PROJECT_PURL_PREFIX = "pkg:pypi/"
REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize_name(name: str) -> str:
    """Return the PEP 503-normalized spelling used by PyPI package URLs."""

    return re.sub(r"[-_.]+", "-", name).lower()


def package_ref(name: str, version: str) -> str:
    """Return a stable purl for one installed Python package."""

    normalized = normalize_name(name)
    return "%s%s@%s" % (PROJECT_PURL_PREFIX, quote(normalized), quote(version))


def requirement_name(requirement: object) -> str | None:
    """Extract a dependency name without evaluating an optional marker.

    The resolved environment is authoritative: a parsed requirement is only
    followed when its normalized name is present in the ``pip inspect`` map.
    This keeps optional extras and platform-inapplicable dependencies out of a
    release SBOM without adding the ``packaging`` dependency to the project.
    """

    if not isinstance(requirement, str):
        return None
    match = REQUIREMENT_NAME.match(requirement)
    return match.group(1) if match else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata_licenses(metadata: dict[str, object]) -> list[dict[str, object]]:
    """Return a CycloneDX license-choice value from package metadata.

    CycloneDX represents an SPDX expression directly as ``{"expression":
    ...}``, while a named or SPDX-identifier license is nested under
    ``{"license": {"name"/"id": ...}}``.  Keeping those shapes separate is
    important: ``{"license": {"expression": ...}}`` looks plausible but is
    rejected by the CycloneDX schema.
    """

    expression = metadata.get("license_expression")
    if isinstance(expression, str) and expression.strip():
        return [{"expression": expression.strip()}]
    license_name = metadata.get("license")
    if isinstance(license_name, str) and license_name.strip():
        return [{"license": {"name": license_name.strip()}}]
    return []


def _load_inspection(path: Path) -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("could not read pip inspection JSON: %s" % error) from error
    installed = payload.get("installed")
    if not isinstance(installed, list):
        raise ValueError("pip inspection JSON has no installed package list")

    packages: dict[str, dict[str, object]] = {}
    for item in installed:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        name = metadata.get("name")
        version = metadata.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        packages[normalize_name(name)] = {
            "metadata": metadata,
            "requested": bool(item.get("requested")),
        }
    return packages


def _runtime_closure(
    packages: dict[str, dict[str, object]], project_name: str
) -> list[str]:
    root = normalize_name(project_name)
    if root not in packages:
        raise ValueError(
            "pip inspection does not contain the installed project %s" % project_name
        )
    pending = [root]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        metadata = packages[current]["metadata"]
        if not isinstance(metadata, dict):
            continue
        requirements = metadata.get("requires_dist", [])
        if not isinstance(requirements, list):
            continue
        for requirement in requirements:
            name = requirement_name(requirement)
            normalized = normalize_name(name) if name else None
            if normalized and normalized in packages and normalized not in seen:
                pending.append(normalized)
    return sorted(seen)


def _python_component(
    normalized_name: str, package: dict[str, object]
) -> tuple[dict[str, object], list[str]]:
    metadata = package["metadata"]
    if not isinstance(metadata, dict):
        raise ValueError("invalid metadata for %s" % normalized_name)
    name = metadata.get("name")
    version = metadata.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise ValueError("package metadata is missing name/version")
    reference = package_ref(name, version)
    component: dict[str, object] = {
        "type": "library",
        "bom-ref": reference,
        "name": name,
        "version": version,
        "purl": reference,
    }
    licenses = _metadata_licenses(metadata)
    if licenses:
        component["licenses"] = licenses
    summary = metadata.get("summary")
    if isinstance(summary, str) and summary.strip():
        component["description"] = summary.strip()
    properties = [{"name": "postfader:requested", "value": str(bool(package["requested"]))}]
    component["properties"] = properties

    dependency_refs: list[str] = []
    requirements = metadata.get("requires_dist", [])
    if isinstance(requirements, list):
        for requirement in requirements:
            dependency_name = requirement_name(requirement)
            if dependency_name:
                dependency_refs.append(normalize_name(dependency_name))
    return component, sorted(set(dependency_refs))


def generate_sbom(
    inspection: Path,
    output: Path,
    *,
    project_name: str,
    project_version: str,
    artifacts: list[Path],
    root: Path = ROOT,
) -> dict[str, object]:
    packages = _load_inspection(inspection)
    closure = _runtime_closure(packages, project_name)
    project_ref = package_ref(project_name, project_version)

    components: list[dict[str, object]] = []
    dependency_edges: dict[str, list[str]] = {}
    package_refs: dict[str, str] = {}
    for normalized_name in closure:
        package = packages[normalized_name]
        metadata = package["metadata"]
        if not isinstance(metadata, dict):
            continue
        name = metadata.get("name")
        version = metadata.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        reference = package_ref(name, version)
        package_refs[normalized_name] = reference
        if normalized_name == normalize_name(project_name):
            continue
        component, dependencies = _python_component(normalized_name, package)
        components.append(component)
        dependency_edges[reference] = dependencies

    project_metadata: dict[str, object] = {
        "type": "application",
        "bom-ref": project_ref,
        "name": project_name,
        "version": project_version,
        "purl": project_ref,
    }
    project_package = packages[normalize_name(project_name)]
    project_meta = project_package.get("metadata")
    if isinstance(project_meta, dict):
        licenses = _metadata_licenses(project_meta)
        if licenses:
            project_metadata["licenses"] = licenses

    artifact_components: list[dict[str, object]] = []
    artifact_fingerprints: list[str] = []
    for raw_path in artifacts:
        path = raw_path if raw_path.is_absolute() else root / raw_path
        path = path.resolve()
        if not path.is_file():
            raise ValueError("release artifact is missing: %s" % raw_path)
        digest = sha256(path)
        try:
            relative = path.relative_to(root.resolve()).as_posix()
        except ValueError:
            relative = path.name
        artifact_fingerprints.append("%s:%s" % (path.name, digest))
        artifact_components.append(
            {
                "type": "file",
                "bom-ref": "artifact:%s" % path.name,
                "name": path.name,
                "hashes": [{"alg": "SHA-256", "content": digest}],
                "properties": [
                    {"name": "postfader:artifact-path", "value": relative},
                    {"name": "postfader:artifact-role", "value": "release-download"},
                ],
            }
        )

    components.extend(artifact_components)
    serial_seed = "%s@%s|%s" % (
        project_name,
        project_version,
        "|".join(sorted(artifact_fingerprints)),
    )
    serial = "urn:uuid:%s" % uuid.uuid5(uuid.NAMESPACE_URL, serial_seed)
    metadata_properties = [
        {
            "name": "postfader:sbom-source",
            "value": "pip inspect --local from a clean wheel installation",
        },
        {
            "name": "postfader:dependency-scope",
            "value": "runtime dependency closure only; build and audit tools excluded",
        },
        {
            "name": "postfader:excluded-component",
            "value": "FL Studio",
        },
        {
            "name": "postfader:excluded-component",
            "value": "virtual MIDI software or drivers",
        },
        {
            "name": "postfader:excluded-component",
            "value": "third-party AI clients or model providers",
        },
        {
            "name": "postfader:excluded-component",
            "value": "user-installed FL Studio plug-ins",
        },
    ]

    bom: dict[str, object] = {
        "$schema": CYCLONEDX_SCHEMA,
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": serial,
        "version": 1,
        "metadata": {
            "component": project_metadata,
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "PostFader SBOM generator",
                        "version": project_version,
                        "supplier": {"name": "PostFader maintainers"},
                    }
                ]
            },
            "properties": metadata_properties,
        },
        "components": sorted(components, key=lambda item: str(item["bom-ref"])),
        "dependencies": [],
    }
    dependencies = bom["dependencies"]
    if not isinstance(dependencies, list):  # pragma: no cover - local construction
        raise AssertionError("CycloneDX dependency list was not initialized")
    dependencies.append(
        {
            "ref": project_ref,
            "dependsOn": sorted(
                package_refs[name]
                for name in closure
                if name != normalize_name(project_name) and name in package_refs
            ),
        }
    )
    for reference, names in sorted(dependency_edges.items()):
        dependencies.append(
            {
                "ref": reference,
                "dependsOn": sorted(
                    package_refs[name] for name in names if name in package_refs
                ),
            }
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(bom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return bom


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project-name", default="postfader-fl-studio-mcp")
    parser.add_argument("--project-version", default=None)
    parser.add_argument("--artifacts", nargs="+", type=Path, required=True)
    args = parser.parse_args(argv)

    project_version = args.project_version
    if project_version is None:
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
            import tomli as tomllib
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project_version = project["project"]["version"]
    try:
        bom = generate_sbom(
            args.inspection,
            args.output,
            project_name=args.project_name,
            project_version=project_version,
            artifacts=args.artifacts,
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print("SBOM generation failed: %s" % error, file=sys.stderr)
        return 1
    components = bom.get("components", [])
    print("CycloneDX SBOM written: %s (%d components)" % (args.output, len(components)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
