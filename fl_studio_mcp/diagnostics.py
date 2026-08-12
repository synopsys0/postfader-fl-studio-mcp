#!/usr/bin/env python3
"""Check every piece of the FL Studio MCP setup and say what to fix.

Run this whenever the connection misbehaves. Each check prints OK / WARN /
FAIL with the exact next action, in the order things need to be true.
Set FL_STUDIO_USER_DATA_DIR when FL Studio uses a custom user data folder.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

from .bridge_install import (
    BridgeInstallError,
    expected_bridge_deployment,
    hardware_dir,
    target_path,
    user_data_dir,
)

# Paths are resolved once at import from the same helpers the installer uses,
# so a relocated FL data folder is reported identically by both commands.
FL_STUDIO_USER_DATA_DIR = str(user_data_dir())
HARDWARE_DIR = str(hardware_dir())
SCRIPT_PATH = str(target_path())
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)

GREEN, YELLOW, RED, DIM, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m")

problems: list[str] = []
skipped_host_checks: list[str] = []


def native_midi_probe_blocked() -> bool:
    """True when the caller declared this process cannot open CoreMIDI.

    Set FL_BRIDGE_SANDBOXED=1 in an automation harness, a CI runner, or any
    process lacking CoreMIDI entitlements. Doctor then skips the port scan and
    the live handshake and says so, instead of risking a native abort.
    """
    marker = os.environ.get("FL_BRIDGE_SANDBOXED", "").strip().lower()
    return bool(marker) and marker not in {"0", "false", "no", "none", "off"}


def ok(msg, detail=""):
    print(f"  {GREEN}OK{RESET}    {msg}")
    if detail:
        print(f"        {DIM}{detail}{RESET}")


def warn(msg, fix=""):
    print(f"  {YELLOW}WARN{RESET}  {msg}")
    if fix:
        print(f"        {fix}")


def fail(msg, fix=""):
    print(f"  {RED}FAIL{RESET}  {msg}")
    if fix:
        print(f"        {fix}")
    problems.append(msg)


def check_fl_studio():
    print("\nFL Studio")
    apps = sorted(glob.glob("/Applications/FL Studio*.app"))
    if not apps:
        fail("FL Studio not found in /Applications.",
             "Install FL Studio, then re-run this.")
        return
    for app in apps:
        try:
            ver = subprocess.run(
                ["defaults", "read", os.path.join(app, "Contents/Info.plist"),
                 "CFBundleShortVersionString"],
                capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            ver = "?"
        ok(f"{os.path.basename(app)}", f"version {ver}")

    py = glob.glob("/Applications/FL Studio*/Contents/Resources/FL/Shared/"
                   "Python/Python.framework/Versions/*")
    if py:
        vers = sorted({os.path.basename(p) for p in py if os.path.basename(p) != "Current"})
        ok(f"Embedded Python {', '.join(vers)}",
           "found inside the FL Studio app bundle")
    else:
        warn("Could not find FL's embedded Python framework.",
             "The bridge needs FL 21+ with Python scripting support.")


def check_script_installed():
    print("\nBridge script")
    if not os.path.isdir(HARDWARE_DIR):
        fail(f"FL's Hardware folder is missing: {HARDWARE_DIR}",
             "Run FL Studio at least once so it creates its settings folders. "
             "For a custom location, set FL_STUDIO_USER_DATA_DIR to FL Studio's "
             "user data folder.")
        return
    if not os.path.isfile(SCRIPT_PATH):
        fail("Bridge script is not installed.",
             "Run: postfader-install-bridge   (or ./scripts/install.sh from a clone)")
        return

    ok(f"Installed at {SCRIPT_PATH}")
    try:
        expected, digest = expected_bridge_deployment()
        with open(SCRIPT_PATH, "rb") as installed:
            actual = installed.read()
    except BridgeInstallError as exc:
        fail(f"Could not verify the installed bridge: {exc}",
             "Reinstall the package, or restore the bridge source and re-run "
             "postfader-install-bridge.")
        return
    if actual == expected:
        ok("Installed bridge matches this version", f"source SHA-256 {digest}")
    else:
        warn("Installed bridge differs from the one this version ships.",
             "Run postfader-install-bridge to update it, then reload the "
             "script in FL (re-pick the controller type).")


def check_midi_ports():
    print("\nMIDI port for FL to attach the script to")
    if native_midi_probe_blocked():
        warn(
            "Native CoreMIDI enumeration skipped in this restricted process.",
            "This prevents disposable Python helpers from triggering macOS "
            "'quit unexpectedly' alerts. Re-run doctor with host MIDI access "
            "for the authoritative check.",
        )
        skipped_host_checks.append("CoreMIDI port enumeration")
        return
    # Some CoreMIDI failures terminate the C++ extension instead of raising a
    # Python exception. Isolate enumeration so doctor itself can continue and
    # still try the live bridge.
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,rtmidi; "
            "mi=rtmidi.MidiIn(); mo=rtmidi.MidiOut(); "
            "print(json.dumps({'in': mi.get_ports(), 'out': mo.get_ports()})); "
            "mi.close_port(); mo.close_port()",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip().splitlines()
        warn(
            "CoreMIDI enumeration failed in an isolated probe; continuing.",
            detail[-1] if detail else "The live bridge check below is authoritative.",
        )
        return
    try:
        found = json.loads(probe.stdout)
        inputs, outputs = found["in"], found["out"]
    except (json.JSONDecodeError, KeyError, TypeError):
        warn("CoreMIDI returned an unreadable port list; continuing.")
        return

    enable_iac = (
        "Open Audio MIDI Setup (in /Applications/Utilities), press Cmd-2 for\n"
        "        the MIDI Studio window, double-click 'IAC Driver', tick\n"
        "        'Device is online', and click Apply. Then restart FL Studio."
    )
    if not inputs:
        fail("No MIDI input ports exist, so FL has nothing to attach the script to.",
             enable_iac)
    else:
        ok(f"{len(inputs)} MIDI input port(s) available", ", ".join(inputs))
        if not any("IAC" in p for p in inputs):
            warn("No IAC Driver bus found, but other MIDI inputs exist.",
                 "Any MIDI input works, as long as an output with the same "
                 "Port number\n        is paired with it in FL.")

    # The bridge sends its replies over MIDI output and raises at startup when
    # none is assigned, so a host with inputs only cannot reach `ready`.
    if not outputs:
        fail("No MIDI output ports exist, so the bridge cannot reply.",
             enable_iac)
    else:
        ok(f"{len(outputs)} MIDI output port(s) available", ", ".join(outputs))
        print("        In FL: Options > MIDI settings > Output, give the same "
              "port the\n        same Port number as the input, or the bridge "
              "refuses to start.")


def check_bridge_live():
    print("\nLive connection")
    if native_midi_probe_blocked():
        warn(
            "Live IAC bridge handshake skipped in this restricted process.",
            "Run doctor with host MIDI access; TCP/file fallbacks are normally "
            "unavailable inside FL Studio's script sandbox.",
        )
        skipped_host_checks.append("live FL Studio IAC handshake")
        return
    os.environ.setdefault("FL_BRIDGE_ENABLE_MIDI", "1")
    try:
        from fl_studio_mcp.bridge_client import BridgeClient, BridgeError
        from fl_studio_mcp.readonly_inspector import ReadOnlyGateway, ReadOnlyInspector
    except ImportError as e:
        fail(f"Cannot import the MCP package: {e}",
             "Reinstall the package, or run ./scripts/install.sh from a clone.")
        return
    client = BridgeClient(timeout=3)
    try:
        info = client.ping()
    except BridgeError:
        fail("Bridge is not answering over MIDI SysEx.",
             "With FL Studio running: Options > MIDI settings > Input, select\n"
             "        the port (e.g. 'IAC Driver Bus 1'), set Controller type to\n"
             "        'Universal Bridge', and make sure the port is enabled.\n"
             "        Under Output, give the same port the SAME Port number --\n"
             "        the bridge refuses to start without an assigned output.\n"
             "        View > Script output should then end with 'ready: MIDI SysEx'.")
        return

    reported_digest = info.get("bridge_source_sha256")
    if reported_digest:
        try:
            _expected, expected_digest = expected_bridge_deployment()
        except BridgeInstallError as exc:
            fail(f"Could not calculate the repository bridge hash: {exc}",
                 "Reinstall the package, or restore the bridge source and re-run "
             "postfader-install-bridge.")
        else:
            if reported_digest == expected_digest:
                ok("Running bridge matches this version",
                   f"source SHA-256 {expected_digest}")
            else:
                fail(
                    "Running FL Studio has a stale bridge loaded.",
                    "Run postfader-install-bridge, then reload the script in FL Studio "
                    "(View > Script output > Reload script).",
                )
    else:
        warn(
            "Running bridge did not report its source hash.",
            "Run postfader-install-bridge, then reload the script in FL Studio.",
        )
    inspector = ReadOnlyInspector(ReadOnlyGateway(client))
    connection = inspector.connection_info()
    if not connection.compatible:
        fail(
            f"Live compatibility gate failed: {connection.compatibility_reason}",
            "Use FL Studio 2026 build 5336 or newer and reload the protocol-2 bridge.",
        )
        return
    # Two supported modes, and the bridge is the only thing that decides which
    # one is in force. Read-only is the default; write_test is a deliberate
    # opt-in the user made when launching FL. Anything else is a bridge that
    # confirmed neither, which is the only failure here.
    if connection.verified_writes_enabled:
        ok(
            f"Connected to {info.get('program_title')} {info.get('fl_version')}",
            f"protocol {connection.bridge_protocol_version}, mode "
            f"{connection.bridge_mode}: the verified mutation commands are "
            "enabled, so the fl_set_* tools can change this project. Relaunch "
            "FL without FL_BRIDGE_ENABLE_WRITES for a read-only-locked bridge.",
        )
    elif connection.bridge_read_only_enforced:
        ok(
            f"Connected to {info.get('program_title')} {info.get('fl_version')}",
            f"protocol {connection.bridge_protocol_version}, mode "
            f"{connection.bridge_mode}: writes are not dispatchable. Launch FL "
            "with FL_BRIDGE_ENABLE_WRITES=1 to enable them.",
        )
    else:
        fail(
            "The running bridge confirmed neither its read-only lock nor the "
            "verified write mode.",
            "Run postfader-install-bridge and reload the script in FL Studio "
            "(View > Script output > Reload script).",
        )
        return
    try:
        proj = client.call("project.info")
        ok("Project readable",
           f"{proj.get('tempo_bpm')} BPM, "
           f"{proj.get('mixer_track_count')} mixer tracks, "
           f"{proj.get('channel_count')} channels")
    except Exception as e:
        warn(f"Connected, but reading the project failed: {e}")
    try:
        selection = inspector.selected_range()
        ok(
            "Playlist selection getter available",
            f"{selection.raw_start_time} to {selection.raw_end_time}; "
            "raw-only; unit and presence unvalidated; "
            f"safe_for_rendering={selection.safe_for_rendering}",
        )
    except Exception as e:
        fail(
            f"Raw Playlist selection probe failed: {e}",
            "Run postfader-install-bridge, reload the FL bridge, then retry.",
        )


def check_python_deps():
    print("\nPython environment")
    missing = []
    for mod, label in [("mcp", "mcp"), ("numpy", "numpy"), ("scipy", "scipy"),
                       ("soundfile", "soundfile"), ("pyloudnorm", "pyloudnorm")]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(label)
    if missing:
        fail(f"Missing packages: {', '.join(missing)}",
             "Run postfader-install-bridge")
    else:
        ok("All required packages present",
           f"running {sys.executable}")


def main():
    print("FL Studio MCP — setup check")
    print(f"FL Studio user data: {FL_STUDIO_USER_DATA_DIR}")
    if "FL_STUDIO_USER_DATA_DIR" in os.environ:
        print("  selected by FL_STUDIO_USER_DATA_DIR")
    check_fl_studio()
    check_python_deps()
    check_script_installed()
    check_midi_ports()
    check_bridge_live()

    print()
    if problems:
        print(f"{RED}{len(problems)} problem(s) to fix:{RESET}")
        for p in problems:
            print(f"  - {p}")
        return 1
    if skipped_host_checks:
        print(
            f"{YELLOW}No failures detected, but host-only checks were skipped: "
            f"{', '.join(skipped_host_checks)}.{RESET}"
        )
        return 0
    print(f"{GREEN}Everything checks out.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
