"""Safely register the local PostFader stdio server with Codex.

The supported Codex CLI owns its configuration file. This module never parses
or edits ``config.toml`` directly, never invokes a shell, and never replaces an
existing server with different settings.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from typing import Any

from .client_config import (
    ClientConfigurationFacts,
    codex_add_argv,
    render_codex_toml,
)


Runner = Callable[..., subprocess.CompletedProcess[str]]


class CodexRegistrationError(RuntimeError):
    """Codex registration could not be completed or verified safely."""


def _run(
    runner: Runner, arguments: tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise CodexRegistrationError("could not run Codex CLI: %s" % exc) from exc


def _detail(completed: subprocess.CompletedProcess[str]) -> str:
    return (completed.stderr or completed.stdout or "unknown Codex CLI error").strip()


def _list_servers(
    runner: Runner, codex_command: str
) -> list[Mapping[str, Any]]:
    completed = _run(runner, (codex_command, "mcp", "list", "--json"))
    if completed.returncode != 0:
        raise CodexRegistrationError(
            "could not inspect existing Codex MCP servers: %s" % _detail(completed)
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CodexRegistrationError(
            "Codex MCP inventory was not valid JSON"
        ) from exc
    if not isinstance(value, list) or not all(
        isinstance(item, Mapping) for item in value
    ):
        raise CodexRegistrationError("Codex MCP inventory had an unexpected shape")
    return value


def _find_server(
    servers: list[Mapping[str, Any]], server_name: str
) -> Mapping[str, Any] | None:
    matches = [server for server in servers if server.get("name") == server_name]
    if len(matches) > 1:
        raise CodexRegistrationError(
            "Codex reported more than one MCP server named %r" % server_name
        )
    return matches[0] if matches else None


def _matches(server: Mapping[str, Any], facts: ClientConfigurationFacts) -> bool:
    transport = server.get("transport")
    if not isinstance(transport, Mapping):
        return False
    return (
        server.get("enabled") is True
        and transport.get("type") == "stdio"
        and transport.get("command") == os.fspath(facts.interpreter)
        and transport.get("args") == ["-m", "fl_studio_mcp.mcp_server"]
        and transport.get("env") == facts.environment
        and transport.get("env_vars") in (None, [])
        and transport.get("cwd") in (None, os.fspath(facts.repository_root))
    )


def render_registration_plan(facts: ClientConfigurationFacts) -> str:
    """Show the exact stdio fields that ``codex mcp add`` will persist."""

    return json.dumps(
        {
            "server_name": facts.server_name,
            "transport": {
                "type": "stdio",
                "command": os.fspath(facts.interpreter),
                "args": ["-m", "fl_studio_mcp.mcp_server"],
                "env": facts.environment,
            },
        },
        indent=2,
        sort_keys=True,
    )


def register_codex(
    facts: ClientConfigurationFacts,
    *,
    apply: bool,
    codex_command: str = "codex",
    runner: Runner | None = None,
) -> dict[str, object]:
    """Plan or perform one fail-closed Codex MCP registration."""

    fallback = render_codex_toml(facts)
    if not apply:
        return {
            "status": "planned",
            "changed": False,
            "server_name": facts.server_name,
            "manual_toml": fallback,
        }

    if runner is None:
        runner = subprocess.run
        resolved_command = shutil.which(codex_command)
        if resolved_command is None:
            return {
                "status": "manual",
                "changed": False,
                "server_name": facts.server_name,
                "reason": "Codex CLI was not found",
                "manual_toml": fallback,
            }
        codex_command = resolved_command

    try:
        servers = _list_servers(runner, codex_command)
    except FileNotFoundError:
        return {
            "status": "manual",
            "changed": False,
            "server_name": facts.server_name,
            "reason": "Codex CLI was not found",
            "manual_toml": fallback,
        }

    existing = _find_server(servers, facts.server_name)
    if existing is not None:
        if _matches(existing, facts):
            return {
                "status": "current",
                "changed": False,
                "server_name": facts.server_name,
                "manual_toml": fallback,
            }
        return {
            "status": "conflict",
            "changed": False,
            "server_name": facts.server_name,
            "reason": "an existing Codex MCP server with this name has different settings",
            "manual_toml": fallback,
        }

    added = _run(runner, codex_add_argv(facts, codex_command=codex_command))
    if added.returncode != 0:
        raise CodexRegistrationError(
            "Codex MCP registration failed: %s" % _detail(added)
        )

    verified = _find_server(_list_servers(runner, codex_command), facts.server_name)
    if verified is None or not _matches(verified, facts):
        raise CodexRegistrationError(
            "Codex MCP registration did not verify; inspect the saved entry before "
            "changing or removing it"
        )
    return {
        "status": "added",
        "changed": True,
        "server_name": facts.server_name,
        "manual_toml": fallback,
    }
