#!/usr/bin/env python3
"""Verify wheel/sdist contents before any clean-environment installation."""

from __future__ import annotations

import argparse
import ast
import email.parser
import json
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # pyright: ignore[reportMissingImports]


ROOT = Path(__file__).resolve().parents[1]
MCP_OWNERSHIP_MARKER = "mcp-name: io.github.synopsys0/postfader-fl-studio-mcp"
V013_REQUIRED_RUNTIME_MODULES = {
    "fl_studio_mcp/acceptance.py",
    "fl_studio_mcp/client_config.py",
    "fl_studio_mcp/evidence.py",
    "fl_studio_mcp/host_config.py",
}
ATLAS_PACKAGE_ROOT = ROOT / "fl_studio_mcp" / "plugin_atlas"
ATLAS_DATA_ROOT = ROOT / "fl_studio_mcp" / "plugin_atlas_data"
ATLAS_RUNTIME_MODULES = {
    path.relative_to(ROOT).as_posix()
    for path in ATLAS_PACKAGE_ROOT.rglob("*.py")
}
ATLAS_RUNTIME_MODULES.add("fl_studio_mcp/plugin_atlas_data/__init__.py")
ATLAS_DATA_FILES = {
    path.relative_to(ROOT).as_posix()
    for path in ATLAS_DATA_ROOT.rglob("*.json")
}
SOUND_SELECTION_PACKAGE_ROOT = ROOT / "fl_studio_mcp" / "sound_selection"
SOUND_SELECTION_DATA_ROOT = SOUND_SELECTION_PACKAGE_ROOT / "data"
SOUND_SELECTION_RUNTIME_MODULES = {
    path.relative_to(ROOT).as_posix()
    for path in SOUND_SELECTION_PACKAGE_ROOT.rglob("*.py")
}
SOUND_SELECTION_DATA_FILES = {
    path.relative_to(ROOT).as_posix()
    for path in SOUND_SELECTION_DATA_ROOT.rglob("*.json")
}
CREATION_PIPELINE_ROOT = ROOT / "fl_studio_mcp" / "creation_pipeline"
CREATION_PIPELINE_RUNTIME_MODULES = {
    path.relative_to(ROOT).as_posix()
    for path in CREATION_PIPELINE_ROOT.rglob("*.py")
}
CREATION_REVIEW_ROOT = ROOT / "fl_studio_mcp" / "creation_review"
CREATION_REVIEW_RUNTIME_MODULES = {
    path.relative_to(ROOT).as_posix()
    for path in CREATION_REVIEW_ROOT.rglob("*.py")
}
RUNTIME_MODULES = V013_REQUIRED_RUNTIME_MODULES | {
    "fl_studio_mcp/%s" % path.name
    for path in (ROOT / "fl_studio_mcp").glob("*.py")
} | {"fl_studio_mcp/_bridge/device_UniversalBridge.py"} | ATLAS_RUNTIME_MODULES | SOUND_SELECTION_RUNTIME_MODULES | CREATION_PIPELINE_RUNTIME_MODULES | CREATION_REVIEW_RUNTIME_MODULES
EXPECTED_TOOL_COUNT = 127
EXPECTED_RESOURCE_COUNT = 8
CONSOLE_SCRIPTS = {
    "fl-studio-mcp = fl_studio_mcp.mcp_server:main",
    "postfader = fl_studio_mcp.cli:main",
    "postfader-install-bridge = fl_studio_mcp.bridge_install:main",
    "postfader-doctor = fl_studio_mcp.diagnostics:main",
    "postfader-plugin-report = fl_studio_mcp.plugin_report:main",
    "postfader-plugin-atlas = fl_studio_mcp.plugin_atlas.cli:main",
    "postfader-setup = fl_studio_mcp.setup_wizard:main",
}
WHEEL_FORBIDDEN_PARTS = {
    ".private",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}
SDIST_FORBIDDEN_PARTS = WHEEL_FORBIDDEN_PARTS | {"mcpb-dist"}
SDIST_REQUIRED_SUFFIXES = (
    "/LICENSE",
    "/NOTICE",
    "/README.md",
    "/SECURITY.md",
    "/CONTRIBUTING.md",
    "/docs/plugin-matrix.md",
    "/docs/architecture.md",
    "/docs/creation-pipeline.md",
    "/docs/creation-review.md",
    "/docs/distribution.md",
    "/docs/fl-constraints.md",
    "/docs/plugin-atlas.md",
    "/docs/plugin-support.md",
    "/docs/production-runs.md",
    "/docs/releases/dev-v10.md",
    "/docs/setup.md",
    "/docs/sound-selection.md",
    "/docs/tool-contracts.md",
    "/tests/fixtures/write-scenario-v1.json",
    "/scripts/install.ps1",
    "/scripts/launch_fl_studio.ps1",
    "/scripts/generate_mcp_config.py",
    "/scripts/live_read_acceptance.py",
    "/scripts/live_sound_selection_acceptance.py",
    "/scripts/live_creation_acceptance.py",
    "/scripts/live_creation_review_acceptance.py",
    "/scripts/generate_creation_review_fixtures.py",
    "/scripts/live_write_acceptance.py",
    "/scripts/live_note_acceptance.py",
    "/scripts/verify_distribution.py",
    "/scripts/verify_installed_package.py",
    "/scripts/clean_wheel_smoke.py",
)


def _folded_parts(name: str) -> set[str]:
    return {part.casefold() for part in PurePosixPath(name).parts}


def inspect_wheel(wheel: Path, version: str) -> list[str]:
    """Return publication-blocking problems found in one wheel."""

    failures: list[str] = []
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            metadata_names = sorted(
                name for name in names if name.endswith(".dist-info/METADATA")
            )
            if len(metadata_names) != 1:
                failures.append(
                    "wheel must contain exactly one .dist-info/METADATA file"
                )
                metadata_text = ""
                metadata = email.parser.Parser().parsestr("")
            else:
                metadata_text = archive.read(metadata_names[0]).decode("utf-8")
                metadata = email.parser.Parser().parsestr(metadata_text)

            entry_names = sorted(
                name for name in names if name.endswith(".dist-info/entry_points.txt")
            )
            if len(entry_names) != 1:
                failures.append(
                    "wheel must contain exactly one .dist-info/entry_points.txt file"
                )
                entry_text = ""
            else:
                entry_text = archive.read(entry_names[0]).decode("utf-8")

            if metadata.get("Version") != version:
                failures.append("wheel metadata version does not match pyproject")
            if metadata.get("License-Expression") != "Apache-2.0":
                failures.append("wheel lacks Apache-2.0 License-Expression")
            if MCP_OWNERSHIP_MARKER not in metadata_text:
                failures.append(
                    "wheel METADATA is missing the MCP Registry ownership marker"
                )
            for license_name in ("LICENSE", "NOTICE"):
                if not any(
                    name.endswith(".dist-info/licenses/" + license_name)
                    for name in names
                ):
                    failures.append("wheel is missing %s" % license_name)
            for command in sorted(CONSOLE_SCRIPTS):
                if command not in entry_text:
                    failures.append("missing console entry point: %s" % command)
            for required in sorted(RUNTIME_MODULES):
                if required not in names:
                    failures.append("wheel is missing %s" % required)
            for required in sorted(ATLAS_DATA_FILES):
                if required not in names:
                    failures.append("wheel is missing Atlas data %s" % required)
            for required in sorted(SOUND_SELECTION_DATA_FILES):
                if required not in names:
                    failures.append(
                        "wheel is missing Sound Selection data %s" % required
                    )

            bridge_name = "fl_studio_mcp/_bridge/device_UniversalBridge.py"
            if bridge_name in names and any(
                byte >= 128 for byte in archive.read(bridge_name)
            ):
                failures.append("packaged bridge is not pure ASCII")
            private = sorted(
                name
                for name in names
                if WHEEL_FORBIDDEN_PARTS & _folded_parts(name)
            )
            if private:
                failures.append("private/generated wheel paths: %s" % private[:10])
    except (OSError, UnicodeError, zipfile.BadZipFile) as error:
        failures.append("wheel could not be inspected: %s" % error)
    return failures


def inspect_sdist(sdist: Path) -> list[str]:
    """Return publication-blocking problems found in one source archive."""

    failures: list[str] = []
    try:
        with tarfile.open(sdist) as archive:
            members = {member.name: member for member in archive.getmembers()}
            names = set(members)
            for suffix in SDIST_REQUIRED_SUFFIXES:
                if not any(name.endswith(suffix) for name in names):
                    failures.append("sdist is missing %s" % suffix.lstrip("/"))
            for required in sorted(
                ATLAS_RUNTIME_MODULES
                | ATLAS_DATA_FILES
                | SOUND_SELECTION_RUNTIME_MODULES
                | SOUND_SELECTION_DATA_FILES
                | CREATION_PIPELINE_RUNTIME_MODULES
                | CREATION_REVIEW_RUNTIME_MODULES
            ):
                suffix = "/" + required
                if not any(name.endswith(suffix) for name in names):
                    failures.append("sdist is missing %s" % required)

            readmes = sorted(
                name
                for name in names
                if name.endswith("/README.md")
                and len(PurePosixPath(name).parts) == 2
            )
            if len(readmes) != 1:
                failures.append("sdist must contain exactly one top-level README.md")
            else:
                stream = archive.extractfile(members[readmes[0]])
                readme_text = (
                    stream.read().decode("utf-8") if stream is not None else ""
                )
                if MCP_OWNERSHIP_MARKER not in readme_text:
                    failures.append(
                        "sdist README is missing the MCP Registry ownership marker"
                    )

            leaked = sorted(
                name
                for name in names
                if SDIST_FORBIDDEN_PARTS & _folded_parts(name)
            )
            if leaked:
                failures.append("private/generated sdist paths: %s" % leaked[:10])
    except (OSError, UnicodeError, tarfile.TarError) as error:
        failures.append("sdist could not be inspected: %s" % error)
    return failures


def version_failures(version: str) -> list[str]:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    versions: list[object] = [version, manifest.get("version"), server.get("version")]
    versions.extend(package.get("version") for package in server.get("packages", []))
    namespace: dict[str, object] = {}
    exec(
        (ROOT / "fl_studio_mcp" / "__init__.py").read_text(encoding="utf-8"),
        namespace,
    )
    versions.append(namespace.get("__version__"))
    failures: list[str] = []
    if any(value != version for value in versions):
        failures.append("public version declarations disagree: %r" % versions)
    tools = manifest.get("tools")
    if not isinstance(tools, list) or len(tools) != EXPECTED_TOOL_COUNT:
        failures.append(
            "manifest tool count is not %d: %s"
            % (
                EXPECTED_TOOL_COUNT,
                len(tools) if isinstance(tools, list) else "invalid",
            )
        )
    server_tree = ast.parse(
        (ROOT / "fl_studio_mcp" / "mcp_server.py").read_text(encoding="utf-8")
    )
    resource_count = sum(
        1
        for node in ast.walk(server_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "resource"
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "mcp"
            for decorator in node.decorator_list
        )
    )
    if resource_count != EXPECTED_RESOURCE_COUNT:
        failures.append(
            "registered resource count is not %d: %d"
            % (EXPECTED_RESOURCE_COUNT, resource_count)
        )
    return failures


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args(argv)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    wheels = sorted(args.dist_dir.glob("*.whl"))
    sdists = sorted(args.dist_dir.glob("*.tar.gz"))
    failures: list[str] = []
    if len(wheels) != 1:
        failures.append(
            "expected exactly one wheel in %s, found %d" % (args.dist_dir, len(wheels))
        )
    if len(sdists) != 1:
        failures.append(
            "expected exactly one sdist in %s, found %d" % (args.dist_dir, len(sdists))
        )
    if len(wheels) == 1:
        failures.extend(inspect_wheel(wheels[0], version))
    if len(sdists) == 1:
        failures.extend(inspect_sdist(sdists[0]))
    failures.extend(version_failures(version))

    if failures:
        print("distribution verification failed:")
        for failure in failures:
            print("  - %s" % failure)
        return 1
    print(
        "distribution archives verified: %s and %s" % (wheels[0].name, sdists[0].name)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
