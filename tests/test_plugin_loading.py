"""Plugin loading through fake FL and a synthetic menu; no real desktop calls."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_creative import DirectFakeClient, _state, bridge
from fl_studio_mcp import plugin_loading as loading


INSTRUMENT = loading.PluginMenuEntry(name="3x Osc", kind="instrument", menu_path=("Add", "3x Osc"))
EFFECT = loading.PluginMenuEntry(name="Fruity Reeverb 2", kind="effect", menu_path=("Add", "Effect", "Fruity Reeverb 2"))


class FakeMenu:
    def __init__(self, callback=None, entries=(INSTRUMENT, EFFECT)):
        self.callback = callback
        self.rows = entries
        self.calls = []

    def entries(self):
        return self.rows

    def load(self, entry):
        self.calls.append(entry)
        if self.callback:
            return self.callback(entry)
        if entry.kind == "instrument":
            _state.CHANNELS.append(_state.Channel("3xOsc"))
        else:
            target = next(track for track in _state.TRACKS if track.selected)
            slot = next(index for index in range(10) if index not in target.slots)
            target.slots[slot] = _state.Plugin(entry.name, [("Wet", 0.5)])
        return loading.MenuDispatch(status="dispatched")


class PluginLoadingTests(unittest.TestCase):
    def setUp(self):
        _state.reset()
        self.previous_mode = bridge.LEAN_WRITES_ENABLED, bridge.WRITE_MODE_ORIGIN
        bridge.LEAN_WRITES_ENABLED = False
        bridge.WRITE_MODE_ORIGIN = "disabled"
        self.client = DirectFakeClient()
        for name in ("plugin_loading", "performance", "verified_writer"):
            guard = patch(f"fl_studio_mcp.{name}.get_client", return_value=self.client)
            guard.start()
            self.addCleanup(guard.stop)
        self.os_guard = patch.object(loading.subprocess, "run", side_effect=AssertionError("real desktop dispatch in offline test"))
        self.os_guard.start()
        self.addCleanup(self.os_guard.stop)
        sleeper = patch.object(loading.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def tearDown(self):
        bridge.LEAN_WRITES_ENABLED, bridge.WRITE_MODE_ORIGIN = self.previous_mode

    def request(self, **values):
        return loading.PluginLoadRequest(name="3x Osc", kind="instrument", **values)

    def test_instrument_verifies_real_bridge_inventory_and_spacing(self):
        menu = FakeMenu()
        count = len(_state.CHANNELS)
        result = loading.load_plugin(self.request(), backend=menu)
        self.assertEqual(result.status, "loaded")
        self.assertTrue(result.verified)
        self.assertEqual(result.loaded_plugin.index, count)
        self.assertEqual(result.loaded_plugin.name, "3xOsc")
        self.assertEqual(len(menu.calls), 1)
        self.assertFalse(bridge.LEAN_WRITES_ENABLED)

    def test_effect_selects_requested_track_and_releases_temporary_write_mode(self):
        menu = FakeMenu()
        before = len(_state.TRACKS[1].slots)
        result = loading.load_plugin(loading.PluginLoadRequest(name=EFFECT.name, kind="effect", track_index=1), backend=menu)
        self.assertEqual(result.status, "loaded", result.warnings)
        self.assertEqual(len(_state.TRACKS[1].slots), before + 1)
        self.assertEqual(result.loaded_plugin.name, EFFECT.name)
        self.assertFalse(bridge.LEAN_WRITES_ENABLED)

    def test_existing_write_mode_is_preserved(self):
        bridge.LEAN_WRITES_ENABLED = True
        bridge.WRITE_MODE_ORIGIN = "runtime_request"
        result = loading.load_plugin(loading.PluginLoadRequest(name=EFFECT.name, kind="effect", track_index=1), backend=FakeMenu())
        self.assertTrue(result.verified, result.warnings)
        self.assertTrue(bridge.LEAN_WRITES_ENABLED)

    def test_wrong_kind_missing_and_ambiguous_names_never_click(self):
        for entries in ((EFFECT,), (), (INSTRUMENT, INSTRUMENT)):
            menu = FakeMenu(entries=entries)
            self.assertEqual(loading.load_plugin(self.request(), backend=menu).status, "not_dispatched")
            self.assertEqual(menu.calls, [])

    def test_changed_session_and_full_track_never_click(self):
        menu = FakeMenu()
        with self.assertRaisesRegex(loading.PluginLoadingError, "session changed"):
            loading.load_plugin(self.request(session_fingerprint="f" * 32), backend=menu)
        _state.TRACKS[1].slots = {i: _state.Plugin("Existing", []) for i in range(10)}
        with self.assertRaisesRegex(loading.PluginLoadingError, "no free effect slot"):
            loading.load_plugin(loading.PluginLoadRequest(name=EFFECT.name, kind="effect", track_index=1), backend=menu)
        self.assertEqual(menu.calls, [])

    def test_backend_failure_after_click_is_unknown_and_never_replayed(self):
        def fail(entry):
            _state.CHANNELS.append(_state.Channel("3xOsc"))
            raise TimeoutError("response lost after click")
        menu = FakeMenu(fail)
        result = loading.load_plugin(self.request(), backend=menu)
        self.assertEqual(result.status, "unknown_outcome")
        self.assertTrue(result.dispatched)
        self.assertEqual(len(menu.calls), 1)

    def test_slow_initialization_is_observed_without_another_click(self):
        before = (loading.LoadedPlugin(index=0, name="Existing"),)
        pending = (*before, loading.LoadedPlugin(index=1, name="Loading"))
        after = (*before, loading.LoadedPlugin(index=1, name="3xOsc"))
        menu = FakeMenu(lambda _: loading.MenuDispatch(status="dispatched"))
        with patch.object(loading, "_observe", side_effect=[before, pending, after]):
            result = loading.load_plugin(self.request(), backend=menu)
        self.assertTrue(result.verified)
        self.assertEqual(len(menu.calls), 1)

    def test_unmatched_addition_times_out_without_success(self):
        before = (loading.LoadedPlugin(index=0, name="Existing"),)
        wrong = (*before, loading.LoadedPlugin(index=1, name="Unrelated"))
        menu = FakeMenu(lambda _: loading.MenuDispatch(status="dispatched"))
        with patch.object(loading, "_observe", side_effect=[before, wrong]), patch.object(loading.time, "monotonic", side_effect=[0, 100]):
            result = loading.load_plugin(self.request(), backend=menu)
        self.assertEqual(result.status, "unknown_outcome")
        self.assertFalse(result.verified)
        self.assertEqual(len(menu.calls), 1)

    def test_changed_old_instance_or_duplicate_indexes_are_not_verified(self):
        old = loading.LoadedPlugin(index=0, name="Old")
        added = loading.LoadedPlugin(index=1, name="3xOsc")
        for before, after in (((old,), (old.model_copy(update={"name":"Changed"}), added)), ((old, old), (old, added))):
            self.assertIsNone(loading._new_instance(before, after, "3x Osc"))

    def test_desktop_adapter_is_blocked_in_offline_environment(self):
        with patch.dict(os.environ, {"FL_BRIDGE_SANDBOXED": "1"}):
            result = loading.list_available_plugins()
        self.assertFalse(result.supported)
        self.assertIn("offline test environment", result.error)

    def test_menu_args_are_json_data_and_subprocess_is_mocked(self):
        entry = loading.PluginMenuEntry(name="A $(test) ' quote", kind="instrument", menu_path=("Add", "A $(test) ' quote"))
        completed = SimpleNamespace(returncode=0, stdout='{"status":"dispatched"}', stderr="")
        with patch.dict(os.environ, {"FL_BRIDGE_SANDBOXED": "0"}), patch.object(loading.platform, "system", return_value="Darwin"), patch.object(loading.subprocess, "run", return_value=completed) as runner:
            result = loading.MacOSPluginMenu().load(entry)
        self.assertEqual(result.status, "dispatched")
        args = runner.call_args.args[0]
        self.assertEqual(json.loads(args[-1])["path"], list(entry.menu_path))
        self.assertNotIn(entry.name, args[-2])

    def test_adapter_timeout_is_unknown(self):
        with patch.dict(os.environ, {"FL_BRIDGE_SANDBOXED": "0"}), patch.object(loading.platform, "system", return_value="Darwin"), patch.object(loading.subprocess, "run", side_effect=subprocess.TimeoutExpired("synthetic", 25)):
            result = loading.MacOSPluginMenu().load(INSTRUMENT)
        self.assertEqual(result.status, "unknown_outcome")

    def test_destinations_are_explicit(self):
        for values in ({"kind":"effect"}, {"kind":"instrument", "track_index":1}, {"kind":"effect", "track_index":0}):
            with self.assertRaises(ValueError):
                loading.PluginLoadRequest(name="Plugin", **values)


if __name__ == "__main__":
    unittest.main()
