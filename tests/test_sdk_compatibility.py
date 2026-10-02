"""Compatibility contract for the supported MCP SDK version range."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import pkgutil
import unittest
from unittest import mock

from jsonschema import Draft202012Validator
from pydantic import BaseModel

import fl_studio_mcp
from fl_studio_mcp import mcp_server as server_module
from fl_studio_mcp.mcp_server import mcp
from fl_studio_mcp.workflows import validate_batch_operations


def _schema_nodes(node):
    """Yield every schema object, skipping data such as examples and defaults."""

    if not isinstance(node, dict):
        return
    yield node
    for key, value in node.items():
        if key in {"items", "additionalProperties", "not"} and isinstance(value, dict):
            yield from _schema_nodes(value)
        elif key in {"anyOf", "oneOf", "allOf", "prefixItems"}:
            for item in value:
                yield from _schema_nodes(item)
        elif key in {"properties", "$defs"}:
            for item in value.values():
                yield from _schema_nodes(item)


class MCPCompatibilityTests(unittest.TestCase):
    def test_advertised_input_schemas_are_closed_valid_and_compact(self) -> None:
        tools = asyncio.run(mcp.list_tools())
        self.assertTrue(tools)
        open_objects = set()
        sizes = {}
        for tool in tools:
            schema = tool.input_schema
            Draft202012Validator.check_schema(schema)
            sizes[tool.name] = len(json.dumps(schema, separators=(",", ":")))
            definitions = schema.get("$defs", {})
            for node in _schema_nodes(schema):
                reference = node.get("$ref")
                if reference is not None:
                    self.assertIn(reference.rsplit("/", 1)[-1], definitions, tool.name)
                if "properties" in node and node.get("additionalProperties") is not False:
                    open_objects.add(tuple(sorted(node["properties"])))
        # Only the Production Run operation item is open: its fields depend on
        # the operation, are validated by the server, and are served on demand.
        self.assertEqual(open_objects, {("after", "operation", "operation_id")})
        # Clients load this listing into the model's context. Plans alone once
        # advertised about 240 KB each; keep the whole listing far below that.
        self.assertLess(max(sizes.values()), 40_000)
        self.assertLess(sum(sizes.values()), 350_000)

    def test_operation_schemas_are_served_on_demand(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
        plan = tools["postfader_execute_run"].input_schema["$defs"]["ProductionRunPlan"]
        advertised = plan["properties"]["operations"]["items"]["properties"]["operation"]["enum"]

        result = asyncio.run(
            mcp.call_tool("postfader_describe_operations", {"operations": ["generate_melody"]})
        )
        listed = {item["operation"]: item for item in result.structured_content["operations"]}
        self.assertEqual(sorted(listed), sorted(advertised))
        self.assertEqual(
            [name for name, item in listed.items() if not item["summary"].endswith(".")], []
        )
        self.assertIsNone(listed["write_note_sequence"]["json_schema"])

        validator = Draft202012Validator(listed["generate_melody"]["json_schema"])
        operation = {"operation": "generate_melody", "operation_id": "melody-1", "bars": 8}
        validator.validate(operation)
        self.assertTrue(list(validator.iter_errors({**operation, "bar_count": 8})))

    def test_every_contract_model_rejects_unknown_fields(self) -> None:
        models = set()
        for info in pkgutil.walk_packages(fl_studio_mcp.__path__, "fl_studio_mcp."):
            if "._bridge" in info.name:
                continue
            module = importlib.import_module(info.name)
            models.update(
                value
                for _, value in inspect.getmembers(module, inspect.isclass)
                if issubclass(value, BaseModel)
                and value.__module__.startswith("fl_studio_mcp.")
            )
        self.assertTrue(models)
        self.assertEqual(
            sorted(
                model.__qualname__
                for model in models
                if model.model_config.get("extra") != "forbid"
            ),
            [],
        )
        # Results describe something that already happened, so nothing
        # downstream may edit them. The original read contracts predate this.
        self.assertEqual(
            sorted(
                model.__qualname__
                for model in models
                if model.__module__ != "fl_studio_mcp.contracts"
                and not model.model_config.get("frozen")
            ),
            [],
        )

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
            # The listing advertises plan operations by name only; the call is
            # still validated against each operation's full model.
            "Production Run operation": (
                "postfader_validate_run",
                {
                    "request": {
                        "brief": "Write a melody.",
                        "scope": {"kind": "whole_project", "description": "Project."},
                        "allowed_changes": ["composition"],
                        "completion_target": "A melody.",
                    },
                    "plan": {
                        "plan_id": "plan-1",
                        "operations": [
                            {
                                "operation": "generate_melody",
                                "operation_id": "melody-1",
                                "unexpected_nested": True,
                            }
                        ],
                    },
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
