"""Deterministic tests for the audio advisory tool layer; no FL Studio required.

Every measurement case uses synthetic, script-generated audio. The measurement
maths itself is covered by tests/test_audio.py, so this file asserts the public
boundary: typed shapes, provenance passthrough, path rules, and bounded file
discovery.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, os.fspath(ROOT))

from fl_studio_mcp.advisory import (  # noqa: E402
    AdvisoryError,
    AudioComparison,
    AudioFileAnalysis,
    MaskingAnalysis,
    RecentAudioListing,
    analyze_audio_file,
    analyze_masking,
    compare_audio_files,
    find_recent_audio_files,
    resolve_audio_path,
)
from fl_studio_mcp import advisory  # noqa: E402


FIXTURES = ROOT / "tests" / "fixtures" / "audio"
REFERENCE = FIXTURES / "reference_mix.wav"
CANDIDATE = FIXTURES / "candidate_delayed_minus6db.wav"


def path_of(fixture: Path) -> str:
    """Tools take absolute path strings, exactly as an MCP client sends them."""
    return os.fspath(fixture)


def temporary_tree() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="flmcp-advisory-")


def write_audio_file(directory: Path, name: str, *, mtime: datetime) -> Path:
    """Create a discoverable file with a controlled modification time.

    Discovery never opens a file, so the bytes are irrelevant; the timestamp is
    the whole point of these cases.
    """
    path = directory / name
    path.write_bytes(b"RIFF----WAVE")
    stamp = mtime.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def write_silent_pcm(directory: Path, name: str, *, frames: int) -> Path:
    """Write a valid synthetic stereo WAV with a controlled frame count."""
    path = directory / name
    with wave.open(os.fspath(path), "wb") as target:
        target.setnchannels(2)
        target.setsampwidth(2)
        target.setframerate(48_000)
        target.writeframes(b"\x00\x00" * 2 * frames)
    return path


class AnalyzeFileTests(unittest.TestCase):
    def test_a_synthetic_fixture_returns_the_full_typed_measurement_set(self):
        result = analyze_audio_file(path_of(REFERENCE))
        self.assertIsInstance(result, AudioFileAnalysis)
        self.assertEqual(result.schema_version, "1.0")
        # Provenance must describe the file that was actually opened.
        self.assertEqual(result.file.path, os.fspath(REFERENCE))
        self.assertEqual(result.file.source_frames, 192_000)
        self.assertEqual(result.file.sample_rate, 48_000)
        self.assertEqual(result.file.channels, 2)
        self.assertFalse(result.file.truncated)
        self.assertEqual(len(result.file.sha256), 64)
        # The measurements a mix decision actually rests on.
        self.assertIsNotNone(result.loudness.true_peak_dbtp)
        self.assertIsNotNone(result.loudness.rms_db)
        self.assertIsNotNone(result.loudness.crest_factor_db)
        self.assertEqual(result.loudness.clipped_samples, 0)
        self.assertEqual(result.loudness.channel_count, 2)
        self.assertIsNotNone(result.loudness.dc_offset)
        self.assertEqual(
            set(result.spectrum.bands),
            {"sub", "low", "low_mid", "mid", "high_mid", "presence", "air"},
        )
        self.assertIsNotNone(result.dynamics.dynamic_spread_db)
        self.assertEqual(result.stereo.channels, 2)
        self.assertIsNotNone(result.stereo.correlation)

    def test_the_engines_own_confidence_and_limitations_are_forwarded(self):
        # The agent needs to know how far to trust the numbers, and that has to
        # come from the engine rather than being restated by this layer.
        result = analyze_audio_file(path_of(REFERENCE))
        self.assertIn(result.confidence.level, {"high", "medium", "low"})
        self.assertTrue(result.confidence.basis)
        self.assertTrue(result.limitations)
        self.assertTrue(
            any("not a certified" in item for item in result.limitations),
            result.limitations,
        )
        self.assertEqual(result.analyzer_version, "audio-analysis-2.0")
        self.assertEqual(
            result.analyzer_versions.true_peak, "4x-polyphase-chunked-1"
        )

    def test_the_tool_layer_emits_no_advice_text(self):
        # The engine can phrase readings prescriptively ("a de-esser would
        # help"). Judgement belongs to the agent reading the mixer state, so
        # the tool surface carries measurements only.
        result = analyze_audio_file(path_of(REFERENCE))
        self.assertEqual(result.interpretation, "measurements_only")
        self.assertNotIn("readings", result.model_dump())

    def test_pitch_is_measured_only_when_it_is_asked_for(self):
        # Pitch tracking is meaningful for a vocal stem and misleading for a
        # full mix, so it must never be attached silently.
        self.assertIsNone(analyze_audio_file(path_of(REFERENCE)).pitch)
        with_pitch = analyze_audio_file(path_of(REFERENCE), include_pitch=True)
        self.assertIsNotNone(with_pitch.pitch)
        self.assertEqual(with_pitch.pitch.median_note, "A3")
        self.assertEqual(with_pitch.analyzer_versions.pitch, "autocorrelation-monophonic-1")

    def test_a_shorter_analysis_bound_is_honoured_and_disclosed(self):
        result = analyze_audio_file(path_of(REFERENCE), max_seconds=1.0)
        self.assertTrue(result.file.truncated)
        self.assertEqual(result.file.analyzed_frames, 48_000)
        self.assertLess(result.file.analyzed_frames, result.file.source_frames)

    def test_an_out_of_range_analysis_bound_is_refused(self):
        with self.assertRaises(AdvisoryError):
            analyze_audio_file(path_of(REFERENCE), max_seconds=0.0)
        with self.assertRaises(AdvisoryError):
            analyze_audio_file(path_of(REFERENCE), max_seconds=6000.0)

    def test_results_are_immutable(self):
        # A returned measurement is evidence; nothing downstream may edit it.
        result = analyze_audio_file(path_of(REFERENCE))
        with self.assertRaises(ValueError):
            result.confidence.score = 1.0


class CompareFilesTests(unittest.TestCase):
    def test_two_synthetic_fixtures_produce_alignment_and_band_deltas(self):
        result = compare_audio_files(path_of(REFERENCE), path_of(CANDIDATE))
        self.assertIsInstance(result, AudioComparison)
        self.assertEqual(result.reference.canonical_path, os.fspath(REFERENCE))
        self.assertEqual(result.target.canonical_path, os.fspath(CANDIDATE))
        self.assertEqual(
            set(result.band_deltas),
            {"sub", "low", "low_mid", "mid", "high_mid", "presence", "air"},
        )
        for name, delta in result.band_deltas.items():
            with self.subTest(band=name):
                self.assertIsInstance(delta.difference_db, float)
        self.assertIsNotNone(result.centroid_hz.reference)
        self.assertIsNotNone(result.alignment.target_lag_seconds)
        self.assertIn(result.alignment.confidence.level, {"high", "medium", "low"})
        self.assertIsInstance(result.comparison_ready, bool)
        self.assertTrue(result.limitations)

    def test_loudness_matching_is_reported_as_in_memory_only(self):
        # The comparison applies gain to make the two files measurable against
        # each other. The result has to say plainly that neither file changed.
        result = compare_audio_files(path_of(REFERENCE), path_of(CANDIDATE))
        self.assertTrue(result.loudness_matching.in_memory_only)
        self.assertFalse(result.loudness_matching.source_files_modified)
        self.assertIsNotNone(result.loudness_matching.reference_lufs)
        self.assertIsNotNone(result.loudness_matching.target_lufs_before)

    def test_both_sides_are_path_checked(self):
        with self.assertRaises(AdvisoryError) as bad_reference:
            compare_audio_files("reference_mix.wav", path_of(CANDIDATE))
        self.assertIn("reference_path", str(bad_reference.exception))
        with self.assertRaises(AdvisoryError) as bad_candidate:
            compare_audio_files(path_of(REFERENCE), "candidate.wav")
        self.assertIn("candidate_path", str(bad_candidate.exception))


class MaskingTests(unittest.TestCase):
    def test_synchronous_fixtures_report_overlap_and_level_margins(self):
        result = analyze_masking(path_of(REFERENCE), path_of(CANDIDATE))
        self.assertIsInstance(result, MaskingAnalysis)
        self.assertTrue(result.context_ready)
        self.assertEqual(result.readiness_reasons, ())
        self.assertIsNotNone(result.masking)
        self.assertEqual(result.masking.frequency_scope_hz, (80, 12000))
        self.assertEqual(
            set(result.masking.bands),
            {"sub", "low", "low_mid", "mid", "high_mid", "presence", "air"},
        )
        for name, band in result.masking.bands.items():
            with self.subTest(band=name):
                # The two numbers an EQ or ducking decision needs: how much the
                # two sources share, and how much room the vocal has.
                self.assertGreaterEqual(band.spectral_overlap, 0.0)
                self.assertIsInstance(band.vocal_minus_instrument_median_db, float)
        self.assertIsNotNone(result.balance.vocal_minus_instrument_lu)
        self.assertGreater(result.vocal_activity.active_frame_count, 0)

    def test_unsynchronised_inputs_return_reasons_instead_of_a_score(self):
        # A different frame count must fail closed rather than inventing a
        # masking number from mismatched material.
        with temporary_tree() as raw:
            shorter = write_silent_pcm(Path(raw), "shorter.wav", frames=96_000)
            result = analyze_masking(path_of(REFERENCE), path_of(shorter))
            self.assertFalse(result.context_ready)
            self.assertTrue(result.readiness_reasons)
            self.assertIsNone(result.masking)
            self.assertIsNone(result.balance)
            self.assertIsNone(result.vocal_activity)
            # Provenance still shows exactly what was compared.
            self.assertEqual(result.vocal.canonical_path, os.fspath(REFERENCE))
            self.assertEqual(
                result.instrumental.canonical_path,
                os.fspath(shorter.resolve()),
            )


class PathRuleTests(unittest.TestCase):
    def test_a_relative_path_is_refused(self):
        # An MCP client's working directory is not the user's; a relative path
        # cannot be resolved to a file the owner meant.
        with self.assertRaises(AdvisoryError) as refused:
            analyze_audio_file("tests/fixtures/audio/reference_mix.wav")
        self.assertIn("absolute", str(refused.exception))

    def test_a_home_relative_path_is_not_expanded(self):
        with self.assertRaises(AdvisoryError):
            analyze_audio_file("~/Documents/Image-Line/FL Studio/Audio/Rendered/mix.wav")

    def test_a_parent_traversal_component_is_refused(self):
        with self.assertRaises(AdvisoryError) as refused:
            analyze_audio_file(os.fspath(FIXTURES / ".." / "audio" / "reference_mix.wav"))
        self.assertIn("..", str(refused.exception))

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(AdvisoryError) as refused:
            analyze_audio_file(os.fspath(FIXTURES / "no_such_bounce.wav"))
        self.assertIn("does not exist", str(refused.exception))

    def test_a_non_audio_extension_is_refused(self):
        with self.assertRaises(AdvisoryError) as refused:
            analyze_audio_file(os.fspath(ROOT / "pyproject.toml"))
        self.assertIn("audio extension", str(refused.exception))

    def test_a_directory_is_refused(self):
        with self.assertRaises(AdvisoryError) as refused:
            analyze_audio_file(os.fspath(FIXTURES))
        self.assertIn("directory", str(refused.exception))

    def test_a_directory_named_like_a_render_is_still_refused(self):
        # The suffix check alone would wave this through, so the regular-file
        # rule has to be checked independently of the extension.
        with temporary_tree() as raw:
            decoy = Path(raw) / "full-mix.wav"
            decoy.mkdir()
            with self.assertRaises(AdvisoryError) as refused:
                analyze_audio_file(os.fspath(decoy))
        self.assertIn("directory", str(refused.exception))

    def test_a_symlink_escaping_the_allowed_roots_is_refused(self):
        # A link named like a bounce is the cheapest way to turn a read tool
        # into a general file reader.
        with temporary_tree() as raw:
            link = Path(raw) / "innocent_stem.wav"
            link.symlink_to("/etc/hosts")
            with self.assertRaises(AdvisoryError) as refused:
                analyze_audio_file(os.fspath(link))
        self.assertIn("outside the allowed", str(refused.exception))

    def test_a_symlink_resolving_inside_the_repository_is_allowed(self):
        # The rule is about where a link points, not that links are banned.
        with temporary_tree() as raw:
            link = Path(raw) / "linked_reference.wav"
            link.symlink_to(REFERENCE)
            self.assertEqual(resolve_audio_path(os.fspath(link)), REFERENCE)

    def test_an_oversized_file_is_refused_before_it_is_opened(self):
        # The real cap is half a gigabyte; lowering it is the only offline way
        # to prove the check exists and fires before any decode.
        original = advisory.MAX_AUDIO_FILE_BYTES
        advisory.MAX_AUDIO_FILE_BYTES = 1024
        try:
            with self.assertRaises(AdvisoryError) as refused:
                analyze_audio_file(path_of(REFERENCE))
        finally:
            advisory.MAX_AUDIO_FILE_BYTES = original
        self.assertIn("analysis cap", str(refused.exception))

    def test_an_empty_file_is_refused(self):
        with temporary_tree() as raw:
            empty = Path(raw) / "empty.wav"
            empty.write_bytes(b"")
            with self.assertRaises(AdvisoryError) as refused:
                analyze_audio_file(os.fspath(empty))
        self.assertIn("empty", str(refused.exception))

    def test_an_accepted_path_returns_the_resolved_regular_file(self):
        self.assertEqual(resolve_audio_path(path_of(REFERENCE)), REFERENCE)


class DiscoveryTests(unittest.TestCase):
    def test_discovery_is_newest_first_and_respects_its_limit(self):
        now = datetime.now(timezone.utc)
        with temporary_tree() as raw:
            tree = Path(raw)
            nested = tree / "project" / "Audio"
            nested.mkdir(parents=True)
            write_audio_file(tree, "oldest.wav", mtime=now - timedelta(days=3))
            write_audio_file(nested, "middle.flac", mtime=now - timedelta(days=2))
            write_audio_file(tree, "newest.wav", mtime=now - timedelta(minutes=5))
            listing = find_recent_audio_files(limit=2, roots=[tree])
            self.assertIsInstance(listing, RecentAudioListing)
            self.assertEqual(
                [Path(item.path).name for item in listing.files],
                ["newest.wav", "middle.flac"],
            )
            # The limit trims the answer without hiding how much was found.
            self.assertEqual(listing.returned_file_count, 2)
            self.assertEqual(listing.matched_file_count, 3)
            self.assertEqual(listing.limit, 2)

    def test_each_listed_file_carries_size_and_modification_time(self):
        stamp = datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)
        with temporary_tree() as raw:
            tree = Path(raw)
            written = write_audio_file(tree, "bounce.wav", mtime=stamp)
            listing = find_recent_audio_files(roots=[tree])
            self.assertEqual(len(listing.files), 1)
            entry = listing.files[0]
            self.assertEqual(entry.path, os.fspath(written))
            self.assertEqual(entry.root, os.fspath(tree))
            self.assertEqual(entry.size_bytes, written.stat().st_size)
            self.assertEqual(entry.suffix, ".wav")
            self.assertEqual(entry.modified_at, stamp)

    def test_non_audio_hidden_and_linked_entries_are_skipped(self):
        # Discovery reports bounces, and it never follows a link out of the
        # roots it was told to look in.
        now = datetime.now(timezone.utc)
        with temporary_tree() as raw:
            tree = Path(raw)
            write_audio_file(tree, "keep.wav", mtime=now)
            (tree / "notes.txt").write_text("not audio", encoding="utf-8")
            write_audio_file(tree, ".hidden.wav", mtime=now)
            (tree / "linked.wav").symlink_to(REFERENCE)
            listing = find_recent_audio_files(roots=[tree])
            self.assertEqual(
                [Path(item.path).name for item in listing.files], ["keep.wav"]
            )

    def test_a_missing_root_is_reported_rather_than_raised(self):
        with temporary_tree() as raw:
            absent = Path(raw) / "not-created"
            listing = find_recent_audio_files(roots=[absent])
            self.assertEqual(listing.files, ())
            self.assertEqual(len(listing.roots), 1)
            self.assertFalse(listing.roots[0].exists)
            self.assertEqual(listing.roots[0].matched_file_count, 0)

    def test_the_walk_is_depth_bounded(self):
        now = datetime.now(timezone.utc)
        with temporary_tree() as raw:
            tree = Path(raw)
            deep = tree.joinpath(*["level"] * (advisory.MAX_DISCOVERY_DEPTH + 1))
            deep.mkdir(parents=True)
            write_audio_file(deep, "too_deep.wav", mtime=now)
            listing = find_recent_audio_files(roots=[tree])
            self.assertEqual(listing.files, ())
            self.assertTrue(listing.scan_truncated)

    def test_the_same_file_reached_through_two_roots_is_listed_once(self):
        # FL's Rendered folder sits inside its Audio folder, so overlapping
        # roots are the normal case, not an edge case.
        now = datetime.now(timezone.utc)
        with temporary_tree() as raw:
            tree = Path(raw)
            inner = tree / "Rendered"
            inner.mkdir()
            write_audio_file(inner, "mix.wav", mtime=now)
            listing = find_recent_audio_files(roots=[inner, tree])
            self.assertEqual(len(listing.files), 1)
            self.assertEqual(listing.matched_file_count, 1)

    def test_an_out_of_range_limit_is_refused(self):
        with self.assertRaises(AdvisoryError):
            find_recent_audio_files(limit=0)
        with self.assertRaises(AdvisoryError):
            find_recent_audio_files(limit=201)

    def test_the_default_roots_are_fixed_fl_locations(self):
        # An agent cannot choose where this connector reads; the roots are
        # compiled in and stay inside the FL user folder.
        self.assertEqual(len(advisory.DEFAULT_DISCOVERY_ROOTS), 3)
        for root in advisory.DEFAULT_DISCOVERY_ROOTS:
            with self.subTest(root=root):
                self.assertTrue(root.is_absolute())
                self.assertTrue(root.is_relative_to(advisory.FL_STUDIO_USER_ROOT))

    def test_the_default_roots_produce_a_newest_first_listing(self):
        now = datetime.now(timezone.utc)
        with temporary_tree() as raw:
            tree = Path(raw)
            write_audio_file(tree, "older.wav", mtime=now - timedelta(minutes=2))
            write_audio_file(tree, "newer.wav", mtime=now - timedelta(minutes=1))
            original = advisory.DEFAULT_DISCOVERY_ROOTS
            advisory.DEFAULT_DISCOVERY_ROOTS = (tree,)
            try:
                listing = find_recent_audio_files(limit=5)
            finally:
                advisory.DEFAULT_DISCOVERY_ROOTS = original
            self.assertEqual(
                [Path(item.path).name for item in listing.files],
                ["newer.wav", "older.wav"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
