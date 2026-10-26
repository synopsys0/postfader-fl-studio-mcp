"""Win32 shortcut diagnostics with fake APIs; no desktop input is sent."""

from __future__ import annotations

import ctypes
import os
import sys
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fl_studio_mcp import windows_input  # noqa: E402


class WindowsInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = mock.Mock(spec=windows_input._InputAPI)
        self.api.fl_windows.return_value = [windows_input._FLWindow(0x12345678ABCDEF, 42)]
        self.api.integrity_level.return_value = 0x2000
        self.api.held_modifiers.return_value = False
        self.api.focus.return_value = True
        self.api.foreground_window.return_value = 0x12345678ABCDEF
        self.api.send_shortcut.return_value = 6

    def dispatch(self):
        with mock.patch.object(windows_input, "_WindowsInputAPI", side_effect=AssertionError("real Win32 prohibited")):
            return windows_input.dispatch_piano_roll_shortcut(_api=self.api)

    def test_success_preserves_pointer_width_and_checks_six_inserted_events(self) -> None:
        result = self.dispatch()
        self.assertTrue(result["fl_window_found"])
        self.assertTrue(result["fl_window_focused"])
        self.assertTrue(result["hotkey_dispatched"])
        self.assertIsNone(result["error"])
        self.api.focus.assert_called_once_with(0x12345678ABCDEF)
        self.api.send_shortcut.assert_called_once_with()
        self.api.release_shortcut.assert_not_called()

    def test_no_fl_process_never_sends_input(self) -> None:
        self.api.fl_windows.return_value = []
        result = self.dispatch()
        self.assertFalse(result["fl_window_found"])
        self.assertFalse(result["hotkey_dispatched"])
        self.api.send_shortcut.assert_not_called()
        self.api.focus.assert_not_called()

    def test_ambiguous_fl_instances_never_choose_first_window(self) -> None:
        self.api.fl_windows.return_value.append(windows_input._FLWindow(99, 100))
        result = self.dispatch()
        self.assertIn("Multiple FL Studio", result["error"])
        self.assertFalse(result["hotkey_dispatched"])
        self.api.send_shortcut.assert_not_called()

    def test_elevation_mismatch_reports_uipi_before_input_or_focus(self) -> None:
        self.api.integrity_level.side_effect = lambda pid: 0x3000 if pid == 42 else 0x2000
        result = self.dispatch()
        self.assertIn("Windows UIPI", result["error"])
        self.assertFalse(result["hotkey_dispatched"])
        self.api.focus.assert_not_called()
        self.api.send_shortcut.assert_not_called()

    def test_privileged_caller_can_send_to_lower_integrity_fl(self) -> None:
        self.api.integrity_level.side_effect = lambda pid: 0x2000 if pid == 42 else 0x3000
        self.assertTrue(self.dispatch()["hotkey_dispatched"])

    def test_held_modifiers_are_not_released_or_combined_with_shortcut(self) -> None:
        self.api.held_modifiers.return_value = True
        result = self.dispatch()
        self.assertIn("Release held", result["error"])
        self.api.focus.assert_not_called()
        self.api.send_shortcut.assert_not_called()
        self.api.release_shortcut.assert_not_called()

    def test_actual_foreground_is_authoritative(self) -> None:
        self.api.foreground_window.return_value = 77
        result = self.dispatch()
        self.assertFalse(result["fl_window_focused"])
        self.assertFalse(result["hotkey_dispatched"])
        self.assertIn("foreground focus", result["error"])
        self.api.send_shortcut.assert_not_called()

    def test_set_foreground_return_is_not_a_substitute_for_readback(self) -> None:
        self.api.focus.return_value = False
        self.assertTrue(self.dispatch()["hotkey_dispatched"])

    def test_zero_and_partial_insertion_release_injected_modifiers(self) -> None:
        for inserted in (0, 1, 3, 5):
            with self.subTest(inserted=inserted):
                self.api.send_shortcut.return_value = inserted
                self.api.release_shortcut.reset_mock()
                result = self.dispatch()
                self.assertFalse(result["hotkey_dispatched"])
                self.assertIn(f"{inserted} of 6", result["error"])
                self.api.release_shortcut.assert_called_once_with()

    def test_native_failures_are_reported_without_input(self) -> None:
        self.api.integrity_level.side_effect = OSError("access denied")
        result = self.dispatch()
        self.assertTrue(result["fl_window_found"])
        self.assertIn("access denied", result["error"])
        self.api.send_shortcut.assert_not_called()

    def test_backend_rejects_non_windows_before_loading_dlls(self) -> None:
        with mock.patch.object(windows_input.os, "name", "posix"):
            result = windows_input.dispatch_piano_roll_shortcut()
        self.assertFalse(result["hotkey_dispatched"])
        self.assertIn("only on Windows", result["error"])

    def test_backend_filters_by_process_image_and_excludes_owned_dialogs(self) -> None:
        backend = windows_input._WindowsInputAPI.__new__(windows_input._WindowsInputAPI)
        backend.ctypes, backend.wintypes = ctypes, wintypes
        backend.enum_callback = lambda callback: callback
        backend.user32 = mock.Mock()
        backend.user32.IsWindowVisible.return_value = True
        backend.user32.GetWindow.side_effect = lambda handle, relation: 1 if handle == 3 else 0
        backend.user32.GetWindowTextLengthW.return_value = 20
        backend.user32.EnumWindows.side_effect = lambda callback, value: all(callback(handle, value) for handle in (1, 2, 3, 4))

        def process_id(handle, output):
            ctypes.cast(output, ctypes.POINTER(wintypes.DWORD)).contents.value = handle + 40
            return 5

        backend.user32.GetWindowThreadProcessId.side_effect = process_id
        backend._process_image = mock.Mock(side_effect=lambda pid: {
            41: r"C:\Downloads\FL Studio Setup.exe", 42: r"C:\FL Studio\FL64.exe",
            44: r"C:\Browser\firefox.exe",
        }[pid])
        self.assertEqual(backend.fl_windows(), [windows_input._FLWindow(2, 42)])
        self.assertNotIn(mock.call(43), backend._process_image.call_args_list)

    @unittest.skipUnless(os.name == "nt", "Native structure layout is Windows-specific")
    def test_native_handle_signatures_and_input_layout_without_sending_input(self) -> None:
        backend = windows_input._WindowsInputAPI()
        self.assertIs(backend.user32.GetForegroundWindow.restype, wintypes.HWND)
        self.assertIs(backend.kernel32.OpenProcess.restype, wintypes.HANDLE)
        self.assertEqual(ctypes.sizeof(backend.input_type), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


if __name__ == "__main__":
    unittest.main()
