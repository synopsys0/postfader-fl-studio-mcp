"""Focused MCP registration and boundary tests for the generic Plugin Atlas tools."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from fl_studio_mcp.mcp_server import mcp
from fl_studio_mcp.plugin_atlas import (
    AdapterControl,
    AtlasManifest,
    AtlasRegistry,
    ControlAdapter,
    ProductKnowledge,
    VendorKnowledge,
)
from fl_studio_mcp.plugin_atlas_mcp import (
    AtlasGetProductRequest,
    AtlasInspectLoadedRequest,
    AtlasSearchRequest,
    get_atlas_product,
    inspect_loaded_atlas,
    search_atlas,
)
from fl_studio_mcp.track_b_contracts import (
    MixerEffectTarget,
    TargetedLoadedPluginInventory,
    TargetedPluginSummary,
)


class PluginAtlasMCPTests(unittest.TestCase):
    @staticmethod
    def registry() -> AtlasRegistry:
        product = ProductKnowledge(
            product_id="vendor.delay",
            vendor_id="vendor",
            name="Example Delay",
            plugin_kinds=("effect",),
            kind="effect",
            origin="stock",
            catalog_scope="current_edition_matrix",
            lifecycle="current",
            categories=("delay",),
            description="A test delay.",
        )
        vendor = VendorKnowledge(vendor_id="vendor", name="Example Vendor")
        adapter = ControlAdapter(
            adapter_id="vendor.delay.adapter",
            product_id=product.product_id,
            reported_names=(product.name,),
            controls=(
                AdapterControl(
                    control_id="time",
                    parameter_index=0,
                    names=("Time",),
                    kind="numeric",
                ),
            ),
        )
        return AtlasRegistry.from_parts(
            AtlasManifest(dataset_version="test"),
            products=(product,),
            vendors=(vendor,),
            adapters=(adapter,),
        )

    def test_four_generic_atlas_tools_are_registered_at_111_total(self) -> None:
        tools = asyncio.run(mcp.list_tools())
        self.assertEqual(len(tools), 111)
        names = {tool.name for tool in tools}
        self.assertEqual(
            names & {
                "plugins_atlas_search",
                "plugins_atlas_get_product",
                "plugins_atlas_recommend",
                "plugins_atlas_inspect_loaded",
            },
            {
                "plugins_atlas_search",
                "plugins_atlas_get_product",
                "plugins_atlas_recommend",
                "plugins_atlas_inspect_loaded",
            },
        )

    def test_static_tools_are_closed_world_read_only_and_strict(self) -> None:
        tools = {
            tool.name: tool
            for tool in asyncio.run(mcp.list_tools())
            if tool.name.startswith("plugins_atlas_")
        }
        for name in (
            "plugins_atlas_search",
            "plugins_atlas_get_product",
            "plugins_atlas_recommend",
        ):
            with self.subTest(tool=name):
                annotations = tools[name].annotations
                self.assertIsNotNone(annotations)
                assert annotations is not None
                self.assertIs(annotations.read_only_hint, True)
                self.assertIs(annotations.destructive_hint, False)
                self.assertIs(annotations.open_world_hint, False)
                self.assertIs(
                    tools[name].input_schema.get("additionalProperties"), False
                )

    def test_live_tool_is_read_only_open_world_and_strict(self) -> None:
        tool = next(
            tool
            for tool in asyncio.run(mcp.list_tools())
            if tool.name == "plugins_atlas_inspect_loaded"
        )
        self.assertIsNotNone(tool.annotations)
        assert tool.annotations is not None
        self.assertIs(tool.annotations.read_only_hint, True)
        self.assertIs(tool.annotations.destructive_hint, False)
        self.assertIs(tool.annotations.open_world_hint, True)
        self.assertIs(tool.input_schema.get("additionalProperties"), False)

    def test_static_lookup_reuses_registry_and_does_not_touch_track_b(self) -> None:
        registry = self.registry()
        result = search_atlas(AtlasSearchRequest(query="delay"), registry=registry)
        self.assertEqual([row.product.product_id for row in result.results], ["vendor.delay"])
        product = get_atlas_product(
            # The lookup is exact-ID based; names and aliases are intentionally
            # resolved by the search tool instead.
            AtlasGetProductRequest(product_id="vendor.delay"),
            registry=registry,
        )
        self.assertEqual(product.product.product_id, "vendor.delay")

    def test_live_match_preserves_target_and_keeps_name_only_unproven(self) -> None:
        registry = self.registry()
        target = MixerEffectTarget(track_index=4, slot_index=2)
        summary = TargetedPluginSummary(
            target=target,
            name="Example Delay",
            reported_parameter_count=1,
            mix_level_normalized=1.0,
        )
        inventory = TargetedLoadedPluginInventory(
            observed_at=datetime.now(timezone.utc),
            plugins=[summary],
        )

        class FakeInspector:
            calls: list[bool] = []

            def scan_loaded_plugins(self, *, only_used: bool = False):
                self.calls.append(only_used)
                return inventory

        inspector = FakeInspector()
        response = inspect_loaded_atlas(
            AtlasInspectLoadedRequest(only_used=True),
            registry=registry,
            inspector=inspector,
        )
        self.assertEqual(inspector.calls, [True])
        self.assertEqual(len(response.plugins), 1)
        row = response.plugins[0]
        self.assertEqual(row.target, target)
        self.assertEqual(row.plugin.target, target)
        self.assertEqual(row.runtime.instance_id, "mixer_effect:4:2")
        self.assertIsNotNone(row.best_match)
        assert row.best_match is not None
        self.assertEqual(row.best_match.control_status, "name_only")
        self.assertEqual(row.best_match.parameter_evidence, ())
        self.assertIsNotNone(row.compatibility)
        assert row.compatibility is not None
        self.assertEqual(row.compatibility.compatibility, "name_only")
        self.assertIs(row.compatibility.control_proven, False)

    def test_unknown_top_level_atlas_arguments_fail_closed(self) -> None:
        async def invoke() -> None:
            await mcp.call_tool(
                "plugins_atlas_search",
                {"request": {"query": "delay"}, "unexpected": True},
            )

        with self.assertRaisesRegex(Exception, "Extra inputs are not permitted"):
            asyncio.run(invoke())


if __name__ == "__main__":
    unittest.main(verbosity=2)
