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
    render_markdown,
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


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.rendered = render_markdown(summarise(SCAN))

    def test_no_display_text_reaches_the_report(self):
        # Every one of these is a setting, not a property of the plug-in.
        # Deliberately distinctive strings: a substring check against the
        # report's own prose gives false positives on ordinary words.
        for setting in ("Auto mode", "Major", "42 ms", "75 %", "-3.5 dB",
                        "Ensemble"):
            with self.subTest(setting=setting):
                self.assertNotIn(setting, self.rendered)

    def test_plugin_vocabulary_is_kept(self):
        # Parameter names come from the plug-in, not from the user, and they
        # are what makes the report useful.
        self.assertIn("Example Tuner VST3", self.rendered)

    def test_the_table_row_is_present_and_pasteable(self):
        rows = [l for l in self.rendered.splitlines()
                if l.startswith("|") and "Example Tuner VST3" in l]
        self.assertEqual(len(rows), 1, self.rendered)
        self.assertEqual(rows[0].count("|"), 5, rows[0])

    def test_it_states_that_settings_were_withheld(self):
        self.assertIn("describe the session, not the plug-in", self.rendered)


class TruncationDisclosureTests(unittest.TestCase):
    def test_a_truncated_scan_says_so(self):
        scan = dict(SCAN, truncated=True, truncated_by="max_results")
        self.assertIn("truncated by max_results", render_markdown(summarise(scan)))

    def test_a_row_count_that_disagrees_with_the_scan_is_disclosed(self):
        # Presenting per-control figures from a partial row set as though they
        # covered the whole plug-in would misstate the evidence.
        scan = dict(SCAN, real_count=99)
        self.assertIn("rows supplied only", render_markdown(summarise(scan)))

    def test_an_empty_scan_does_not_explode(self):
        profile = summarise({"plugin": {}, "parameters": []})
        self.assertEqual(profile.real_count, 0)
        self.assertIsNone(profile.highest_real_index)
        self.assertEqual(profile.largest_index_gap, 0)
        self.assertIsInstance(render_markdown(profile), str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
