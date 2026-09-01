"""Creation-pipeline integration at the Production Run and MCP boundaries."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from fl_studio_mcp import production_runs as runs
from fl_studio_mcp.contracts import (
    ConnectionInfo,
    ProjectSummary,
    TransportState,
    VerifiedMixerVolumeWrite,
)
from fl_studio_mcp.creation_pipeline import live_readiness
from fl_studio_mcp.creation_pipeline.context import build_context_snapshot
from fl_studio_mcp.creation_pipeline.live_readiness import (
    CollectedCreationReadiness,
    collect_creation_readiness,
)
from fl_studio_mcp.creation_pipeline.models import (
    CreationReadinessInput,
    CreationReadinessReport,
    ReadinessBlocker,
)
from fl_studio_mcp.creation_pipeline.processing import (
    MissingProcessingCapability,
    ProcessingActionReceipt,
    ProcessingGoal,
    ProcessingPlan,
    ProcessingPlanReceipt,
    ProcessingRequest,
    ResolvedSemanticControl,
    SemanticControlResolution,
    SemanticControlValue,
    SemanticPluginAction,
    evaluate_effect_coverage,
)
from fl_studio_mcp.plugin_atlas import load_bundled_registry
from fl_studio_mcp.sound_selection.models import (
    SoundInventory,
    SoundRoleRequest,
    SoundSelectionRequest,
    SoundTargetInventory,
)
from fl_studio_mcp.track_b_contracts import (
    ChannelGeneratorTarget,
    MixerEffectTarget,
    PatternList,
    VerifiedPatternSelectionWrite,
)
from fl_studio_mcp.workflows import BatchItemResult


SESSION = "a" * 32
PROJECT = "b" * 64
WHEN = datetime(2026, 8, 31, tzinfo=timezone.utc)


def request(*, policy: runs.InteractionPolicy = "execute_once") -> runs.ProductionRunRequest:
    return runs.ProductionRunRequest(
        brief="Plan processing, then prepare the requested pattern.",
        scope=runs.ProductionScope(
            kind="whole_project", description="The disposable creation project."
        ),
        allowed_changes=("plugin_parameters", "pattern_metadata"),
        completion_target="A restrained first-pass production draft.",
        interaction_policy=policy,
        authorized_to_modify=policy != "plan_only",
    )


def processing_plan() -> ProcessingPlan:
    return ProcessingPlan(
        plan_id="semantic-test",
        request_id="creation-test",
        completion_target="restrained_first_pass",
    )


def semantic_action() -> SemanticPluginAction:
    control = SemanticControlValue(
        control_role="reverb.decay",
        display_value=1.8,
        parameter=2,
    )
    return SemanticPluginAction(
        action_id="depth-action",
        goal_id="depth",
        role="lead",
        target=MixerEffectTarget(track_index=2, slot_index=0),
        target_fingerprint="c" * 64,
        plugin_name="Synthetic Effect",
        product_id="synthetic-effect",
        adapter_id="synthetic-effect-adapter",
        control=control,
        resolution=SemanticControlResolution(
            request=control,
            status="resolved",
            control=ResolvedSemanticControl(
                control_role="reverb.decay",
                control_id="decay",
                parameter_index=2,
                setter="fl_set_plugin_param_display",
                display_value=1.8,
            ),
        ),
    )


def verified_pattern() -> VerifiedPatternSelectionWrite:
    return VerifiedPatternSelectionWrite(
        applied_at=WHEN,
        requested_pattern_number=2,
        before_pattern_number=1,
        after_pattern_number=2,
        verified=True,
        verification_summary="verified",
        session_fingerprint=SESSION,
        session_precondition_applied=True,
    )


def verified_mixer_batch() -> runs.VerifiedBatchResult:
    write = VerifiedMixerVolumeWrite(
        applied_at=WHEN,
        track_index=1,
        requested_volume_normalized=0.5,
        verified=True,
        verification_summary="verified",
        session_fingerprint=SESSION,
    )
    item = BatchItemResult(
        operation_index=0,
        operation_id="volume",
        operation="mixer_volume",
        status="verified",
        outcome_known=True,
        verified=True,
        receipt=write,
    )
    return runs.VerifiedBatchResult(
        applied_at=WHEN,
        requested_count=1,
        attempted_count=1,
        skipped_count=0,
        completed=True,
        verified=True,
        stop_on_unverified=True,
        session_fingerprint=SESSION,
        results=[item],
    )


def ready_collection() -> tuple[CreationReadinessReport, CollectedCreationReadiness]:
    connection = ConnectionInfo(
        connected=True,
        compatible=True,
        compatibility_reason="ok",
        bridge_transport="midi",
        bridge_mode="read_only",
        runtime_write_mode_control=True,
        bridge_provenance="matching",
        bridge_provenance_verified=True,
        session_fingerprint=SESSION,
    )
    project = ProjectSummary(
        observed_at=WHEN,
        connection=connection,
        transport=TransportState(),
    )
    context = build_context_snapshot(
        session_fingerprint=SESSION,
        project_checkpoint_digest=PROJECT,
        mcp_process_identity=live_readiness.MCP_PROCESS_IDENTITY,
        captured_at=WHEN,
    )
    report = CreationReadinessReport(
        observed_at=WHEN,
        overall_state="ready",
        score=100.0,
        context_snapshot=context,
    )
    facts = CreationReadinessInput(
        observed_at=WHEN,
        context_snapshot=context,
        project_checkpoint_digest=PROJECT,
    )
    inventory = SoundInventory(observed_at=WHEN, session_fingerprint=SESSION)
    return report, CollectedCreationReadiness(
        readiness_input=facts,
        context_snapshot=context,
        sound_inventory=inventory,
        loaded_processing_observations=(
            {
                "target": {"kind": "mixer_effect", "track_index": 2, "slot_index": 0},
                "plugin_name": "Synthetic Effect",
                "runtime_parameters": (),
            },
        ),
        full_inventory_scan_count=1,
        target_refresh_count=2,
        preset_enumeration_count=1,
        connection=connection,
        project=project,
    )


class CreationPipelineIntegrationTests(unittest.TestCase):
    def test_instrument_readiness_requires_distinct_targets_for_required_roles(
        self,
    ) -> None:
        sound_request = SoundSelectionRequest(
            brief="Choose two distinct role sounds.",
            roles=(
                SoundRoleRequest(role_id="main_chords", role_type="chords"),
                SoundRoleRequest(role_id="main_lead", role_type="lead"),
                SoundRoleRequest(
                    role_id="optional_texture",
                    role_type="texture",
                    required=False,
                ),
            ),
            persist_history=False,
        )
        inventory = SoundInventory(
            observed_at=WHEN,
            session_fingerprint=SESSION,
            loaded_generators=(
                SoundTargetInventory(
                    target=ChannelGeneratorTarget(channel_index=1),
                    product_name="Synthetic Generator",
                    current_preset="Current",
                ),
            ),
        )

        coverage = live_readiness._instrument_coverage(inventory, sound_request)

        self.assertEqual(len(coverage.covered_roles), 1)
        self.assertEqual(len(coverage.missing_roles), 1)
        self.assertNotIn("optional_texture", coverage.missing_roles)

    def test_semantic_effect_coverage_requires_runtime_control_evidence(self) -> None:
        request = ProcessingRequest(
            request_id="coverage-evidence",
            completion_target="restrained_first_pass",
            goals=(
                ProcessingGoal(
                    goal_id="lead-depth",
                    role="main_lead",
                    goal="add_depth",
                    controls=(
                        SemanticControlValue(
                            control_role="decay",
                            display_value=1.8,
                            display_unit="seconds",
                        ),
                    ),
                ),
            ),
        )
        semantic = evaluate_effect_coverage(
            request,
            loaded_plugins=(
                {
                    "target": MixerEffectTarget(
                        track_index=2, slot_index=0
                    ).model_dump(mode="python"),
                    "plugin_name": "Fruity Reeverb 2",
                    "target_fingerprint": "c" * 64,
                    "runtime_parameters": (),
                },
            ),
            registry=load_bundled_registry(),
        )

        coverage = live_readiness._creation_effect_coverage(semantic)

        self.assertEqual(coverage.state, "loaded_but_unresolved")
        self.assertTrue(coverage.loaded_capabilities)
        self.assertFalse(coverage.loaded_capabilities[0].controllable)

    def test_live_preflight_collects_one_discovered_inventory_snapshot(self) -> None:
        connection = ConnectionInfo(
            connected=True,
            compatible=True,
            compatibility_reason="ok",
            bridge_transport="midi",
            bridge_mode="read_only",
            runtime_write_mode_control=True,
            bridge_provenance="matching",
            bridge_provenance_verified=True,
            session_fingerprint=SESSION,
        )
        inspector = mock.Mock()
        inspector.connection_info.return_value = connection
        inspector.project_summary.return_value = ProjectSummary(
            observed_at=WHEN,
            connection=connection,
            transport=TransportState(),
        )
        tracks = mock.Mock()
        tracks.list_patterns.return_value = PatternList(
            observed_at=WHEN,
            current_pattern_number=1,
            reported_pattern_count=0,
            maximum_pattern_number=1,
            patterns=[],
        )
        inventory = SoundInventory(
            observed_at=WHEN,
            session_fingerprint=SESSION,
        )
        sound = mock.Mock()
        sound.inventory.return_value = inventory
        piano_status = SimpleNamespace(
            script_exists=True,
            armed_this_session=True,
            last_request_id="armed-request",
            setup_instruction="",
            automatic_trigger_supported=True,
        )

        with (
            mock.patch.object(
                live_readiness.PIANO_ROLL, "status", return_value=piano_status
            ),
            mock.patch.object(live_readiness, "midi_port_query", return_value="PF"),
        ):
            collected = collect_creation_readiness(
                operations=(),
                completion_target_text="A playable draft.",
                allowed_mutation_categories=(),
                required_mutation_categories=(),
                inspector=inspector,
                track_inspector=tracks,
                sound_service=sound,
            )

        inspector.connection_info.assert_called_once_with()
        inspector.project_summary.assert_called_once_with()
        tracks.list_patterns.assert_called_once_with()
        sound.inventory.assert_called_once()
        self.assertTrue(sound.inventory.call_args.kwargs["discover_presets"])
        self.assertIs(collected.sound_inventory, inventory)
        self.assertEqual(collected.full_inventory_scan_count, 1)
        self.assertEqual(
            collected.context_snapshot.sound_inventory.model_dump(mode="json"),
            inventory.model_dump(mode="json"),
        )

    def test_one_preflight_context_and_inventory_are_reused_across_phases(self) -> None:
        registry = runs.ProductionRunRegistry()
        plan = runs.ProductionRunPlan(
            plan_id="phased-run",
            operations=(
                runs.PlanProcessingOperation(
                    operation_id="processing-plan",
                    request=ProcessingRequest(request_id="creation-test"),
                ),
                runs.SelectPatternOperation(
                    operation_id="select-pattern", pattern_number=2
                ),
            ),
        )
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION, after_enabled=True
        )
        dispatch = mock.Mock(side_effect=(processing_plan(), verified_pattern()))
        collection = ready_collection()
        with (
            mock.patch.object(
                runs, "_live_validation", return_value=([], [], SESSION, PROJECT)
            ),
            mock.patch.object(runs, "_collect_run_readiness", return_value=collection) as preflight,
            mock.patch.object(runs, "_current_session_matches", return_value=(True, "")),
            mock.patch.object(runs, "_capture_project_state", return_value=(PROJECT, "")),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(runs, "_dispatch_operation", dispatch),
        ):
            result = registry.execute(request(), plan)

        self.assertEqual(result.status, "completed")
        preflight.assert_called_once()
        self.assertEqual(result.write_mode_enable_count, 1)
        self.assertEqual(result.write_mode_disable_count, 1)
        self.assertEqual(mode.set_write_mode.call_count, 2)
        lookup = registry.get(result.run_id)
        assert lookup.state is not None
        state = lookup.state
        self.assertEqual(state.readiness_preflight_count, 1)
        self.assertEqual(state.run_context, collection[1].context_snapshot)
        self.assertIsNotNone(state.timing_report)
        assert state.timing_report is not None
        self.assertEqual(
            state.timing_report.operation_summary.full_inventory_scan_count, 1
        )
        self.assertEqual(state.timing_report.operation_summary.target_refresh_count, 2)
        self.assertEqual(
            {row.phase for row in state.timing_report.phase_timings},
            {"preflight", "processing", "note_application", "finalization"},
        )
        self.assertIsNotNone(state.creation_outcome)
        assert state.creation_outcome is not None
        self.assertEqual(state.creation_outcome.audible_quality.status, "not_evaluated")
        for call in dispatch.call_args_list:
            self.assertIs(call.kwargs["sound_inventory"], collection[1].sound_inventory)
            self.assertEqual(
                call.kwargs["processing_observations"],
                collection[1].loaded_processing_observations,
            )

    def test_processing_plan_output_is_applied_in_the_same_run(self) -> None:
        action = semantic_action()
        planned = ProcessingPlan(
            plan_id="same-run-processing",
            request_id="same-run",
            completion_target="restrained_first_pass",
            session_fingerprint=SESSION,
            actions=(action.model_copy(update={"session_fingerprint": SESSION}),),
        )
        receipt = ProcessingPlanReceipt(
            plan_id=planned.plan_id,
            requested_count=1,
            attempted_count=1,
            completed=True,
            stopped=False,
            verified=True,
            results=(
                ProcessingActionReceipt(
                    action_id=action.action_id,
                    status="verified",
                    outcome_known=True,
                    verified=True,
                ),
            ),
        )
        plan = runs.ProductionRunPlan(
            plan_id="same-run-processing",
            operations=(
                runs.PlanProcessingOperation(
                    operation_id="plan-processing",
                    request=ProcessingRequest(request_id="same-run"),
                ),
                runs.ApplyProcessingPlanOperation(
                    operation_id="apply-processing",
                    plan=runs.OperationOutputReference(
                        operation_id="plan-processing",
                        output="processing_plan",
                    ),
                ),
            ),
        )
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )
        collection = ready_collection()
        with (
            mock.patch.object(
                runs,
                "_collect_run_readiness",
                return_value=collection,
            ),
            mock.patch.object(runs, "_current_session_matches", return_value=(True, "")),
            mock.patch.object(runs, "_capture_project_state", return_value=(PROJECT, "")),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(runs, "plan_processing", return_value=planned) as planner,
            mock.patch.object(
                runs,
                "_apply_semantic_processing_plan",
                return_value=receipt,
            ) as apply,
        ):
            result = runs.ProductionRunRegistry().execute(request(), plan)

        self.assertEqual(result.status, "completed")
        planner.assert_called_once()
        apply.assert_called_once_with(planned)
        self.assertEqual(result.completed_count, 2)
        assert result.creation_outcome is not None
        self.assertEqual(
            result.creation_outcome.processing.status,
            "restrained_first_pass",
        )

    def test_non_processing_verified_batch_does_not_count_as_processing(self) -> None:
        batch = runs.ApplyVerifiedBatchOperation(
            operation_id="mix-batch",
            operations=(
                {
                    "operation_id": "volume",
                    "operation": "mixer_volume",
                    "track_index": 1,
                    "volume_normalized": 0.5,
                },
            ),
        )
        plan = runs.ProductionRunPlan(
            plan_id="mix-only",
            operations=(batch,),
        )
        registry = runs.ProductionRunRegistry()
        record = registry._create_record(request(), plan)
        receipt = runs.ProductionOperationReceipt(
            operation_index=0,
            operation_id=batch.operation_id,
            operation=batch.operation,
            status="verified",
            mutating=True,
            outcome_known=True,
            verified=True,
            result=verified_mixer_batch(),
        )
        record.state = record.state.model_copy(
            update={
                "status": "completed",
                "receipts": (receipt,),
                "completed_operations": (batch.operation_id,),
            }
        )

        outcome = registry._build_creation_outcome(record)

        self.assertEqual(outcome.processing.status, "not_requested")
        self.assertEqual(outcome.processing.applied_actions, 0)
        self.assertEqual(outcome.processing.verified_actions, 0)

    def test_inline_processing_gaps_are_dry_missing_effects(self) -> None:
        missing = MissingProcessingCapability(
            role="lead",
            category="reverb",
            requested_techniques=("add_depth",),
            reason="No loaded compatible effect covers the requested technique.",
        )
        operation = runs.ApplyProcessingPlanOperation(
            operation_id="apply-processing",
            plan=ProcessingPlan(
                plan_id="inline-missing",
                request_id="inline-missing",
                completion_target="restrained_first_pass",
                missing_capabilities=(missing,),
            ),
        )
        plan = runs.ProductionRunPlan(
            plan_id="inline-missing",
            operations=(operation,),
        )
        registry = runs.ProductionRunRegistry()
        record = registry._create_record(request(), plan)

        outcome = registry._build_creation_outcome(record)

        self.assertEqual(outcome.processing.status, "dry_missing_effects")
        self.assertEqual(
            outcome.processing.missing_effects,
            (missing.reason,),
        )

    def test_inline_processing_gaps_with_applied_actions_are_partial(self) -> None:
        action = semantic_action()
        missing = MissingProcessingCapability(
            role="lead",
            category="compression",
            requested_techniques=("control_dynamics",),
            reason="No loaded compressor covers the requested technique.",
        )
        operation = runs.ApplyProcessingPlanOperation(
            operation_id="apply-processing",
            plan=ProcessingPlan(
                plan_id="inline-partial",
                request_id="inline-partial",
                completion_target="restrained_first_pass",
                actions=(action,),
                missing_capabilities=(missing,),
            ),
        )
        result = ProcessingPlanReceipt(
            plan_id=operation.plan.plan_id,
            requested_count=1,
            attempted_count=1,
            completed=True,
            stopped=False,
            verified=True,
            results=(
                ProcessingActionReceipt(
                    action_id=action.action_id,
                    status="verified",
                    outcome_known=True,
                    verified=True,
                ),
            ),
        )
        plan = runs.ProductionRunPlan(
            plan_id="inline-partial",
            operations=(operation,),
        )
        registry = runs.ProductionRunRegistry()
        record = registry._create_record(request(), plan)
        receipt = runs.ProductionOperationReceipt(
            operation_index=0,
            operation_id=operation.operation_id,
            operation=operation.operation,
            status="verified",
            mutating=True,
            outcome_known=True,
            verified=True,
            result=result,
        )
        record.state = record.state.model_copy(
            update={
                "status": "completed",
                "receipts": (receipt,),
                "completed_operations": (operation.operation_id,),
            }
        )

        outcome = registry._build_creation_outcome(record)

        self.assertEqual(outcome.processing.status, "partially_processed")
        self.assertEqual(outcome.processing.applied_actions, 1)
        self.assertEqual(outcome.processing.verified_actions, 1)
        self.assertEqual(outcome.processing.missing_effects, (missing.reason,))

    def test_processing_dispatch_error_is_partial_unknown(self) -> None:
        operation = runs.ApplyProcessingPlanOperation(
            operation_id="apply-processing",
            plan=processing_plan(),
        )
        plan = runs.ProductionRunPlan(
            plan_id="unknown-processing",
            operations=(operation,),
        )
        registry = runs.ProductionRunRegistry()
        record = registry._create_record(request(), plan)
        receipt = runs.ProductionOperationReceipt(
            operation_index=0,
            operation_id=operation.operation_id,
            operation=operation.operation,
            status="error_unknown",
            mutating=True,
            outcome_known=False,
            verified=False,
            error="RuntimeError: reply lost after dispatch",
        )
        record.state = record.state.model_copy(
            update={
                "status": "blocked",
                "receipts": (receipt,),
            }
        )

        outcome = registry._build_creation_outcome(record)

        self.assertEqual(outcome.processing.status, "partially_processed")
        self.assertIn(
            "apply-processing: unknown_outcome",
            outcome.processing.unresolved_controls,
        )

    def test_unresolved_inline_processing_is_not_reported_dry_by_design(self) -> None:
        action = semantic_action().model_copy(update={"session_fingerprint": SESSION})
        planned = ProcessingPlan(
            plan_id="stopped-processing",
            request_id="stopped",
            completion_target="restrained_first_pass",
            session_fingerprint=SESSION,
            actions=(action,),
        )
        stopped = ProcessingPlanReceipt(
            plan_id=planned.plan_id,
            requested_count=1,
            attempted_count=0,
            completed=False,
            stopped=True,
            stopped_on=action.action_id,
            verified=False,
            results=(
                ProcessingActionReceipt(
                    action_id=action.action_id,
                    status="stale_target",
                    outcome_known=True,
                    verified=False,
                ),
            ),
        )
        plan = runs.ProductionRunPlan(
            plan_id="stopped-processing",
            operations=(
                runs.ApplyProcessingPlanOperation(
                    operation_id="apply-processing",
                    plan=planned,
                ),
            ),
        )
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )
        with (
            mock.patch.object(
                runs,
                "_live_validation",
                return_value=([], [], SESSION, PROJECT),
            ),
            mock.patch.object(runs, "_current_session_matches", return_value=(True, "")),
            mock.patch.object(runs, "_capture_project_state", return_value=(PROJECT, "")),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(
                runs,
                "_apply_semantic_processing_plan",
                return_value=stopped,
            ),
        ):
            result = runs.ProductionRunRegistry().execute(request(), plan)

        self.assertEqual(result.status, "blocked")
        assert result.creation_outcome is not None
        self.assertEqual(
            result.creation_outcome.processing.status,
            "partially_processed",
        )
        self.assertIn(
            "depth-action: stale_target",
            result.creation_outcome.processing.unresolved_controls,
        )

    def test_stale_processing_sessions_stop_before_any_writer(self) -> None:
        stale = "d" * 32
        action = semantic_action().model_copy(update={"session_fingerprint": stale})
        planned = ProcessingPlan(
            plan_id="stale-processing",
            request_id="stale",
            completion_target="restrained_first_pass",
            session_fingerprint=stale,
            actions=(action,),
        )
        apply_plan = runs.ApplyProcessingPlanOperation(
            operation_id="apply-plan",
            plan=planned,
        )
        apply_action = runs.ApplySemanticPluginActionOperation(
            operation_id="apply-action",
            action=action,
        )

        with (
            mock.patch.object(runs, "_apply_semantic_processing_plan") as plan_writer,
            mock.patch.object(runs, "_apply_one_semantic_action") as action_writer,
        ):
            with self.assertRaisesRegex(ValueError, "another FL Studio session"):
                runs._dispatch_operation(
                    apply_plan,
                    session_fingerprint=SESSION,
                    outputs={},
                )
            with self.assertRaisesRegex(ValueError, "another FL Studio session"):
                runs._dispatch_operation(
                    apply_action,
                    session_fingerprint=SESSION,
                    outputs={},
                )

        plan_writer.assert_not_called()
        action_writer.assert_not_called()

    def test_readiness_aggregates_blockers_and_performs_zero_mutations(self) -> None:
        registry = runs.ProductionRunRegistry()
        plan = runs.ProductionRunPlan(
            plan_id="blocked-run",
            operations=(
                runs.PlanProcessingOperation(
                    operation_id="processing-plan",
                    request=ProcessingRequest(request_id="blocked-test"),
                ),
                runs.SelectPatternOperation(
                    operation_id="select-pattern", pattern_number=2
                ),
            ),
        )
        base, collected = ready_collection()
        blocked = base.model_copy(
            update={
                "overall_state": "blocked",
                "score": 40.0,
                "blockers": (
                    ReadinessBlocker(
                        code="piano-roll-not-armed",
                        dimension="piano_roll",
                        message="Arm the Piano Roll bridge once.",
                    ),
                    ReadinessBlocker(
                        code="drum-generator-missing",
                        dimension="drum_coverage",
                        message="Load one drum-capable generator.",
                    ),
                ),
            }
        )
        with (
            mock.patch.object(
                runs, "_live_validation", return_value=([], [], SESSION, PROJECT)
            ),
            mock.patch.object(
                runs, "_collect_run_readiness", return_value=(blocked, collected)
            ),
            mock.patch.object(runs, "_dispatch_operation") as dispatch,
            mock.patch.object(runs, "WriteModeManager") as mode,
        ):
            result = registry.execute(request(), plan)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(
            {item.code for item in result.blockers},
            {"piano-roll-not-armed", "drum-generator-missing"},
        )
        dispatch.assert_not_called()
        mode.assert_not_called()
        self.assertEqual(result.attempted_count, 0)
        self.assertIsNotNone(result.creation_outcome)
        assert result.creation_outcome is not None
        self.assertEqual(result.creation_outcome.technical_execution.status, "blocked")

    def test_public_readiness_includes_operation_specific_live_blockers(self) -> None:
        plan = runs.ProductionRunPlan(
            plan_id="readiness-live-blocker",
            operations=(
                runs.PlanProcessingOperation(
                    operation_id="processing-plan",
                    request=ProcessingRequest(request_id="readiness-live"),
                ),
            ),
        )
        blocker = runs.ProductionBlocker(
            category="unavailable_in_project",
            code="loaded_control_unavailable",
            message="The required loaded control could not be resolved.",
        )
        base, collected = ready_collection()

        def collect_with_blocker(
            _request: runs.ProductionRunRequest,
            _plan: runs.ProductionRunPlan,
            *,
            start_index: int = 0,
            structural_blockers: tuple[runs.ProductionBlocker, ...] = (),
        ) -> tuple[CreationReadinessReport, CollectedCreationReadiness]:
            self.assertEqual(start_index, 0)
            return (
                runs._merge_structural_readiness_blockers(
                    base, structural_blockers
                ),
                collected,
            )

        with (
            mock.patch.object(runs, "_live_validation") as live,
            mock.patch.object(
                runs,
                "_collect_run_readiness",
                side_effect=collect_with_blocker,
            ),
            mock.patch.object(
                runs,
                "_cached_live_validation",
                return_value=([blocker], [], SESSION, PROJECT),
            ) as cached,
            mock.patch.object(runs, "WriteModeManager") as mode,
        ):
            report = runs.creation_readiness(request(), plan)

        live.assert_not_called()
        cached.assert_called_once()
        mode.assert_not_called()
        self.assertEqual(report.overall_state, "blocked")
        self.assertIn("loaded_control_unavailable", {item.code for item in report.blockers})

    def test_plan_only_never_enables_write_mode(self) -> None:
        registry = runs.ProductionRunRegistry()
        plan = runs.ProductionRunPlan(
            plan_id="plan-only",
            operations=(
                runs.PlanProcessingOperation(
                    operation_id="processing-plan",
                    request=ProcessingRequest(request_id="plan-only"),
                ),
            ),
        )
        collection = ready_collection()
        with (
            mock.patch.object(
                runs,
                "_collect_run_readiness",
                return_value=collection,
            ) as preflight,
            mock.patch.object(
                runs,
                "_dispatch_operation",
                return_value=processing_plan(),
            ) as dispatch,
            mock.patch.object(
                runs,
                "_current_session_matches",
                return_value=(True, ""),
            ),
            mock.patch.object(
                runs,
                "_capture_project_state",
                return_value=(PROJECT, ""),
            ),
            mock.patch.object(runs, "WriteModeManager") as mode,
        ):
            result = registry.execute(request(policy="plan_only"), plan)

        self.assertEqual(result.status, "completed")
        preflight.assert_called_once()
        self.assertEqual(
            dispatch.call_args.kwargs["processing_observations"],
            collection[1].loaded_processing_observations,
        )
        mode.assert_not_called()
        self.assertEqual(result.write_mode_enable_count, 0)
        self.assertEqual(result.write_mode_disable_count, 0)

    def test_continuation_merges_timing_into_the_resumed_phase(self) -> None:
        registry = runs.ProductionRunRegistry()
        plan = runs.ProductionRunPlan(
            plan_id="resumed-phase",
            operations=(
                runs.PlanProcessingOperation(
                    operation_id="processing-plan",
                    request=ProcessingRequest(request_id="resume-test"),
                ),
                runs.SelectPatternOperation(
                    operation_id="select-pattern-two", pattern_number=2
                ),
            ),
        )
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION, after_enabled=True
        )
        dispatch = mock.Mock(
            side_effect=(
                processing_plan(),
                verified_pattern(),
                verified_pattern().model_copy(
                    update={
                        "requested_pattern_number": 3,
                        "before_pattern_number": 2,
                        "after_pattern_number": 3,
                    }
                ),
            )
        )
        collection = ready_collection()
        with (
            mock.patch.object(
                runs, "_live_validation", return_value=([], [], SESSION, PROJECT)
            ),
            mock.patch.object(
                runs, "_collect_run_readiness", return_value=collection
            ) as preflight,
            mock.patch.object(runs, "_current_session_matches", return_value=(True, "")),
            mock.patch.object(runs, "_capture_project_state", return_value=(PROJECT, "")),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(runs, "_dispatch_operation", dispatch),
        ):
            first = registry.execute(request(), plan)
            continued = registry.continue_run(
                first.run_id,
                runs.ProductionRunDelta(
                    mode="append",
                    operations=(
                        runs.SelectPatternOperation(
                            operation_id="select-pattern-three",
                            pattern_number=3,
                        ),
                    ),
                ),
            )

        self.assertEqual(continued.status, "completed")
        preflight.assert_called_once()
        lookup = registry.get(first.run_id)
        assert lookup.state is not None
        assert lookup.state.timing_report is not None
        rows = lookup.state.timing_report.phase_timings
        self.assertEqual(len(rows), len({row.phase for row in rows}))
        note_phase = next(row for row in rows if row.phase == "note_application")
        self.assertEqual(note_phase.operation_count, 2)
        self.assertEqual(lookup.state.readiness_preflight_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
