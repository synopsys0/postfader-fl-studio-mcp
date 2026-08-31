#!/usr/bin/env python3
"""Verify an installed wheel from outside its source checkout."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import tempfile
from pathlib import Path


EXPECTED_TOOL_COUNT = 111
EXPECTED_RESOURCE_COUNT = 8


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args(argv)

    os.environ["FL_BRIDGE_ENABLE_MIDI"] = "0"
    os.environ["FL_BRIDGE_SANDBOXED"] = "1"
    import fl_studio_mcp
    from fl_studio_mcp import acceptance, client_config, host_config  # noqa: F401
    from fl_studio_mcp.bridge_client import _MidiPortOwnership
    from fl_studio_mcp.bridge_install import (
        bridge_source_path,
        expected_bridge_deployment,
    )
    from fl_studio_mcp.mcp_server import mcp
    from fl_studio_mcp.plugin_atlas import load_bundled_registry
    from fl_studio_mcp.sound_selection import load_bundled_descriptors

    failures: list[str] = []
    distribution = importlib.metadata.distribution("postfader-fl-studio-mcp")
    if distribution.version != args.expected_version:
        failures.append("installed metadata version mismatch")
    if fl_studio_mcp.__version__ != args.expected_version:
        failures.append("installed package __version__ mismatch")
    entries = {
        entry.name: entry.value
        for entry in distribution.entry_points
        if entry.group == "console_scripts"
    }
    expected_entries = {
        "fl-studio-mcp": "fl_studio_mcp.mcp_server:main",
        "postfader": "fl_studio_mcp.cli:main",
        "postfader-install-bridge": "fl_studio_mcp.bridge_install:main",
        "postfader-doctor": "fl_studio_mcp.diagnostics:main",
        "postfader-plugin-report": "fl_studio_mcp.plugin_report:main",
        "postfader-plugin-atlas": "fl_studio_mcp.plugin_atlas.cli:main",
        "postfader-setup": "fl_studio_mcp.setup_wizard:main",
    }
    if entries != expected_entries:
        failures.append("installed console entry points differ: %s" % entries)

    try:
        atlas = load_bundled_registry()
        if not atlas.products:
            failures.append("installed Plugin Atlas contains no products")
        if not atlas.digest():
            failures.append("installed Plugin Atlas did not produce a content digest")
    except Exception as error:  # pragma: no cover - exercised in clean installs
        failures.append("installed Plugin Atlas could not be loaded: %s" % error)

    try:
        descriptors = load_bundled_descriptors()
        if not descriptors.descriptors:
            failures.append("installed Sound Selection descriptors are empty")
    except Exception as error:  # pragma: no cover - exercised in clean installs
        failures.append(
            "installed Sound Selection descriptors could not be loaded: %s" % error
        )

    bridge = bridge_source_path().read_bytes()
    if any(byte >= 128 for byte in bridge):
        failures.append("installed bridge is not pure ASCII")
    stamped, digest = expected_bridge_deployment()
    if not stamped or len(digest) != 64:
        failures.append("installed bridge stamping failed")

    manifest_names = {
        item["name"]
        for item in json.loads(args.manifest.read_text(encoding="utf-8"))["tools"]
    }
    installed_names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    if len(installed_names) != EXPECTED_TOOL_COUNT:
        failures.append(
            "installed MCP tool count is not %d: %d"
            % (EXPECTED_TOOL_COUNT, len(installed_names))
        )
    installed_resources = asyncio.run(mcp.list_resources())
    if len(installed_resources) != EXPECTED_RESOURCE_COUNT:
        failures.append(
            "installed MCP resource count is not %d: %d"
            % (EXPECTED_RESOURCE_COUNT, len(installed_resources))
        )
    if installed_names != manifest_names:
        failures.append(
            "installed MCP tool surface differs from manifest: %s"
            % sorted(installed_names ^ manifest_names)
        )

    with tempfile.TemporaryDirectory(prefix="postfader-installed-lock-") as raw:
        ownership = _MidiPortOwnership(
            "input=Installed Smoke; output=Installed Smoke", lock_dir=raw
        )
        ownership.acquire()
        evidence = ownership.evidence()
        ownership.release()
    if evidence.get("acquired") is not True:
        failures.append("installed platform ownership lock did not acquire")

    if failures:
        print("installed wheel verification failed:")
        for failure in failures:
            print("  - %s" % failure)
        return 1
    print(
        "installed wheel verified: version %s, %d tools, %d resources, platform lock acquired"
        % (args.expected_version, len(installed_names), len(installed_resources))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
