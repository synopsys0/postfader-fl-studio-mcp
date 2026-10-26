"""Checked Windows input delivery for FL's run-last-Piano-Roll-script shortcut.

Successful input insertion is not evidence that a particular script ran. The
caller must still authenticate the request's script-runtime receipt.
"""

from __future__ import annotations

import ntpath
import os
import time
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict


@dataclass(frozen=True)
class _FLWindow:
    handle: int
    process_id: int


class _DispatchResult(TypedDict):
    platform: Literal["windows"]
    shortcut: str
    fl_window_found: bool
    fl_window_focused: bool
    hotkey_dispatched: bool
    error: str | None


class _InputAPI(Protocol):
    def fl_windows(self) -> list[_FLWindow]: ...
    def integrity_level(self, process_id: int | None) -> int: ...
    def focus(self, window: int) -> bool: ...
    def foreground_window(self) -> int: ...
    def held_modifiers(self) -> bool: ...
    def send_shortcut(self) -> int: ...
    def release_shortcut(self) -> None: ...


def dispatch_piano_roll_shortcut(*, _api: _InputAPI | None = None) -> _DispatchResult:
    """Find a unique FL process, verify privilege/focus, and check SendInput.

    ``_api`` exists for isolated tests, which must never inject desktop input.
    The ctypes backend is instantiated lazily so importing on macOS is safe.
    """
    result: _DispatchResult = {
        "platform": "windows", "shortcut": "Ctrl+Alt+Y",
        "fl_window_found": False, "fl_window_focused": False,
        "hotkey_dispatched": False, "error": None,
    }
    try:
        api = _api if _api is not None else _WindowsInputAPI()
        windows = api.fl_windows()
        if not windows:
            result["error"] = "FL Studio main window was not found (FL64.exe or FL.exe)."
            return result
        result["fl_window_found"] = True
        if len(windows) != 1:
            result["error"] = "Multiple FL Studio windows were found; close the extra instances or run Postfader Apply manually."
            return result
        window = windows[0]
        if api.integrity_level(window.process_id) > api.integrity_level(None):
            result["error"] = (
                "FL Studio runs at a higher Windows privilege level than PostFader; "
                "Windows UIPI blocks synthetic input. Run both at the same privilege "
                "level or run Scripts > Postfader > Postfader Apply manually."
            )
            return result
        if api.held_modifiers():
            result["error"] = "Release held Ctrl, Alt, Shift, or Windows keys before triggering the Piano Roll script."
            return result
        api.focus(window.handle)
        if api.foreground_window() != window.handle:
            result["error"] = "Windows did not grant FL Studio foreground focus; run Postfader Apply manually."
            return result
        result["fl_window_focused"] = True
        inserted = api.send_shortcut()
        if inserted != 6:
            api.release_shortcut()
            result["error"] = (
                f"Windows SendInput inserted {inserted} of 6 shortcut events; "
                "script execution is unconfirmed. Run Postfader Apply manually."
            )
            return result
        result["hotkey_dispatched"] = True
        return result
    except (OSError, RuntimeError, ValueError) as error:
        result["error"] = f"Windows shortcut dispatch failed: {type(error).__name__}: {error}"[:512]
        return result


class _WindowsInputAPI:
    """Small typed Win32 boundary; every native handle retains pointer width."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        if os.name != "nt":
            raise OSError("Windows input is available only on Windows")
        self.ctypes = ctypes
        self.wintypes = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        signatures = (
            (self.user32, "EnumWindows", [self.enum_callback, wintypes.LPARAM], wintypes.BOOL),
            (self.user32, "IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
            (self.user32, "GetWindow", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
            (self.user32, "GetWindowTextLengthW", [wintypes.HWND], ctypes.c_int),
            (self.user32, "GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (self.user32, "ShowWindow", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            (self.user32, "SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
            (self.user32, "GetForegroundWindow", [], wintypes.HWND),
            (self.user32, "GetAsyncKeyState", [ctypes.c_int], ctypes.c_short),
            (self.kernel32, "OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            (self.kernel32, "GetCurrentProcess", [], wintypes.HANDLE),
            (self.kernel32, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            (self.kernel32, "QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (self.advapi32, "OpenProcessToken", [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL),
            (self.advapi32, "GetTokenInformation", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (self.advapi32, "GetSidSubAuthorityCount", [ctypes.c_void_p], ctypes.POINTER(wintypes.BYTE)),
            (self.advapi32, "GetSidSubAuthority", [ctypes.c_void_p, wintypes.DWORD], ctypes.POINTER(wintypes.DWORD)),
        )
        for library, name, arguments, result in signatures:
            function = getattr(library, name)
            function.argtypes = arguments
            function.restype = result

        class KeyboardInput(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        class MouseInput(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class HardwareInput(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD)]

        class InputUnion(ctypes.Union):
            _fields_ = [("ki", KeyboardInput), ("mi", MouseInput), ("hi", HardwareInput)]

        class Input(ctypes.Structure):
            _anonymous_ = ("data",)
            _fields_ = [("type", wintypes.DWORD), ("data", InputUnion)]

        self.input_type = Input
        self.keyboard_type = KeyboardInput
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT

    def _process_image(self, process_id: int) -> str | None:
        ctypes, wintypes = self.ctypes, self.wintypes
        process = self.kernel32.OpenProcess(0x1000, False, process_id)
        if not process:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if not self.kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(length)):
                return None
            return buffer.value
        finally:
            self.kernel32.CloseHandle(process)

    def fl_windows(self) -> list[_FLWindow]:
        ctypes, wintypes = self.ctypes, self.wintypes
        found: list[_FLWindow] = []
        images: dict[int, str | None] = {}

        def inspect(window: int, _parameter: int) -> bool:
            # Owned windows include plug-ins/dialogs and must not substitute
            # for the FL application's main window.
            if (not self.user32.IsWindowVisible(window)
                    or self.user32.GetWindow(window, 4)
                    or not self.user32.GetWindowTextLengthW(window)):
                return True
            process_id = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
            pid = int(process_id.value)
            if pid not in images:
                images[pid] = self._process_image(pid)
            image = images[pid]
            if image is not None and ntpath.basename(image).casefold() in {"fl64.exe", "fl.exe"}:
                found.append(_FLWindow(int(window), pid))
            return True

        callback = self.enum_callback(inspect)
        if not self.user32.EnumWindows(callback, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return found

    def integrity_level(self, process_id: int | None) -> int:
        ctypes, wintypes = self.ctypes, self.wintypes
        owned = process_id is not None
        process = (self.kernel32.OpenProcess(0x1000, False, process_id)
                   if owned else self.kernel32.GetCurrentProcess())
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        token = wintypes.HANDLE()
        try:
            if not self.advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
                raise ctypes.WinError(ctypes.get_last_error())
            size = wintypes.DWORD()
            self.advapi32.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
            if not size.value:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(size.value)
            if not self.advapi32.GetTokenInformation(token, 25, buffer, size, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            # TOKEN_MANDATORY_LABEL starts with SID_AND_ATTRIBUTES; its first
            # field is the SID pointer on both 32-bit and 64-bit Windows.
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
            count = self.advapi32.GetSidSubAuthorityCount(sid).contents.value
            if not count:
                raise ValueError("Windows returned an empty integrity SID")
            return int(self.advapi32.GetSidSubAuthority(sid, count - 1).contents.value)
        finally:
            if token:
                self.kernel32.CloseHandle(token)
            if owned:
                self.kernel32.CloseHandle(process)

    def focus(self, window: int) -> bool:
        self.user32.ShowWindow(window, 9)
        granted = bool(self.user32.SetForegroundWindow(window))
        time.sleep(0.3)
        return granted

    def foreground_window(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def held_modifiers(self) -> bool:
        return any(self.user32.GetAsyncKeyState(key) & 0x8000
                   for key in (0x10, 0x11, 0x12, 0x5B, 0x5C))

    def _send(self, keys: tuple[tuple[int, bool], ...]) -> int:
        inputs = (self.input_type * len(keys))()
        for event, (key, release) in zip(inputs, keys):
            event.type = 1
            event.ki = self.keyboard_type(key, 0, 0x0002 if release else 0, 0, 0)
        return int(self.user32.SendInput(len(inputs), inputs, self.ctypes.sizeof(self.input_type)))

    def send_shortcut(self) -> int:
        return self._send(((0x11, False), (0x12, False), (0x59, False),
                           (0x59, True), (0x12, True), (0x11, True)))

    def release_shortcut(self) -> None:
        self._send(((0x59, True), (0x12, True), (0x11, True)))
