#!/usr/bin/env python3
"""Install one wheel into a clean temporary venv and verify it."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(command, *, cwd, env):
    completed = subprocess.run(
        [os.fspath(value) for value in command],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    print(completed.stdout, end="")
    print(completed.stderr, end="", file=sys.stderr)
    if completed.returncode != 0:
        raise RuntimeError("command exited %d: %s" % (completed.returncode, command))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, nargs="?")
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args(argv)
    wheel = args.wheel
    if wheel is None:
        wheels = sorted(args.dist_dir.glob("*.whl"))
        if len(wheels) != 1:
            parser.error("expected exactly one wheel in %s" % args.dist_dir)
        wheel = wheels[0]
    wheel = wheel.resolve()
    if not wheel.is_file():
        parser.error("wheel does not exist: %s" % wheel)
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        import tomli as tomllib
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    environment = os.environ.copy()
    environment.update(
        {
            "FL_BRIDGE_ENABLE_MIDI": "0",
            "FL_BRIDGE_ENABLE_WRITES": "0",
            "FL_BRIDGE_SANDBOXED": "1",
        }
    )
    with tempfile.TemporaryDirectory(prefix="postfader-clean-wheel-") as raw:
        root = Path(raw)
        venv_dir = root / "clean venv"
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        python = (
            venv_dir / "Scripts" / "python.exe"
            if os.name == "nt"
            else venv_dir / "bin" / "python"
        )
        run([python, "-m", "pip", "install", wheel], cwd=root, env=environment)
        run(
            [
                python,
                ROOT / "scripts" / "verify_installed_package.py",
                "--manifest",
                ROOT / "manifest.json",
                "--expected-version",
                version,
            ],
            cwd=root,
            env=environment,
        )
        scripts_dir = python.parent
        suffix = ".exe" if os.name == "nt" else ""
        for command in (
            "fl-studio-mcp",
            "postfader",
            "postfader-install-bridge",
            "postfader-doctor",
            "postfader-plugin-report",
            "postfader-setup",
        ):
            run([scripts_dir / (command + suffix), "--help"], cwd=root, env=environment)
        run(
            [scripts_dir / ("postfader" + suffix), "setup", "--help"],
            cwd=root,
            env=environment,
        )
    print("clean wheel smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
