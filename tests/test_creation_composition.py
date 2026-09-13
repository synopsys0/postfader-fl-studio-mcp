#!/usr/bin/env python3
"""Deterministic sound-aware composition contract tests."""

from __future__ import annotations

import unittest
from unittest import mock

from pydantic import ValidationError

from fl_studio_mcp import production_runs as runs
from fl_studio_mcp.creation_pipeline.composition_adaptation import (
    adapt_note_sequence,
    derive_composition_profile,
    develop_section_variation,
)
from fl_studio_mcp.creation_pipeline.sound_characteristics import (
    SelectedSoundCharacteristics,
    SoundCharacteristic,
    characteristics_from_palette_assignment,
)
from fl_studio_mcp.creative import CreativeNote, make_sequence
from fl_studio_mcp.sound_selection.models import (
    DescriptorEvidence,
    DrumPadMap,
    DrumRoleMapping,
    SoundInventory,
    SoundPaletteAssignment,
    SoundTargetInventory,
)
from fl_studio_mcp.track_b_contracts import ChannelGeneratorTarget


def selected(
    role_id: str,
    *characteristics: SoundCharacteristic,
) -> SelectedSoundCharacteristics:
    return SelectedSoundCharacteristics(
        role_id=role_id,
        preset_name="Synthetic Fixture",
        preset_identity_verified=True,
        characteristics=characteristics,
        coverage="reviewed_exact" if characteristics else "unknown",
    )


def characteristic(name: str, value: object, confidence: str = "high"):
    return SoundCharacteristic(
        name=name,
        value=value,
        confidence=confidence,
        provenance="reviewed_bundled_preset_metadata",
    )


def sequence(*notes: CreativeNote):
    return make_sequence(name="Fixture", generator="fixture", notes=notes)


class SoundAwareCompositionTests(unittest.TestCase):
    @staticmethod
    def _drum_inventory(drum_map: DrumPadMap) -> SoundInventory:
        target = ChannelGeneratorTarget(channel_index=4)
        return SoundInventory(
            session_fingerprint="a" * 32,
            loaded_generators=(
                SoundTargetInventory(
                    target=target,
                    product_name="Synthetic Kit",
                    pad_map=drum_map,
                ),
            ),
        )

    def test_contracts_are_strict_frozen_and_name_inference_cannot_be_high(self) -> None:
        with self.assertRaises(ValidationError):
            SoundCharacteristic(
                name="plucked",
                value=True,
                confidence="high",
                provenance="normalized_preset_name_inference",
            )
        item = selected("lead", characteristic("plucked", True))
        with self.assertRaises(ValidationError):
            item.role_id = "changed"  # type: ignore[misc]

    def test_pluck_shortens_notes_and_reduces_overlap(self) -> None:
        source = sequence(
            CreativeNote(pitch=60, start_beats=0, duration_beats=1.0),
            CreativeNote(pitch=62, start_beats=1, duration_beats=1.0),
        )
        profile = derive_composition_profile(
            selected("main_lead", characteristic("plucked", True)),
            role_kind="lead",
        )
        result = adapt_note_sequence(source, profile)
        self.assertTrue(
            all(note.duration_beats < 1.0 for note in result.sequence.notes)
        )
        self.assertIn(
            "articulation", {change.parameter for change in result.adaptation.changes}
        )

    def test_slow_pad_lengthens_notes_and_reduces_density(self) -> None:
        source = sequence(
            *(
                CreativeNote(
                    pitch=60 + index % 5,
                    start_beats=index * 0.5,
                    duration_beats=0.25,
                )
                for index in range(12)
            )
        )
        profile = derive_composition_profile(
            selected(
                "main_chords",
                characteristic("articulation", "sustained"),
                characteristic("attack_speed", "slow"),
            ),
            role_kind="chords",
        )
        result = adapt_note_sequence(source, profile)
        self.assertLess(result.sequence.note_count, source.note_count)
        self.assertTrue(
            all(note.duration_beats > 0.25 for note in result.sequence.notes)
        )

    def test_monophonic_line_has_no_simultaneous_or_overlapping_notes(self) -> None:
        source = sequence(
            CreativeNote(pitch=60, start_beats=0, duration_beats=3),
            CreativeNote(pitch=64, start_beats=0, duration_beats=2),
            CreativeNote(pitch=67, start_beats=1, duration_beats=2),
        )
        profile = derive_composition_profile(
            selected("lead", characteristic("monophonic", True)),
            role_kind="lead",
        )
        result = adapt_note_sequence(source, profile)
        starts = [note.start_beats for note in result.sequence.notes]
        self.assertEqual(len(starts), len(set(starts)))
        for left, right in zip(
            result.sequence.notes, result.sequence.notes[1:]
        ):
            self.assertLessEqual(
                left.start_beats + left.duration_beats,
                right.start_beats,
            )

    def test_polyphony_and_register_are_bounded(self) -> None:
        source = sequence(
            *(
                CreativeNote(pitch=pitch, start_beats=0, duration_beats=1)
                for pitch in (36, 48, 60, 64, 67, 72)
            )
        )
        profile = derive_composition_profile(
            selected(
                "main_chords",
                characteristic("practical_polyphony", 3),
                characteristic("usable_pitch_low", 52),
                characteristic("usable_pitch_high", 76),
            ),
            role_kind="chords",
        )
        result = adapt_note_sequence(source, profile)
        self.assertEqual(result.sequence.note_count, 3)
        self.assertTrue(
            all(52 <= note.pitch <= 76 for note in result.sequence.notes)
        )

    def test_sub_bass_uses_bounded_low_register(self) -> None:
        source = sequence(
            CreativeNote(pitch=72, start_beats=0, duration_beats=1),
            CreativeNote(pitch=76, start_beats=1, duration_beats=1),
        )
        profile = derive_composition_profile(selected("sub"), role_kind="sub_bass")
        result = adapt_note_sequence(source, profile)
        self.assertTrue(
            all(24 <= note.pitch <= 48 for note in result.sequence.notes)
        )
        self.assertEqual(profile.metadata_confidence, "unknown")
        self.assertTrue(result.adaptation.warnings)

    def test_dense_timbre_reduces_supporting_note_density(self) -> None:
        source = sequence(
            *(
                CreativeNote(pitch=60, start_beats=index, duration_beats=0.5)
                for index in range(10)
            )
        )
        profile = derive_composition_profile(
            selected("texture", characteristic("timbral_density", "dense")),
            role_kind="texture",
        )
        result = adapt_note_sequence(source, profile)
        self.assertEqual(result.sequence.note_count, 6)

    def test_section_variation_preserves_pitch_class_and_start_identity(self) -> None:
        source = sequence(
            CreativeNote(pitch=60, start_beats=0, duration_beats=1),
            CreativeNote(pitch=64, start_beats=1, duration_beats=1),
            CreativeNote(pitch=67, start_beats=2, duration_beats=1),
        )
        varied = develop_section_variation(
            source,
            density_scale=1.0,
            register_shift=12,
            articulation_scale=0.75,
        )
        self.assertEqual(
            [note.start_beats for note in varied.notes],
            [note.start_beats for note in source.notes],
        )
        self.assertEqual(
            [note.pitch % 12 for note in varied.notes],
            [note.pitch % 12 for note in source.notes],
        )

    def test_production_run_emits_adapted_sequence_and_report_outputs(self) -> None:
        request = runs.ProductionRunRequest(
            brief="Generate and adapt a fixture melody.",
            scope=runs.ProductionScope(
                kind="whole_project", description="Read-only composition."
            ),
            allowed_changes=("composition",),
            completion_target="An adapted note sequence.",
            interaction_policy="execute_once",
            authorized_to_modify=False,
        )
        operations = (
            runs.GenerateMelodyOperation(
                operation_id="melody",
                bars=1,
                density=0.8,
                seed=7,
            ),
            runs.AdaptNoteSequenceOperation(
                operation_id="adapt",
                sequence=runs.OperationOutputReference(
                    operation_id="melody", output="note_sequence"
                ),
                characteristics=selected(
                    "main_lead", characteristic("plucked", True)
                ),
                role_kind="lead",
            ),
        )
        plan = runs.ProductionRunPlan(plan_id="adapt-plan", operations=operations)
        registry = runs.ProductionRunRegistry()
        with mock.patch.object(
            runs, "_live_validation", return_value=([], [], None, None)
        ):
            result = registry.execute(request, plan)
        self.assertEqual(result.status, "completed")
        lookup = registry.get(result.run_id)
        assert lookup.state is not None
        selectors = {
            (item.operation_id, item.output)
            for item in lookup.state.generated_outputs
        }
        self.assertIn(("adapt", "note_sequence"), selectors)
        self.assertIn(("adapt", "composition_adaptation"), selectors)

    def test_implicit_incomplete_drum_map_uses_general_midi_fallback(self) -> None:
        incomplete = DrumPadMap(
            map_id="incomplete-kit",
            target=ChannelGeneratorTarget(channel_index=4),
            pad_count=3,
            mappings=(
                DrumRoleMapping(role="kick", pad_index=0, midi_note=52),
                DrumRoleMapping(role="snare", pad_index=1, midi_note=60),
                DrumRoleMapping(role="closed_hat", pad_index=2, midi_note=71),
            ),
        )
        result = runs._dispatch_operation(
            runs.GenerateDrumsOperation(
                operation_id="drums",
                style="house",
                bars=1,
            ),
            session_fingerprint="a" * 32,
            outputs={},
            sound_inventory=self._drum_inventory(incomplete),
        )

        self.assertTrue(any("General MIDI" in warning for warning in result.warnings))
        self.assertTrue({36, 38, 42, 46}.issubset({note.pitch for note in result.notes}))

    def test_explicit_incomplete_drum_map_remains_strict(self) -> None:
        incomplete = DrumPadMap(
            map_id="explicit-incomplete-kit",
            pad_count=3,
            mappings=(
                DrumRoleMapping(role="kick", pad_index=0, midi_note=52),
                DrumRoleMapping(role="snare", pad_index=1, midi_note=60),
                DrumRoleMapping(role="closed_hat", pad_index=2, midi_note=71),
            ),
        )
        with self.assertRaisesRegex(ValueError, "open_hat"):
            runs._dispatch_operation(
                runs.GenerateDrumsOperation(
                    operation_id="drums",
                    style="house",
                    bars=1,
                    drum_map=incomplete,
                ),
                session_fingerprint=None,
                outputs={},
            )

    def test_implicit_complete_drum_map_is_preferred_over_general_midi(self) -> None:
        complete = DrumPadMap(
            map_id="complete-kit",
            target=ChannelGeneratorTarget(channel_index=4),
            pad_count=4,
            mappings=(
                DrumRoleMapping(role="kick", pad_index=0, midi_note=52),
                DrumRoleMapping(role="snare", pad_index=1, midi_note=60),
                DrumRoleMapping(role="closed_hat", pad_index=2, midi_note=71),
                DrumRoleMapping(role="open_hat", pad_index=3, midi_note=79),
            ),
        )
        result = runs._dispatch_operation(
            runs.GenerateDrumsOperation(
                operation_id="drums",
                style="house",
                bars=1,
            ),
            session_fingerprint="a" * 32,
            outputs={},
            sound_inventory=self._drum_inventory(complete),
        )

        self.assertEqual(result.warnings, [])
        self.assertIn(79, {note.pitch for note in result.notes})
        self.assertNotIn(46, {note.pitch for note in result.notes})

    def test_palette_assignment_drives_adaptation_without_name_guessing(self) -> None:
        assignment = SoundPaletteAssignment(
            role_id="main_lead",
            target=ChannelGeneratorTarget(channel_index=2),
            product_id="synthetic-reviewed-product",
            product_name="Synthetic Reviewed Product",
            selected_preset="Synthetic Fixture",
            descriptors=("plucked", "bright"),
            descriptor_provenance=(
                DescriptorEvidence(
                    descriptor="plucked",
                    provenance="bundled_reviewed",
                    confidence=0.8,
                    source_id="synthetic-review-fixture",
                ),
            ),
            registers=("mid_high",),
            mono_poly="mono",
            characteristic_provenance="bundled_reviewed",
            metadata_confidence="medium",
            metadata_provenance="bundled_reviewed",
            metadata_family_id="synthetic-family",
            metadata_source_id="synthetic-review-fixture",
        )
        projected = characteristics_from_palette_assignment(assignment)
        self.assertEqual(projected.coverage, "family")
        self.assertIsNotNone(projected.get("plucked"))
        self.assertIsNone(projected.get("brightness"))

        source = sequence(
            CreativeNote(pitch=60, start_beats=0, duration_beats=1.0),
            CreativeNote(pitch=64, start_beats=1, duration_beats=1.0),
        )
        outputs = {
            ("melody", "note_sequence", None): runs.ProductionGeneratedOutput(
                operation_id="melody",
                output="note_sequence",
                value=source,
            ),
            (
                "palette",
                "palette_assignment",
                "main_lead",
            ): runs.ProductionGeneratedOutput(
                operation_id="palette",
                output="palette_assignment",
                role_id="main_lead",
                value=assignment,
            ),
        }
        operation = runs.AdaptNoteSequenceOperation(
            operation_id="adapt",
            sequence=runs.OperationOutputReference(
                operation_id="melody", output="note_sequence"
            ),
            palette_assignment=runs.OperationOutputReference(
                operation_id="palette",
                output="palette_assignment",
                role_id="main_lead",
            ),
            role_kind="lead",
        )
        result = runs._dispatch_operation(
            operation,
            session_fingerprint=None,
            outputs=outputs,
        )
        self.assertTrue(hasattr(result, "adaptation"))
        assert hasattr(result, "sequence")
        self.assertTrue(
            all(note.duration_beats < 1.0 for note in result.sequence.notes)
        )

    def test_adaptation_rejects_incompatible_or_future_references(self) -> None:
        request = runs.ProductionRunRequest(
            brief="Fixture.",
            scope=runs.ProductionScope(
                kind="whole_project", description="Read-only composition."
            ),
            allowed_changes=("composition",),
            completion_target="Fixture.",
        )
        operation = runs.AdaptNoteSequenceOperation(
            operation_id="adapt",
            sequence=runs.OperationOutputReference(
                operation_id="later", output="note_sequence"
            ),
            characteristics=selected(
                "main_lead", characteristic("plucked", True)
            ),
            role_kind="lead",
        )
        plan = runs.ProductionRunPlan(
            plan_id="future-adapt",
            operations=(
                operation,
                runs.GenerateMelodyOperation(operation_id="later"),
            ),
        )
        validation = runs.validate_production_run(
            request, plan, inspect_live=False
        )
        self.assertFalse(validation.valid)
        self.assertIn(
            "future_operation_reference", {item.code for item in validation.blockers}
        )


if __name__ == "__main__":
    unittest.main()
