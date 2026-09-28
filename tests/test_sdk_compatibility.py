"""Compatibility contract for the supported MCP SDK version range."""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from jsonschema import Draft202012Validator

from fl_studio_mcp import mcp_server as server_module
from fl_studio_mcp.mcp_server import mcp
from fl_studio_mcp.workflows import validate_batch_operations


class MCPCompatibilityTests(unittest.TestCase):
    def test_all_tools_register_with_strict_input_schemas(self) -> None:
        tools = asyncio.run(mcp.list_tools())
        self.assertTrue(tools)
        non_strict = [
            tool.name
            for tool in tools
            if tool.input_schema.get("additionalProperties") is not False
            or any(
                definition.get("type") == "object"
                and definition.get("additionalProperties") is not False
                for definition in tool.input_schema.get("$defs", {}).values()
            )
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

    def test_mix_plan_examples_validate_through_exported_schema_and_batch_kernel(self) -> None:
        tool = next(tool for tool in asyncio.run(mcp.list_tools()) if tool.name == "mix_create_plan")
        schema = tool.input_schema
        properties = schema["properties"]
        arguments = {
            name: properties[name]["examples"][0]
            for name in ("title", "operations", "rationale")
        }
        Draft202012Validator(schema).validate(arguments)
        operations = validate_batch_operations(arguments["operations"])
        self.assertEqual([item.operation_id for item in operations], [item["operation_id"] for item in arguments["operations"]])
        for reference in properties["operations"]["items"]["oneOf"]:
            variant = schema["$defs"][reference["$ref"].rsplit("/", 1)[-1]]
            self.assertTrue(variant.get("description"))
            self.assertIs(variant.get("additionalProperties"), False)

        invalid = {**arguments, "operations": [{**arguments["operations"][0], "volume_db": 7.0}]}
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(invalid)))
        with self.assertRaises(ValueError):
            validate_batch_operations(arguments["operations"] * 2)

    def test_atlas_request_guidance_and_examples_survive_sdk_registration(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
        for name in ("plugins_atlas_search", "plugins_atlas_get_product", "plugins_atlas_recommend", "plugins_atlas_inspect_loaded"):
            with self.subTest(tool=name):
                schema = tools[name].input_schema
                request = schema["properties"]["request"]
                self.assertTrue(request.get("description"))
                definition = schema["$defs"][request["$ref"].rsplit("/", 1)[-1]]
                self.assertIs(definition.get("additionalProperties"), False)
                for field in definition["properties"].values():
                    self.assertTrue(field.get("description"))
                    for example in field.get("examples", []):
                        Draft202012Validator({**field, "$defs": schema["$defs"]}).validate(example)

        for name in ("plugins_atlas_search", "plugins_atlas_recommend", "plugins_atlas_inspect_loaded"):
            Draft202012Validator(tools[name].input_schema).validate({"request": {}})
        invalid = {"request": {"only_used": False, "match_limit": 129}}
        self.assertTrue(list(Draft202012Validator(tools["plugins_atlas_inspect_loaded"].input_schema).iter_errors(invalid)))

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

    def test_note_pagination_rejects_invalid_bounds_before_adapter(self) -> None:
        with mock.patch.object(
            server_module, "read_piano_roll_notes",
            side_effect=AssertionError("invalid SDK input must never reach FL inspection"),
        ) as adapter:
            for invalid_page in (
                {"offset": -1}, {"offset": 1_000_001},
                {"limit": 0}, {"limit": 2049},
            ):
                with self.subTest(page=invalid_page), self.assertRaises(Exception):
                    asyncio.run(mcp.call_tool("piano_roll_read_notes", {
                        "channel_index": 0, "pattern_number": 1, **invalid_page,
                    }))
                adapter.assert_not_called()

    def test_render_request_rejects_unknown_nested_options_before_adapter(self) -> None:
        with mock.patch.object(
            server_module, "get_saved_project_render_jobs",
            side_effect=AssertionError("invalid SDK input must never create a render manager"),
        ) as manager:
            for extra in (
                {"sample_rate_hz": 48_000},
                {"options": {"bit_depth": 24, "stems": True}},
            ):
                with self.subTest(extra=extra), self.assertRaisesRegex(
                    Exception, "Extra inputs are not permitted"
                ):
                    asyncio.run(mcp.call_tool("postfader_render_saved_project", {
                        "request": {
                            "project_path": "/unused/project.flp",
                            "output_directory": "/unused/renders",
                            **extra,
                        },
                    }))
                manager.assert_not_called()

    def test_render_job_ids_are_validated_before_registry_lookup(self) -> None:
        with mock.patch.object(
            server_module, "get_saved_project_render_jobs",
            side_effect=AssertionError("invalid job IDs must never reach the render registry"),
        ) as manager:
            for tool_name in ("postfader_render_get_job", "postfader_render_cancel"):
                for job_id in ("", "a" * 31, "A" * 32, "../project.flp"):
                    with self.subTest(tool=tool_name, job_id=job_id), self.assertRaises(Exception):
                        asyncio.run(mcp.call_tool(tool_name, {"job_id": job_id}))
                    manager.assert_not_called()

    def test_plugin_destination_is_checked_before_desktop_dispatch(self) -> None:
        with mock.patch.object(server_module, "load_plugin") as adapter:
            for request in (
                {"name": "FLEX", "kind": "instrument", "track_index": 1},
                {"name": "Fruity Reeverb 2", "kind": "effect"},
                {"name": "Fruity Reeverb 2", "kind": "effect", "track_index": True},
                {"name": "FLEX", "kind": "instrument", "menu_script": "unexpected"},
            ):
                with self.subTest(request=request), self.assertRaises(Exception):
                    asyncio.run(mcp.call_tool("plugins_load", {"request": request}))
                adapter.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
