"""Cover the plug-in profile reduction, especially what it refuses to emit.

The fixture below mirrors the shapes a real padded VST3 produces -- a huge
reported count, real controls sparse inside it, nameless controls carrying
their identity in the display, trailing whitespace on every display string,
units embedded in the text -- with invented values throughout. No real
project's settings appear here, which is the same rule the report itself
enforces.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fl_studio_mcp.plugin_profile import (  # noqa: E402
    classify,
    summarise,
)

# Values invented. Shapes copied from what a padded VST3 really returns.
SCAN = {
    "plugin": {"name": "Example Tuner VST3"},
    "reported_parameter_count": 4240,
    "examined_count": 60,
    "real_count": 10,
    "padding_skipped": 50,
    "truncated": False,
    "truncated_by": None,
    "parameters": [
        # Nameless, identified only by display text.
        {"index": 0, "reported_name": "", "display_text": "Auto mode ",
         "display_text_available": True},
        {"index": 1, "reported_name": "Scale", "display_text": "Major ",
         "display_text_available": True},
        {"index": 2, "reported_name": "Speed", "display_text": "42 ms",
         "display_text_available": True},
        {"index": 3, "reported_name": "Depth", "display_text": "75 %",
         "display_text_available": True},
        {"index": 4, "reported_name": "Bypass", "display_text": "Off ",
         "display_text_available": True},
        {"index": 5, "reported_name": "Gain", "display_text": "-3.5 dB",
         "display_text_available": True},
        {"index": 6, "reported_name": "Plain", "display_text": "17 ",
         "display_text_available": True},
        # A gap, then stragglers far up the range.
        {"index": 40, "reported_name": "", "display_text": "Ensemble ",
         "display_text_available": True},
        {"index": 41, "reported_name": "Mix", "display_text": "50 ",
         "display_text_available": True},
        {"index": 59, "reported_name": "", "display_text": None,
         "display_text_available": False},
    ],
}


class ClassifyTests(unittest.TestCase):
    def test_trailing_whitespace_does_not_change_the_verdict(self):
        # Every display string on the live VST3 carried a trailing space.
        self.assertEqual(classify("Off "), ("on_off", None))
        self.assertEqual(classify("Off"), ("on_off", None))

    def test_units_are_kept_and_numbers_are_not(self):
        self.assertEqual(classify("42 ms"), ("numeric", "ms"))
        self.assertEqual(classify("-3.5 dB"), ("numeric", "dB"))
        self.assertEqual(classify("75 %"), ("numeric", "%"))
        self.assertEqual(classify("17 "), ("numeric", None))

    def test_arbitrary_numeric_suffix_is_not_published_as_a_unit(self):
        self.assertEqual(classify("42 PrivateSessionLabel"), ("numeric", None))

    def test_text_that_is_not_a_number_is_an_enumeration(self):
        self.assertEqual(classify("Major "), ("enumerated", None))
        self.assertEqual(classify("Low Male "), ("enumerated", None))

    def test_absent_display_is_unknown(self):
        self.assertEqual(classify(None), ("unknown", None))
        self.assertEqual(classify("   "), ("unknown", None))


class SummariseTests(unittest.TestCase):
    def setUp(self):
        self.profile = summarise(SCAN)

    def test_shape_is_captured(self):
        self.assertEqual(self.profile.plugin_name, "Example Tuner VST3")
        self.assertEqual(self.profile.reported_count, 4240)
        self.assertEqual(self.profile.nameless_count, 3)
        self.assertEqual(self.profile.highest_real_index, 59)

    def test_raw_bridge_shape_is_normalised_too(self):
        raw = {
            "plugin": "Example Tuner VST3",
            "reported_count": 2,
            "scan_start": 0,
            "scan_end": 2,
            "examined": 2,
            "real": 1,
            "padding_skipped": 1,
            "truncated": False,
            "truncated_by": None,
            "params": [
                {"index": 0, "name": "Gain", "value": 0.75, "display": "3 dB"}
            ],
        }
        profile = summarise(raw)
        self.assertEqual(profile.plugin_name, "Example Tuner VST3")
        self.assertEqual(profile.real_count, 1)
        self.assertTrue(profile.complete)

    def test_the_widest_gap_is_measured_not_the_average(self):
        # Two gaps exist: 6 -> 40 leaves 33 empty indices and 41 -> 59 leaves
        # 17. The widest is what decides whether a name search gives up early,
        # so the smaller one must not be what gets reported.
        self.assertEqual(self.profile.largest_index_gap, 33)

    def test_units_are_collected_in_order_without_duplicates(self):
        self.assertEqual(self.profile.units, ["ms", "%", "dB"])

    def test_enumerated_controls_are_identified(self):
        self.assertEqual({p.index for p in self.profile.enumerated}, {0, 1, 40})

    def test_no_shape_carries_a_value_or_display(self):
        # Structural sanitisation: there is no field to leak through.
        for shape in self.profile.parameters:
            self.assertFalse(hasattr(shape, "value"))
            self.assertFalse(hasattr(shape, "display"))
            self.assertFalse(hasattr(shape, "display_text"))
            self.assertFalse(hasattr(shape, "normalized_value"))


class TruncationDisclosureTests(unittest.TestCase):
    def test_a_truncated_scan_says_so(self):
        scan = dict(SCAN, truncated=True, truncated_by="max_results")
        self.assertTrue(any(
            "stopped by max_results" in note for note in summarise(scan).notes
        ))

    def test_a_row_count_that_disagrees_with_the_scan_is_disclosed(self):
        # Presenting per-control figures from a partial row set as though they
        # covered the whole plug-in would misstate the evidence.
        scan = dict(SCAN, real_count=99)
        profile = summarise(scan)
        self.assertFalse(profile.internally_consistent)
        self.assertTrue(any(
            "internally inconsistent" in note for note in profile.notes
        ))

    def test_an_empty_scan_does_not_explode(self):
        profile = summarise({
            "plugin": {"name": "unknown"},
            "reported_parameter_count": 0,
            "scan_start": 0,
            "scan_end": 0,
            "examined_count": 0,
            "real_count": 0,
            "padding_skipped": 0,
            "truncated": False,
            "truncated_by": None,
            "parameters": [],
        })
        self.assertEqual(profile.real_count, 0)
        self.assertIsNone(profile.highest_real_index)
        self.assertEqual(profile.largest_index_gap, 0)
        self.assertTrue(profile.complete)

    def test_missing_or_coerced_aggregate_integers_are_refused(self):
        required_fields = (
            "reported_parameter_count",
            "examined_count",
            "real_count",
            "padding_skipped",
        )
        for field in required_fields:
            for value in (None, "0", 0.0, False):
                with self.subTest(field=field, value=value):
                    scan = dict(SCAN, **{field: value})
                    with self.assertRaisesRegex(ValueError, "must be an integer"):
                        summarise(scan)
        for field in ("scan_start", "scan_end"):
            for value in ("0", 0.0, False):
                with self.subTest(field=field, value=value):
                    scan = dict(SCAN, **{field: value})
                    with self.assertRaisesRegex(ValueError, "must be an integer"):
                        summarise(scan)

    def test_coerced_parameter_index_is_refused(self):
        for value in (None, "0", 0.0, False):
            with self.subTest(value=value):
                scan = dict(SCAN)
                scan["parameters"] = [dict(SCAN["parameters"][0], index=value)]
                with self.assertRaisesRegex(ValueError, "must be an integer"):
                    summarise(scan)

    def test_missing_truncation_marker_is_refused(self):
        scan = dict(SCAN)
        scan.pop("truncated")
        with self.assertRaisesRegex(ValueError, "truncated must be a boolean"):
            summarise(scan)

    def test_coerced_or_conflicting_row_fields_are_refused(self):
        cases = []
        wrong_availability = dict(SCAN)
        wrong_availability["parameters"] = [
            dict(SCAN["parameters"][0], display_text_available="false")
        ]
        cases.append(wrong_availability)
        conflicting = dict(SCAN)
        conflicting["parameters"] = [
            dict(SCAN["parameters"][0], name="different")
        ]
        cases.append(conflicting)
        for scan in cases:
            with self.subTest(scan=scan["parameters"][0]):
                with self.assertRaises(ValueError):
                    summarise(scan)


class AggregateAliasTests(unittest.TestCase):
    def test_identical_raw_and_typed_aliases_are_accepted(self):
        scan = dict(SCAN)
        scan.update({
            "reported_count": SCAN["reported_parameter_count"],
            "examined": SCAN["examined_count"],
            "real": SCAN["real_count"],
            "params": SCAN["parameters"],
        })
        self.assertTrue(summarise(scan).internally_consistent)

    def test_every_conflicting_aggregate_alias_is_refused(self):
        cases = {
            "reported-count": {"reported_count": 4239},
            "examined-count": {"examined": 59},
            "real-count": {"real": 9},
            "parameter-list": {"params": SCAN["parameters"][:-1]},
        }
        for label, extra in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, f"conflicting {label}"):
                    summarise(dict(SCAN, **extra))

    def test_alias_equality_does_not_coerce_booleans_to_integers(self):
        with self.assertRaisesRegex(ValueError, "conflicting reported-count"):
            summarise(dict(SCAN, reported_count=True))

    def test_nested_and_aggregate_reported_counts_must_agree(self):
        plugin = dict(SCAN["plugin"], reported_parameter_count=1)
        with self.assertRaisesRegex(ValueError, "conflicting aggregate"):
            summarise(dict(SCAN, plugin=plugin))


if __name__ == "__main__":
    unittest.main(verbosity=2)
