"""Compatibility contract for the supported MCP SDK version range."""

from __future__ import annotations

import asyncio
import importlib.metadata
import unittest

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase

from fl_studio_mcp.mcp_server import mcp


class MCPCompatibilityTests(unittest.TestCase):
    def test_installed_sdk_is_inside_the_declared_minor_range(self) -> None:
        version = importlib.metadata.version("mcp")
        major, minor = (int(part) for part in version.split(".", 2)[:2])
        self.assertEqual((major, minor), (2, 0), version)

    def test_lower_level_imports_used_by_the_server_remain_available(self) -> None:
        self.assertIsInstance(mcp, MCPServer)
        self.assertEqual(ArgModelBase.model_config.get("extra"), "forbid")

    def test_all_tools_register_with_strict_top_level_input_schemas(self) -> None:
        tools = asyncio.run(mcp.list_tools())
        self.assertEqual(len(tools), 114)
        non_strict = [
            tool.name
            for tool in tools
            if tool.input_schema.get("additionalProperties") is not False
        ]
        self.assertEqual(non_strict, [])

    def test_production_validate_run_nested_contracts_are_strict(self) -> None:
        tool = next(
            tool
            for tool in asyncio.run(mcp.list_tools())
            if tool.name == "postfader_validate_run"
        )
        schema = tool.input_schema
        definitions = schema.get("$defs", {})
        for field_name in ("request", "plan"):
            with self.subTest(field=field_name):
                field_schema = schema["properties"][field_name]
                reference = field_schema.get("$ref")
                if reference is not None:
                    field_schema = definitions[reference.rsplit("/", 1)[-1]]
                self.assertEqual(field_schema.get("type"), "object")
                self.assertIs(field_schema.get("additionalProperties"), False)

    def test_all_live_resources_register_through_the_sdk(self) -> None:
        resources = asyncio.run(mcp.list_resources())
        self.assertEqual(len(resources), 8)
        self.assertEqual(
            {str(resource.uri) for resource in resources},
            {
                "fl://capabilities",
                "fl://status",
                "fl://project",
                "fl://transport",
                "fl://mixer",
                "fl://channels",
                "fl://plugins",
                "fl://patterns",
            },
        )

    def test_unknown_tool_arguments_still_fail_closed(self) -> None:
        cases = {
            "read": ("fl_get_transport_state", {"unexpected": True}),
            "guarded write": (
                "fl_set_mixer_volume",
                {
                    "track_index": 3,
                    "volume_normalized": 0.5,
                    "unexpected": True,
                },
            ),
            "batch": (
                "fl_apply_verified_batch",
                {
                    "operations": [
                        {
                            "operation_id": "volume-1",
                            "operation": "mixer_volume",
                            "track_index": 3,
                            "volume_normalized": 0.5,
                        }
                    ],
                    "unexpected": True,
                },
            ),
            "nested batch operation": (
                "fl_apply_verified_batch",
                {
                    "operations": [
                        {
                            "operation_id": "volume-1",
                            "operation": "mixer_volume",
                            "track_index": 3,
                            "volume_normalized": 0.5,
                            "unexpected_nested": True,
                        }
                    ]
                },
            ),
        }

        for label, (name, arguments) in cases.items():
            with self.subTest(label=label):
                async def invoke() -> None:
                    await mcp.call_tool(name, arguments)

                with self.assertRaisesRegex(Exception, "Extra inputs are not permitted"):
                    asyncio.run(invoke())


if __name__ == "__main__":
    unittest.main(verbosity=2)
