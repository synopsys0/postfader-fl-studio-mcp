#!/usr/bin/env python3
"""Cross-platform, evidence-first setup doctor for Postfader."""

from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import __version__
from .bridge_install import (
    BridgeInstallError,
    bridge_source_path,
    expected_bridge_deployment,
    hardware_dir,
    target_path,
)
from .host_config import (
    FL_BRIDGE_MIDI_PORT_ENV,
    HostConfigurationError,
    MidiPortMatchError,
    fl_studio_user_data_selection,
    match_midi_port,
    midi_port_query,
    platform_family,
)


Evidence = dict[str, object]
MidiProbe = Callable[[], Mapping[str, Sequence[object]]]
LiveProbe = Callable[[], Mapping[str, object]]


def _package_version() -> str:
    try:
        return importlib.metadata.version("postfader-fl-studio-mcp")
    except importlib.metadata.PackageNotFoundError:
        return __version__


def _truthy_marker(value: str | None) -> bool:
    marker = (value or "").strip().lower()
    return bool(marker) and marker not in {"0", "false", "no", "none", "off"}


def native_midi_probe_blocked(
    environ: Mapping[str, str] | None = None,
) -> bool:
    environment = os.environ if environ is None else environ
    return _truthy_marker(environment.get("FL_BRIDGE_SANDBOXED"))


def discover_fl_studio_candidates(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Return existing FL Studio installation candidates for this host."""

    family = platform_family(platform_name)
    environment = os.environ if environ is None else environ
    candidates: list[Path] = []
    if family == "windows":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            root = environment.get(variable, "").strip()
            if not root:
                continue
            image_line = Path(root) / "Image-Line"
            candidates.extend(image_line.glob("FL Studio 2026*/FL64.exe"))
            candidates.extend(image_line.glob("FL Studio 2026*/FL.exe"))
    elif family == "macos":
        candidates.extend(Path(value) for value in glob.glob("/Applications/FL Studio*.app"))
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=lambda value: os.fspath(value).casefold()):
        rendered = os.fspath(candidate.resolve())
        folded = rendered.casefold()
        if folded not in seen and candidate.exists():
            seen.add(folded)
            unique.append(rendered)
    return unique


def _native_midi_probe() -> Mapping[str, Sequence[object]]:
    source = (
        "import json,rtmidi; "
        "mi=rtmidi.MidiIn(); mo=rtmidi.MidiOut(); "
        "print(json.dumps({'in':mi.get_ports(),'out':mo.get_ports()})); "
        "mi.close_port(); mo.close_port()"
    )
    completed = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise RuntimeError(
            detail[-1] if detail else "native MIDI probe exited nonzero"
        )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("native MIDI probe returned a non-object")
    return value


def _default_live_probe() -> Mapping[str, object]:
    # The doctor is an explicitly live entry point.  Enable MIDI before the
    # bridge module freezes its transport list, including for macOS's
    # no-argument IAC default and a Windows endpoint supplied via environment.
    os.environ.setdefault("FL_BRIDGE_ENABLE_MIDI", "1")
    from .bridge_client import BridgeClient
    from .readonly_inspector import connection_from_ping

    client = BridgeClient(timeout=3)
    try:
        ping = client.ping()
        connection = connection_from_ping(ping, client.transport)
        return {
            "ping": ping,
            "connection": connection.model_dump(mode="json"),
            "selected_transport": client.transport,
            "attempted_transports": [
                transport.name for transport in client._transports
            ],
        }
    finally:
        client.close()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _failure(result: Evidence, code: str, message: str, action: str) -> None:
    failures = result["failures"]
    assert isinstance(failures, list)
    failures.append({"code": code, "message": message, "action": action})


def _skipped(result: Evidence, code: str, message: str) -> None:
    skipped = result["skipped_checks"]
    assert isinstance(skipped, list)
    skipped.append({"code": code, "message": message})


def _endpoint_action(family: str) -> str:
    if family == "macos":
        return (
            "Open Audio MIDI Setup, show MIDI Studio, enable the selected IAC "
            "bus, then give FL Studio input and output the same Port number."
        )
    return (
        "Configure a virtual MIDI endpoint outside Postfader, set "
        "FL_BRIDGE_MIDI_PORT to its unique exact name, and retry."
    )


def collect_evidence(
    *,
    user_data_dir: str | os.PathLike[str] | None = None,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    fl_candidates: Sequence[str | os.PathLike[str]] | None = None,
    midi_port: str | None = None,
    fl_executable: str | os.PathLike[str] | None = None,
    midi_probe: MidiProbe | None = None,
    live_probe: LiveProbe | None = None,
) -> Evidence:
    """Collect strict setup evidence; injected probes keep tests hermetic."""

    environment = os.environ if environ is None else environ
    family = platform_family(platform_name)
    selection = fl_studio_user_data_selection(
        user_data_dir,
        environ=environment,
        platform_name=platform_name,
    )
    hardware = hardware_dir(os.fspath(selection.path))
    script = target_path(os.fspath(selection.path))
    query = midi_port_query(
        midi_port, environ=environment, platform_name=platform_name
    )
    if fl_executable is not None:
        configured_executable = Path(fl_executable)
        if not configured_executable.is_absolute():
            raise HostConfigurationError(
                "--fl-executable must be an absolute path; received %r"
                % os.fspath(configured_executable)
            )
        candidates = [os.fspath(configured_executable)]
        candidate_source = "explicit"
    elif fl_candidates is not None:
        candidates = [os.fspath(Path(value)) for value in fl_candidates]
        candidate_source = "injected"
    else:
        candidates = discover_fl_studio_candidates(
            platform_name=platform_name, environ=environment
        )
        candidate_source = "standard_discovery"
    installed = (
        bool(candidates)
        if candidate_source == "injected"
        else any(Path(value).exists() for value in candidates)
    )
    result: Evidence = {
        "schema_version": 1,
        "overall": "pending",
        "host": {
            "platform": family,
            "sys_platform": platform_name or sys.platform,
            "architecture": platform.machine() or "unknown",
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "package_version": _package_version(),
        },
        "fl_studio": {
            "installation_candidates": candidates,
            "installed": installed,
            "selection_source": candidate_source,
            "selected_executable": candidates[0] if candidates else None,
        },
        "paths": {
            "user_data_root": os.fspath(selection.path),
            "user_data_source": selection.source,
            "hardware_directory": os.fspath(hardware),
            "bridge_script": os.fspath(script),
        },
        "bridge_deployment": {},
        "midi": {
            "configured_query": query,
            "enumeration_status": "not_started",
        },
        "live": {
            "status": "not_started",
            "attempted_transports": [],
            "selected_transport": None,
        },
        "failures": [],
        "skipped_checks": [],
    }

    if not installed:
        if candidate_source == "explicit":
            message = "The explicitly selected FL Studio executable does not exist."
            action = "Correct --fl-executable to an existing absolute path."
        elif family == "windows":
            message = "FL Studio 2026 executable was not found in standard Image-Line folders."
            action = "Install FL Studio 2026 or pass its absolute executable to the launcher."
        elif family == "macos":
            message = "FL Studio was not found in /Applications."
            action = "Install FL Studio, then rerun the doctor."
        else:
            message = "No FL Studio installation candidate was found on this host."
            action = "Use a supported host and provide evidence for its FL Studio installation."
        _failure(result, "missing_fl_installation", message, action)

    deployment = result["bridge_deployment"]
    assert isinstance(deployment, dict)
    try:
        repository_bytes = bridge_source_path().read_bytes()
        stamped_bytes, stamped_source_hash = expected_bridge_deployment()
        deployment.update(
            {
                "repository_file_sha256": _sha256(repository_bytes),
                "stamped_source_sha256": stamped_source_hash,
                "stamped_file_sha256": _sha256(stamped_bytes),
                "deployed_file_sha256": None,
            }
        )
    except (BridgeInstallError, OSError) as exc:
        deployment["status"] = "source_unavailable"
        _failure(
            result,
            "bridge_source_unavailable",
            "The packaged bridge source could not be verified: %s" % exc,
            "Reinstall the package or restore the packaged bridge source.",
        )
        stamped_bytes = None

    if not hardware.is_dir():
        deployment["status"] = "missing_hardware_directory"
        _failure(
            result,
            "missing_hardware_folder",
            "FL Studio Hardware folder is missing: %s" % hardware,
            "Launch FL Studio once, quit it, and rerun the bridge installer.",
        )
    elif not script.is_file():
        deployment["status"] = "missing_bridge"
        _failure(
            result,
            "missing_bridge",
            "The Postfader bridge is not installed at %s" % script,
            "Run postfader-install-bridge with the same absolute user-data root.",
        )
    elif stamped_bytes is not None:
        try:
            deployed = script.read_bytes()
        except OSError as exc:
            deployment["status"] = "unreadable_bridge"
            _failure(
                result,
                "deployed_bridge_unreadable",
                "The deployed bridge could not be read: %s" % exc,
                "Check file permissions, then rerun the doctor.",
            )
        else:
            deployment["deployed_file_sha256"] = _sha256(deployed)
            if deployed == stamped_bytes:
                deployment["status"] = "current"
            else:
                deployment["status"] = "stale"
                _failure(
                    result,
                    "stale_bridge",
                    "The deployed bridge differs from the bridge packaged with this version.",
                    "Run postfader-install-bridge, then reload the script in FL Studio.",
                )

    midi = result["midi"]
    live = result["live"]
    assert isinstance(midi, dict) and isinstance(live, dict)
    if query is None:
        midi["enumeration_status"] = "blocked_missing_configuration"
        live["status"] = "blocked_missing_midi_configuration"
        _failure(
            result,
            "missing_midi_configuration",
            "No virtual MIDI endpoint is configured for this host.",
            "Set FL_BRIDGE_MIDI_PORT to the unique exact endpoint name; Postfader does not install MIDI drivers.",
        )
        result["overall"] = "fail"
        return result

    if native_midi_probe_blocked(environment):
        midi["enumeration_status"] = "skipped_restricted_host"
        live["status"] = "skipped_restricted_host"
        _skipped(
            result,
            "native_midi_enumeration",
            "FL_BRIDGE_SANDBOXED=1 prevented native MIDI endpoint enumeration.",
        )
        _skipped(
            result,
            "live_bridge_handshake",
            "FL_BRIDGE_SANDBOXED=1 prevented the live FL Studio handshake.",
        )
        result["overall"] = "fail" if result["failures"] else "partial"
        return result

    probe = midi_probe or _native_midi_probe
    try:
        raw_ports = probe()
        inputs = [str(value) for value in raw_ports.get("in", [])]
        outputs = [str(value) for value in raw_ports.get("out", [])]
    except Exception as exc:
        midi["enumeration_status"] = "failed"
        live["status"] = "blocked_midi_probe_failed"
        _failure(
            result,
            "native_midi_probe_failed",
            "Native MIDI endpoint enumeration failed: %s" % exc,
            _endpoint_action(family),
        )
        result["overall"] = "fail"
        return result

    midi.update(
        {
            "enumeration_status": "complete",
            "input_count": len(inputs),
            "output_count": len(outputs),
            "selected_input": None,
            "selected_output": None,
        }
    )
    endpoint_failures = 0
    if not inputs:
        endpoint_failures += 1
        _failure(
            result,
            "zero_midi_endpoints",
            "No MIDI input endpoints are available.",
            _endpoint_action(family),
        )
    else:
        try:
            _index, selected = match_midi_port(query, inputs, direction="input")
            midi["selected_input"] = selected
        except MidiPortMatchError as exc:
            endpoint_failures += 1
            code = (
                "ambiguous_endpoint"
                if exc.reason == "ambiguous"
                else "missing_configured_endpoint"
            )
            _failure(result, code, str(exc), _endpoint_action(family))

    if not outputs:
        endpoint_failures += 1
        _failure(
            result,
            "zero_midi_endpoints",
            "No MIDI output endpoints are available for bridge replies.",
            _endpoint_action(family),
        )
    else:
        try:
            _index, selected = match_midi_port(query, outputs, direction="output")
            midi["selected_output"] = selected
        except MidiPortMatchError as exc:
            endpoint_failures += 1
            code = (
                "ambiguous_endpoint"
                if exc.reason == "ambiguous"
                else "missing_matching_output"
            )
            _failure(result, code, str(exc), _endpoint_action(family))

    if endpoint_failures:
        live["status"] = "blocked_endpoint_selection"
        result["overall"] = "fail"
        return result

    try:
        live_value = dict((live_probe or _default_live_probe)())
    except Exception as exc:
        live["status"] = "unavailable"
        _failure(
            result,
            "live_bridge_unavailable",
            "No live Postfader bridge handshake was available: %s" % exc,
            "Start FL Studio with the bridge attached, then rerun the doctor.",
        )
        result["overall"] = "fail"
        return result

    connection = live_value.get("connection")
    connection = connection if isinstance(connection, dict) else {}
    ping = live_value.get("ping")
    ping = ping if isinstance(ping, dict) else {}
    live.update(
        {
            "status": "connected",
            "attempted_transports": list(live_value.get("attempted_transports") or []),
            "selected_transport": live_value.get("selected_transport"),
            "program_title": connection.get("program_title") or ping.get("program_title"),
            "fl_version": connection.get("fl_app_version") or ping.get("fl_version"),
            "fl_build": connection.get("fl_build"),
            "midi_scripting_api": connection.get("midi_scripting_api_version"),
            "bridge_protocol": connection.get("bridge_protocol_version") or ping.get("protocol"),
            "midi_wire_protocol_version": ping.get("midi_wire_protocol_version"),
            "bridge_mode": connection.get("bridge_mode") or ping.get("bridge_mode"),
            "bridge_source_sha256": connection.get("bridge_source_sha256") or ping.get("bridge_source_sha256"),
            "session_fingerprint": connection.get("session_fingerprint") or ping.get("session_fingerprint"),
            "compatible": connection.get("compatible"),
            "compatibility_reason": connection.get("compatibility_reason"),
            "verified_writes_enabled": bool(connection.get("verified_writes_enabled")),
            "read_only_bridge": bool(connection.get("bridge_read_only_enforced")),
        }
    )
    if connection.get("compatible") is not True:
        _failure(
            result,
            "incompatible_fl_or_protocol",
            "Live compatibility gate failed: %s"
            % (connection.get("compatibility_reason") or "unknown reason"),
            "Use the documented FL Studio, MIDI scripting API, and bridge protocol versions.",
        )
    if connection.get("bridge_provenance") not in {None, "matching"}:
        _failure(
            result,
            "stale_running_bridge",
            "The running bridge hash does not match this Postfader package.",
            "Reinstall the bridge and reload its script inside FL Studio.",
        )
    result["overall"] = "fail" if result["failures"] else "pass"
    return result


def render_human(evidence: Evidence) -> str:
    """Render the same evidence object used by strict JSON mode."""

    host = evidence["host"]
    fl = evidence["fl_studio"]
    paths = evidence["paths"]
    deployment = evidence["bridge_deployment"]
    midi = evidence["midi"]
    live = evidence["live"]
    assert all(isinstance(value, dict) for value in (host, fl, paths, deployment, midi, live))
    lines = [
        "Postfader setup doctor",
        "Host: {platform} / {architecture}; Python {python_version}; package {package_version}".format(**host),
        "FL Studio candidates: %d" % len(fl["installation_candidates"]),
        "User data: %s (%s)" % (paths["user_data_root"], paths["user_data_source"]),
        "Hardware: %s" % paths["hardware_directory"],
        "Bridge: %s (%s)" % (paths["bridge_script"], deployment.get("status", "unknown")),
        "MIDI query: %r" % midi.get("configured_query"),
    ]
    if midi.get("enumeration_status") == "complete":
        input_count = int(midi.get("input_count") or 0)
        output_count = int(midi.get("output_count") or 0)
        if input_count:
            lines.append("MIDI inputs: %d; selected %r" % (input_count, midi.get("selected_input")))
        else:
            lines.append("MIDI inputs: none")
        if output_count:
            lines.append("MIDI outputs: %d; selected %r" % (output_count, midi.get("selected_output")))
        else:
            lines.append("MIDI outputs: none")
    else:
        lines.append("MIDI enumeration: %s" % midi.get("enumeration_status"))
    lines.append(
        "Live bridge: %s; transport %r; mode %r"
        % (live.get("status"), live.get("selected_transport"), live.get("bridge_mode"))
    )
    if live.get("status") == "connected":
        lines.append(
            "Protocols: command %r; MIDI wire %r"
            % (
                live.get("bridge_protocol"),
                live.get("midi_wire_protocol_version"),
            )
        )
    failures = evidence["failures"]
    skipped = evidence["skipped_checks"]
    assert isinstance(failures, list) and isinstance(skipped, list)
    if failures:
        lines.append("Failures:")
        for item in failures:
            lines.append("  FAIL [%s] %s" % (item["code"], item["message"]))
            lines.append("       %s" % item["action"])
    if skipped:
        lines.append("Skipped host checks:")
        for item in skipped:
            lines.append("  SKIP [%s] %s" % (item["code"], item["message"]))
    lines.append("Overall: %s" % str(evidence["overall"]).upper())
    return "\n".join(lines) + "\n"


# Compatibility helpers retained for callers that used the earlier doctor
# module directly. New code should consume collect_evidence instead.
try:
    USER_DATA_SELECTION = fl_studio_user_data_selection()
except HostConfigurationError:
    # CLI main emits one strict configuration-failure object. Importing this
    # module must not turn an invalid environment variable into a traceback.
    USER_DATA_SELECTION = None
FL_STUDIO_USER_DATA_DIR = (
    os.fspath(USER_DATA_SELECTION.path) if USER_DATA_SELECTION is not None else ""
)
HARDWARE_DIR = (
    os.fspath(hardware_dir(FL_STUDIO_USER_DATA_DIR))
    if FL_STUDIO_USER_DATA_DIR
    else ""
)
SCRIPT_PATH = (
    os.fspath(target_path(FL_STUDIO_USER_DATA_DIR))
    if FL_STUDIO_USER_DATA_DIR
    else ""
)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)
HOST_PLATFORM = platform_family()
MIDI_PORT_QUERY = midi_port_query()
problems: list[str] = []
skipped_host_checks: list[str] = []


def ok(message: str, detail: str = "") -> None:
    print("  OK    %s" % message)
    if detail:
        print("        %s" % detail)


def warn(message: str, fix: str = "") -> None:
    print("  WARN  %s" % message)
    if fix:
        print("        %s" % fix)


def fail(message: str, fix: str = "") -> None:
    print("  FAIL  %s" % message)
    if fix:
        print("        %s" % fix)
    problems.append(message)


def check_midi_ports() -> None:
    """Legacy human check backed by the deterministic shared matcher."""

    print("\nMIDI port for FL to attach the script to")
    if MIDI_PORT_QUERY is None:
        fail(
            "No virtual MIDI endpoint is configured for this host.",
            "Set FL_BRIDGE_MIDI_PORT before any native probe.",
        )
        return
    if native_midi_probe_blocked():
        warn("Native MIDI endpoint enumeration skipped in this restricted process.")
        skipped_host_checks.append("native MIDI endpoint enumeration")
        return
    try:
        ports = _native_midi_probe()
        inputs = [str(value) for value in ports.get("in", [])]
        outputs = [str(value) for value in ports.get("out", [])]
    except Exception as exc:
        fail("Native MIDI endpoint enumeration failed: %s" % exc)
        return
    for direction, names in (("input", inputs), ("output", outputs)):
        if not names:
            fail("No MIDI %s endpoints are available." % direction)
            continue
        ok("%d MIDI %s endpoint(s) available" % (len(names), direction))
        try:
            _index, selected = match_midi_port(
                MIDI_PORT_QUERY, names, direction=direction
            )
        except MidiPortMatchError as exc:
            fail(str(exc))
        else:
            ok("Selected MIDI %s endpoint" % direction, selected)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect Postfader host setup evidence.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one strict JSON evidence object with no human preamble.",
    )
    parser.add_argument("--user-data-dir")
    parser.add_argument(
        "--midi-port",
        help="Exact virtual MIDI endpoint name; configures MIDI before live probing.",
    )
    parser.add_argument(
        "--fl-executable",
        help="Absolute path to a custom FL Studio executable or macOS app.",
    )
    return parser.parse_args(argv)


def _configuration_failure_evidence(exc: BaseException) -> Evidence:
    family = platform_family()
    return {
        "schema_version": 1,
        "overall": "fail",
        "host": {
            "platform": family,
            "sys_platform": sys.platform,
            "architecture": platform.machine() or "unknown",
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "package_version": _package_version(),
        },
        "fl_studio": {
            "installation_candidates": [],
            "installed": False,
            "selection_source": "configuration_refused",
            "selected_executable": None,
        },
        "paths": {
            "user_data_root": None,
            "user_data_source": "configuration_refused",
            "hardware_directory": None,
            "bridge_script": None,
        },
        "bridge_deployment": {"status": "not_checked"},
        "midi": {"configured_query": None, "enumeration_status": "not_started"},
        "live": {
            "status": "not_started",
            "attempted_transports": [],
            "selected_transport": None,
        },
        "failures": [
            {
                "code": "invalid_host_configuration",
                "message": str(exc),
                "action": (
                    "Use absolute paths for --user-data-dir, --fl-executable, "
                    "and FL_STUDIO_USER_DATA_DIR, then retry."
                ),
            }
        ],
        "skipped_checks": [],
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.midi_port is not None and not args.midi_port.strip():
            raise HostConfigurationError("--midi-port must not be empty")
        query = midi_port_query(args.midi_port)
        if query is not None:
            os.environ["FL_BRIDGE_ENABLE_MIDI"] = "1"
            if args.midi_port is not None:
                os.environ[FL_BRIDGE_MIDI_PORT_ENV] = query
        evidence = collect_evidence(
            user_data_dir=args.user_data_dir,
            midi_port=args.midi_port,
            fl_executable=args.fl_executable,
        )
    except (HostConfigurationError, OSError) as exc:
        evidence = _configuration_failure_evidence(exc)
    if args.json:
        print(json.dumps(evidence, indent=2, sort_keys=True))
    else:
        print(render_human(evidence), end="")
    return 1 if evidence["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
