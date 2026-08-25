"""Guided, fail-closed first-time setup for PostFader.

This module deliberately does not install or configure a virtual MIDI driver.
It inventories the endpoints the operating system already exposes, accepts
only a name present in both directions, deploys the packaged FL Studio bridge
after explicit confirmation, renders one client configuration, and records the
same evidence as :mod:`fl_studio_mcp.diagnostics`.

The console entry point is wired separately in ``pyproject.toml``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence, cast

from . import bridge_install, client_config, codex_installer, diagnostics
from .host_config import (
    FL_BRIDGE_MIDI_PORT_ENV,
    FL_STUDIO_USER_DATA_ENV,
    HostConfigurationError,
    fl_studio_user_data_selection,
    platform_family,
)


MidiProbe = Callable[[], Mapping[str, Sequence[object]]]
Prompt = Callable[[str], str]
DoctorCollector = Callable[..., dict[str, object]]


class SetupError(RuntimeError):
    """The requested setup was unsafe, ambiguous, or could not complete."""


def _console_prompt(message: str) -> str:
    """Keep prompts off stdout so generated configuration stays pipeable."""

    print(message, end="", file=sys.stderr, flush=True)
    return input()


@dataclass(frozen=True)
class MidiChoice:
    """One endpoint name that can carry commands and bridge replies."""

    name: str
    input_name: str
    output_name: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "input": self.input_name,
            "output": self.output_name,
        }


@dataclass(frozen=True)
class MidiInventory:
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    choices: tuple[MidiChoice, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "bidirectional_choices": [choice.as_dict() for choice in self.choices],
        }


@dataclass(frozen=True)
class SetupRequest:
    user_data_dir: Path
    midi_choice: MidiChoice
    client_format: client_config.OutputFormat
    repository_root: Path
    interpreter: Path
    server_name: str = "fl-studio"
    output: Path | None = None
    dry_run: bool = False
    confirmed: bool = False
    interactive: bool = False
    platform_name: str | None = None


def _exact_names(values: Sequence[object]) -> tuple[str, ...]:
    """Render endpoint names once and reject empty values."""

    names = tuple(str(value) for value in values)
    if any(not name.strip() for name in names):
        raise SetupError("MIDI endpoint enumeration returned an empty name")
    return names


def _unique_by_casefold(names: Sequence[str]) -> dict[str, str]:
    unique: dict[str, str] = {}
    duplicates: set[str] = set()
    for name in names:
        key = name.casefold()
        if key in unique:
            duplicates.add(key)
        else:
            unique[key] = name
    for key in duplicates:
        unique.pop(key, None)
    return unique


def inventory_midi_endpoints(midi_probe: MidiProbe | None = None) -> MidiInventory:
    """Enumerate exact endpoint names and retain only safe two-way choices.

    A duplicate case-insensitive name in either direction is not selectable:
    the runtime matcher would correctly reject it as ambiguous. Names that
    appear in only one direction are reported but are not choices.
    """

    if midi_probe is None and diagnostics.native_midi_probe_blocked():
        raise SetupError(
            "native MIDI enumeration is disabled by FL_BRIDGE_SANDBOXED; "
            "run setup from the local interactive host"
        )
    probe = diagnostics._native_midi_probe if midi_probe is None else midi_probe
    try:
        raw = probe()
    except Exception as exc:
        raise SetupError("could not enumerate local MIDI endpoints: %s" % exc) from exc
    inputs = _exact_names(raw.get("in", ()))
    outputs = _exact_names(raw.get("out", ()))
    input_names = _unique_by_casefold(inputs)
    output_names = _unique_by_casefold(outputs)
    choices = tuple(
        MidiChoice(
            name=input_names[key],
            input_name=input_names[key],
            output_name=output_names[key],
        )
        for key in sorted(input_names.keys() & output_names.keys())
    )
    return MidiInventory(inputs=inputs, outputs=outputs, choices=choices)


def select_midi_choice(query: str, inventory: MidiInventory) -> MidiChoice:
    """Select one bidirectional endpoint by exact, case-insensitive name."""

    normalized = query.strip().casefold()
    matches = [
        choice
        for choice in inventory.choices
        if normalized in {choice.input_name.casefold(), choice.output_name.casefold()}
    ]
    if len(matches) == 1:
        return matches[0]
    available = [choice.name for choice in inventory.choices]
    if not matches:
        raise SetupError(
            "MIDI endpoint %r is not an exact bidirectional choice; available: %s"
            % (query, available or "none")
        )
    raise SetupError(
        "MIDI endpoint %r is ambiguous; available: %s" % (query, available)
    )


def _bridge_plan(user_data_dir: Path) -> dict[str, object]:
    target = bridge_install.target_path(os.fspath(user_data_dir))
    hardware = bridge_install.hardware_dir(os.fspath(user_data_dir))
    expected, digest = bridge_install.expected_bridge_deployment()
    if not hardware.is_dir():
        status = "blocked_missing_hardware_directory"
        action = "blocked"
    elif not target.is_file():
        status = "missing"
        action = "install"
    else:
        try:
            current = target.read_bytes()
        except OSError as exc:
            raise SetupError("could not inspect the deployed bridge: %s" % exc) from exc
        status = "current" if current == expected else "stale"
        action = "none" if status == "current" else "update_with_backup"
    return {
        "target": os.fspath(target),
        "source_sha256": digest,
        "status": status,
        "action": action,
    }


def _python_version(interpreter: Path) -> str:
    if not interpreter.is_absolute():
        raise SetupError("Python interpreter must be an absolute path")
    if not interpreter.is_file():
        raise SetupError("Python interpreter does not exist: %s" % interpreter)
    try:
        completed = subprocess.run(
            [
                os.fspath(interpreter),
                "-c",
                "import sys; print('%d.%d.%d' % sys.version_info[:3])",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError("could not inspect Python interpreter: %s" % exc) from exc
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        detail = (completed.stderr or completed.stdout).strip()
        raise SetupError(
            "Python interpreter check failed: %s" % (detail or "unknown error")
        )
    try:
        major, minor, _patch = (int(part) for part in value.split(".", 2))
    except ValueError as exc:
        raise SetupError(
            "Python interpreter returned an invalid version: %r" % value
        ) from exc
    if not (3, 10) <= (major, minor) < (3, 15):
        raise SetupError(
            "PostFader requires Python 3.10 through 3.14; found %s" % value
        )
    return value


def _next_fl_action(port_name: str, doctor: Mapping[str, object]) -> str:
    live = doctor.get("live")
    if _doctor_ready(doctor):
        return (
            "Restart or connect your AI client, then ask PostFader to inspect "
            "the open FL Studio project in read-only mode."
        )
    if (
        doctor.get("overall") == "pass"
        and isinstance(live, Mapping)
        and live.get("status") == "connected"
    ):
        return (
            "Restart FL Studio without write mode enabled, reload Universal "
            "Bridge, then rerun setup until the doctor reports "
            "bridge_mode='read_only' and verified_writes_enabled=false."
        )
    failures = doctor.get("failures")
    if isinstance(failures, Sequence) and failures:
        first = failures[0]
        if isinstance(first, Mapping) and first.get("action"):
            return str(first["action"])
    return (
        "In FL Studio, open Options > MIDI settings; under Input select %r, "
        "enable it, and choose Controller type 'Universal Bridge', then under "
        "Output select %r with the same Port number and reload the script."
        % (port_name, port_name)
    )


def _doctor_ready(doctor: Mapping[str, object]) -> bool:
    """Require positive evidence that the first connection is read-only."""

    live = doctor.get("live")
    return bool(
        doctor.get("overall") == "pass"
        and isinstance(live, Mapping)
        and live.get("status") == "connected"
        and live.get("bridge_mode") == "read_only"
        and live.get("verified_writes_enabled") is False
        and live.get("read_only_bridge") is True
    )


def _configuration_state(output: Path | None, rendered: str) -> str:
    """Classify create-only output while allowing an identical setup resume."""

    if output is None:
        return "stdout"
    if not os.path.lexists(os.fspath(output)):
        return "planned_create"
    if output.is_symlink() or not output.is_file():
        raise SetupError(
            "refusing client configuration path that is not a regular file: %s"
            % output
        )
    try:
        existing = output.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SetupError(
            "could not inspect existing client configuration %s: %s"
            % (output, exc)
        ) from exc
    if existing != rendered:
        raise SetupError(
            "refusing to overwrite different client configuration: %s" % output
        )
    return "current"


def _windows_running_fl_studio_process_ids() -> tuple[int, ...]:
    """Return matching Windows process IDs before a bridge file mutation."""

    try:
        completed = subprocess.run(
            ["tasklist.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError(
            "could not verify whether FL Studio is running: %s" % exc
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SetupError(
            "could not verify whether FL Studio is running: %s"
            % (detail or "tasklist exited nonzero")
        )
    process_names = {"fl.exe", "fl64.exe", "fl studio.exe"}
    matches: list[int] = []
    try:
        rows = csv.reader(completed.stdout.splitlines())
        for row in rows:
            if len(row) < 2 or row[0].strip().casefold() not in process_names:
                continue
            matches.append(int(row[1].replace(",", "").strip()))
    except (csv.Error, ValueError) as exc:
        raise SetupError(
            "could not parse the Windows process list safely: %s" % exc
        ) from exc
    return tuple(sorted(set(matches)))


@contextmanager
def _configured_environment(user_data_dir: Path, midi_port: str):
    updates = {
        "FL_BRIDGE_ENABLE_MIDI": "1",
        "FL_BRIDGE_ENABLE_WRITES": "0",
        FL_BRIDGE_MIDI_PORT_ENV: midi_port,
        FL_STUDIO_USER_DATA_ENV: os.fspath(user_data_dir),
    }
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def execute_setup(
    request: SetupRequest,
    inventory: MidiInventory,
    *,
    prompt: Prompt = _console_prompt,
    deployer: Callable[[str | None], dict] | None = None,
    doctor_collector: DoctorCollector | None = None,
) -> dict[str, object]:
    """Execute an already-resolved setup request and return serializable facts."""

    selected = select_midi_choice(request.midi_choice.name, inventory)
    if selected != request.midi_choice:
        raise SetupError(
            "selected MIDI endpoint does not match the enumerated bidirectional pair"
        )
    python_version = _python_version(request.interpreter)
    facts = client_config.configuration_facts(
        repository_root=request.repository_root,
        interpreter=request.interpreter,
        user_data_dir=request.user_data_dir,
        transport="midi",
        midi_port=request.midi_choice.name,
        server_name=request.server_name,
        platform_name=request.platform_name,
    )
    rendered = client_config.render_configuration(facts, request.client_format)
    configuration_state = _configuration_state(request.output, rendered)
    bridge = _bridge_plan(request.user_data_dir)
    if bridge["action"] == "blocked":
        raise SetupError(
            "FL Studio's Hardware folder was not found at %s; launch FL Studio "
            "once so it creates the folder, then rerun setup" %
            bridge_install.hardware_dir(os.fspath(request.user_data_dir))
        )

    needs_bridge_change = bridge["action"] != "none"
    if (
        needs_bridge_change
        and not request.dry_run
        and platform_family(request.platform_name) == "windows"
    ):
        running_fl_studio = _windows_running_fl_studio_process_ids()
        if running_fl_studio:
            raise SetupError(
                "FL Studio is running (PID(s): %s). Close FL Studio before "
                "installing or updating Universal Bridge, then rerun setup."
                % ", ".join(str(value) for value in running_fl_studio)
            )
    approved = request.confirmed
    if needs_bridge_change and not request.dry_run and not approved:
        if not request.interactive:
            raise SetupError(
                "bridge %s requires explicit confirmation; rerun with --yes"
                % bridge["action"]
            )
        answer = prompt(
            "%s the packaged bridge at %s? [y/N] "
            % (str(bridge["action"]).split("_", 1)[0].capitalize(), bridge["target"])
        )
        approved = answer.strip().casefold() in {"y", "yes"}
        if not approved:
            raise SetupError(
                "bridge installation was not confirmed; no files were changed"
            )

    deployment: dict[str, object]
    if request.dry_run:
        deployment = {"performed": False, "planned_action": bridge["action"]}
    elif needs_bridge_change:
        outcome = (deployer or bridge_install.deploy)(os.fspath(request.user_data_dir))
        deployment = {
            "performed": True,
            "changed": bool(outcome["changed"]),
            "target": os.fspath(outcome["target"]),
            "source_sha256": outcome["digest"],
            "backup": os.fspath(outcome["backup"]) if outcome["backup"] else None,
        }
    else:
        deployment = {"performed": False, "planned_action": "none"}

    config_written = None
    if configuration_state == "planned_create" and not request.dry_run:
        assert request.output is not None
        try:
            config_written = client_config.write_new_configuration(
                request.output, rendered
            )
        except FileExistsError as exc:
            raise SetupError(
                "refusing to overwrite client configuration: %s" % request.output
            ) from exc
        configuration_state = "created"

    if request.interactive and not request.dry_run:
        prompt(
            "Now open FL Studio > Options > MIDI settings. Enable %r under "
            "Input and Output with the same Port number, choose 'Universal "
            "Bridge' for Input, then reload the script. Press Enter to run "
            "the connection doctor: " % request.midi_choice.name
        )

    if request.dry_run:
        doctor = {
            "overall": "not_run",
            "live": {"status": "skipped_dry_run"},
            "failures": [],
            "skipped_checks": [
                {
                    "code": "live_bridge_handshake",
                    "message": (
                        "Dry-run does not open MIDI or contact FL Studio."
                    ),
                }
            ],
        }
    else:
        collector = (
            diagnostics.collect_evidence
            if doctor_collector is None
            else doctor_collector
        )
        with _configured_environment(request.user_data_dir, request.midi_choice.name):
            doctor = collector(
                user_data_dir=request.user_data_dir,
                platform_name=request.platform_name,
                environ=dict(os.environ),
                midi_port=request.midi_choice.name,
                midi_probe=lambda: {
                    "in": inventory.inputs,
                    "out": inventory.outputs,
                },
            )
    if request.dry_run and needs_bridge_change:
        next_action = (
            "Rerun postfader setup without --dry-run and confirm the bridge %s; "
            "in non-interactive mode add --yes." % bridge["action"]
        )
    else:
        next_action = _next_fl_action(request.midi_choice.name, doctor)
    ready = _doctor_ready(doctor)
    status = "planned" if request.dry_run else "ready" if ready else "needs_action"
    bridge_status_after = (
        str(bridge["status"])
        if request.dry_run
        else "current"
        if needs_bridge_change
        else str(bridge["status"])
    )
    return {
        "schema_version": 1,
        "status": status,
        "mode": "dry-run" if request.dry_run else "apply",
        "platform": platform_family(request.platform_name),
        "python": {
            "executable": os.fspath(request.interpreter),
            "version": python_version,
        },
        "user_data": os.fspath(request.user_data_dir),
        "midi": {
            **inventory.as_dict(),
            "selected": request.midi_choice.as_dict(),
            "driver_managed_by_postfader": False,
        },
        "bridge": {
            **bridge,
            "status_before": bridge["status"],
            "status": bridge_status_after,
            "deployment": deployment,
        },
        "client_configuration": {
            "format": request.client_format,
            "content": rendered,
            "output": os.fspath(request.output) if request.output else None,
            "state": configuration_state,
            "written": os.fspath(config_written) if config_written else None,
        },
        "doctor_timing": "skipped_dry_run" if request.dry_run else "post-change",
        "doctor": doctor,
        "next_fl_action": next_action,
    }


def _prompt_path(prompt: Prompt, detected: Path) -> Path:
    value = prompt("FL Studio user-data folder [%s]: " % detected).strip()
    selected = detected if not value else Path(value)
    if not selected.is_absolute():
        raise SetupError("FL Studio user-data folder must be an absolute path")
    return selected


def _prompt_choice(prompt: Prompt, inventory: MidiInventory) -> MidiChoice:
    if not inventory.choices:
        raise SetupError(
            "no exact bidirectional MIDI endpoint is available; create or enable "
            "one outside PostFader, then rerun setup"
        )
    print("Exact bidirectional MIDI choices:", file=sys.stderr)
    for index, choice in enumerate(inventory.choices, start=1):
        print("  %d. %s" % (index, choice.name), file=sys.stderr)
    raw = prompt("Choose a MIDI endpoint by number or exact name: ").strip()
    try:
        index = int(raw)
    except ValueError:
        return select_midi_choice(raw, inventory)
    if index < 1 or index > len(inventory.choices):
        raise SetupError("MIDI choice number is out of range")
    return inventory.choices[index - 1]


def _prompt_client_format(prompt: Prompt) -> client_config.OutputFormat:
    formats: tuple[client_config.OutputFormat, ...] = (
        "codex-toml",
        "codex-command",
        "claude-json",
    )
    labels = {
        "codex-toml": "codex-toml",
        "codex-command": "codex-command (PowerShell)",
        "claude-json": "claude-json",
    }
    print("Client configuration formats:", file=sys.stderr)
    for index, value in enumerate(formats, start=1):
        print("  %d. %s" % (index, labels[value]), file=sys.stderr)
    raw = prompt("Choose a client format [1]: ").strip() or "1"
    if raw in formats:
        return cast(client_config.OutputFormat, raw)
    try:
        index = int(raw)
    except ValueError as exc:
        raise SetupError("unknown client format %r" % raw) from exc
    if index < 1 or index > len(formats):
        raise SetupError("client format choice is out of range")
    return formats[index - 1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="postfader setup",
        description="Safely prepare PostFader for a first FL Studio session.",
    )
    parser.add_argument("--user-data-dir", metavar="PATH")
    parser.add_argument("--midi-port", help="exact bidirectional endpoint name")
    parser.add_argument(
        "--client",
        choices=("codex-toml", "codex-command", "claude-json"),
        help="client configuration format",
    )
    parser.add_argument("--repository-root", metavar="PATH")
    parser.add_argument("--interpreter", metavar="PATH")
    parser.add_argument("--server-name", default="fl-studio")
    parser.add_argument(
        "--output",
        metavar="NEW_FILE",
        help="create this new config file instead of printing config to stdout",
    )
    parser.add_argument(
        "--register-codex",
        action="store_true",
        help="register the resolved local server through the Codex CLI",
    )
    parser.add_argument(
        "--yes-register-codex",
        action="store_true",
        help="separately confirm Codex MCP registration without a prompt",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--interactive", action="store_true")
    mode.add_argument("--non-interactive", action="store_true")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm installation/update of the packaged bridge",
    )
    parser.add_argument("--dry-run", action="store_true", help="plan without writing")
    parser.add_argument("--json", action="store_true", help="emit one JSON result")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    prompt: Prompt = _console_prompt,
    midi_probe: MidiProbe | None = None,
    deployer: Callable[[str | None], dict] | None = None,
    doctor_collector: DoctorCollector | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    interactive = args.interactive or (
        not args.non_interactive and not args.json and sys.stdin.isatty()
    )
    codex_registration: Mapping[str, object] | None = None
    try:
        if args.yes_register_codex and not args.register_codex:
            raise SetupError("--yes-register-codex requires --register-codex")
        if args.register_codex and args.client == "claude-json":
            raise SetupError(
                "--register-codex requires a Codex client format, not claude-json"
            )
        if (
            args.register_codex
            and not args.dry_run
            and not interactive
            and not args.yes_register_codex
        ):
            raise SetupError(
                "Codex registration requires separate confirmation; add "
                "--yes-register-codex"
            )
        family = platform_family()
        if family not in {"windows", "macos"}:
            raise SetupError(
                "first-time setup supports Windows and macOS; detected %s" % family
            )
        selection = fl_studio_user_data_selection(args.user_data_dir)
        user_data = (
            _prompt_path(prompt, selection.path) if interactive else selection.path
        )
        inventory = inventory_midi_endpoints(midi_probe)
        if args.midi_port:
            midi_choice = select_midi_choice(args.midi_port, inventory)
        elif interactive:
            midi_choice = _prompt_choice(prompt, inventory)
        else:
            raise SetupError("--midi-port is required in non-interactive mode")
        if args.client:
            output_format = args.client
        elif args.register_codex:
            output_format = "codex-toml"
        elif interactive:
            output_format = _prompt_client_format(prompt)
        else:
            raise SetupError("--client is required in non-interactive mode")

        package_root = Path(__file__).resolve().parents[1]
        repository_root = (
            Path(args.repository_root) if args.repository_root else package_root
        )
        interpreter = (
            Path(args.interpreter) if args.interpreter else Path(sys.executable)
        )
        output = Path(args.output) if args.output else None
        for label, path in (
            ("repository root", repository_root),
            ("Python interpreter", interpreter),
            ("output", output),
        ):
            if path is not None and not path.is_absolute():
                raise SetupError("%s must be an absolute path" % label)
        if not repository_root.is_dir():
            raise SetupError(
                "repository root does not exist or is not a directory: %s"
                % repository_root
            )
        request = SetupRequest(
            user_data_dir=user_data,
            midi_choice=midi_choice,
            client_format=output_format,
            repository_root=repository_root,
            interpreter=interpreter,
            server_name=args.server_name,
            output=output,
            dry_run=args.dry_run,
            confirmed=args.yes,
            interactive=interactive,
        )
        result = execute_setup(
            request,
            inventory,
            prompt=prompt,
            deployer=deployer,
            doctor_collector=doctor_collector,
        )
        if args.register_codex:
            facts = client_config.configuration_facts(
                repository_root=request.repository_root,
                interpreter=request.interpreter,
                user_data_dir=request.user_data_dir,
                transport="midi",
                midi_port=request.midi_choice.name,
                server_name=request.server_name,
                platform_name=request.platform_name,
            )
            register_confirmed = args.yes_register_codex
            if not request.dry_run and not register_confirmed:
                print(
                    "Codex registration plan:\n%s"
                    % codex_installer.render_registration_plan(facts),
                    file=sys.stderr,
                )
                answer = prompt(
                    "Register this resolved read-only server in Codex? [y/N] "
                )
                register_confirmed = answer.strip().casefold() in {"y", "yes"}
            if register_confirmed or request.dry_run:
                codex_registration = codex_installer.register_codex(
                    facts, apply=not request.dry_run
                )
            else:
                codex_registration = {
                    "status": "skipped",
                    "changed": False,
                    "server_name": request.server_name,
                }
            result["codex_registration"] = codex_registration
    except (
        SetupError,
        HostConfigurationError,
        bridge_install.BridgeInstallError,
        codex_installer.CodexRegistrationError,
        EOFError,
        OSError,
    ) as exc:
        if args.json:
            print(
                json.dumps(
                    {"schema_version": 1, "status": "error", "error": str(exc)}
                )
            )
        else:
            print("error: %s" % exc, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        configuration = result["client_configuration"]
        assert isinstance(configuration, dict)
        if request.client_format == "codex-command":
            print(
                "Run the generated Codex command in PowerShell.",
                file=sys.stderr,
            )
        if request.output is None:
            content = configuration["content"]
            if (
                codex_registration is not None
                and codex_registration["status"] == "manual"
            ):
                content = codex_registration["manual_toml"]
            print(str(content), end="")
        elif configuration["state"] == "current":
            print(
                "Client configuration already current: %s" % request.output,
                file=sys.stderr,
            )
        elif request.dry_run:
            print(
                "Would create client configuration: %s" % request.output,
                file=sys.stderr,
            )
        else:
            print("Created client configuration: %s" % request.output, file=sys.stderr)
        if args.register_codex:
            assert codex_registration is not None
            registration_status = str(codex_registration["status"])
            if registration_status == "planned":
                print("Would register PostFader in Codex.", file=sys.stderr)
            elif registration_status == "added":
                print("Registered PostFader in Codex.", file=sys.stderr)
            elif registration_status == "current":
                print("Codex registration is already current.", file=sys.stderr)
            elif registration_status == "conflict":
                print(
                    "Codex registration was not changed: an existing server named "
                    "%r has different settings." % request.server_name,
                    file=sys.stderr,
                )
            elif registration_status == "manual":
                if request.output is not None:
                    print(str(codex_registration["manual_toml"]), end="")
                print(
                    "Codex CLI was not found. Use the generated Codex TOML manually.",
                    file=sys.stderr,
                )
            else:
                print("Codex registration was skipped.", file=sys.stderr)
        bridge = result["bridge"]
        doctor = result["doctor"]
        assert isinstance(bridge, dict) and isinstance(doctor, dict)
        print(
            "Bridge: %s; doctor: %s"
            % (bridge["status"], doctor.get("overall", "unknown")),
            file=sys.stderr,
        )
        deployment = bridge.get("deployment")
        if isinstance(deployment, Mapping) and deployment.get("backup"):
            print("Bridge backup: %s" % deployment["backup"], file=sys.stderr)
        print("Next FL action: %s" % result["next_fl_action"], file=sys.stderr)
    if args.register_codex and codex_registration is not None:
        if codex_registration["status"] in {"manual", "conflict", "skipped"}:
            return 2
    if request.dry_run or result["status"] == "ready":
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
