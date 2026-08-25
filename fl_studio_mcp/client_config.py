"""Generate MCP client configuration without touching active client files.

The installer and configuration generator share these pure functions so the
interpreter, checkout, FL user-data root, and optional MIDI selection cannot
drift between Codex and Claude examples.  Nothing in this module writes unless
the caller explicitly requests a new output file.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from .host_config import (
    FL_BRIDGE_MIDI_PORT_ENV,
    FL_STUDIO_USER_DATA_ENV,
    HostConfigurationError,
    fl_studio_user_data_dir,
    require_midi_port_query,
)


TransportSelection = Literal["automatic", "midi"]
OutputFormat = Literal["codex-toml", "codex-command", "claude-json"]
_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class ClientConfigurationFacts:
    server_name: str
    repository_root: Path
    interpreter: Path
    environment: dict[str, str]
    transport: TransportSelection


def _absolute_path(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise HostConfigurationError(
            "%s must be absolute; got %r" % (label, os.fspath(value))
        )
    return path


def configuration_facts(
    *,
    repository_root: str | os.PathLike[str],
    interpreter: str | os.PathLike[str],
    user_data_dir: str | os.PathLike[str] | None = None,
    transport: TransportSelection = "automatic",
    midi_port: str | None = None,
    server_name: str = "fl-studio",
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> ClientConfigurationFacts:
    """Resolve immutable facts shared by every generated client format."""

    if not _SERVER_NAME.fullmatch(server_name):
        raise HostConfigurationError(
            "server name must contain only letters, numbers, hyphens, and "
            "underscores"
        )
    root = _absolute_path(repository_root, "repository root")
    python = _absolute_path(interpreter, "Python interpreter")
    selected_user_data = fl_studio_user_data_dir(
        user_data_dir,
        environ=environ,
        platform_name=platform_name,
    )
    environment = {
        FL_STUDIO_USER_DATA_ENV: os.fspath(selected_user_data),
    }
    if transport == "midi":
        selected_port = require_midi_port_query(
            midi_port,
            environ=environ,
            platform_name=platform_name,
        )
        environment.update(
            {
                "FL_BRIDGE_ENABLE_MIDI": "1",
                FL_BRIDGE_MIDI_PORT_ENV: selected_port,
            }
        )
    elif transport == "automatic":
        if midi_port is not None and midi_port.strip():
            raise HostConfigurationError(
                "--midi-port requires --transport midi; automatic transport "
                "must not silently enable a physical MIDI endpoint"
            )
    else:
        raise HostConfigurationError(
            "transport must be 'automatic' or 'midi'; got %r" % transport
        )
    if "FL_BRIDGE_ENABLE_WRITES" in environment:
        raise AssertionError("MCP client configuration must never enable writes")
    return ClientConfigurationFacts(
        server_name=server_name,
        repository_root=root,
        interpreter=python,
        environment=environment,
        transport=transport,
    )


def _toml_string(value: str | os.PathLike[str]) -> str:
    return json.dumps(os.fspath(value), ensure_ascii=True)


def render_codex_toml(facts: ClientConfigurationFacts) -> str:
    """Return an append-only TOML fragment for Codex config.toml."""

    prefix = "mcp_servers.%s" % facts.server_name
    lines = [
        "[%s]" % prefix,
        "command = %s" % _toml_string(facts.interpreter),
        'args = ["-m", "fl_studio_mcp.mcp_server"]',
        "cwd = %s" % _toml_string(facts.repository_root),
        "",
        "[%s.env]" % prefix,
    ]
    for key in sorted(facts.environment):
        lines.append("%s = %s" % (key, _toml_string(facts.environment[key])))
    return "\n".join(lines) + "\n"


def render_claude_json(facts: ClientConfigurationFacts) -> str:
    """Return a standalone Claude-compatible JSON example."""

    value = {
        "mcpServers": {
            facts.server_name: {
                "command": os.fspath(facts.interpreter),
                "args": ["-m", "fl_studio_mcp.mcp_server"],
                "cwd": os.fspath(facts.repository_root),
                "env": facts.environment,
            }
        }
    }
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_codex_add_command(facts: ClientConfigurationFacts) -> str:
    """Return the documented Codex CLI equivalent as PowerShell syntax."""

    pieces = ["codex", "mcp", "add", facts.server_name]
    for key in sorted(facts.environment):
        pieces.extend(
            ["--env", _powershell_quote("%s=%s" % (key, facts.environment[key]))]
        )
    pieces.extend(
        [
            "--",
            _powershell_quote(os.fspath(facts.interpreter)),
            "-m",
            "fl_studio_mcp.mcp_server",
        ]
    )
    return " ".join(pieces) + "\n"


def codex_add_argv(
    facts: ClientConfigurationFacts,
    *,
    codex_command: str = "codex",
) -> tuple[str, ...]:
    """Return a shell-free ``codex mcp add`` argument vector."""

    pieces = [codex_command, "mcp", "add", facts.server_name]
    for key in sorted(facts.environment):
        pieces.extend(["--env", "%s=%s" % (key, facts.environment[key])])
    pieces.extend(
        [
            "--",
            os.fspath(facts.interpreter),
            "-m",
            "fl_studio_mcp.mcp_server",
        ]
    )
    return tuple(pieces)


def render_configuration(
    facts: ClientConfigurationFacts, output_format: OutputFormat
) -> str:
    if output_format == "codex-toml":
        return render_codex_toml(facts)
    if output_format == "codex-command":
        return render_codex_add_command(facts)
    if output_format == "claude-json":
        return render_claude_json(facts)
    raise HostConfigurationError("unsupported output format %r" % output_format)


def write_new_configuration(path: str | os.PathLike[str], content: str) -> Path:
    """Write one explicitly selected new file, refusing every overwrite."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    return destination
