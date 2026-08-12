#!/usr/bin/env python3
"""Synchronize ``manifest.json`` tools with the MCP decorators in runtime code.

This deliberately parses the server instead of importing it, so the check is
hermetic and cannot initialize a transport. The first line of each tool
function's docstring becomes the MCPB catalog description.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "fl_studio_mcp" / "mcp_server.py"
MANIFEST = ROOT / "manifest.json"


def _decorated_tool_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        function = decorator.func
        if not (isinstance(function, ast.Attribute) and function.attr == "tool"):
            continue
        for keyword in decorator.keywords:
            if (
                keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                return keyword.value.value
    return None


def discover_tools(server: Path = SERVER) -> list[dict[str, str]]:
    """Return manifest entries in the order the runtime registers its tools."""
    tree = ast.parse(server.read_text(encoding="utf-8"), filename=str(server))
    tools: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = _decorated_tool_name(node)
        if name is None:
            continue
        if name in seen:
            raise ValueError(f"duplicate MCP tool name in {server}: {name}")
        docstring = (ast.get_docstring(node, clean=True) or "").strip()
        description = docstring.splitlines()[0] if docstring else ""
        if not description:
            raise ValueError(f"MCP tool {name!r} has no docstring description")
        seen.add(name)
        tools.append({"name": name, "description": description})
    if not tools:
        raise ValueError(f"no @mcp.tool(name=...) decorators found in {server}")
    return tools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if manifest.json is stale instead of rewriting it",
    )
    args = parser.parse_args(argv)

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    current = manifest.get("tools")
    expected = discover_tools()
    if current == expected:
        print(f"manifest.json tools are synchronized ({len(expected)} tools)")
        return 0

    if args.check:
        current_text = json.dumps(current, indent=2, ensure_ascii=False).splitlines()
        expected_text = json.dumps(expected, indent=2, ensure_ascii=False).splitlines()
        print("manifest.json tools are stale; regenerate with:", file=sys.stderr)
        print("  python3 scripts/sync_mcpb_manifest.py", file=sys.stderr)
        print(
            "\n".join(
                difflib.unified_diff(
                    current_text,
                    expected_text,
                    fromfile="manifest.json tools",
                    tofile="runtime tools",
                    lineterm="",
                )
            ),
            file=sys.stderr,
        )
        return 1

    manifest["tools"] = expected
    MANIFEST.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"updated manifest.json with {len(expected)} tools")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
