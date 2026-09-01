"""Focused deterministic tests for the isolated semantic processing layer."""

from __future__ import annotations

import unittest
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
)
from fl_studio_mcp.track_b_contracts import MixerEffectTarget


class SemanticProcessingTests(unittest.TestCase):
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
