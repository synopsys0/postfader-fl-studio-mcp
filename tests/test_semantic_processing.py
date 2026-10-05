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
    RoleProcessingRequest,
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
        self.assertTrue(all(action.resolution.control.setter == "display_value" for action in plans[0].actions))

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
        receipt = apply_processing_plan(plan, setter_callbacks={"display_value": setter})
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
        result = apply_processing_plan(plan, setter_callbacks={"display_value": legacy})
        self.assertEqual(writes, [])
        self.assertEqual(result.attempted_count, 0)
        self.assertTrue(result.outcome_known)
        self.assertEqual(result.results[0].status, "missing_setter")
        def modern(parameter, target_value, target_unit):
            writes.append(target_unit)
            return {"verified": True, "outcome_known": True}
        result = apply_processing_plan(plan, setter_callbacks={"display_value": modern})
        self.assertTrue(result.completed)
        self.assertEqual(writes, ["Hz", "dB"])
        def with_action(action):
            return {"verified": bool(action.resolution.control.display_unit), "outcome_known": True}
        self.assertTrue(apply_processing_plan(plan, setter_callbacks={"display_value": with_action}).completed)

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

    def bundled_observations(self):
        names = {
            "equalizer": ("Band 1 freq", "Band 1 level", "Band 2 freq", "Band 2 level", "Band 3 freq", "Band 3 level"),
            "compressor": ("Threshold", "Ratio", "Attack", "Release", "Gain"),
            "limiter": ("Gain", "Limiter ceiling", "Limiter attack time", "Limiter release time"),
            "reverb": ("Decay time", "Wet level", "Dry level", "HighCut"),
            "delay": ("Output wet", "Feedback level", "Time"),
        }
        displays = {"Decay time": "2.0sec", "Wet level": "40%", "HighCut": "12kHz", "Time": "4:0"}
        registry = load_bundled_registry()
        return {
            adapter.category: {
                "plugin_name": registry.product(adapter.product_id).name,
                "track_index": 5, "slot_index": slot,
                "parameters": [
                    {"index": index + 37, "name": name, "display": displays.get(name)}
                    for index, name in enumerate(names[adapter.category])
                ],
            }
            for slot, adapter in enumerate(registry.adapters)
        }

    def test_every_bundled_intent_has_a_resolved_plan_and_truthful_coverage(self):
        registry = load_bundled_registry()
        observations = self.bundled_observations()
        expected = {
            "equalizer": {"reduce_mud", "tame_harshness", "add_presence", "add_air", "tighten_low_end"},
            "compressor": {"control_dynamics", "add_punch", "level_vocal", "tighten_low_end"},
            "limiter": {"limit_peaks", "control_dynamics"},
            "reverb": {"add_depth", "shorten_space", "darken_reverb"},
            "delay": {"add_depth", "rhythmic_echo"},
        }
        for adapter in registry.adapters:
            self.assertEqual(set(adapter.supported_intents), expected[adapter.category])
            for intent in adapter.supported_intents:
                with self.subTest(adapter=adapter.adapter_id, intent=intent):
                    loaded = tuple(observations[c] for c in ("equalizer", "compressor")) if intent == "tighten_low_end" else (observations[adapter.category],)
                    request = ProcessingRequest(goals=(ProcessingGoal(goal=intent, required=True),))
                    plan = plan_processing(request, loaded_plugins=loaded, registry=registry)
                    coverage = evaluate_effect_coverage(request, loaded_plugins=loaded, registry=registry)
                    self.assertFalse(plan.missing_capabilities)
                    self.assertTrue(plan.actions)
                    self.assertIn(adapter.adapter_id, {action.adapter_id for action in plan.actions})
                    self.assertTrue(all(action.resolution.status == "resolved" for action in plan.actions))
                    self.assertEqual(coverage.state, "effect_covered")
                    self.assertFalse(coverage.required_processing_missing)
                    self.assertFalse(coverage.missing_capabilities)
                    self.assertEqual(len(plan.actions), len({action.action_id for action in plan.actions}))

    def test_tightening_requires_each_category_and_keeps_partial_work_visible(self):
        registry = load_bundled_registry()
        observations = self.bundled_observations()
        request = ProcessingRequest(goals=(ProcessingGoal(goal="tighten_low_end", required=True),))
        for categories, absent, count in (((), {"equalizer", "compressor"}, 0), (("equalizer",), {"compressor"}, 2), (("compressor",), {"equalizer"}, 4), (("equalizer", "compressor"), set(), 6)):
            with self.subTest(categories=categories):
                loaded = tuple(observations[c] for c in categories)
                plan = plan_processing(request, loaded_plugins=loaded, registry=registry)
                coverage = evaluate_effect_coverage(request, loaded_plugins=loaded, registry=registry)
                self.assertEqual(len(plan.actions), count)
                self.assertEqual({gap.category for gap in plan.missing_capabilities}, absent)
                self.assertEqual({gap.category for gap in coverage.missing_capabilities}, absent)
                self.assertEqual(coverage.required_processing_missing, bool(absent))
                self.assertEqual(coverage.state == "effect_covered", not absent)

    def test_new_recipes_preserve_delay_timing_and_darken_current_cutoff(self):
        registry = load_bundled_registry()
        observations = self.bundled_observations()
        for strength, expected in ((0.25, 12000.0 / 1.75), (0.75, 12000.0 / 3.25)):
            request = ProcessingRequest(goals=(ProcessingGoal(goal="darken_reverb", strength=strength),))
            plan = plan_processing(request, loaded_plugins=(observations["reverb"],), registry=registry)
            self.assertEqual(len(plan.actions), 1)
            self.assertAlmostEqual(plan.actions[0].control.display_value, expected, places=3)
            self.assertEqual(plan.actions[0].control.display_unit, "Hz")
        delay = plan_processing(ProcessingRequest(goals=(ProcessingGoal(goal="add_depth"),)), loaded_plugins=(observations["delay"],), registry=registry)
        self.assertEqual({action.control.control_role for action in delay.actions}, {"wet", "feedback"})
        self.assertTrue(all(action.control.display_unit == "percent" for action in delay.actions))
        limiter = plan_processing(ProcessingRequest(goals=(ProcessingGoal(goal="control_dynamics"),)), loaded_plugins=(observations["limiter"],), registry=registry)
        self.assertEqual([action.control.control_role for action in limiter.actions], ["ceiling"])

    def test_darkening_refuses_unknown_cutoff_but_allows_explicit_control(self):
        registry = load_bundled_registry()
        observation = self.bundled_observations()["reverb"]
        for display in (None, "Off", "0Hz"):
            with self.subTest(display=display):
                observation["parameters"][-1]["display"] = display
                request = ProcessingRequest(goals=(ProcessingGoal(goal="darken_reverb", required=True),))
                plan = plan_processing(request, loaded_plugins=(observation,), registry=registry)
                coverage = evaluate_effect_coverage(request, loaded_plugins=(observation,), registry=registry)
                self.assertFalse(plan.actions)
                self.assertTrue(plan.missing_capabilities)
                self.assertEqual(coverage.state, "unresolved_effect")
                self.assertTrue(coverage.required_processing_missing)
        explicit = ProcessingRequest(goals=(ProcessingGoal(goal="darken_reverb", controls=(SemanticControlValue(control_role="high_cut", display_value=3000.0, display_unit="Hz"),)),))
        self.assertEqual(len(plan_processing(explicit, loaded_plugins=(observation,), registry=registry).actions), 1)

    def test_category_presence_cannot_cover_unsupported_or_unresolved_goals(self):
        registry = load_bundled_registry()
        observation = self.bundled_observations()["equalizer"]
        cases = (
            ProcessingGoal(goal="keep_low_end_centered", required=True),
            ProcessingGoal(goal="unknown_intent", required=True),
            ProcessingGoal(goal="add_air", controls=(SemanticControlValue(control_role="unknown_control", display_value=1.0),), required=True),
            ProcessingGoal(goal="add_air", target=MixerEffectTarget(track_index=7, slot_index=0), required=True),
        )
        for goal in cases:
            with self.subTest(goal=goal):
                request = ProcessingRequest(goals=(goal,), completion_target="playable_draft")
                plan = plan_processing(request, loaded_plugins=(observation,), registry=registry)
                coverage = evaluate_effect_coverage(request, loaded_plugins=(observation,), registry=registry)
                self.assertFalse(plan.actions)
                self.assertTrue(plan.missing_capabilities)
                self.assertNotEqual(coverage.state, "effect_covered")
                self.assertTrue(coverage.required_processing_missing)

    def test_adapter_intents_cannot_be_overridden_by_capability_claims(self):
        registry = load_bundled_registry()
        capability = self.stock_capability("image-line.fruity-reeverb-2", ("Decay", "Wet"))
        capability = capability.model_copy(update={"supported_techniques": ("restrained_section_contrast",)})
        goal = ProcessingGoal(goal="restrained_section_contrast", controls=(SemanticControlValue(control_role="decay", display_value=1.0),))
        plan = plan_processing(ProcessingRequest(goals=(goal,)), loaded_plugins=(capability,), registry=registry)
        self.assertFalse(plan.actions)
        self.assertTrue(plan.missing_capabilities)

    def test_coverage_uses_exact_requested_controls_not_unrelated_adapter_controls(self):
        registry = load_bundled_registry()
        fixture = json.loads((Path(__file__).parent / "fixtures" / "stock-effect-controls-v1.json").read_text())
        loaded = ({**fixture["reeverb"], "track_index": 5, "slot_index": 2},)
        request = ProcessingRequest(goals=(ProcessingGoal(goal="add_depth"),))
        self.assertFalse(plan_processing(request, loaded_plugins=loaded, registry=registry).missing_capabilities)
        self.assertEqual(evaluate_effect_coverage(request, loaded_plugins=loaded, registry=registry).state, "effect_covered")

    def test_role_targets_dry_intent_and_required_flags_survive_expansion(self):
        registry = load_bundled_registry()
        observations = self.bundled_observations()
        target = MixerEffectTarget(track_index=5, slot_index=3)
        request = ProcessingRequest(completion_target="playable_draft", roles=(
            RoleProcessingRequest(role="lead", target=target, goals=(ProcessingGoal(goal="add_depth"),), processing_required=True),
            RoleProcessingRequest(role="dry", dry_by_design=True, requested_techniques=("unknown",)),
        ))
        plan = plan_processing(request, loaded_plugins=(observations["reverb"],), registry=registry)
        coverage = evaluate_effect_coverage(request, loaded_plugins=(observations["reverb"],), registry=registry)
        self.assertTrue(plan.actions)
        self.assertEqual({action.role for action in plan.actions}, {"lead"})
        self.assertEqual({action.target for action in plan.actions}, {target})
        self.assertEqual(coverage.state, "effect_covered")
        self.assertEqual({row.role: row.state for row in coverage.roles}, {"lead": "effect_covered", "dry": "dry_by_design"})
        absent = evaluate_effect_coverage(request, registry=registry)
        self.assertTrue(absent.required_processing_missing)
        self.assertTrue(all(gap.required for gap in absent.missing_capabilities))
        role_only = evaluate_effect_coverage((RoleProcessingRequest(role="lead", requested_techniques=("add_depth",)),), loaded_plugins=(observations["reverb"],), completion_target="playable_draft", registry=registry)
        self.assertEqual(role_only.state, "effect_covered")
        self.assertEqual(role_only.completion_target, "playable_draft")

    def test_coverage_preserves_conflicts_and_separate_targets_in_one_role(self):
        registry = load_bundled_registry()
        observation = self.bundled_observations()["equalizer"]
        request = ProcessingRequest(goals=(ProcessingGoal(goal="add_presence"), ProcessingGoal(goal="add_air")))
        coverage = evaluate_effect_coverage(request, loaded_plugins=(observation,), registry=registry)
        self.assertTrue(coverage.required_processing_missing)
        self.assertIn("conflicts", coverage.missing_capabilities[0].reason)
        other = {**observation, "slot_index": 9}
        request = ProcessingRequest(goals=(ProcessingGoal(goal="add_presence", target=MixerEffectTarget(track_index=5, slot_index=0)), ProcessingGoal(goal="add_air", target=MixerEffectTarget(track_index=5, slot_index=9))))
        plan = plan_processing(request, loaded_plugins=(observation, other), registry=registry)
        coverage = evaluate_effect_coverage(request, loaded_plugins=(observation, other), registry=registry)
        self.assertEqual(len(plan.actions), 4)
        self.assertEqual(len({action.action_id for action in plan.actions}), 4)
        self.assertEqual(coverage.state, "effect_covered")

    def test_optional_gap_does_not_inherit_a_required_sibling(self):
        registry = load_bundled_registry()
        request = ProcessingRequest(completion_target="playable_draft", goals=(
            ProcessingGoal(goal="add_presence", required=True),
            ProcessingGoal(goal="unknown_optional"),
        ))
        coverage = evaluate_effect_coverage(request, loaded_plugins=(self.bundled_observations()["equalizer"],), registry=registry)
        self.assertFalse(coverage.required_processing_missing)
        self.assertFalse(coverage.roles[0].processing_required_for_completion)
        self.assertFalse(coverage.missing_capabilities[0].required)

    def test_compound_diagnostics_cover_the_maximum_request_without_truncation(self):
        registry = load_bundled_registry()
        request = ProcessingRequest(goals=tuple(ProcessingGoal(goal="tighten_low_end", required=True) for _ in range(128)))
        plan = plan_processing(request, registry=registry)
        coverage = evaluate_effect_coverage(request, registry=registry)
        self.assertEqual(len(plan.missing_capabilities), 256)
        self.assertEqual(len(coverage.missing_capabilities), 256)
        self.assertTrue(coverage.required_processing_missing)
        with self.assertRaisesRegex(ValueError, "at most 128 processing goals"):
            ProcessingRequest(goals=request.goals, roles=(RoleProcessingRequest(role="lead", requested_techniques=("add_depth",)),))

    def test_observed_alternatives_with_missing_controls_are_unresolved(self):
        registry = load_bundled_registry()
        observations = self.bundled_observations()
        for category, goal in (("delay", "add_depth"), ("limiter", "control_dynamics")):
            with self.subTest(category=category):
                observation = {**observations[category], "parameters": observations[category]["parameters"][:1]}
                request = ProcessingRequest(goals=(ProcessingGoal(goal=goal),))
                plan = plan_processing(request, loaded_plugins=(observation,), registry=registry)
                coverage = evaluate_effect_coverage(request, loaded_plugins=(observation,), registry=registry)
                self.assertEqual(plan.missing_capabilities[0].category, category)
                self.assertEqual(coverage.state, "unresolved_effect")
                self.assertTrue(coverage.required_processing_missing)

    def test_action_limit_reports_unplanned_work_and_unique_ids(self):
        registry = load_bundled_registry()
        request = ProcessingRequest(goals=tuple(ProcessingGoal(goal="control_dynamics") for _ in range(65)))
        loaded = (self.bundled_observations()["compressor"],)
        plan = plan_processing(request, loaded_plugins=loaded, registry=registry)
        coverage = evaluate_effect_coverage(request, loaded_plugins=loaded, registry=registry)
        self.assertEqual(len(plan.actions), 256)
        self.assertEqual(len({action.action_id for action in plan.actions}), 256)
        self.assertIn("action limit", plan.missing_capabilities[0].reason)
        self.assertTrue(coverage.required_processing_missing)

    def test_alternative_conflict_and_master_gaps_name_the_observed_effect(self):
        registry = load_bundled_registry()
        delay = self.bundled_observations()["delay"]
        request = ProcessingRequest(goals=(ProcessingGoal(goal="add_depth", strength=0.25), ProcessingGoal(goal="add_depth", strength=0.75)))
        cases = ((request, delay), (ProcessingRequest(goals=(ProcessingGoal(goal="add_depth"),)), {**delay, "track_index": 0}))
        for request, observation in cases:
            with self.subTest(request=request, track=observation["track_index"]):
                plan = plan_processing(request, loaded_plugins=(observation,), registry=registry)
                coverage = evaluate_effect_coverage(request, loaded_plugins=(observation,), registry=registry)
                self.assertEqual(plan.missing_capabilities[0].category, "delay")
                self.assertEqual(coverage.state, "unresolved_effect")
                self.assertTrue(coverage.required_processing_missing)

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
            preferred_write_value="display_value",
        )
        cls.option_control = AdapterControl(
            control_id="mode",
            role="reverb.mode",
            parameter_index=3,
            kind="enumerated",
            options=("Plate", "Room"),
            preferred_write_value="option",
        )
        cls.normalized_control = AdapterControl(
            control_id="mix",
            role="reverb.mix",
            parameter_index=4,
            preferred_write_value="normalized_value",
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
        setter: str = "display_value",
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
            display_value=1.8 if setter == "display_value" else None,
            option="Plate" if setter == "option" else None,
            normalized_value=0.5 if setter == "normalized_value" else None,
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
                    controls=(SemanticControlValue(control_role="reverb.decay", display_value=1.8),),
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
        self.assertEqual(plan.actions[0].resolution.control.setter, "display_value")

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
        self.assertEqual(display.control.setter, "display_value")

        option = resolve_semantic_control(
            SemanticControlValue(
                control_role="reverb.mode",
                option="plate",
                normalized_value=0.1,
                normalized_mapping="known-mode-v1",
            ),
            adapter=self.adapter,
        )
        self.assertEqual(option.control.setter, "option")
        self.assertEqual(option.control.option, "Plate")

        normalized = resolve_semantic_control(
            SemanticControlValue(
                control_role="reverb.mix",
                normalized_value=0.4,
                normalized_mapping="known-mix-v1",
            ),
            adapter=self.adapter,
        )
        self.assertEqual(normalized.control.setter, "normalized_value")

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


    def test_v11_setter_names_in_saved_data_load_as_value_arguments(self) -> None:
        """Plans, runs, and adapters saved before V12 name the old setters."""

        saved = ProcessingPlan(
            plan_id="saved-plan",
            request_id="test",
            completion_target="restrained_first_pass",
            actions=(self.action("first"),),
        ).model_dump(mode="json")
        saved["actions"][0]["resolution"]["control"]["setter"] = "fl_set_plugin_param_display"
        # The same two steps the Production Run checkpoint loader uses.
        loaded = ProcessingPlan.model_validate_json(json.dumps(saved), strict=False)
        loaded = ProcessingPlan.model_validate(loaded.model_dump(mode="python"))
        self.assertEqual(loaded.actions[0].resolution.control.setter, "display_value")
        self.assertNotIn("fl_set_plugin_param", loaded.model_dump_json())
        calls: list[dict[str, object]] = []

        def writer(**kwargs: object) -> object:
            calls.append(kwargs)
            return {"verified": True}

        receipt = apply_processing_plan(loaded, setter_callbacks={"fl_set_plugin_param_display": writer})
        self.assertTrue(receipt.completed)
        self.assertEqual(len(calls), 1)
        for old, new in (
            ("fl_set_plugin_param_display", "display_value"),
            ("fl_set_plugin_param_option", "option"),
            ("fl_set_plugin_param", "normalized_value"),
        ):
            control = AdapterControl.model_validate(
                {"id": "control", "names": ["Control"], "preferred_write_tool": old}
            )
            self.assertEqual(control.preferred_write_value, new)
        bundled = load_bundled_registry()
        self.assertNotIn("fl_set_plugin_param", json.dumps([item.model_dump(mode="json") for item in bundled.adapters]))

if __name__ == "__main__":
    unittest.main(verbosity=2)
