#!/usr/bin/env python3
"""Print or explicitly write inert Codex/Claude MCP configuration examples."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT))

from fl_studio_mcp.client_config import (  # noqa: E402
    configuration_facts,
    render_configuration,
    write_new_configuration,
)
from fl_studio_mcp.host_config import (  # noqa: E402
    HostConfigurationError,
    platform_family,
)


def default_interpreter(platform_name: str | None = None) -> Path:
    """Return the native venv interpreter path, even before the venv exists."""

    if platform_family(platform_name) == "windows":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate configuration without editing Codex, Claude, or project files. "
            "Stdout is the default; --output creates one new file and refuses overwrite."
        )
    )
    parser.add_argument(
        "--format",
        required=True,
        choices=("codex-toml", "codex-command", "claude-json"),
    )
    parser.add_argument("--repository-root", default=os.fspath(ROOT))
    parser.add_argument("--python", default=os.fspath(default_interpreter()))
    parser.add_argument("--user-data-dir")
    parser.add_argument(
        "--transport", choices=("automatic", "midi"), default="automatic"
    )
    parser.add_argument("--midi-port")
    parser.add_argument("--server-name", default="fl-studio")
    parser.add_argument(
        "--output",
        help="Create this new file. Existing files are never overwritten.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        facts = configuration_facts(
            repository_root=args.repository_root,
            interpreter=args.python,
            user_data_dir=args.user_data_dir,
            transport=args.transport,
            midi_port=args.midi_port,
            server_name=args.server_name,
        )
        rendered = render_configuration(facts, args.format)
        if args.output:
            destination = write_new_configuration(args.output, rendered)
            print(destination)
        else:
            print(rendered, end="")
    except (HostConfigurationError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
