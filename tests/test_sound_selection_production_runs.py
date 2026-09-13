"""Sound Selection integration at the closed Production Run boundary."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from fl_studio_mcp import creative
from fl_studio_mcp import production_runs as runs
from fl_studio_mcp.creation_pipeline import live_readiness
from fl_studio_mcp.creative import HotkeyDispatch, NoteSequence
from fl_studio_mcp.sound_selection.models import (
    DrumPad,
    DrumPadMap,
    DrumRoleMapping,
    PaletteApplyReceipt,
    SoundInventory,
    SoundPaletteAssignment,
    SoundPalettePlan,
    SoundPaletteState,
    SoundPaletteVariationPlan,
    SoundRoleRequest,
    SoundSelectionPolicy,
    SoundSelectionRequest,
    SoundTargetInventory,
    canonical_digest,
)
from fl_studio_mcp.track_b_contracts import (
    ChannelGeneratorTarget,
    PluginPresetState,
    TargetedPluginSummary,
    VerifiedPluginPresetSelection,
)


SESSION = "a" * 32
PROJECT = "b" * 64


def sound_request(*, persist_history: bool = False) -> SoundSelectionRequest:
    return SoundSelectionRequest(
        brief="Choose a cohesive lead and drum kit.",
        roles=(
            SoundRoleRequest(role_id="main_lead", role_type="lead"),
            SoundRoleRequest(role_id="drums", role_type="drums"),
        ),
        persist_history=persist_history,
    )


def run_request(
    *,
    policy: runs.InteractionPolicy = "execute_once",
    authorized: bool = True,
    scope: runs.ProductionScope | None = None,
) -> runs.ProductionRunRequest:
    return runs.ProductionRunRequest(
        brief="Create the requested palette and part.",
        scope=scope
        or runs.ProductionScope(kind="whole_project", description="Whole project."),
        allowed_changes=("sound_selection", "composition", "notes"),
        completion_target="A playable draft using the selected sounds.",
        interaction_policy=policy,
        authorized_to_modify=authorized,
    )


def palette() -> SoundPalettePlan:
    request_digest = canonical_digest(sound_request().model_dump(mode="json"))
    assignments = (
        SoundPaletteAssignment(
            role_id="main_lead",
            target=ChannelGeneratorTarget(channel_index=2),
            product_id="synthetic-lead",
            product_name="Synthetic Lead",
            selected_preset="Bright Lead",
            selected_preset_index=3,
            anchor=True,
            selection_reason="Direct role fit.",
        ),
        SoundPaletteAssignment(
            role_id="drums",
            target=ChannelGeneratorTarget(channel_index=4),
            product_id="synthetic-kit",
            product_name="Synthetic Kit",
            selected_preset="Studio Kit",
            selected_preset_index=1,
            anchor=True,
            selection_reason="Mapped kit fit.",
        ),
    )
    draft = SoundPalettePlan(
        palette_id="palette-test",
        request_digest=request_digest,
        inventory_session_fingerprint=SESSION,
        policy=SoundSelectionPolicy(),
        assignments=assignments,
        anchor_roles=("main_lead", "drums"),
        rationale="Synthetic deterministic palette.",
    )
    return draft.model_copy(
        update={
            "plan_digest": canonical_digest(
                draft.model_dump(mode="json") | {"plan_digest": None}
            )
        }
    )


def drum_map() -> DrumPadMap:
    pads = (
        DrumPad(pad_index=0, midi_note=52, semitone_name="Kick"),
        DrumPad(pad_index=1, midi_note=60, semitone_name="Snare"),
        DrumPad(pad_index=2, midi_note=71, semitone_name="Closed Hat"),
    )
    return DrumPadMap(
        map_id="mapped-kit",
        target=ChannelGeneratorTarget(channel_index=4),
        pad_count=3,
        pads=pads,
        mappings=(
            DrumRoleMapping(role="kick", pad_index=0, midi_note=52),
            DrumRoleMapping(role="snare", pad_index=1, midi_note=60),
            DrumRoleMapping(role="closed_hat", pad_index=2, midi_note=71),
        ),
        confidence=1.0,
    )


def variation(base: SoundPalettePlan) -> SoundPaletteVariationPlan:
    assignment = base.assignments[0].model_copy(
        update={
            "selected_preset": "Wide Lead",
            "selected_preset_index": 4,
            "parent_assignment_id": base.assignments[0].assignment_id,
            "section_scope": ("drop_b",),
        }
    )
    return SoundPaletteVariationPlan(
        variation_id="variation-test",
        base_palette_id=base.palette_id,
        request_digest=base.request_digest,
        section="drop_b",
        assignments=(assignment,),
        unchanged_role_ids=("drums",),
        rationale="Synthetic section delta.",
    )


class SoundSelectionProductionRunTests(unittest.TestCase):
    def test_palette_outputs_validate_role_target_reference(self) -> None:
        plan = runs.ProductionRunPlan(
            plan_id="sound-flow",
            operations=(
                runs.PlanSoundPaletteOperation(
                    operation_id="palette", request=sound_request()
                ),
                runs.GenerateMelodyOperation(operation_id="melody"),
                runs.WriteNoteSequenceOperation(
                    operation_id="write",
                    after=("palette", "melody"),
                    sequence=runs.OperationOutputReference(
                        operation_id="melody", output="note_sequence"
                    ),
                    channel_index=runs.OperationOutputReference(
                        operation_id="palette",
                        output="generator_target",
                        role_id="main_lead",
                    ),
                    pattern_number=2,
                ),
            ),
        )
        validation = runs.validate_production_run(
            run_request(), plan, inspect_live=False
        )
        self.assertTrue(validation.valid, validation.blockers)

    def test_incompatible_palette_reference_is_rejected(self) -> None:
        plan = runs.ProductionRunPlan(
            plan_id="bad-ref",
            operations=(
                runs.GenerateMelodyOperation(operation_id="melody"),
                runs.ApplySoundPaletteOperation(
                    operation_id="apply",
                    palette=runs.OperationOutputReference(
                        operation_id="melody", output="sound_palette"
                    ),
                ),
            ),
        )
        validation = runs.validate_production_run(
            run_request(), plan, inspect_live=False
        )
        self.assertIn(
            "incompatible_output_reference",
            {item.code for item in validation.blockers},
        )

    def test_plan_only_rejects_feedback_and_does_not_enable_writes(self) -> None:
        operation = runs.RecordSoundFeedbackOperation(
            operation_id="feedback",
            feedback={
                "palette_id": "palette-test",
                "verdict": "accepted",
                "persist": True,
            },
        )
        registry = runs.ProductionRunRegistry()
        with (
            mock.patch.object(runs, "WriteModeManager") as mode,
            mock.patch.object(runs, "_dispatch_operation") as dispatch,
        ):
            result = registry.execute(
                run_request(policy="plan_only", authorized=False),
                runs.ProductionRunPlan(plan_id="feedback-plan", operations=(operation,)),
            )
        self.assertEqual(result.status, "blocked")
        self.assertIn("plan_only_mutation", {item.code for item in result.blockers})
        mode.assert_not_called()
        dispatch.assert_not_called()

    def test_feedback_only_run_never_enables_fl_write_mode(self) -> None:
        operation = runs.RecordSoundFeedbackOperation(
            operation_id="feedback",
            feedback={
                "palette_id": "palette-test",
                "role_id": "main_lead",
                "verdict": "accepted",
                "persist": False,
            },
        )
        receipt = runs.SoundFeedbackReceipt(
            palette_id="palette-test",
            role_id="main_lead",
            verdict="accepted",
            persisted=False,
        )
        registry = runs.ProductionRunRegistry()
        with (
            mock.patch.object(runs, "WriteModeManager") as mode,
            mock.patch.object(runs, "_dispatch_operation", return_value=receipt),
        ):
            result = registry.execute(
                run_request(authorized=False),
                runs.ProductionRunPlan(plan_id="feedback-run", operations=(operation,)),
            )
        self.assertEqual(result.status, "completed")
        mode.assert_not_called()

    def test_selected_drum_map_feeds_generation(self) -> None:
        mapped = drum_map()
        outputs = {
            ("inspect", "drum_map", None): runs.ProductionGeneratedOutput(
                operation_id="inspect", output="drum_map", value=mapped
            )
        }
        operation = runs.GenerateDrumsOperation(
            operation_id="drums",
            style="dnb",
            bars=1,
            drum_map=runs.OperationOutputReference(
                operation_id="inspect", output="drum_map"
            ),
        )
        result = runs._dispatch_operation(
            operation, session_fingerprint=None, outputs=outputs
        )
        self.assertIsInstance(result, NoteSequence)
        self.assertTrue({note.pitch for note in result.notes}.issubset({52, 60, 71}))

    def test_omitted_drum_map_uses_the_single_reported_map(self) -> None:
        mapped = drum_map()
        inventory = SoundInventory(
            session_fingerprint=SESSION,
            loaded_generators=(
                SoundTargetInventory(
                    target=ChannelGeneratorTarget(channel_index=4),
                    product_name="Synthetic Kit",
                    pad_map=mapped,
                ),
            ),
        )
        generated = runs.compose_drums(style="hiphop", bars=1, drum_map=mapped)
        with mock.patch.object(runs, "compose_drums", return_value=generated) as compose:
            result = runs._dispatch_operation(
                runs.GenerateDrumsOperation(operation_id="drums", style="hiphop"),
                session_fingerprint=SESSION,
                outputs={},
                sound_inventory=inventory,
            )

        self.assertIs(result, generated)
        self.assertEqual(compose.call_args.kwargs["drum_map"], mapped)

    def test_omitted_drum_map_rejects_ambiguous_reported_maps(self) -> None:
        first = drum_map()
        second = first.model_copy(
            update={
                "map_id": "mapped-kit-two",
                "target": ChannelGeneratorTarget(channel_index=5),
            }
        )
        inventory = SoundInventory(
            session_fingerprint=SESSION,
            loaded_generators=(
                SoundTargetInventory(
                    target=ChannelGeneratorTarget(channel_index=4),
                    product_name="Synthetic Kit A",
                    pad_map=first,
                ),
                SoundTargetInventory(
                    target=ChannelGeneratorTarget(channel_index=5),
                    product_name="Synthetic Kit B",
                    pad_map=second,
                ),
            ),
        )

        with self.assertRaisesRegex(ValueError, "multiple loaded drum generators"):
            runs._dispatch_operation(
                runs.GenerateDrumsOperation(operation_id="drums", style="hiphop"),
                session_fingerprint=SESSION,
                outputs={},
                sound_inventory=inventory,
            )

    def test_drum_readiness_uses_explicit_target_and_house_requires_open_hat(
        self,
    ) -> None:
        selected = drum_map()
        other = selected.model_copy(
            update={
                "map_id": "mapped-kit-two",
                "target": ChannelGeneratorTarget(channel_index=5),
            }
        )
        inventory = SoundInventory(
            session_fingerprint=SESSION,
            loaded_generators=(
                SoundTargetInventory(
                    target=ChannelGeneratorTarget(channel_index=5),
                    product_name="Other Kit",
                    pad_map=other,
                ),
                SoundTargetInventory(
                    target=ChannelGeneratorTarget(channel_index=4),
                    product_name="Selected Kit",
                    pad_map=selected,
                ),
            ),
        )
        operations = (
            runs.InspectDrumMapOperation(
                operation_id="inspect",
                target=ChannelGeneratorTarget(channel_index=4),
            ),
            runs.GenerateDrumsOperation(operation_id="generate", style="house"),
        )
        required = live_readiness._required_drum_roles(operations, None)
        coverage = live_readiness._drum_coverage(
            inventory,
            required,
            operations,
            None,
        )

        self.assertEqual(coverage.target, ChannelGeneratorTarget(channel_index=4))
        self.assertEqual(coverage.drum_map, selected)
        self.assertIn("open_hat", coverage.missing_roles)

    def test_dynamic_palette_target_scope_blocks_before_write_mode(self) -> None:
        output = runs.ProductionGeneratedOutput(
            operation_id="palette",
            output="sound_palette",
            value=palette(),
        )
        operation = runs.ApplySoundPaletteOperation(
            operation_id="apply",
            palette=runs.OperationOutputReference(
                operation_id="palette", output="sound_palette"
            ),
        )
        request = run_request(
            scope=runs.ProductionScope(
                kind="selected_targets",
                description="Only channel 9.",
                targets=(runs.ProductionTarget(kind="channel", index=9),),
            )
        )
        blocker = runs._runtime_preflight_blocker(
            request,
            operation,
            {("palette", "sound_palette", None): output},
            expected_session=SESSION,
        )
        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertEqual(blocker.code, "target_outside_scope")

    def test_selected_preset_can_consume_a_prior_generator_target(self) -> None:
        plan = runs.ProductionRunPlan(
            plan_id="preset-target-ref",
            operations=(
                runs.PlanSoundPaletteOperation(
                    operation_id="palette", request=sound_request()
                ),
                runs.SelectPluginPresetOperation(
                    operation_id="preset",
                    target=runs.OperationOutputReference(
                        operation_id="palette",
                        output="generator_target",
                        role_id="main_lead",
                    ),
                    preset_name="Bright Lead",
                ),
            ),
        )

        validation = runs.validate_production_run(
            run_request(), plan, inspect_live=False
        )

        self.assertTrue(validation.valid, validation.blockers)

    def test_palette_target_reference_preserves_and_enforces_fingerprint(self) -> None:
        target_fingerprint = "c" * 64
        assignments = tuple(
            item.model_copy(update={"target_fingerprint": target_fingerprint})
            for item in palette().assignments
        )
        planned = palette().model_copy(update={"assignments": assignments})
        generated = runs._generated_outputs_for(
            runs.PlanSoundPaletteOperation(
                operation_id="palette", request=sound_request()
            ),
            planned,
        )
        outputs = {
            (item.operation_id, item.output, item.role_id): item
            for item in generated
        }
        target_output = outputs[("palette", "generator_target", "main_lead")]
        self.assertEqual(target_output.target_fingerprint, target_fingerprint)

        operation = runs.SelectPluginPresetOperation(
            operation_id="preset",
            target=runs.OperationOutputReference(
                operation_id="palette",
                output="generator_target",
                role_id="main_lead",
            ),
            preset_name="Bright Lead",
        )
        controller = mock.Mock()
        controller.select_plugin_preset.return_value = object()
        with mock.patch.object(runs, "TrackBController", return_value=controller):
            runs._dispatch_operation(
                operation,
                session_fingerprint=SESSION,
                outputs=outputs,
            )
        self.assertEqual(
            controller.select_plugin_preset.call_args.kwargs["target_fingerprint"],
            target_fingerprint,
        )

        mismatched = operation.model_copy(update={"target_fingerprint": "d" * 64})
        blocker = runs._runtime_preflight_blocker(
            run_request(),
            mismatched,
            outputs,
            expected_session=SESSION,
        )
        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertEqual(blocker.code, "target_fingerprint_mismatch")

    def test_piano_roll_generator_reference_carries_target_fingerprint(self) -> None:
        target_fingerprint = "c" * 64
        assignments = tuple(
            item.model_copy(update={"target_fingerprint": target_fingerprint})
            for item in palette().assignments
        )
        generated = runs._generated_outputs_for(
            runs.PlanSoundPaletteOperation(
                operation_id="palette", request=sound_request()
            ),
            palette().model_copy(update={"assignments": assignments}),
        )
        outputs = {
            (item.operation_id, item.output, item.role_id): item
            for item in generated
        }
        sequence = runs.compose_melody()
        outputs[("melody", "note_sequence", None)] = runs.ProductionGeneratedOutput(
            operation_id="melody", output="note_sequence", value=sequence
        )
        operation = runs.WriteNoteSequenceOperation(
            operation_id="write",
            sequence=runs.OperationOutputReference(
                operation_id="melody", output="note_sequence"
            ),
            channel_index=runs.OperationOutputReference(
                operation_id="palette",
                output="generator_target",
                role_id="main_lead",
            ),
            pattern_number=2,
        )

        with mock.patch.object(
            runs, "write_piano_roll_notes", return_value=object()
        ) as writer:
            runs._dispatch_operation(
                operation,
                session_fingerprint=SESSION,
                outputs=outputs,
            )

        self.assertEqual(
            writer.call_args.kwargs["target_fingerprint"], target_fingerprint
        )

    def test_same_index_generator_replacement_blocks_before_piano_roll_script(self) -> None:
        planned_fingerprint = "c" * 64
        replacement_fingerprint = "d" * 64
        target = ChannelGeneratorTarget(channel_index=2)
        outputs = {
            ("palette", "generator_target", "main_lead"): runs.ProductionGeneratedOutput(
                operation_id="palette",
                output="generator_target",
                role_id="main_lead",
                value=target,
                target_fingerprint=planned_fingerprint,
            ),
            ("melody", "note_sequence", None): runs.ProductionGeneratedOutput(
                operation_id="melody",
                output="note_sequence",
                value=runs.compose_melody(),
            ),
        }
        operation = runs.WriteNoteSequenceOperation(
            operation_id="write",
            sequence=runs.OperationOutputReference(
                operation_id="melody", output="note_sequence"
            ),
            channel_index=runs.OperationOutputReference(
                operation_id="palette",
                output="generator_target",
                role_id="main_lead",
            ),
            pattern_number=2,
            mode="replace",
        )

        class ReplacementClient:
            def __init__(self) -> None:
                self.arguments: dict[str, object] = {}

            def call(self, _command: str, **arguments: object) -> dict[str, object]:
                self.arguments = arguments
                if arguments.get("target_fingerprint") != replacement_fingerprint:
                    raise ValueError(
                        "target fingerprint precondition failed; re-read the plug-in target"
                    )
                return {}

        client = ReplacementClient()
        with (
            mock.patch.object(
                creative,
                "_writable_preflight",
                return_value=(client, {}, SESSION),
            ),
            mock.patch.object(creative.PIANO_ROLL, "require_armed"),
            mock.patch.object(creative, "_atomic_text") as atomic_text,
            mock.patch.object(creative, "_trigger_piano_roll_shortcut") as trigger,
        ):
            with self.assertRaisesRegex(ValueError, "target fingerprint precondition"):
                runs._dispatch_operation(
                    operation,
                    session_fingerprint=SESSION,
                    outputs=outputs,
                )

        self.assertEqual(
            client.arguments["target_fingerprint"], planned_fingerprint
        )
        atomic_text.assert_not_called()
        trigger.assert_not_called()

    def test_get_run_rehydrates_sound_selection_apply_receipt_with_track_b_warnings(self) -> None:
        base = palette()
        assignment = base.assignments[0]
        assert assignment.target is not None
        target_fingerprint = "c" * 64
        selection = VerifiedPluginPresetSelection(
            applied_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            verified=True,
            verification_summary="FL read the requested preset identity back.",
            target=assignment.target,
            plugin=TargetedPluginSummary(
                target=assignment.target,
                name="Synthetic Lead",
                target_fingerprint=target_fingerprint,
                reported_parameter_count=0,
            ),
            requested_preset_name=assignment.selected_preset,
            requested_preset_index=assignment.selected_preset_index,
            before=PluginPresetState(
                name="Previous Lead", index=2, identity_status="stable"
            ),
            after=PluginPresetState(
                name=assignment.selected_preset,
                index=assignment.selected_preset_index,
                identity_status="stable",
            ),
            outcome="verified",
            navigation_direction="next",
            navigation_steps=1,
            max_navigation_steps=4,
            settle_tick_limit=1,
            target_fingerprint=target_fingerprint,
            warnings=["FL undo evidence confirms the preset change."],
        )
        assignment_receipt = PaletteApplyReceipt(
            assignment_id=assignment.assignment_id,
            role_id=assignment.role_id,
            verified=True,
            verification_summary="Verified palette assignment.",
            selected_preset=assignment.selected_preset,
        )
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        palette_state = SoundPaletteState(
            palette_id=base.palette_id,
            status="applied",
            created_at=stamp,
            updated_at=stamp,
            session_identity=SESSION,
            assignments=base.assignments,
            apply_receipts=(assignment_receipt,),
        )
        applied = runs.SoundSelectionApplyResult(
            palette_id=base.palette_id,
            status="applied",
            session_fingerprint=SESSION,
            state=palette_state,
            assignment_scope=(assignment,),
            receipts=(selection,),
            assignment_receipts=(assignment_receipt,),
            verified_count=1,
        )
        operation = runs.ApplySoundPaletteOperation(
            operation_id="apply",
            palette=base,
        )
        plan = runs.ProductionRunPlan(
            plan_id="apply-plan",
            operations=(operation,),
        )
        receipt = runs.ProductionOperationReceipt(
            operation_index=0,
            operation_id=operation.operation_id,
            operation=operation.operation,
            status="verified",
            mutating=True,
            outcome_known=True,
            verified=True,
            result=applied,
        )
        state = runs.ProductionRunState(
            run_id="d" * 32,
            request=run_request(),
            plan_id=plan.plan_id,
            plan_digest=runs.production_plan_digest(plan),
            status="completed",
            created_at=stamp,
            updated_at=stamp,
            started_at=stamp,
            finished_at=stamp,
            session_fingerprint=SESSION,
            iteration=1,
            current_operation_index=1,
            total_operations=1,
            completed_operations=(operation.operation_id,),
            receipts=(receipt,),
        )
        registry = runs.ProductionRunRegistry()
        registry._runs[state.run_id] = runs._RunRecord(plan=plan, state=state)

        lookup = registry.get(state.run_id)

        self.assertTrue(lookup.found)
        assert lookup.state is not None
        restored = lookup.state.receipts[0].result
        self.assertIsInstance(restored, runs.SoundSelectionApplyResult)
        assert isinstance(restored, runs.SoundSelectionApplyResult)
        self.assertEqual(
            restored.receipts[0].warnings,
            ["FL undo evidence confirms the preset change."],
        )

    def test_valid_generator_target_reaches_piano_roll_mutation(self) -> None:
        target_fingerprint = "c" * 64
        target = ChannelGeneratorTarget(channel_index=2)
        outputs = {
            ("palette", "generator_target", "main_lead"): runs.ProductionGeneratedOutput(
                operation_id="palette",
                output="generator_target",
                role_id="main_lead",
                value=target,
                target_fingerprint=target_fingerprint,
            ),
            ("melody", "note_sequence", None): runs.ProductionGeneratedOutput(
                operation_id="melody",
                output="note_sequence",
                value=runs.compose_melody(),
            ),
        }
        operation = runs.WriteNoteSequenceOperation(
            operation_id="write",
            sequence=runs.OperationOutputReference(
                operation_id="melody", output="note_sequence"
            ),
            channel_index=runs.OperationOutputReference(
                operation_id="palette",
                output="generator_target",
                role_id="main_lead",
            ),
            pattern_number=2,
        )
        receipt = {
            "command": "creative.prepare_piano_roll",
            "channel_index": 2,
            "target_fingerprint": target_fingerprint,
            "pattern_number": 2,
            "before_channel_indices": [0],
            "after_channel_indices": [2],
            "before_pattern_number": 1,
            "after_pattern_number": 2,
            "selected_target_verified": True,
            "session_fingerprint": SESSION,
            "session_precondition_applied": True,
            "project_saved": False,
        }

        class ValidClient:
            transport = "midi"

            def call(self, _command: str, **arguments: object) -> dict[str, object]:
                self.arguments = arguments
                return receipt

        client = ValidClient()
        trigger_result = HotkeyDispatch(
            platform="macos",
            shortcut="Cmd+Opt+Y",
            fl_window_found=True,
            fl_window_focused=True,
            hotkey_dispatched=True,
        )
        with (
            mock.patch.object(
                creative,
                "_writable_preflight",
                return_value=(client, {}, SESSION),
            ),
            mock.patch.object(creative.PIANO_ROLL, "require_armed"),
            mock.patch.object(creative, "_atomic_text", return_value="e" * 64) as atomic_text,
            mock.patch.object(
                creative,
                "_trigger_piano_roll_shortcut",
                return_value=trigger_result,
            ) as trigger,
        ):
            result = runs._dispatch_operation(
                operation,
                session_fingerprint=SESSION,
                outputs=outputs,
            )

        self.assertIsInstance(result, creative.PianoRollDispatch)
        assert isinstance(result, creative.PianoRollDispatch)
        assert result.target is not None
        self.assertEqual(result.target.target_fingerprint, target_fingerprint)
        atomic_text.assert_called_once()
        trigger.assert_called_once()

    def test_variation_output_can_apply_and_expose_verified_role_target(self) -> None:
        base = palette()
        delta = variation(base)
        assignment = delta.assignments[0]
        receipt = PaletteApplyReceipt(
            assignment_id=assignment.assignment_id,
            role_id=assignment.role_id,
            verified=True,
            verification_summary="Synthetic later-tick readback.",
            selected_preset=assignment.selected_preset,
        )
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state = SoundPaletteState(
            palette_id=base.palette_id,
            status="applied",
            created_at=stamp,
            updated_at=stamp,
            session_identity=SESSION,
            assignments=base.assignments,
            apply_receipts=(receipt,),
        )
        applied = runs.SoundSelectionApplyResult(
            palette_id=base.palette_id,
            status="applied",
            session_fingerprint=SESSION,
            state=state,
            assignment_scope=(assignment,),
            assignment_receipts=(receipt,),
            verified_count=1,
        )
        service = SimpleNamespace(
            create_variation=mock.Mock(return_value=delta),
            apply=mock.Mock(return_value=applied),
        )
        outputs = {
            ("palette", "sound_palette", None): runs.ProductionGeneratedOutput(
                operation_id="palette", output="sound_palette", value=base
            )
        }
        create = runs.CreateSoundPaletteVariationOperation(
            operation_id="variation",
            palette=runs.OperationOutputReference(
                operation_id="palette", output="sound_palette"
            ),
            request=sound_request(),
            section="drop_b",
        )
        with mock.patch(
            "fl_studio_mcp.sound_selection.executor.SOUND_SELECTION", service
        ):
            created = runs._dispatch_operation(
                create, session_fingerprint=SESSION, outputs=outputs
            )
            for output in runs._generated_outputs_for(create, created):
                outputs[(output.operation_id, output.output, output.role_id)] = output
            apply = runs.ApplySoundPaletteOperation(
                operation_id="apply_variation",
                palette=runs.OperationOutputReference(
                    operation_id="variation", output="section_variation"
                ),
            )
            result = runs._dispatch_operation(
                apply, session_fingerprint=SESSION, outputs=outputs
            )

        generated = runs._generated_outputs_for(apply, result)
        self.assertTrue(runs._classify_mutation_result(result)[1])
        self.assertIn(
            ("generator_target", "main_lead"),
            {(item.output, item.role_id) for item in generated},
        )
        service.apply.assert_called_once()

    def test_missing_variation_base_state_blocks_before_write_mode(self) -> None:
        base = palette()
        delta = variation(base)
        operation = runs.ApplySoundPaletteOperation(
            operation_id="apply_variation", palette=delta
        )
        missing = SimpleNamespace(found=False, state=None)
        service = SimpleNamespace(lookup=mock.Mock(return_value=missing))

        with mock.patch(
            "fl_studio_mcp.sound_selection.executor.SOUND_SELECTION", service
        ):
            blocker = runs._runtime_preflight_blocker(
                run_request(),
                operation,
                {},
                expected_session=SESSION,
            )

        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertEqual(blocker.code, "sound_palette_process_state_missing")

    def test_continue_uses_prior_palette_output_for_section_variation(self) -> None:
        base = palette()
        delta = variation(base)
        service = SimpleNamespace(
            plan=mock.Mock(return_value=base),
            create_variation=mock.Mock(return_value=delta),
        )
        registry = runs.ProductionRunRegistry()
        with (
            mock.patch(
                "fl_studio_mcp.sound_selection.executor.SOUND_SELECTION", service
            ),
            mock.patch.object(
                runs,
                "_live_validation",
                return_value=([], [], SESSION, PROJECT),
            ),
        ):
            first = registry.execute(
                run_request(),
                runs.ProductionRunPlan(
                    plan_id="continue-palette",
                    operations=(
                        runs.PlanSoundPaletteOperation(
                            operation_id="palette", request=sound_request()
                        ),
                    ),
                ),
            )
            before = registry.get(first.run_id)
            assert before.state is not None
            original_receipt = before.state.receipts[0]
            continued = registry.continue_run(
                first.run_id,
                runs.ProductionRunDelta(
                    mode="append",
                    operations=(
                        runs.CreateSoundPaletteVariationOperation(
                            operation_id="variation",
                            palette=runs.OperationOutputReference(
                                operation_id="palette", output="sound_palette"
                            ),
                            request=sound_request(),
                            section="drop_b",
                        ),
                    ),
                ),
            )

        self.assertEqual(continued.status, "completed")
        after = registry.get(first.run_id)
        assert after.state is not None
        self.assertEqual(after.state.receipts[0], original_receipt)
        service.create_variation.assert_called_once_with(
            base.palette_id,
            sound_request(),
            section="drop_b",
            replace_roles=(),
        )


if __name__ == "__main__":
    unittest.main()
