"""Focused deterministic tests for the isolated semantic processing layer."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from fl_studio_mcp import production_runs as runs
from fl_studio_mcp.creation_pipeline.processing import (
    EffectCoverageReport,
    LoadedProcessingCapability,
    ProcessingGoal,
    ProcessingPlan,
    ProcessingRequest,
    ResolvedSemanticControl,
    SemanticControlResolution,
    SemanticControlValue,
    SemanticPluginAction,
    evaluate_effect_coverage,
    plan_processing,
    resolve_semantic_control,
)
from fl_studio_mcp.creation_pipeline.semantic_actions import apply_processing_plan
from fl_studio_mcp.plugin_atlas import (
    AdapterControl,
    AtlasManifest,
    AtlasRegistry,
    ControlAdapter,
    ProductKnowledge,
    RuntimeParameterObservation,
    load_bundled_registry,
)
from fl_studio_mcp.track_b_contracts import MixerEffectTarget


class SemanticProcessingTests(unittest.TestCase):
    def stock_capability(self, product_id: str, names: tuple[str, ...]) -> LoadedProcessingCapability:
        registry = load_bundled_registry()
        product = registry.product(product_id)
        adapter = next(row for row in registry.adapters if row.product_id == product_id)
        return LoadedProcessingCapability(
            target=MixerEffectTarget(track_index=5, slot_index=2), plugin_name=product.name,
            product_id=product_id, adapter_id=adapter.adapter_id, category=adapter.category,
            control_evidence=True, adapter_available=True, atlas_match=True,
            runtime_parameters=tuple(RuntimeParameterObservation(index=index + 37, name=name) for index, name in enumerate(names)),
        )

    def test_goal_only_stock_reverb_resolves_real_parameter_names_and_strength(self) -> None:
        registry = load_bundled_registry()
        capability = self.stock_capability("image-line.fruity-reeverb-2", ("Decay", "Wet", "Dry", "HighCut"))
        plans = [plan_processing(ProcessingRequest(goals=(ProcessingGoal(goal="add_depth", strength=strength),)), loaded_plugins=(capability,), registry=registry) for strength in (0.25, 0.75)]
        self.assertEqual(len(plans[0].actions), 2)
        self.assertEqual({action.resolution.control.parameter_index for action in plans[0].actions}, {37, 38})
        self.assertGreater(plans[1].actions[0].control.display_value, plans[0].actions[0].control.display_value)
        self.assertNotEqual(plans[0].plan_id, plans[1].plan_id)
        self.assertTrue(all(action.resolution.control.setter == "fl_set_plugin_param_display" for action in plans[0].actions))

    def test_goal_only_eq_requires_observed_named_band(self) -> None:
        registry = load_bundled_registry()
        capability = self.stock_capability("image-line.fruity-parametric-eq2", ("Band 2 freq", "Band 2 level", "Band 2 width"))
        request = ProcessingRequest(goals=(ProcessingGoal(goal="reduce_mud", strength=0.8),))
        plan = plan_processing(request, loaded_plugins=(capability,), registry=registry)
        self.assertEqual(len(plan.actions), 2)
        self.assertEqual(plan.actions[1].control.display_value, -2.4)
        missing = capability.model_copy(update={"runtime_parameters": ()})
        blocked = plan_processing(request, loaded_plugins=(missing,), registry=registry)
        self.assertFalse(blocked.actions)
        self.assertTrue(blocked.missing_capabilities)

    def test_shortening_space_reduces_current_values_instead_of_lengthening_them(self) -> None:
        registry = load_bundled_registry()
        capability = self.stock_capability("image-line.fruity-reeverb-2", ("Decay", "Wet", "Dry", "HighCut"))
        observations = list(capability.runtime_parameters)
        observations[0] = observations[0].model_copy(update={"display": "100 ms"})
        observations[1] = observations[1].model_copy(update={"display": "4 %"})
        capability = capability.model_copy(update={"runtime_parameters": tuple(observations)})
        plan = plan_processing(ProcessingRequest(goals=(ProcessingGoal(goal="shorten_space", strength=0.5),)), loaded_plugins=(capability,), registry=registry)
        self.assertEqual(len(plan.actions), 2)
        self.assertAlmostEqual(plan.actions[0].control.display_value, 0.065)
        self.assertLess(plan.actions[1].control.display_value, 4.0)

    def test_stock_parameter_contracts_resolve_all_five_recipes(self) -> None:
        registry = load_bundled_registry()
        fixture = json.loads((Path(__file__).parent / "fixtures" / "stock-effect-controls-v1.json").read_text())
        cases = (
            ("reeverb", "add_depth", (5, 12), (1.5, 15.0)),
            ("reeverb", "shorten_space", (5, 12), (1.3, 28.0)),
            ("eq2", "add_presence", (9, 2), (3000.0, 1.25)),
            ("compressor", "level_vocal", (0, 1, 3, 4), (-18.0, 2.5, 10.0, 110.0)),
            ("limiter", "limit_peaks", (2,), (-1.0,)),
            ("delay", "rhythmic_echo", (23, 14), (15.0, 22.5)),
        )
        for key, goal, indices, values in cases:
            with self.subTest(key=key, goal=goal):
                plan = plan_processing(ProcessingRequest(goals=(ProcessingGoal(goal=goal),)), loaded_plugins=({**fixture[key], "track_index": 5, "slot_index": 2},), registry=registry)
                self.assertFalse(plan.missing_capabilities)
                self.assertEqual(tuple(action.resolution.control.parameter_index for action in plan.actions), indices)
                self.assertEqual(tuple(action.control.display_value for action in plan.actions), values)
                if key == "delay":
                    self.assertNotIn(4, indices)  # Tempo-synced Time display is 4:0, not milliseconds.
                    self.assertTrue(any("tempo-sync" in reason for candidate in plan.candidates for reason in candidate.reasons))

    def test_semantic_executor_preserves_requested_display_units(self) -> None:
        registry = load_bundled_registry()
        capability = self.stock_capability("image-line.fruity-parametric-eq2", ("Band 3 freq", "Band 3 level", "Band 3 width"))
        plan = plan_processing(ProcessingRequest(goals=(ProcessingGoal(goal="add_presence"),)), loaded_plugins=(capability,), registry=registry)
        writes = []
        def setter(**kwargs):
            writes.append(kwargs)
            return {"verified": True, "outcome_known": True}
        receipt = apply_processing_plan(plan, setter_callbacks={"fl_set_plugin_param_display": setter})
        self.assertTrue(receipt.completed)
        self.assertEqual(writes[0]["target_value"], 3000.0)
        self.assertEqual(writes[0]["display_unit"], "Hz")
        self.assertEqual(writes[0]["target_unit"], "Hz")
        self.assertEqual(writes[1]["display_unit"], "dB")

    def test_legacy_callback_cannot_drop_units_and_still_dispatch(self) -> None:
        registry = load_bundled_registry()
        capability = self.stock_capability("image-line.fruity-parametric-eq2", ("Band 3 freq", "Band 3 level", "Band 3 width"))
        plan = plan_processing(ProcessingRequest(goals=(ProcessingGoal(goal="add_presence"),)), loaded_plugins=(capability,), registry=registry)
        writes = []
        def legacy(parameter, target_value):
            writes.append((parameter, target_value))
            return {"verified": True, "outcome_known": True}
        result = apply_processing_plan(plan, setter_callbacks={"fl_set_plugin_param_display": legacy})
        self.assertEqual(writes, [])
        self.assertEqual(result.attempted_count, 0)
        self.assertTrue(result.outcome_known)
        self.assertEqual(result.results[0].status, "missing_setter")
        def modern(parameter, target_value, target_unit):
            writes.append(target_unit)
            return {"verified": True, "outcome_known": True}
        result = apply_processing_plan(plan, setter_callbacks={"fl_set_plugin_param_display": modern})
        self.assertTrue(result.completed)
        self.assertEqual(writes, ["Hz", "dB"])
        def with_action(action):
            return {"verified": bool(action.resolution.control.display_unit), "outcome_known": True}
        self.assertTrue(apply_processing_plan(plan, setter_callbacks={"fl_set_plugin_param_display": with_action}).completed)

    def test_explicit_controls_override_starting_recipe_and_dry_disables_actions(self) -> None:
        registry = load_bundled_registry()
        capability = self.stock_capability("image-line.fruity-reeverb-2", ("Decay", "Wet", "Dry", "HighCut"))
        request = ProcessingRequest(goals=(ProcessingGoal(goal="add_depth", controls=(SemanticControlValue(control_role="decay", display_value=0.25),)),))
        plan = plan_processing(request, loaded_plugins=(capability,), registry=registry)
        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(plan.actions[0].control.display_value, 0.25)
        dry = plan_processing(request.model_copy(update={"dry_by_design": True}), loaded_plugins=(capability,), registry=registry)
        self.assertFalse(dry.actions)

    def test_automatic_eq_goals_do_not_silently_replace_each_others_band(self) -> None:
        registry = load_bundled_registry()
        capability = self.stock_capability("image-line.fruity-parametric-eq2", ("Band 3 freq", "Band 3 level", "Band 3 width"))
        request = ProcessingRequest(goals=(ProcessingGoal(goal_id="presence", goal="add_presence"), ProcessingGoal(goal_id="air", goal="add_air")))
        plan = plan_processing(request, loaded_plugins=(capability,), registry=registry)
        self.assertEqual(len(plan.actions), 2)
        self.assertEqual(plan.actions[0].control.display_value, 3000.0)
        self.assertIn("conflicts", plan.missing_capabilities[0].reason)
        second = capability.model_copy(update={"target": MixerEffectTarget(track_index=5, slot_index=3)})
        accommodated = plan_processing(request, loaded_plugins=(capability, second), registry=registry)
        self.assertEqual(len(accommodated.actions), 4)
        self.assertFalse(accommodated.missing_capabilities)

    @classmethod
    def setUpClass(cls) -> None:
        cls.product = ProductKnowledge(
            product_id="example.reverb",
            name="Example Reverb",
            kind="effect",
            origin="stock",
            plugin_kinds=("effect",),
            categories=("reverb",),
        )
        cls.display_control = AdapterControl(
            control_id="decay",
            role="reverb.decay",
            parameter_index=2,
            names=("Decay",),
            unit="s",
            preferred_write_tool="fl_set_plugin_param_display",
        )
        cls.option_control = AdapterControl(
            control_id="mode",
            role="reverb.mode",
            parameter_index=3,
            kind="enumerated",
            options=("Plate", "Room"),
            preferred_write_tool="fl_set_plugin_param_option",
        )
        cls.normalized_control = AdapterControl(
            control_id="mix",
            role="reverb.mix",
            parameter_index=4,
            preferred_write_tool="fl_set_plugin_param",
        )
        cls.adapter = ControlAdapter(
            adapter_id="example.reverb.adapter",
            product_id=cls.product.product_id,
            reported_names=(cls.product.name,),
            category="reverb",
            supported_intents=("add_depth",),
            controls=(
                cls.display_control,
                cls.option_control,
                cls.normalized_control,
            ),
        )
        cls.registry = AtlasRegistry.from_parts(
            AtlasManifest(dataset_version="semantic-tests"),
            products=(cls.product,),
            adapters=(cls.adapter,),
        )
        cls.target = MixerEffectTarget(track_index=4, slot_index=1)

    def capability(
        self,
        *,
        name: str = "Example Reverb",
        category: str = "reverb",
        control_evidence: bool = True,
        target: MixerEffectTarget | None = None,
        product_id: str | None = "example.reverb",
        adapter_id: str | None = "example.reverb.adapter",
    ) -> LoadedProcessingCapability:
        return LoadedProcessingCapability(
            target=target or self.target,
            plugin_name=name,
            product_id=product_id,
            adapter_id=adapter_id,
            category=category,
            supported_techniques=("add_depth",),
            controls=("reverb.decay", "reverb.mode", "reverb.mix"),
            control_evidence=control_evidence,
            adapter_available=adapter_id is not None,
            atlas_match=product_id is not None,
        )

    def action(
        self,
        action_id: str,
        *,
        setter: str = "fl_set_plugin_param_display",
        depends_on: tuple[str, ...] = (),
        target_fingerprint: str | None = None,
        verified_resolution: bool = True,
    ) -> SemanticPluginAction:
        control = SemanticControlValue(
            control_role="reverb.decay", display_value=1.8, parameter=2
        )
        resolved = ResolvedSemanticControl(
            control_role=control.control_role,
            control_id="decay",
            parameter_index=2,
            setter=setter,  # type: ignore[arg-type]
            display_value=1.8 if setter.endswith("display") else None,
            option="Plate" if setter.endswith("option") else None,
            normalized_value=0.5 if setter == "fl_set_plugin_param" else None,
        )
        resolution = SemanticControlResolution(
            request=control,
            control=resolved if verified_resolution else None,
            status="resolved" if verified_resolution else "unresolved",
            reason=None if verified_resolution else "unknown control",
        )
        return SemanticPluginAction(
            action_id=action_id,
            goal_id="g",
            role="lead",
            target=self.target,
            target_fingerprint=target_fingerprint,
            plugin_name="Example Reverb",
            product_id="example.reverb",
            adapter_id="example.reverb.adapter",
            control=control,
            resolution=resolution,
            depends_on=depends_on,
        )

    def test_effect_covered_dry_missing_and_unresolved_states(self) -> None:
        covered_request = ProcessingRequest(
            goals=(
                ProcessingGoal(
                    goal_id="depth",
                    role="lead",
                    goal="add_depth",
                    target=self.target,
                ),
            )
        )
        covered = evaluate_effect_coverage(
            covered_request,
            loaded_plugins=(self.capability(),),
            registry=self.registry,
        )
        self.assertIsInstance(covered, EffectCoverageReport)
        self.assertEqual(covered.state, "effect_covered")

        dry = evaluate_effect_coverage(
            ProcessingRequest(dry_by_design=True),
            registry=self.registry,
        )
        self.assertEqual(dry.state, "dry_by_design")
        self.assertEqual(dry.processing_state, "dry_by_design")

        missing = evaluate_effect_coverage(
            covered_request, registry=self.registry
        )
        self.assertEqual(missing.state, "missing_requested_effect")
        self.assertTrue(missing.required_processing_missing)

        unresolved = evaluate_effect_coverage(
            covered_request,
            loaded_plugins=(self.capability(control_evidence=False),),
            registry=self.registry,
        )
        self.assertEqual(unresolved.state, "unresolved_effect")

    def test_plan_chooses_loaded_compatible_effect_not_unrelated_or_atlas_only(self) -> None:
        request = ProcessingRequest(
            goals=(
                ProcessingGoal(
                    goal_id="depth",
                    role="lead",
                    goal="add_depth",
                    target=self.target,
                    controls=(
                        SemanticControlValue(
                            control_role="reverb.decay", display_value=1.8
                        ),
                    ),
                ),
            )
        )
        unrelated = self.capability(
            category="compressor",
            target=MixerEffectTarget(track_index=5, slot_index=1),
        )
        plan = plan_processing(
            request,
            loaded_plugins=(unrelated, self.capability()),
            registry=self.registry,
        )
        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(plan.actions[0].plugin_name, "Example Reverb")
        self.assertNotIn("Atlas-only", {item.plugin_name for item in plan.candidates})
        self.assertEqual(plan.actions[0].resolution.control.setter, "fl_set_plugin_param_display")

    def test_standalone_processing_plan_requires_and_consumes_live_observations(
        self,
    ) -> None:
        operation = runs.PlanProcessingOperation(
            operation_id="plan-processing",
            request=ProcessingRequest(request_id="standalone-processing"),
        )
        observation = self.capability().model_dump(mode="python")
        planned = ProcessingPlan(
            plan_id="standalone-plan",
            request_id="standalone-processing",
            completion_target="restrained_first_pass",
        )

        self.assertTrue(runs._creation_plan_needs_readiness((operation,)))
        with mock.patch.object(
            runs,
            "plan_processing",
            return_value=planned,
        ) as planner:
            result = runs._dispatch_operation(
                operation,
                session_fingerprint="a" * 32,
                outputs={},
                processing_observations=(observation,),
            )

        self.assertIs(result, planned)
        planner.assert_called_once()
        self.assertEqual(
            planner.call_args.kwargs["loaded_plugins"],
            (observation,),
        )

    def test_semantic_setter_precedence_and_normalized_mapping_guard(self) -> None:
        display = resolve_semantic_control(
            SemanticControlValue(
                control_role="reverb.decay",
                display_value=20,
                normalized_value=0.2,
                normalized_mapping="known-decay-v1",
            ),
            adapter=self.adapter,
        )
        self.assertEqual(display.control.setter, "fl_set_plugin_param_display")

        option = resolve_semantic_control(
            SemanticControlValue(
                control_role="reverb.mode",
                option="plate",
                normalized_value=0.1,
                normalized_mapping="known-mode-v1",
            ),
            adapter=self.adapter,
        )
        self.assertEqual(option.control.setter, "fl_set_plugin_param_option")
        self.assertEqual(option.control.option, "Plate")

        normalized = resolve_semantic_control(
            SemanticControlValue(
                control_role="reverb.mix",
                normalized_value=0.4,
                normalized_mapping="known-mix-v1",
            ),
            adapter=self.adapter,
        )
        self.assertEqual(normalized.control.setter, "fl_set_plugin_param")

        with self.assertRaises(ValueError):
            SemanticControlValue(
                control_role="reverb.mix", normalized_value=0.4
            )

    def test_qualified_control_role_uses_adapter_local_role(self) -> None:
        """Public category-qualified roles map only to declared controls."""

        adapter = self.adapter.model_copy(
            update={
                "controls": (
                    self.display_control.model_copy(update={"role": "decay"}),
                )
            }
        )
        resolution = resolve_semantic_control(
            SemanticControlValue(control_role="reverb.decay", display_value="1.8 s"),
            adapter=adapter,
        )
        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.control.control_id, "decay")

    def test_unknown_control_is_rejected_before_mutation(self) -> None:
        calls: list[dict[str, object]] = []
        plan = ProcessingPlan(
            plan_id="unknown-control-plan",
            request_id="test",
            completion_target="restrained_first_pass",
            actions=(self.action("unknown", verified_resolution=False),),
        )
        receipt = apply_processing_plan(
            plan,
            setter_callbacks={"display": lambda **kwargs: calls.append(kwargs)},
        )
        self.assertEqual(calls, [])
        self.assertEqual(receipt.results[0].status, "unresolved_control")
        self.assertTrue(receipt.stopped)

    def test_stale_target_stops_before_writer(self) -> None:
        calls: list[object] = []
        plan = ProcessingPlan(
            plan_id="stale-target-plan",
            request_id="test",
            completion_target="restrained_first_pass",
            actions=(self.action("stale", target_fingerprint="a" * 64),),
        )
        receipt = apply_processing_plan(
            plan,
            setter_callbacks={"display": lambda **kwargs: calls.append(kwargs)},
            target_checker=lambda **_: False,
        )
        self.assertEqual(calls, [])
        self.assertEqual(receipt.results[0].status, "stale_target")

    def test_unknown_write_is_not_replayed_and_blocks_dependents(self) -> None:
        calls: list[str] = []

        def writer(**_: object) -> object:
            calls.append("write")
            raise RuntimeError("transport disappeared after dispatch")

        plan = ProcessingPlan(
            plan_id="unknown-write-plan",
            request_id="test",
            completion_target="restrained_first_pass",
            actions=(
                self.action("first"),
                self.action("second", depends_on=("first",)),
            ),
        )
        receipt = apply_processing_plan(plan, setter_callbacks={"display": writer})
        self.assertEqual(calls, ["write"])
        self.assertEqual([item.status for item in receipt.results], ["unknown", "blocked"])
        self.assertFalse(receipt.outcome_known)

    def test_receipts_are_preserved_when_a_later_action_stops(self) -> None:
        first_receipt = {"verified": True, "receipt_id": "first"}
        calls = 0

        def writer(**_: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                return first_receipt
            raise RuntimeError("unknown second result")

        plan = ProcessingPlan(
            plan_id="receipt-plan",
            request_id="test",
            completion_target="restrained_first_pass",
            actions=(
                self.action("first"),
                self.action("second", depends_on=()),
            ),
        )
        receipt = apply_processing_plan(plan, setter_callbacks={"display": writer})
        self.assertIs(receipt.results[0].receipt, first_receipt)
        self.assertEqual(receipt.receipts, (first_receipt,))
        self.assertEqual(receipt.results[1].status, "unknown")

    def test_batch_runs_without_confirmation_prompt(self) -> None:
        calls: list[dict[str, object]] = []

        def writer(**kwargs: object) -> object:
            calls.append(kwargs)
            self.assertNotIn("confirm", kwargs)
            return {"verified": True}

        plan = ProcessingPlan(
            plan_id="batch-plan",
            request_id="test",
            completion_target="restrained_first_pass",
            actions=(self.action("one"), self.action("two"), self.action("three")),
        )
        receipt = apply_processing_plan(plan, setter_callbacks={"display": writer})
        self.assertEqual(len(calls), 3)
        self.assertTrue(receipt.completed)
        self.assertTrue(receipt.verified)


if __name__ == "__main__":
    unittest.main(verbosity=2)
