#!/usr/bin/env python3
"""Build byte-stable setup ZIPs with a fixed Python/zlib toolchain."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ROOT_FILES = (
    ".mcp.json.example",
    "CONTRIBUTING.md",
    "LICENSE",
    "MANIFEST.in",
    "NOTICE",
    "README.md",
    "SECURITY.md",
    "pyproject.toml",
)
SOURCE_DIRECTORIES = ("docs", "fl_studio_mcp", "scripts")
BUNDLE_SPECS = (
    ("Windows", False),
    ("macOS", False),
    ("Windows", True),
    ("macOS", True),
)
FORBIDDEN_PARTS = {
    ".git",
    ".github",
    ".private",
    ".venv",
    "__pycache__",
    "artifacts",
    "build",
    "dist",
    "mcpb-dist",
    "tests",
}


def project_version(root: Path = ROOT) -> str:
    """Return the canonical package version used in bundle names and guides."""

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml has no non-empty project.version")
    return version


def _source_files(root: Path) -> list[Path]:
    """Return the reviewed, Git-tracked files eligible for a public bundle."""

    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(root), "ls-files", "-z", "--cached", "--"],
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise ValueError(
            "could not enumerate Git-tracked bundle sources: %s" % error
        ) from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(
            "could not enumerate Git-tracked bundle sources%s"
            % (": %s" % detail if detail else "")
        )

    tracked = {
        Path(value.decode("utf-8", errors="surrogateescape"))
        for value in completed.stdout.split(b"\0")
        if value
    }
    files: list[Path] = []
    for relative_text in ROOT_FILES:
        relative = Path(relative_text)
        if relative not in tracked:
            raise ValueError("required bundle source is not Git-tracked: %s" % relative)
        if not (root / relative).is_file():
            raise FileNotFoundError("required bundle source is missing: %s" % relative)
        files.append(relative)
    for directory_text in SOURCE_DIRECTORIES:
        directory = root / directory_text
        if not directory.is_dir():
            raise FileNotFoundError(
                "required bundle directory is missing: %s" % directory_text
            )
    for relative in tracked:
        if not relative.parts or relative.parts[0] not in SOURCE_DIRECTORIES:
            continue
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError("tracked bundle source is missing: %s" % relative)
        if set(relative.parts) & FORBIDDEN_PARTS or path.suffix == ".pyc":
            raise ValueError(
                "developer-only file is tracked in a bundle source tree: %s"
                % relative
            )
        if path.is_symlink():
            raise ValueError(
                "symbolic links are not allowed in a release bundle: %s" % relative
            )
        files.append(relative)
    return sorted(set(files), key=lambda value: value.as_posix())


def _zip_info(name: str, *, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, ARCHIVE_TIMESTAMP)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o100755 if executable else 0o100644
    info.external_attr = mode << 16
    return info


def _write_member(
    archive: zipfile.ZipFile,
    name: str,
    content: bytes,
    *,
    executable: bool = False,
) -> None:
    archive.writestr(_zip_info(name, executable=executable), content)


def _windows_launcher(version: str, *, codex: bool = False) -> bytes:
    product = "PostFader v%s Codex installer for Windows" % version if codex else (
        "PostFader v%s installer for Windows" % version
    )
    setup_args = (
        "setup --interactive --client codex-toml --register-codex"
        if codex
        else "setup --interactive"
    )
    text = f"""@echo off
setlocal
cd /d "%~dp0"

echo.
echo {product}
echo.
echo Checking requirements and planned changes...
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\\install.ps1" -DryRun -SkipBridgeDeployment
if errorlevel 1 goto failed
if "%POSTFADER_BUNDLE_DRY_RUN%"=="1" exit /b 0

echo.
choice /C YN /M "Continue with the installation"
if errorlevel 2 goto cancelled

echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\\install.ps1" -SkipBridgeDeployment
if errorlevel 1 goto failed

echo.
echo PostFader is installed. Starting guided setup...
echo.
"%~dp0.venv\\Scripts\\postfader.exe" {setup_args}
set "POSTFADER_SETUP_STATUS=%ERRORLEVEL%"
if "%POSTFADER_SETUP_STATUS%"=="0" goto setup_complete

echo.
echo PostFader is installed, but guided setup is not complete yet.
echo This is expected if the MIDI endpoint or FL Studio is not ready.
echo Complete the action shown above, then resume with:
echo   "%~dp0.venv\\Scripts\\postfader.exe" {setup_args}
goto installed

:setup_complete
echo.
echo Guided setup finished.

:installed
echo Read START HERE - Windows.md for the manual FL Studio action and safety boundaries.
echo.
pause
exit /b 0

:cancelled
echo.
echo Installation cancelled. No installation changes were requested.
echo.
pause
exit /b 0

:failed
echo.
echo Installation did not complete. Review the error above.
echo.
pause
exit /b 1
"""
    return text.replace("\n", "\r\n").encode("utf-8")


def _macos_launcher(version: str, *, codex: bool = False) -> bytes:
    product = "PostFader v%s Codex installer for macOS" % version if codex else (
        "PostFader v%s installer for macOS" % version
    )
    setup_args = (
        "setup --interactive --client codex-toml --register-codex"
        if codex
        else "setup --interactive"
    )
    return f"""#!/bin/bash

set -u

POSTFADER_RELEASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$POSTFADER_RELEASE_DIR" || exit 1

printf '\n{product}\n\n'

if [ "${{POSTFADER_BUNDLE_DRY_RUN:-0}}" = "1" ]; then
    bash -n "$POSTFADER_RELEASE_DIR/scripts/install.sh"
    POSTFADER_STATUS=$?
else
    bash "$POSTFADER_RELEASE_DIR/scripts/install.sh" --skip-bridge-deployment
    POSTFADER_STATUS=$?
fi

if [ "${{POSTFADER_BUNDLE_DRY_RUN:-0}}" = "1" ]; then
    if [ "$POSTFADER_STATUS" -eq 0 ]; then
        printf '\nBundle launcher dry run passed. No installation changes were made.\n'
    fi
    exit "$POSTFADER_STATUS"
elif [ "$POSTFADER_STATUS" -eq 0 ]; then
    printf '\nPostFader is installed. Starting guided setup...\n\n'
    "$POSTFADER_RELEASE_DIR/.venv/bin/postfader" {setup_args}
    POSTFADER_SETUP_STATUS=$?
    if [ "$POSTFADER_SETUP_STATUS" -eq 0 ]; then
        printf '\nGuided setup finished.\n'
    else
        printf '\nPostFader is installed, but guided setup is not complete yet.\n'
        printf 'This is expected if the MIDI endpoint or FL Studio is not ready.\n'
        printf 'Complete the action shown above, then resume with:\n'
        printf '  "%s/.venv/bin/postfader" {setup_args}\n' "$POSTFADER_RELEASE_DIR"
    fi
    printf 'Read START HERE - macOS.md for the manual FL Studio action and safety boundaries.\n'
    POSTFADER_STATUS=0
else
    printf '\nInstallation did not complete. Review the error above.\n'
fi

printf '\nPress Return to close this window.'
read -r _
exit "$POSTFADER_STATUS"
""".encode("utf-8")


def _start_here(platform: str, version: str, *, codex: bool = False) -> bytes:
    if platform == "Windows":
        endpoint = (
            "Create one bidirectional virtual MIDI endpoint with the local provider "
            "of your choice."
        )
        launcher = (
            "`Install PostFader for Codex.cmd`"
            if codex
            else "`Install PostFader.cmd`"
        )
        setup_command = ".\\.venv\\Scripts\\postfader.exe setup"
        doctor_command = (
            ".\\.venv\\Scripts\\postfader-doctor.exe "
            "--midi-port \"Exact Virtual MIDI Endpoint Name\" --json"
        )
        installer_note = "Review the dry-run summary, then press **Y** to continue."
    else:
        endpoint = "Enable an IAC bus in Audio MIDI Setup."
        launcher = (
            "`Install PostFader for Codex.command`"
            if codex
            else "`Install PostFader.command`"
        )
        setup_command = "./.venv/bin/postfader setup"
        doctor_command = (
            "./.venv/bin/postfader-doctor "
            "--midi-port \"IAC Driver Bus 1\" --json"
        )
        installer_note = (
            "If Finder blocks the first launch, Control-click the file and choose "
            "**Open**."
        )

    if codex:
        setup_command += " --client codex-toml --register-codex"
        codex_requirement = "- Install the Codex CLI so the `codex` command is available.\n"
        client_setup = """Guided setup uses the discovered machine-specific values and asks for a
separate confirmation before it runs `codex mcp add`. It treats an identical
`fl-studio` entry as already current and refuses to replace a different entry.
If the Codex CLI is unavailable, PostFader remains installed and setup prints
the exact `codex-toml` configuration as a manual fallback.
"""
    else:
        codex_requirement = ""
        client_setup = """Guided setup asks which client configuration format to print. If you
select `codex-command`, run the emitted command in **PowerShell**. The Windows
installer window itself is Command Prompt and uses different quoting.
"""

    title_suffix = " for Codex" if codex else ""
    text = f"""# Start here — PostFader v{version}{title_suffix} on {platform}

PostFader is the verified AI copilot for FL Studio. It starts read-only, never
saves your project automatically, and reports whether FL Studio actually
accepted each supported change.

## Before installing

- Install FL Studio 2026 version 26.1.3 build 5336 or newer.
- Install Python 3.10 through 3.14.
{codex_requirement}- Launch FL Studio once so it creates its user-data folders, then quit it.
- Keep an internet connection available while Python dependencies download.
- {endpoint}

PostFader does not install or configure virtual MIDI software.

## Install

1. Extract the entire ZIP to a stable, writable folder and keep it there;
   PostFader's `.venv` and generated client configuration point into it.
2. Quit FL Studio.
3. Open {launcher}.
4. {installer_note}

The installer creates `.venv` inside this folder, installs PostFader there,
and starts the guided setup command. Guided setup asks for the FL Studio
user-data folder and confirmation before it copies Universal Bridge into the
controller-script folder:

```text
{setup_command}
```

Guided setup detects the supported host and FL Studio user-data folder,
enumerates virtual MIDI endpoints that already exist, walks through client
configuration, and runs the connection doctor. It shows what it found and
what it intends to change before continuing.

{client_setup.rstrip()}

If setup stops because the endpoint is missing or FL Studio is still closed,
PostFader remains installed. Complete the action it names and run the same
setup command again; rerunning setup is the supported resume path.

## The one FL Studio action you must complete

PostFader cannot operate FL Studio's MIDI Settings window for you:

1. Start FL Studio and open **Options → MIDI settings**.
2. Under **Input**, select the exact endpoint chosen during setup, enable it,
   and set Controller type to **Universal Bridge**.
3. Assign that Input an unused FL Studio Port number.
4. Under **Output**, enable the matching endpoint and assign the same Port
   number.
5. Open **View → Script output** and reload **Universal Bridge**.
6. Return to the waiting setup prompt. If you closed it, rerun:

```text
{setup_command}
```

The expected Script output ends with `ready: MIDI SysEx`. Guided setup then
runs the doctor and reports the exact next action if the connection is not yet
complete.

## Safety boundaries

- PostFader does not install or configure a virtual MIDI driver or IAC bus.
- It does not click through FL Studio, choose an FL Port number, or reload a
  controller script for you.
- Setup never enables write mode, changes project controls, or saves a project.
- Client configuration prints to the terminal by default. An explicit
  `--output` creates one new file. A resume accepts that same file only when
  its content is already identical; different files are never overwritten.
  Write mode is never stored in that configuration.
- The first live connection remains read-only. Try project writes later in a
  blank or disposable project and close without saving until you trust the
  workflow.

## Verify directly

Guided setup runs this check. You can repeat it directly when troubleshooting:


```text
{doctor_command}
```

A healthy result reports `overall: "pass"`, a matching controller script,
`bridge_mode: "read_only"`, and `verified_writes_enabled: false`.

For client-specific details and troubleshooting, see `docs/setup.md`.
"""
    return text.encode("utf-8")


def _generated_members(
    platform: str,
    version: str,
    *,
    codex: bool = False,
) -> dict[str, tuple[bytes, bool]]:
    if platform == "Windows":
        launcher = (
            "Install PostFader for Codex.cmd"
            if codex
            else "Install PostFader.cmd"
        )
        return {
            launcher: (_windows_launcher(version, codex=codex), False),
            "START HERE - Windows.md": (
                _start_here(platform, version, codex=codex),
                False,
            ),
        }
    if platform == "macOS":
        launcher = (
            "Install PostFader for Codex.command"
            if codex
            else "Install PostFader.command"
        )
        return {
            launcher: (_macos_launcher(version, codex=codex), True),
            "START HERE - macOS.md": (
                _start_here(platform, version, codex=codex),
                False,
            ),
        }
    raise ValueError("unsupported release platform: %s" % platform)


def build_bundle(
    platform: str,
    output_dir: Path,
    *,
    root: Path = ROOT,
    version: str | None = None,
    codex: bool = False,
) -> Path:
    """Build one deterministic platform bundle and return its path."""

    version = version or project_version(root)
    archive_root = "PostFader-v%s-%s%s" % (
        version,
        "Codex-" if codex else "",
        platform,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / (archive_root + ".zip")

    with tempfile.NamedTemporaryFile(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=output_dir,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for relative in _source_files(root):
                executable = relative.suffix == ".sh"
                _write_member(
                    archive,
                    "%s/%s" % (archive_root, relative.as_posix()),
                    (root / relative).read_bytes(),
                    executable=executable,
                )
            for relative_text, (content, executable) in sorted(
                _generated_members(platform, version, codex=codex).items()
            ):
                _write_member(
                    archive,
                    "%s/%s" % (archive_root, relative_text),
                    content,
                    executable=executable,
                )
        failures = inspect_bundle(
            target=temporary,
            platform=platform,
            version=version,
            root=root,
            codex=codex,
        )
        if failures:
            raise ValueError("release bundle verification failed: %s" % "; ".join(failures))
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def inspect_bundle(
    target: Path,
    *,
    platform: str,
    version: str,
    root: Path = ROOT,
    codex: bool = False,
) -> list[str]:
    """Return verification failures for one platform bundle."""

    failures: list[str] = []
    if not target.is_file():
        return ["bundle does not exist: %s" % target]
    if not zipfile.is_zipfile(target):
        return ["bundle is not a ZIP archive: %s" % target]

    archive_root = "PostFader-v%s-%s%s" % (
        version,
        "Codex-" if codex else "",
        platform,
    )
    generated = _generated_members(platform, version, codex=codex)
    expected = {
        "%s/%s" % (archive_root, relative.as_posix())
        for relative in _source_files(root)
    }
    expected.update(
        "%s/%s" % (archive_root, relative) for relative in generated
    )

    try:
        with zipfile.ZipFile(target) as archive:
            corrupt = archive.testzip()
            if corrupt:
                failures.append("archive CRC failed for %s" % corrupt)
            names = archive.namelist()
            name_set = set(names)
            if len(names) != len(name_set):
                failures.append("archive contains duplicate member names")
            missing = sorted(expected - name_set)
            unexpected = sorted(name_set - expected)
            failures.extend("expected member is missing: %s" % name for name in missing)
            failures.extend("unexpected member is present: %s" % name for name in unexpected)
            for name in names:
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name:
                    failures.append("unsafe archive member path: %s" % name)
                    continue
                if not path.parts or path.parts[0] != archive_root:
                    failures.append("member escapes the single bundle root: %s" % name)
                if set(path.parts[1:]) & FORBIDDEN_PARTS or name.endswith(".pyc"):
                    failures.append("private or developer-only member included: %s" % name)
                info = archive.getinfo(name)
                if info.date_time != ARCHIVE_TIMESTAMP:
                    failures.append("member has a non-reproducible timestamp: %s" % name)
            for relative_text, (expected, executable) in generated.items():
                name = "%s/%s" % (archive_root, relative_text)
                if name not in name_set:
                    continue
                if archive.read(name) != expected:
                    failures.append("generated entry point differs from its template: %s" % name)
                mode = archive.getinfo(name).external_attr >> 16
                if executable and mode & 0o111 == 0:
                    failures.append("entry point is not executable: %s" % name)
    except (OSError, zipfile.BadZipFile) as error:
        failures.append("could not inspect bundle: %s" % error)
    return failures


def build_bundles(output_dir: Path, *, root: Path = ROOT) -> list[Path]:
    version = project_version(root)
    return [
        build_bundle(
            platform,
            output_dir,
            root=root,
            version=version,
            codex=codex,
        )
        for platform, codex in BUNDLE_SPECS
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release-bundles")
    args = parser.parse_args(argv)

    try:
        bundles = build_bundles(args.output_dir)
    except (OSError, KeyError, TypeError, ValueError) as error:
        print("release bundle build failed: %s" % error, file=sys.stderr)
        return 1
    for bundle in bundles:
        print("%s  %s" % (_sha256(bundle), bundle))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
