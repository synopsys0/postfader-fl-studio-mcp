"""Reusable, evidence-producing live acceptance harness primitives.

The public MCP registry is the authority for coverage.  The harness derives
read, session-control, directly restorable write, specialized workflow, and
ephemeral mutation surfaces from registered schemas and annotations at runtime.
It rejects scenario drift without pretending that process-registry reads or
non-restorable creative workflows belong in the one-step live fixture.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import multiprocessing
import os
import platform
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from jsonschema import Draft202012Validator

from . import __version__
from .host_config import midi_port_query, platform_family


ToolCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]
EvidenceCheckpoint = Callable[[Mapping[str, Any]], None]
ReadWorkerHandler = Callable[[str, dict[str, Any]], Any]


class AcceptanceConfigurationError(RuntimeError):
    """The supervised scenario is incomplete or unsafe to execute."""


class ReadToolInvocationError(RuntimeError):
    """One isolated read worker completed unsuccessfully."""

    def __init__(self, message: str, *, cleanup: Mapping[str, Any]):
        super().__init__(message)
        self.cleanup = dict(cleanup)


class ReadToolTimeoutError(TimeoutError):
    """One isolated read worker exceeded its parent-owned deadline."""

    def __init__(
        self,
        message: str,
        *,
        timeout_seconds: float,
        cleanup: Mapping[str, Any],
    ):
        super().__init__(message)
        self.timeout_seconds = timeout_seconds
        self.cleanup = dict(cleanup)


@dataclass(frozen=True)
class ReadToolInvocationResult:
    value: Any
    cleanup: Mapping[str, Any]


@dataclass(frozen=True)
class ToolSurface:
    all_tools: tuple[str, ...]
    read_tools: tuple[str, ...]
    workflow_read_tools: tuple[str, ...]
    session_control_tools: tuple[str, ...]
    persistent_write_tools: tuple[str, ...]
    specialized_write_tools: tuple[str, ...]
    ephemeral_tools: tuple[str, ...]
    input_schemas: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class PreparedWriteOperation:
    tool: str
    before: Any
    mutation_arguments: dict[str, Any]
    restore_actions: tuple[tuple[str, dict[str, Any]], ...]
    verify_specs: tuple[Any, ...]
    verify_expected: tuple[Any, ...]


async def authoritative_tool_surface() -> ToolSurface:
    from . import mcp_server

    tools = await mcp_server.mcp.list_tools()
    all_names = tuple(sorted(tool.name for tool in tools))
    all_reads = tuple(
        sorted(
            tool.name
            for tool in tools
            if tool.annotations and tool.annotations.read_only_hint is True
        )
    )
    # A read that needs an opaque process-local registry ID cannot run in the
    # isolated one-tool acceptance worker: that worker intentionally starts
    # with fresh process state. Its creating workflow has dedicated tests.
    workflow_reads = tuple(
        name
        for name in all_reads
        if set(
            next(tool for tool in tools if tool.name == name).input_schema.get(
                "required", ()
            )
        )
        & {"watch_id", "plan_id"}
    )
    reads = tuple(name for name in all_reads if name not in set(workflow_reads))

    all_writes = tuple(
        sorted(
            tool.name
            for tool in tools
            if tool.annotations
            and tool.annotations.read_only_hint is False
            and tool.annotations.destructive_hint is True
            and tool.annotations.idempotent_hint is False
        )
    )
    # The reversible live fixture covers only direct writes that expose both
    # the bridge-session guard and an independent before-state/digest guard.
    # Batch, plan, local-file, Piano Roll, marker, and automation workflows
    # have dedicated acceptance boundaries because a generic inverse cannot
    # be synthesized safely for them.
    writes = tuple(
        name
        for name in all_writes
        if (
            "session_fingerprint"
            in next(tool for tool in tools if tool.name == name).input_schema.get(
                "properties", {}
            )
            and (
                "expected_before"
                in next(tool for tool in tools if tool.name == name).input_schema.get(
                    "properties", {}
                )
                or "expected_digest"
                in next(tool for tool in tools if tool.name == name).input_schema.get(
                    "properties", {}
                )
            )
        )
    )
    specialized_writes = tuple(name for name in all_writes if name not in set(writes))
    session_controls = tuple(
        sorted(
            tool.name
            for tool in tools
            if tool.annotations
            and tool.annotations.read_only_hint is False
            and tool.annotations.destructive_hint is True
            and tool.annotations.idempotent_hint is True
        )
    )
    ephemeral = tuple(
        sorted(
            tool.name
            for tool in tools
            if tool.annotations
            and tool.annotations.read_only_hint is False
            and tool.annotations.destructive_hint is False
        )
    )
    classified = (
        set(reads)
        | set(workflow_reads)
        | set(session_controls)
        | set(writes)
        | set(specialized_writes)
        | set(ephemeral)
    )
    if classified != set(all_names):
        raise AcceptanceConfigurationError(
            "public MCP tools have missing or contradictory mutability annotations: %s"
            % sorted(set(all_names) - classified)
        )
    return ToolSurface(
        all_tools=all_names,
        read_tools=reads,
        workflow_read_tools=workflow_reads,
        session_control_tools=session_controls,
        persistent_write_tools=writes,
        specialized_write_tools=specialized_writes,
        ephemeral_tools=ephemeral,
        input_schemas={tool.name: tool.input_schema for tool in tools},
    )


async def _default_caller(name: str, arguments: dict[str, Any]) -> Any:
    from . import mcp_server

    return await mcp_server.mcp.call_tool(name, arguments)


def _live_read_worker_handler(name: str, arguments: dict[str, Any]) -> Any:
    """Run one MCP read to completion inside its dedicated worker process."""

    return asyncio.run(_default_caller(name, arguments))


def _read_worker_entry(
    connection: Connection,
    name: str,
    arguments: dict[str, Any],
    handler: ReadWorkerHandler,
) -> None:
    """Return one serialisable result after process-local client cleanup."""

    outcome: dict[str, Any]
    try:
        result = handler(name, arguments)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        outcome = {"status": "passed", "value": tool_payload(result)}
    except BaseException as exc:
        outcome = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }

    try:
        from .bridge_client import close_client

        client_existed = close_client()
        outcome["client_cleanup"] = {
            "status": "closed" if client_existed else "not_created"
        }
    except BaseException as exc:
        outcome["client_cleanup"] = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        if outcome["status"] == "passed":
            outcome.update(
                {
                    "status": "failed",
                    "error_type": "BridgeClientCleanupError",
                    "error": "BridgeClient cleanup failed: %s" % exc,
                }
            )

    try:
        connection.send(outcome)
    finally:
        connection.close()


class IsolatedReadToolSupervisor:
    """Own one spawned read worker at a time and always reap it before return."""

    def __init__(
        self,
        *,
        handler: ReadWorkerHandler = _live_read_worker_handler,
        context: multiprocessing.context.BaseContext | None = None,
    ):
        self._handler = handler
        self._context = context or multiprocessing.get_context("spawn")
        self._active: multiprocessing.Process | None = None

    @property
    def has_active_worker(self) -> bool:
        return self._active is not None

    @staticmethod
    def _stop_worker(process: multiprocessing.Process) -> dict[str, Any]:
        termination_requested = False
        kill_requested = False
        if process.is_alive():
            termination_requested = True
            process.terminate()
            process.join(5.0)
        if process.is_alive():
            kill_requested = True
            process.kill()
            process.join(5.0)
        if process.is_alive():
            raise RuntimeError(
                "isolated read worker did not exit after terminate and kill"
            )
        process.join()
        return {
            "worker_pid": process.pid,
            "worker_exitcode": process.exitcode,
            "worker_reaped": process.exitcode is not None,
            "termination_requested": termination_requested,
            "kill_requested": kill_requested,
            "resource_release": "worker_exit_released_process_resources",
        }

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
    ) -> ReadToolInvocationResult:
        if self._active is not None:
            raise RuntimeError("an older isolated read worker is still active")
        receive, send = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_read_worker_entry,
            args=(send, name, dict(arguments), self._handler),
            name="postfader-read-%s" % name,
        )
        started = time.monotonic()
        message: Any = None
        timed_out = False
        cleanup: dict[str, Any] = {}
        worker_stopped = False
        try:
            process.start()
            self._active = process
            send.close()
            while True:
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    timed_out = True
                    break
                if receive.poll(min(remaining, 0.05)):
                    try:
                        message = receive.recv()
                    except EOFError:
                        message = None
                    break
                if not process.is_alive():
                    break

            if message is not None and not timed_out:
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining > 0:
                    process.join(remaining)
                if remaining <= 0 or process.is_alive():
                    timed_out = True
            elif not timed_out and not process.is_alive():
                process.join()
                if receive.poll():
                    try:
                        message = receive.recv()
                    except EOFError:
                        message = None
        finally:
            try:
                if process.pid is not None:
                    cleanup = self._stop_worker(process)
                    worker_stopped = True
            finally:
                receive.close()
                try:
                    send.close()
                except OSError:
                    pass
                if process.pid is None:
                    worker_stopped = True
                elif process.exitcode is not None:
                    process.close()
                    worker_stopped = True
                self._active = None if worker_stopped else process

        if timed_out:
            cleanup["client_cleanup"] = {
                "status": "process_terminated",
                "detail": "worker exit released any process-local bridge endpoint",
            }
            raise ReadToolTimeoutError(
                "read tool %r exceeded its %.3f second deadline"
                % (name, timeout_seconds),
                timeout_seconds=timeout_seconds,
                cleanup=cleanup,
            )
        if not isinstance(message, dict):
            raise ReadToolInvocationError(
                "read worker exited without a structured result",
                cleanup=cleanup,
            )
        cleanup["client_cleanup"] = message.get(
            "client_cleanup", {"status": "not_reported"}
        )
        if message.get("status") != "passed":
            detail = message.get("error") or "read worker failed"
            error_type = message.get("error_type")
            if error_type:
                detail = "%s: %s" % (error_type, detail)
            raise ReadToolInvocationError(detail, cleanup=cleanup)
        return ReadToolInvocationResult(message.get("value"), cleanup)

    def close(self) -> None:
        """Terminate and reap an interrupted active worker, if any."""

        process = self._active
        if process is None:
            return
        worker_stopped = False
        try:
            self._stop_worker(process)
            worker_stopped = True
        finally:
            if process.exitcode is not None:
                process.close()
                worker_stopped = True
            self._active = None if worker_stopped else process


def tool_payload(result: Any) -> Any:
    if isinstance(result, (dict, list, str, int, float, bool)) or result is None:
        return result
    if getattr(result, "is_error", False):
        messages = [
            getattr(block, "text", "")
            for block in getattr(result, "content", [])
            if getattr(block, "type", None) == "text"
        ]
        raise RuntimeError(
            "; ".join(value for value in messages if value) or "tool failed"
        )
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return (
            structured.get("result", structured)
            if isinstance(structured, dict)
            else structured
        )
    for block in getattr(result, "content", []):
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    raise RuntimeError("tool returned no structured evidence")


def read_acceptance_arguments(
    *,
    mixer_track_index: int,
    plugin_track_index: int,
    plugin_slot_index: int,
    pattern_number: int,
    channel_index: int,
    fixture_root: str | os.PathLike[str],
) -> dict[str, dict[str, Any]]:
    """Build bounded large-read arguments for every current read tool."""

    fixtures = Path(fixture_root)
    reference = fixtures / "reference_mix.wav"
    candidate = fixtures / "candidate_delayed_minus6db.wav"
    vocal = fixtures / "boundary_impulses.wav"
    return {
        "fl_get_capabilities": {},
        "fl_get_project_summary": {},
        "fl_get_transport_state": {},
        "fl_get_selected_range": {},
        "fl_list_mixer_tracks": {
            "only_used": False,
            "include_peaks": False,
            "max_tracks": None,
        },
        "fl_inspect_mixer_track": {"track_index": mixer_track_index},
        "plugins_scan_loaded_plugins": {
            "only_used": False,
            "include_channel_generators": True,
        },
        "plugins_inspect_parameter_map": {
            "track_index": plugin_track_index,
            "slot_index": plugin_slot_index,
            "limit": 128,
            "offset": 0,
        },
        "plugins_scan_parameters": {
            "track_index": plugin_track_index,
            "slot_index": plugin_slot_index,
            "start": 0,
            "max_indices": 8192,
            "max_results": 2048,
        },
        "copilot_capture_readonly_inspection": {
            "only_used": False,
            "parameter_limit": 64,
            "max_plugins": 64,
        },
        "fl_list_channels": {},
        "fl_list_patterns": {},
        "fl_find_empty_pattern": {"start_pattern_number": pattern_number},
        "fl_list_playlist_tracks": {},
        "fl_get_project_history": {},
        "fl_get_plugin_preset_count": {
            "target": {
                "kind": "mixer_effect",
                "track_index": plugin_track_index,
                "slot_index": plugin_slot_index,
            }
        },
        "fl_get_step_sequence": {
            "pattern_number": pattern_number,
            "channel_index": channel_index,
        },
        "audio_analyze_file": {"path": os.fspath(reference), "max_seconds": 30.0},
        "audio_compare_files": {
            "reference_path": os.fspath(reference),
            "candidate_path": os.fspath(candidate),
            "max_seconds": 30.0,
        },
        "audio_analyze_masking": {
            "vocal_path": os.fspath(vocal),
            "instrument_path": os.fspath(reference),
            "max_seconds": 30.0,
        },
        "audio_find_recent_bounces": {"limit": 200},
        "mix_doctor": {
            "candidate_path": os.fspath(candidate),
            "reference_path": os.fspath(reference),
            "vocal_path": os.fspath(vocal),
            "instrumental_path": os.fspath(reference),
            "max_seconds": 30.0,
        },
        "mix_reference_recommendations": {
            "reference_path": os.fspath(reference),
            "candidate_path": os.fspath(candidate),
            "max_seconds": 30.0,
        },
        "mix_masking_recommendations": {
            "vocal_path": os.fspath(vocal),
            "instrumental_path": os.fspath(reference),
            "max_seconds": 30.0,
        },
        "mix_list_plugin_profiles": {},
        "mix_inspect_plugin_compatibility": {"only_used": False},
        "mix_resolve_processing_intent": {
            "intent": "reduce_mud",
            "track_index": mixer_track_index,
            "strength": 0.5,
        },
        "mix_finish_assessment": {
            "candidate_path": os.fspath(candidate),
            "reference_path": os.fspath(reference),
            "vocal_path": os.fspath(vocal),
            "instrumental_path": os.fspath(reference),
            "max_seconds": 30.0,
        },
        "postfader_validate_run": {
            "request": {
                "brief": "Generate a bounded read-only melody proposal.",
                "scope": {
                    "kind": "whole_project",
                    "description": "Proposal only; do not change the project.",
                },
                "allowed_changes": ["composition"],
                "completion_target": "One structured melody option.",
                "interaction_policy": "plan_only",
                "authorized_to_modify": False,
            },
            "plan": {
                "plan_id": "acceptance-read-plan",
                "operations": [
                    {
                        "operation_id": "acceptance-melody",
                        "operation": "generate_melody",
                        "bars": 1,
                        "seed": 20,
                    }
                ],
            },
        },
        "postfader_get_run": {"run_id": "0" * 32},
        "compose_chord_progression": {
            "progression": ["I", "vi", "IV", "V7"],
        },
        "compose_melody": {"bars": 4, "seed": 20},
        "compose_bassline": {
            "progression": ["I", "vi", "IV", "V"],
            "seed": 20,
        },
        "compose_drums": {"style": "house", "bars": 4, "seed": 20},
        "audio_estimate_tempo_and_key": {
            "path": os.fspath(reference),
            "max_seconds": 30.0,
        },
        "audio_transcribe_melody": {
            "path": os.fspath(vocal),
            "tempo_bpm": 120.0,
            "max_seconds": 30.0,
        },
    }


def _validate_read_coverage(
    surface: ToolSurface, arguments: Mapping[str, Mapping[str, Any]]
) -> None:
    expected = set(surface.read_tools)
    actual = set(arguments)
    if actual != expected:
        raise AcceptanceConfigurationError(
            "read harness arguments do not match the authoritative read surface; "
            "missing=%s extra=%s"
            % (sorted(expected - actual), sorted(actual - expected))
        )
    large_read_requirements = {
        "fl_list_mixer_tracks": arguments["fl_list_mixer_tracks"].get("only_used")
        is False
        and arguments["fl_list_mixer_tracks"].get("max_tracks") is None,
        "plugins_inspect_parameter_map": arguments["plugins_inspect_parameter_map"].get(
            "limit"
        )
        == 128,
        "plugins_scan_parameters": arguments["plugins_scan_parameters"].get(
            "max_indices"
        )
        == 8192,
        "plugins_scan_loaded_plugins": arguments["plugins_scan_loaded_plugins"].get(
            "include_channel_generators"
        )
        is True,
    }
    missing = [
        name for name, satisfied in large_read_requirements.items() if not satisfied
    ]
    if missing:
        raise AcceptanceConfigurationError(
            "bounded large-read coverage is incomplete for %s" % missing
        )


def _json_digest(value: Any) -> tuple[int, str]:
    encoded = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return len(encoded), hashlib.sha256(encoded).hexdigest()


def _validated_deadline(value: float | None, *, label: str) -> float | None:
    if value is None:
        return None
    try:
        resolved = float(value)
    except (TypeError, ValueError) as exc:
        raise AcceptanceConfigurationError("%s must be a number" % label) from exc
    if not math.isfinite(resolved) or resolved <= 0:
        raise AcceptanceConfigurationError(
            "%s must be a finite number greater than zero" % label
        )
    return resolved


async def run_read_acceptance(
    arguments: Mapping[str, Mapping[str, Any]],
    *,
    caller: ToolCaller | None = None,
    surface: ToolSurface | None = None,
    environ: Mapping[str, str] | None = None,
    checkpoint: EvidenceCheckpoint | None = None,
    bounded_caller: Callable[
        [str, dict[str, Any], float], Awaitable[ReadToolInvocationResult]
    ]
    | None = None,
    per_tool_timeout_seconds: float | None = None,
    overall_timeout_seconds: float | None = None,
    started_at: float | None = None,
) -> dict[str, Any]:
    """Exercise every authoritative read tool with durable per-call state."""

    selected_surface = surface or await authoritative_tool_surface()
    _validate_read_coverage(selected_surface, arguments)
    invoke = caller or _default_caller
    per_tool_timeout = _validated_deadline(
        per_tool_timeout_seconds, label="per-tool timeout"
    )
    overall_timeout = _validated_deadline(
        overall_timeout_seconds, label="overall timeout"
    )
    if bounded_caller is not None and per_tool_timeout is None:
        raise AcceptanceConfigurationError(
            "a bounded read caller requires a per-tool timeout"
        )
    if bounded_caller is None and (
        per_tool_timeout is not None or overall_timeout is not None
    ):
        raise AcceptanceConfigurationError(
            "read deadlines require an isolated bounded caller"
        )
    environment = os.environ if environ is None else environ
    clock_started = time.monotonic() if started_at is None else float(started_at)
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "postfader_read_acceptance",
        "phase": "read_execution",
        "contact_started": True,
        "project_saved": False,
        "host": {
            "platform": platform_family(),
            "architecture": platform.machine() or "unknown",
            "python_version": platform.python_version(),
            "package_version": __version__,
        },
        "configured_midi_query": midi_port_query(environ=environment),
        "authoritative_tool_count": len(selected_surface.all_tools),
        "authoritative_read_tool_count": len(selected_surface.read_tools),
        "tools": [],
        "connection": None,
        "failures": [],
        "per_tool_timeout_seconds": per_tool_timeout,
        "overall_timeout_seconds": overall_timeout,
    }

    def elapsed() -> float:
        return round(max(0.0, time.monotonic() - clock_started), 6)

    def save_checkpoint(entry: Mapping[str, Any]) -> bool:
        report["last_checkpoint"] = {
            "phase": "read_invocation",
            "tool_index": entry["tool_index"],
            "tool_count": entry["tool_count"],
            "tool": entry["tool"],
            "arguments": entry["arguments"],
            "status": entry["status"],
            "monotonic_elapsed_seconds": entry["monotonic_elapsed_seconds"],
        }
        if "response_json_bytes" in entry:
            report["last_checkpoint"].update(
                {
                    "response_json_bytes": entry["response_json_bytes"],
                    "response_sha256": entry["response_sha256"],
                }
            )
        if "error" in entry:
            report["last_checkpoint"]["error"] = entry["error"]
        if "worker_cleanup" in entry:
            report["last_checkpoint"]["worker_cleanup"] = entry["worker_cleanup"]
        if checkpoint is None:
            return True
        try:
            checkpoint(report)
        except Exception as exc:
            failure = {
                "stage": "evidence_output",
                "tool": entry["tool"],
                "tool_index": entry["tool_index"],
                "checkpoint_status": entry["status"],
                "error": str(exc),
            }
            report.setdefault("evidence_output_failures", []).append(failure)
            report["failures"].append(failure)
            report["overall"] = "fail"
            report["phase"] = "evidence_output_failure"
            return False
        return True

    stop_after_index: int | None = None
    tool_count = len(selected_surface.read_tools)
    for index, name in enumerate(selected_surface.read_tools, start=1):
        entry: dict[str, Any] = {
            "tool_index": index,
            "tool_count": tool_count,
            "tool": name,
            "arguments": dict(arguments[name]),
            "status": "in_flight",
            "monotonic_elapsed_seconds": elapsed(),
        }
        report["tools"].append(entry)

        if overall_timeout is not None:
            remaining = overall_timeout - (time.monotonic() - clock_started)
            if remaining <= 0:
                entry.update(
                    {
                        "status": "timed_out",
                        "timeout_kind": "overall_before_invocation",
                        "error": "overall read deadline expired before invocation",
                        "monotonic_elapsed_seconds": elapsed(),
                    }
                )
                report["failures"].append(
                    {
                        "stage": "read_timeout",
                        "tool": name,
                        "tool_index": index,
                        "error": entry["error"],
                    }
                )
                save_checkpoint(entry)
                stop_after_index = index
                break

        if not save_checkpoint(entry):
            entry["status"] = "not_invoked_evidence_failure"
            stop_after_index = index
            break

        try:
            if bounded_caller is None:
                payload = tool_payload(await invoke(name, dict(arguments[name])))
            else:
                assert per_tool_timeout is not None
                effective_timeout = per_tool_timeout
                timeout_kind = "per_tool"
                if overall_timeout is not None:
                    remaining = overall_timeout - (time.monotonic() - clock_started)
                    if remaining < effective_timeout:
                        effective_timeout = max(remaining, 0.000001)
                        timeout_kind = "overall_during_invocation"
                invocation = await bounded_caller(
                    name, dict(arguments[name]), effective_timeout
                )
                payload = invocation.value
                entry["worker_cleanup"] = dict(invocation.cleanup)
            size, digest = _json_digest(payload)
            entry.update(
                {
                    "status": "passed",
                    "response_json_bytes": size,
                    "response_sha256": digest,
                    "monotonic_elapsed_seconds": elapsed(),
                }
            )
            if name == "fl_get_project_summary" and isinstance(payload, dict):
                connection = payload.get("connection")
                if isinstance(connection, dict):
                    report["connection"] = {
                        "program_title": connection.get("program_title"),
                        "fl_version": connection.get("fl_app_version"),
                        "fl_build": connection.get("fl_build"),
                        "midi_scripting_api": connection.get(
                            "midi_scripting_api_version"
                        ),
                        "bridge_protocol": connection.get("bridge_protocol_version"),
                        "bridge_hash": connection.get("bridge_source_sha256"),
                        "session_fingerprint": connection.get("session_fingerprint"),
                        "selected_transport": connection.get("bridge_transport"),
                        "bridge_mode": connection.get("bridge_mode"),
                    }
        except ReadToolTimeoutError as exc:
            entry.update(
                {
                    "status": "timed_out",
                    "timeout_kind": timeout_kind,
                    "timeout_seconds": exc.timeout_seconds,
                    "error": str(exc),
                    "worker_cleanup": dict(exc.cleanup),
                    "monotonic_elapsed_seconds": elapsed(),
                }
            )
            report["failures"].append(
                {
                    "stage": "read_timeout",
                    "tool": name,
                    "tool_index": index,
                    "error": str(exc),
                }
            )
            save_checkpoint(entry)
            stop_after_index = index
            break
        except ReadToolInvocationError as exc:
            entry["worker_cleanup"] = dict(exc.cleanup)
            entry.update(
                {
                    "status": "failed",
                    "error": str(exc),
                    "monotonic_elapsed_seconds": elapsed(),
                }
            )
            report["failures"].append(
                {"stage": "read_execution", "tool": name, "error": str(exc)}
            )
        except Exception as exc:
            entry.update(
                {
                    "status": "failed",
                    "error": str(exc),
                    "monotonic_elapsed_seconds": elapsed(),
                }
            )
            report["failures"].append(
                {"stage": "read_execution", "tool": name, "error": str(exc)}
            )
        if not save_checkpoint(entry):
            stop_after_index = index
            break

    if stop_after_index is not None:
        for index, name in enumerate(
            selected_surface.read_tools[stop_after_index:],
            start=stop_after_index + 1,
        ):
            report["tools"].append(
                {
                    "tool_index": index,
                    "tool_count": tool_count,
                    "tool": name,
                    "arguments": dict(arguments[name]),
                    "status": "skipped",
                    "reason": "a timeout or evidence failure stopped later reads",
                    "monotonic_elapsed_seconds": elapsed(),
                }
            )
    report["overall"] = "pass" if not report["failures"] else "fail"
    if not report["failures"]:
        report["phase"] = "complete"
    elif report.get("evidence_output_failures"):
        report["phase"] = "evidence_output_failure"
    elif any(item.get("stage") == "read_timeout" for item in report["failures"]):
        report["phase"] = "read_timeout"
    else:
        report["phase"] = "read_execution"
    return report


def _path(value: Any, dotted: str) -> Any:
    current = value
    if not dotted:
        return current
    for piece in dotted.split("."):
        if isinstance(current, list):
            try:
                current = current[int(piece)]
            except (IndexError, ValueError) as exc:
                raise AcceptanceConfigurationError(
                    "evidence path %r was not present" % dotted
                ) from exc
        elif isinstance(current, dict) and piece in current:
            current = current[piece]
        else:
            raise AcceptanceConfigurationError(
                "evidence path %r was not present" % dotted
            )
    return current


def _select_unique(value: Any, selector: Any) -> Any:
    """Resolve one logical list member by identity, never by list position."""

    if not isinstance(selector, dict):
        raise AcceptanceConfigurationError("$select must contain an object")
    allowed = {"path", "where", "value"}
    if set(selector) - allowed:
        raise AcceptanceConfigurationError(
            "$select contains unsupported fields: %s" % sorted(set(selector) - allowed)
        )
    path = selector.get("path")
    where = selector.get("where")
    result_path = selector.get("value", "")
    if not isinstance(path, str) or not path:
        raise AcceptanceConfigurationError("$select.path must be a non-empty string")
    if not isinstance(where, dict) or not where:
        raise AcceptanceConfigurationError("$select.where must be a non-empty object")
    if not isinstance(result_path, str):
        raise AcceptanceConfigurationError("$select.value must be a string")
    candidates = _path(value, path)
    if not isinstance(candidates, list):
        raise AcceptanceConfigurationError(
            "$select path %r did not resolve to a list" % path
        )
    matches = []
    for candidate in candidates:
        try:
            matched = all(
                _path(candidate, str(identity_path)) == expected
                for identity_path, expected in where.items()
            )
        except AcceptanceConfigurationError:
            matched = False
        if matched:
            matches.append(candidate)
    if len(matches) != 1:
        raise AcceptanceConfigurationError(
            "$select path %r with identity %r matched %d entries; exactly one is required"
            % (path, where, len(matches))
        )
    if result_path == "$exists":
        return True
    return _path(matches[0], result_path)


def resolve_evidence_reference(spec: Any, evidence: Any) -> Any:
    """Resolve a dotted path or identity selector against captured evidence."""

    if isinstance(spec, str) and spec:
        return _path(evidence, spec)
    if isinstance(spec, dict) and set(spec) == {"$select"}:
        return _select_unique(evidence, spec["$select"])
    raise AcceptanceConfigurationError(
        "evidence reference must be a non-empty path or one $select object"
    )


def _resolve_templates(value: Any, before: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$before"}:
        return _path(before, str(value["$before"]))
    if isinstance(value, dict) and set(value) == {"$select"}:
        return _select_unique(before, value["$select"])
    if isinstance(value, dict):
        return {key: _resolve_templates(item, before) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_templates(item, before) for item in value]
    return value


def _resolve_restore_templates(
    value: Any,
    before: Any,
    *,
    mutation_tool: str,
    mutation_arguments: Mapping[str, Any],
) -> Any:
    """Resolve restoration values that can be proven before mutation starts."""

    if isinstance(value, dict) and set(value) == {"$after_step_digest"}:
        if (
            value["$after_step_digest"] is not True
            or mutation_tool != "fl_set_step_sequence"
        ):
            raise AcceptanceConfigurationError(
                "$after_step_digest is only valid for fl_set_step_sequence restoration"
            )
        from .track_b_contracts import compute_step_sequence_digest

        try:
            pattern_number = int(_path(before, "pattern_number"))
            channel_index = int(_path(before, "channel_index"))
            step_count = int(_path(before, "step_count"))
            cells = list(_path(before, "cells"))
            for update in mutation_arguments["updates"]:
                cells[int(update["step_index"])] = bool(update["enabled"])
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise AcceptanceConfigurationError(
                "could not compute the post-mutation step digest for restoration: %s"
                % exc
            ) from exc
        return compute_step_sequence_digest(
            pattern_number=pattern_number,
            channel_index=channel_index,
            step_count=step_count,
            cells=cells,
        )
    if isinstance(value, dict) and set(value) == {"$before_loop_mode"}:
        if value["$before_loop_mode"] is not True:
            raise AcceptanceConfigurationError(
                "$before_loop_mode must have the literal value true"
            )
        raw = _path(before, "loop_mode")
        mapping = {0: "pattern", 1: "song", "pattern": "pattern", "song": "song"}
        try:
            resolved_loop_mode = mapping[raw]
        except (KeyError, TypeError):
            raise AcceptanceConfigurationError(
                "captured loop_mode %r cannot be restored as pattern or song" % raw
            )
        return resolved_loop_mode
    if isinstance(value, dict) and set(value) == {"$before"}:
        return _path(before, str(value["$before"]))
    if isinstance(value, dict) and set(value) == {"$select"}:
        return _select_unique(before, value["$select"])
    if isinstance(value, dict):
        return {
            key: _resolve_restore_templates(
                item,
                before,
                mutation_tool=mutation_tool,
                mutation_arguments=mutation_arguments,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_restore_templates(
                item,
                before,
                mutation_tool=mutation_tool,
                mutation_arguments=mutation_arguments,
            )
            for item in value
        ]
    return value


def _contains_before_template(value: Any) -> bool:
    if isinstance(value, dict):
        if set(value) in ({"$before"}, {"$select"}):
            return True
        return any(_contains_before_template(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_before_template(item) for item in value)
    return False


def _contains_deferred_template(value: Any) -> bool:
    if isinstance(value, dict):
        if set(value) in (
            {"$before"},
            {"$after_step_digest"},
            {"$before_loop_mode"},
            {"$select"},
        ):
            return True
        return any(_contains_deferred_template(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_deferred_template(item) for item in value)
    return False


def _validate_tool_arguments(
    surface: ToolSurface,
    tool: str,
    arguments: Mapping[str, Any],
    *,
    label: str,
) -> None:
    schema = surface.input_schemas.get(tool)
    if schema is None:
        raise AcceptanceConfigurationError(
            "%s references unknown tool %r" % (label, tool)
        )
    errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(arguments)),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(piece) for piece in error.absolute_path) or "<root>"
        raise AcceptanceConfigurationError(
            "%s arguments fail the registered MCP input schema at %s: %s"
            % (label, location, error.message)
        )


def _with_session(arguments: Mapping[str, Any], session: str) -> dict[str, Any]:
    resolved = dict(arguments)
    resolved["session_fingerprint"] = session
    return resolved


def validate_write_scenario(surface: ToolSurface, scenario: Mapping[str, Any]) -> None:
    operations = scenario.get("operations")
    if not isinstance(operations, list):
        raise AcceptanceConfigurationError(
            "write scenario must contain an operations list"
        )
    names = [item.get("tool") for item in operations if isinstance(item, dict)]
    expected = set(surface.persistent_write_tools)
    actual = set(names)
    if actual != expected or len(names) != len(expected):
        raise AcceptanceConfigurationError(
            "write scenario does not cover the authoritative persistent-write surface exactly once; "
            "missing=%s extra_or_duplicate=%s"
            % (
                sorted(expected - actual),
                sorted(
                    name
                    for name in names
                    if names.count(name) > 1 or name not in expected
                ),
            )
        )
    if set(surface.ephemeral_tools) & actual:
        raise AcceptanceConfigurationError(
            "ephemeral live-note tools must be tested separately from persistent writes"
        )
    if scenario.get("safe_to_edit") is not True:
        raise AcceptanceConfigurationError(
            "write scenario must declare safe_to_edit=true after human review"
        )
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise AcceptanceConfigurationError(
                "write scenario operation %d must be an object" % index
            )
        tool = str(operation["tool"])
        if tool not in set(surface.persistent_write_tools):
            raise AcceptanceConfigurationError(
                "%s is not an authoritative persistent-write tool" % tool
            )
        mutation_arguments = operation.get("mutation_arguments")
        if not isinstance(mutation_arguments, dict):
            raise AcceptanceConfigurationError(
                "%s mutation_arguments must be an object" % tool
            )
        before = operation.get("before")
        if (
            not isinstance(before, dict)
            or not isinstance(before.get("tool"), str)
            or not before["tool"]
            or not isinstance(before.get("arguments", {}), dict)
        ):
            raise AcceptanceConfigurationError(
                "%s must have a before read with tool and object arguments" % tool
            )
        before_tool = str(before["tool"])
        if before_tool not in set(surface.read_tools):
            raise AcceptanceConfigurationError(
                "%s before.tool %r is not authoritatively read-only"
                % (tool, before_tool)
            )
        _validate_tool_arguments(
            surface,
            before_tool,
            dict(before.get("arguments") or {}),
            label="%s before-state read" % tool,
        )
        if not _contains_before_template(mutation_arguments):
            _validate_tool_arguments(
                surface,
                tool,
                _with_session(mutation_arguments, "0" * 32),
                label="%s mutation" % tool,
            )
        restores = operation.get("restore")
        if not isinstance(restores, list) or not restores:
            raise AcceptanceConfigurationError(
                "%s must have at least one restore action" % tool
            )
        for restore in restores:
            if (
                not isinstance(restore, dict)
                or ("tool" in restore and not isinstance(restore["tool"], str))
                or not isinstance(restore.get("arguments", {}), dict)
            ):
                raise AcceptanceConfigurationError(
                    "%s has a malformed restore action" % tool
                )
            restore_tool = str(restore.get("tool") or tool)
            if restore_tool not in set(surface.persistent_write_tools):
                raise AcceptanceConfigurationError(
                    "%s restore tool %r is not an authoritative persistent-write tool"
                    % (tool, restore_tool)
                )
            restore_arguments = dict(restore.get("arguments") or {})
            if not _contains_deferred_template(restore_arguments):
                _validate_tool_arguments(
                    surface,
                    restore_tool,
                    _with_session(restore_arguments, "0" * 32),
                    label="%s restore via %s" % (tool, restore_tool),
                )
        paths = operation.get("verify_paths")
        if not isinstance(paths, list) or not paths:
            raise AcceptanceConfigurationError(
                "%s must have non-empty independent restoration paths" % tool
            )
        for reference in paths:
            if isinstance(reference, str) and reference:
                continue
            if isinstance(reference, dict) and set(reference) == {"$select"}:
                selector = reference["$select"]
                if (
                    isinstance(selector, dict)
                    and isinstance(selector.get("path"), str)
                    and selector["path"]
                    and isinstance(selector.get("where"), dict)
                    and selector["where"]
                    and isinstance(selector.get("value"), str)
                    and selector["value"]
                ):
                    continue
            raise AcceptanceConfigurationError(
                "%s has a malformed independent restoration reference" % tool
            )


def validate_live_write_marker(scenario: Mapping[str, Any]) -> None:
    """Require the reviewed fixture identity before any live setup or contact."""

    if (
        type(scenario.get("scenario_version")) is not int
        or scenario.get("scenario_version") != 1
    ):
        raise AcceptanceConfigurationError(
            "live write acceptance requires scenario_version=1"
        )
    if scenario.get("fixture_status") != "REVIEWED_FOR_THIS_DISPOSABLE_PROJECT":
        raise AcceptanceConfigurationError(
            "the versioned fixture template must be copied, reviewed against "
            "read-only observations, and marked "
            "fixture_status=REVIEWED_FOR_THIS_DISPOSABLE_PROJECT"
        )
    if scenario.get("safe_to_edit") is not True:
        raise AcceptanceConfigurationError(
            "live write acceptance requires safe_to_edit=true"
        )


def prepare_write_scenario(
    surface: ToolSurface,
    scenario: Mapping[str, Any],
    *,
    before_values: Sequence[Any],
    session_fingerprint: str,
    acknowledged_master_tools: Sequence[str] = (),
) -> tuple[PreparedWriteOperation, ...]:
    """Resolve and validate an entire scenario without invoking a mutation."""

    validate_write_scenario(surface, scenario)
    operations = scenario["operations"]
    if len(before_values) != len(operations):
        raise AcceptanceConfigurationError(
            "before-state evidence count does not match scenario operations"
        )
    acknowledgements = set(acknowledged_master_tools)
    prepared: list[PreparedWriteOperation] = []
    for operation, before in zip(operations, before_values):
        tool = str(operation["tool"])
        mutation_arguments = _with_session(
            _resolve_templates(dict(operation["mutation_arguments"]), before),
            session_fingerprint,
        )
        _validate_tool_arguments(
            surface, tool, mutation_arguments, label="%s resolved mutation" % tool
        )
        if _targets_master(mutation_arguments) and tool not in acknowledgements:
            raise AcceptanceConfigurationError(
                "Master target for %s requires its own --acknowledge-master-tool value"
                % tool
            )
        restore_actions: list[tuple[str, dict[str, Any]]] = []
        for restore in operation["restore"]:
            restore_tool = str(restore.get("tool") or tool)
            restore_arguments = _with_session(
                _resolve_restore_templates(
                    dict(restore.get("arguments") or {}),
                    before,
                    mutation_tool=tool,
                    mutation_arguments=mutation_arguments,
                ),
                session_fingerprint,
            )
            _validate_tool_arguments(
                surface,
                restore_tool,
                restore_arguments,
                label="%s resolved restore via %s" % (tool, restore_tool),
            )
            if (
                _targets_master(restore_arguments)
                and restore_tool not in acknowledgements
            ):
                raise AcceptanceConfigurationError(
                    "Master restore target for %s requires its own "
                    "--acknowledge-master-tool value" % restore_tool
                )
            restore_actions.append((restore_tool, restore_arguments))
        verify_specs = tuple(operation["verify_paths"])
        verify_expected = tuple(
            resolve_evidence_reference(reference, before) for reference in verify_specs
        )
        if tool == "fl_set_playing" and mutation_arguments.get("playing") is True:
            captured_playing = _path(before, "playing")
            captured_position = _path(before, "song_position_normalized")
            if captured_playing is not False:
                raise AcceptanceConfigurationError(
                    "fl_set_playing=true requires captured playing=false"
                )
            if (
                len(restore_actions) < 2
                or restore_actions[0][0] != "fl_set_playing"
                or restore_actions[0][1].get("playing") is not False
                or restore_actions[1][0] != "fl_set_song_position"
                or restore_actions[1][1].get("position_normalized") != captured_position
            ):
                raise AcceptanceConfigurationError(
                    "fl_set_playing=true must restore playing=false first and "
                    "then restore captured song_position_normalized"
                )
            if (
                "playing" not in verify_specs
                or "song_position_normalized" not in verify_specs
            ):
                raise AcceptanceConfigurationError(
                    "fl_set_playing=true must independently verify playing and "
                    "song_position_normalized"
                )
        prepared.append(
            PreparedWriteOperation(
                tool=tool,
                before=before,
                mutation_arguments=mutation_arguments,
                restore_actions=tuple(restore_actions),
                verify_specs=verify_specs,
                verify_expected=verify_expected,
            )
        )
    return tuple(prepared)


def validate_write_scenario_plan(
    surface: ToolSurface,
    scenario: Mapping[str, Any],
    *,
    acknowledged_master_tools: Sequence[str] = (),
) -> tuple[PreparedWriteOperation, ...]:
    """Fully resolve a fixture scenario using embedded no-I/O before evidence."""

    validate_write_scenario(surface, scenario)
    before_values: list[Any] = []
    for operation in scenario["operations"]:
        if "fixture_before" not in operation:
            raise AcceptanceConfigurationError(
                "%s needs fixture_before for full no-I/O plan validation"
                % operation["tool"]
            )
        before_values.append(operation["fixture_before"])
    return prepare_write_scenario(
        surface,
        scenario,
        before_values=before_values,
        session_fingerprint="0" * 32,
        acknowledged_master_tools=acknowledged_master_tools,
    )


def _targets_master(value: Any) -> bool:
    """Return whether reviewed arguments address mixer track zero anywhere."""

    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"track_index", "destination_track_index"} and item == 0:
                return True
            if _targets_master(item):
                return True
    elif isinstance(value, list):
        return any(_targets_master(item) for item in value)
    return False


async def run_write_acceptance(
    scenario: Mapping[str, Any],
    *,
    confirm_user_present: bool,
    confirm_disposable_project: bool,
    confirm_safe_to_edit: bool,
    acknowledged_master_tools: Sequence[str] = (),
    caller: ToolCaller | None = None,
    surface: ToolSurface | None = None,
    checkpoint: EvidenceCheckpoint | None = None,
) -> dict[str, Any]:
    """Run each persistent mutation once, restore, and independently reread."""

    validate_live_write_marker(scenario)
    if not confirm_user_present or not confirm_disposable_project:
        raise AcceptanceConfigurationError(
            "write acceptance requires --confirm-user-present and "
            "--confirm-disposable-project"
        )
    if not confirm_safe_to_edit or scenario.get("safe_to_edit") is not True:
        raise AcceptanceConfigurationError(
            "write acceptance requires an explicit safe-to-edit confirmation in both CLI and scenario"
        )
    selected_surface = surface or await authoritative_tool_surface()
    validate_write_scenario(selected_surface, scenario)
    invoke = caller or _default_caller
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "postfader_write_acceptance",
        "phase": "preflight",
        "contact_started": True,
        "project_saved": False,
        "operations": [],
        "failures": [],
    }

    def save_checkpoint(phase: str, **details: Any) -> bool:
        report["phase"] = phase
        report["last_checkpoint"] = {"phase": phase, **details}
        if checkpoint is None:
            return True
        try:
            checkpoint(report)
        except Exception as exc:
            failure = {
                "stage": "evidence_output",
                "checkpoint_phase": phase,
                "reason": str(exc),
            }
            report.setdefault("evidence_output_failures", []).append(failure)
            report["failures"].append(failure)
            report["overall"] = "fail"
            report["phase"] = "evidence_output_failure"
            return False
        return True

    report["preflight"] = {"status": "attempted"}
    if not save_checkpoint("preflight_attempt", writes_attempted=0):
        return report
    try:
        project = tool_payload(await invoke("fl_get_project_summary", {}))
        transport = tool_payload(await invoke("fl_get_transport_state", {}))
    except Exception as exc:
        report["overall"] = "fail"
        report["preflight"] = {"status": "failed", "error": str(exc)}
        report["failures"].append(
            {"stage": "preflight", "reason": str(exc), "writes_attempted": 0}
        )
        save_checkpoint("preflight_result", status="failed", writes_attempted=0)
        return report
    report["preflight"] = {
        "status": "completed",
        "project": project,
        "transport": transport,
    }
    if not save_checkpoint("preflight_result", status="completed", writes_attempted=0):
        return report
    if not isinstance(project, dict) or not isinstance(transport, dict):
        report["overall"] = "fail"
        report["failures"].append(
            {
                "stage": "preflight",
                "reason": "preflight reads returned malformed evidence",
                "writes_attempted": 0,
            }
        )
        save_checkpoint("preflight_validation", status="failed", writes_attempted=0)
        return report
    if transport.get("playing") is True or transport.get("recording") is True:
        report["overall"] = "fail"
        report["failures"].append(
            {
                "stage": "preflight",
                "reason": "refusing writes while FL Studio reports playing or recording",
                "writes_attempted": 0,
            }
        )
        save_checkpoint("preflight_validation", status="failed", writes_attempted=0)
        return report
    connection = project.get("connection")
    if not isinstance(connection, dict):
        report["overall"] = "fail"
        report["failures"].append(
            {
                "stage": "preflight",
                "reason": "project summary has no connection evidence",
                "writes_attempted": 0,
            }
        )
        save_checkpoint("preflight_validation", status="failed", writes_attempted=0)
        return report
    if connection.get("bridge_provenance_verified") is not True:
        report["overall"] = "fail"
        report["failures"].append(
            {
                "stage": "preflight",
                "reason": "bridge provenance is not verified",
                "writes_attempted": 0,
            }
        )
        save_checkpoint("preflight_validation", status="failed", writes_attempted=0)
        return report
    if connection.get("verified_writes_enabled") is not True:
        report["overall"] = "fail"
        report["failures"].append(
            {
                "stage": "preflight",
                "reason": "the running bridge has verified writes disabled",
                "writes_attempted": 0,
            }
        )
        save_checkpoint("preflight_validation", status="failed", writes_attempted=0)
        return report
    session = connection.get("session_fingerprint")
    if not isinstance(session, str) or not session:
        report["overall"] = "fail"
        report["failures"].append(
            {
                "stage": "preflight",
                "reason": "the running bridge has no session fingerprint",
                "writes_attempted": 0,
            }
        )
        save_checkpoint("preflight_validation", status="failed", writes_attempted=0)
        return report

    report.update(
        {
            "selected_transport": connection.get("bridge_transport"),
            "bridge_protocol": connection.get("bridge_protocol_version"),
            "bridge_hash": connection.get("bridge_source_sha256"),
            "session_fingerprint": session,
        }
    )
    if not save_checkpoint("preflight_validation", status="passed", writes_attempted=0):
        return report
    before_values: list[Any] = []
    report["before_state_captures"] = []
    for operation_index, operation in enumerate(scenario["operations"]):
        before_spec = operation["before"]
        capture: dict[str, Any] = {
            "operation_index": operation_index,
            "tool": operation["tool"],
            "read_tool": before_spec["tool"],
            "status": "attempted",
        }
        report["before_state_captures"].append(capture)
        if not save_checkpoint(
            "before_state_capture_attempt",
            operation_index=operation_index,
            tool=operation["tool"],
            writes_attempted=0,
        ):
            return report
        try:
            before_value = tool_payload(
                await invoke(
                    str(before_spec["tool"]),
                    dict(before_spec.get("arguments") or {}),
                )
            )
            before_values.append(before_value)
            capture.update({"status": "completed", "value": before_value})
            if not save_checkpoint(
                "before_state_capture_result",
                operation_index=operation_index,
                tool=operation["tool"],
                status="completed",
                writes_attempted=0,
            ):
                return report
        except Exception as exc:
            report["overall"] = "fail"
            capture.update({"status": "failed", "error": str(exc)})
            report["failures"].append(
                {
                    "stage": "before_state_capture",
                    "tool": operation["tool"],
                    "reason": str(exc),
                    "writes_attempted": 0,
                }
            )
            save_checkpoint(
                "before_state_capture_result",
                operation_index=operation_index,
                tool=operation["tool"],
                status="failed",
                writes_attempted=0,
            )
            return report
    try:
        prepared = prepare_write_scenario(
            selected_surface,
            scenario,
            before_values=before_values,
            session_fingerprint=session,
            acknowledged_master_tools=acknowledged_master_tools,
        )
    except AcceptanceConfigurationError as exc:
        report["overall"] = "fail"
        report["phase"] = "scenario_preparation"
        report["failures"].append(
            {
                "stage": "scenario_preparation",
                "reason": str(exc),
                "writes_attempted": 0,
            }
        )
        save_checkpoint("scenario_preparation", status="failed", writes_attempted=0)
        return report

    if not save_checkpoint("scenario_preparation", status="passed", writes_attempted=0):
        return report
    for operation_index, operation in enumerate(prepared):
        tool = operation.tool
        entry: dict[str, Any] = {
            "operation_index": operation_index,
            "tool": tool,
            "before": operation.before,
            "mutation_attempts": 0,
            "mutation_status": "not_attempted",
            "restore": [],
        }
        report["operations"].append(entry)
        if not save_checkpoint(
            "mutation_attempt",
            operation_index=operation_index,
            tool=tool,
            attempt=1,
            ambiguous=False,
        ):
            entry["status"] = "evidence_output_failure_before_mutation"
            return report
        entry["mutation_attempts"] = 1
        entry["mutation_status"] = "attempted"
        operation_evidence_failure_start = len(
            report.get("evidence_output_failures", [])
        )
        try:
            mutation = tool_payload(await invoke(tool, operation.mutation_arguments))
        except Exception as exc:
            entry.update(
                {
                    "status": "ambiguous_transport_failure",
                    "stage": "mutation",
                    "mutation_status": "ambiguous",
                    "error": str(exc),
                    "automatic_replay": False,
                    "restoration_status": "uncertain_not_attempted",
                }
            )
            report["failures"].append(
                {
                    "stage": "mutation",
                    "tool": tool,
                    "reason": "AMBIGUOUS MUTATION OUTCOME; restoration not attempted; manual inspection required",
                }
            )
            report["overall"] = "fail"
            save_checkpoint(
                "mutation_result",
                operation_index=operation_index,
                tool=tool,
                status="ambiguous",
                ambiguous=True,
                automatic_replay=False,
                restoration_status="uncertain_not_attempted",
            )
            return report
        entry["mutation"] = mutation
        entry["mutation_status"] = "completed"
        save_checkpoint(
            "mutation_result",
            operation_index=operation_index,
            tool=tool,
            status="completed",
            ambiguous=False,
        )
        restore_failed = False
        for restore_index, (restore_tool, restore_arguments) in enumerate(
            operation.restore_actions
        ):
            restore_record: dict[str, Any] = {
                "restore_index": restore_index,
                "tool": restore_tool,
                "status": "attempted",
            }
            entry["restore"].append(restore_record)
            save_checkpoint(
                "restoration_attempt",
                operation_index=operation_index,
                tool=tool,
                restore_index=restore_index,
                restore_tool=restore_tool,
            )
            try:
                restored = tool_payload(await invoke(restore_tool, restore_arguments))
                restore_record.update({"status": "completed", "result": restored})
                save_checkpoint(
                    "restoration_result",
                    operation_index=operation_index,
                    tool=tool,
                    restore_index=restore_index,
                    restore_tool=restore_tool,
                    status="completed",
                )
                if (
                    not isinstance(restored, dict)
                    or restored.get("verified") is not True
                ):
                    restore_record["status"] = "unverified"
                    save_checkpoint(
                        "restoration_result",
                        operation_index=operation_index,
                        tool=tool,
                        restore_index=restore_index,
                        restore_tool=restore_tool,
                        status="unverified",
                    )
                    restore_failed = True
                    break
            except Exception as exc:
                restore_record.update({"status": "failed", "error": str(exc)})
                save_checkpoint(
                    "restoration_result",
                    operation_index=operation_index,
                    tool=tool,
                    restore_index=restore_index,
                    restore_tool=restore_tool,
                    status="failed",
                )
                restore_failed = True
                break
        if restore_failed:
            entry["status"] = "restore_unverified"
            entry["restoration_status"] = "uncertain"
            report["failures"].append(
                {
                    "stage": "restoration",
                    "tool": tool,
                    "reason": "RESTORE UNVERIFIED; stop and inspect the disposable project",
                }
            )
            report["overall"] = "fail"
            save_checkpoint(
                "restoration",
                operation_index=operation_index,
                tool=tool,
                status="uncertain",
            )
            return report
        before_spec = next(
            item["before"] for item in scenario["operations"] if item["tool"] == tool
        )
        save_checkpoint(
            "independent_restoration_reread_attempt",
            operation_index=operation_index,
            tool=tool,
        )
        try:
            after_restore = tool_payload(
                await invoke(
                    str(before_spec["tool"]),
                    dict(before_spec.get("arguments") or {}),
                )
            )
        except Exception as exc:
            entry.update(
                {
                    "status": "reread_failed",
                    "stage": "independent_restoration_reread",
                    "restoration_status": "uncertain",
                    "error": str(exc),
                }
            )
            report["failures"].append(
                {
                    "stage": "independent_restoration_reread",
                    "tool": tool,
                    "reason": "RESTORATION REREAD FAILED; restored state is uncertain",
                }
            )
            report["overall"] = "fail"
            save_checkpoint(
                "independent_restoration_reread_result",
                operation_index=operation_index,
                tool=tool,
                status="uncertain",
            )
            return report
        entry["after_restore"] = after_restore
        save_checkpoint(
            "independent_restoration_reread_result",
            operation_index=operation_index,
            tool=tool,
            status="completed",
        )
        mismatches: list[Any] = []
        selector_errors: list[dict[str, Any]] = []
        for reference, expected in zip(
            operation.verify_specs, operation.verify_expected
        ):
            try:
                actual = resolve_evidence_reference(reference, after_restore)
            except AcceptanceConfigurationError as exc:
                mismatches.append(reference)
                selector_errors.append({"reference": reference, "error": str(exc)})
                continue
            if expected != actual:
                mismatches.append(reference)
        mutation_verified = (
            isinstance(mutation, dict) and mutation.get("verified") is True
        )
        if mismatches or not mutation_verified:
            entry["status"] = "failed"
            entry["restoration_mismatches"] = mismatches
            if selector_errors:
                entry["restoration_selector_errors"] = selector_errors
            report["failures"].append(
                {
                    "tool": tool,
                    "stage": "mutation_verification"
                    if not mutation_verified
                    else "independent_restoration_reread",
                    "reason": "mutation unverified"
                    if not mutation_verified
                    else "independent restoration mismatch",
                }
            )
            report["overall"] = "fail"
            save_checkpoint(
                "mutation_verification"
                if not mutation_verified
                else "independent_restoration_verification",
                operation_index=operation_index,
                tool=tool,
                status="failed",
                restoration_uncertain=bool(mismatches),
            )
            return report
        entry["status"] = "passed"
        entry["restoration_status"] = "verified"
        evidence_failed = len(report.get("evidence_output_failures", [])) > (
            operation_evidence_failure_start
        )
        if evidence_failed:
            entry["status"] = "evidence_output_failure"
            report["overall"] = "fail"
            save_checkpoint(
                "operation_complete",
                operation_index=operation_index,
                tool=tool,
                status="evidence_output_failure",
            )
            report["phase"] = "evidence_output_failure"
            return report
        if not save_checkpoint(
            "operation_complete",
            operation_index=operation_index,
            tool=tool,
            status="passed",
        ):
            entry["status"] = "evidence_output_failure"
            return report
    report["overall"] = "pass"
    save_checkpoint("complete", status="passed")
    return report


def run(coroutine):
    return asyncio.run(coroutine)
