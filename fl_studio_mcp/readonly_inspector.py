"""Fail-closed, read-only FL Studio 2026 inspection core.

The gateway has a fixed command allowlist, so an MCP client cannot reach a
setter merely by inventing a bridge command name.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from .bridge_client import BridgeError, get_client
from .contracts import (
    CapabilitiesReport,
    CapabilityEvidence,
    CapabilityRecord,
    CapabilityStatus,
    ConnectionInfo,
    EqBandState,
    EvidenceKind,
    LoadedPluginInventory,
    MixerRoute,
    MixerTrackInspection,
    MixerTrackList,
    MixerTrackSummary,
    PluginParameter,
    PluginParameterPage,
    PluginParameterScan,
    PluginSlotSummary,
    ProjectSummary,
    ReadOnlyInspectionReport,
    SelectedRangeObservation,
    TransportState,
)


MIDI_SCRIPTING_DOC = (
    "https://www.image-line.com/fl-studio-learning/"
    "fl-studio-online-manual/html/midi_scripting.htm"
)
EXPORT_DOC = (
    "https://www.image-line.com/fl-studio-learning/"
    "fl-studio-online-manual/html/fformats_save_export.htm"
)
CHANGELOG = (
    "https://www.image-line.com/fl-studio-learning-content/"
    "fl-studio-online-manual/html/WhatsNew.htm"
)

REQUIRED_PROGRAM_TITLE = "FL Studio 2026"
MINIMUM_VERSION = (26, 1, 3, 5336)
MINIMUM_MIDI_API_VERSION = 44
SUPPORTED_BRIDGE_PROTOCOLS = frozenset({1, 2})
MAX_JSON_SAFE_INTEGER = 9_007_199_254_740_991


class BridgeLike(Protocol):
    @property
    def transport(self) -> str: ...

    def ping(self) -> dict[str, Any]: ...

    def call(self, cmd: str, **args: Any) -> dict[str, Any]: ...


class ReadOnlyViolation(RuntimeError):
    """Raised when code attempts to cross the read-only command boundary."""


class IncompatibleFLStudio(RuntimeError):
    """Raised when the bridge is attached to FL Studio 2025 or an old build."""


class ReadOnlyGateway:
    """Narrow bridge adapter that cannot dispatch mutation commands."""

    ALLOWED_COMMANDS = frozenset(
        {
            "project.info",
            "arrangement.selection",
            "mixer.list",
            "mixer.track",
            "plugin.params",
            # A read, exactly like plugin.params: it walks the same indices
            # with the same padding rule and calls nothing that changes FL.
            "plugin.scan_params",
            "channels.list",
        }
    )

    def __init__(self, client: BridgeLike | None = None):
        self._client = client or get_client()

    @property
    def transport(self) -> str:
        value = getattr(self._client, "transport", "unknown")
        return value if value in {"tcp", "files", "midi", "none"} else "unknown"

    def ping(self) -> dict[str, Any]:
        return self._client.ping()

    def call(self, command: str, **arguments: Any) -> dict[str, Any]:
        if command not in self.ALLOWED_COMMANDS:
            raise ReadOnlyViolation(
                f"bridge operation {command!r} is not in the read-only allowlist"
            )
        return self._client.call(command, **arguments)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _strict_raw_int(value: Any, field: str) -> int:
    """Accept only an actual JSON-safe integer, never bool/float/string coercion."""
    if type(value) is not int:
        raise ValueError(f"FL bridge returned non-integer {field}")
    if value < -MAX_JSON_SAFE_INTEGER or value > MAX_JSON_SAFE_INTEGER:
        raise ValueError(f"FL bridge returned out-of-range {field}")
    return value


def _optional_hint(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"FL bridge returned non-string {field}")
    if len(value) > 128:
        raise ValueError(f"FL bridge returned oversized {field}")
    return value


def _dirty(value: Any) -> int | None:
    parsed = _int(value)
    return parsed if parsed in (0, 1, 2) else None


def _dirty_name(value: int | None) -> str:
    return {0: "clean", 1: "dirty", 2: "autosave_dirty"}.get(value, "unknown")


def _parse_version(value: str | None) -> tuple[int, int, int, int] | None:
    if not value:
        return None
    match = re.search(
        r"(?:^|\s)v?(\d+)\.(\d+)\.(\d+)(?:\s*\[build\s+(\d+)\])?",
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    major, minor, patch, build = match.groups()
    return int(major), int(minor), int(patch), int(build or 0)


def _project_token(raw: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    """Best public consistency token; useful but not an atomic revision."""
    return (
        _dirty(raw.get("unsaved_changes")),
        _int(raw.get("undo_history_position")),
        _int(raw.get("undo_history_count")),
    )


def _observation_warnings(
    before: dict[str, Any], after: dict[str, Any]
) -> list[str]:
    warnings = [
        "This is a non-atomic live observation; concurrent user or plug-in "
        "changes can produce a torn view even when the dirty flag is unchanged."
    ]
    if _project_token(before) != _project_token(after):
        warnings.append(
            "The public dirty/undo consistency token changed during observation; retry."
        )
    return warnings


def connection_from_ping(
    ping: dict[str, Any], transport: str, error: str | None = None
) -> ConnectionInfo:
    """Judge one bridge handshake.

    Shared by the read-only inspector and the verified write surface, so the
    FL Studio version, protocol, and MIDI-API gates are stated exactly once.
    Both bridge modes are compatible for reading; whether writes may be
    dispatched is a separate question answered by ``verified_writes_enabled``,
    which mirrors what the running bridge reports and nothing else.
    """
    title = str(ping.get("program_title") or "") or None
    version_text = str(ping.get("fl_version") or "") or None
    parsed = _parse_version(version_text)
    build = parsed[3] if parsed else None
    protocol = _int(ping.get("protocol"))
    midi_api = _int(
        ping.get("midi_scripting_api_version", ping.get("fl_version_int"))
    )
    reported_mode = str(ping.get("bridge_mode") or "legacy_unknown")
    bridge_mode = (
        reported_mode
        if reported_mode in {"read_only", "write_test"}
        else "legacy_unknown"
    )
    # Only a literal true from the bridge enables the write tools. A missing
    # or non-boolean field means an older bridge that cannot dispatch them.
    writes_enabled = ping.get("verified_writes_enabled") is True and bridge_mode == "write_test"

    if error is not None:
        compatible = False
        reason = "no live FL Studio 2026 handshake was available"
    elif ping.get("pong") is not True:
        compatible = False
        reason = "bridge handshake did not contain the required pong=true marker"
    elif title != REQUIRED_PROGRAM_TITLE:
        compatible = False
        reason = (
            f"connected application is {title or 'unknown'}, but this project is "
            f"pinned to {REQUIRED_PROGRAM_TITLE}"
        )
    elif parsed is None:
        compatible = False
        reason = "FL Studio version string could not be parsed; refusing to guess"
    elif parsed < MINIMUM_VERSION:
        compatible = False
        reason = (
            "FL Studio 26.1.3 build 5336 or newer is required because that build "
            "contains relevant MIDI-scripting stability fixes"
        )
    elif protocol not in SUPPORTED_BRIDGE_PROTOCOLS:
        compatible = False
        reason = (
            f"bridge protocol {protocol!r} is not supported; expected one of "
            f"{sorted(SUPPORTED_BRIDGE_PROTOCOLS)}"
        )
    elif midi_api is None or midi_api < MINIMUM_MIDI_API_VERSION:
        compatible = False
        reason = (
            f"MIDI scripting API {midi_api!r} is below the validated minimum "
            f"{MINIMUM_MIDI_API_VERSION}"
        )
    elif protocol >= 2 and bridge_mode not in {"read_only", "write_test"}:
        # A protocol-2 bridge that names neither supported mode is refused.
        compatible = False
        reason = (
            "bridge protocol 2 confirmed neither its read-only mode nor the "
            "verified write mode"
        )
    elif protocol >= 2 and bridge_mode == "write_test":
        compatible = True
        reason = (
            "FL Studio 2026 and bridge compatibility gates passed; the bridge "
            "has the verified write commands enabled, so reads are no "
            "longer backed by a read-only-locked bridge"
        )
    else:
        compatible = True
        reason = (
            "FL Studio 2026 and bridge compatibility gates passed"
            if protocol >= 2
            else "FL Studio 2026 gate passed on legacy bridge protocol 1"
        )

    return ConnectionInfo(
        connected=error is None and ping.get("pong") is True,
        compatible=compatible,
        compatibility_reason=reason,
        program_title=title,
        fl_app_version=version_text,
        fl_build=build,
        midi_scripting_api_version=midi_api,
        bridge_protocol_version=protocol,
        bridge_transport=transport if transport in {"tcp", "files", "midi", "none"} else "unknown",
        bridge_mode=bridge_mode,
        bridge_read_only_enforced=protocol is not None and protocol >= 2 and bridge_mode == "read_only",
        verified_writes_enabled=writes_enabled,
        error=error,
    )


def _plugin(track_index: int, raw: dict[str, Any]) -> PluginSlotSummary:
    slot = _int(raw.get("slot"))
    if slot is None:
        raise ValueError("FL bridge returned a plug-in without a slot index")
    return PluginSlotSummary(
        track_index=track_index,
        slot_index=slot,
        name=str(raw.get("name") or ""),
        user_name=str(raw.get("user_name")) if raw.get("user_name") else None,
        reported_parameter_count=_int(raw.get("param_count")),
        mix_level_normalized=_float(raw.get("mix_level")),
    )


def _track(raw: dict[str, Any]) -> MixerTrackSummary:
    index = _int(raw.get("index"))
    if index is None:
        raise ValueError("FL bridge returned a mixer track without an index")
    return MixerTrackSummary(
        index=index,
        name=str(raw.get("name") or ""),
        volume_normalized=_float(raw.get("volume")),
        volume_db=_float(raw.get("volume_db")),
        pan=_float(raw.get("pan")),
        stereo_separation=_float(raw.get("stereo_sep")),
        muted=_bool(raw.get("muted")),
        soloed=_bool(raw.get("solo")),
        armed=_bool(raw.get("armed")),
        selected=_bool(raw.get("selected")),
        track_enabled=_bool(raw.get("enabled")),
        effect_slots_enabled=_bool(raw.get("slots_enabled")),
        polarity_reversed=_bool(raw.get("polarity_reversed")),
        channels_swapped=_bool(raw.get("channels_swapped")),
        color_rgba=_int(raw.get("color")),
        peak_left=_float(raw.get("peak_l")),
        peak_right=_float(raw.get("peak_r")),
        plugins=[_plugin(index, item) for item in raw.get("plugins") or []],
    )


class ReadOnlyInspector:
    """High-level typed inspector used by CLI and MCP entry points."""

    def __init__(self, gateway: ReadOnlyGateway | None = None):
        self.gateway = gateway or ReadOnlyGateway()

    def connection_info(self) -> ConnectionInfo:
        try:
            ping = self.gateway.ping()
        except BridgeError as exc:
            return ConnectionInfo(
                connected=False,
                compatible=False,
                compatibility_reason="no live FL Studio 2026 handshake was available",
                bridge_transport="none",
                error=str(exc),
            )
        return connection_from_ping(ping, self.gateway.transport)

    def _require_compatible(self) -> ConnectionInfo:
        connection = self.connection_info()
        if not connection.connected or not connection.compatible:
            raise IncompatibleFLStudio(
                connection.error or connection.compatibility_reason
            )
        return connection

    def capabilities(self) -> CapabilitiesReport:
        connection = self.connection_info()
        official = EvidenceKind.OFFICIAL_DOCUMENTATION
        live = EvidenceKind.LIVE_READ_ONLY_TEST
        fixture = EvidenceKind.FIXTURE_TEST
        static = EvidenceKind.LOCAL_STATIC_INSPECTION
        project_probe = False
        mixer_probe = False
        selection_probe = False
        project_probe_limitations: list[str] = []
        mixer_probe_limitations: list[str] = []
        selection_probe_limitations: list[str] = []
        if connection.connected and connection.compatible:
            try:
                self.gateway.call("project.info")
                project_probe = True
            except (BridgeError, ValueError) as exc:
                project_probe_limitations.append(
                    f"Current project probe failed: {exc}"
                )
            try:
                self.gateway.call(
                    "mixer.list", only_used=False, peaks=False, max_tracks=1
                )
                mixer_probe = True
            except (BridgeError, ValueError) as exc:
                mixer_probe_limitations.append(
                    f"Current mixer probe failed: {exc}"
                )
            try:
                selection = self.selected_range()
                selection_probe = True
            except (BridgeError, IncompatibleFLStudio, ValueError) as exc:
                selection_probe_limitations.append(
                    f"Current selection probe failed: {exc}"
                )

        current_handshake_detail = (
            f"Current handshake: {connection.fl_app_version}, bridge protocol "
            f"{connection.bridge_protocol_version}, MIDI API "
            f"{connection.midi_scripting_api_version}."
            if connection.connected
            else "No compatible current handshake was observed."
        )

        if not connection.connected:
            boundary_status = CapabilityStatus.UNVALIDATED
            boundary_limits = ["No running bridge was available to verify its mode."]
        elif connection.bridge_read_only_enforced:
            boundary_status = CapabilityStatus.AVAILABLE
            boundary_limits = []
        elif connection.bridge_mode == "write_test":
            boundary_status = CapabilityStatus.PARTIAL
            boundary_limits = [
                "The running protocol-2 bridge has the verified write "
                "commands enabled, so the mixer and plug-in write tools can "
                "change this project. Each write asks FL for an undo point and "
                "reports whether one was observed; false or null means Ctrl+Z "
                "cannot be relied on for that write.",
                "Restart FL Studio without FL_BRIDGE_ENABLE_WRITES to get an "
                "independently read-only-locked bridge back.",
            ]
        else:
            boundary_status = CapabilityStatus.PARTIAL
            boundary_limits = [
                "The running protocol-1 bridge predates the independent bridge lock; "
                "the MCP gateway allowlist is active, but install and reload protocol 2."
            ]

        records = [
            CapabilityRecord(
                capability="fl_2026_handshake",
                status=(
                    CapabilityStatus.AVAILABLE
                    if connection.connected and connection.compatible
                    else CapabilityStatus.UNVALIDATED
                ),
                access_path="MIDI scripting ui/general modules",
                evidence=[
                    CapabilityEvidence(
                        kind=official,
                        detail="FL exposes version information to MIDI scripts.",
                        source_url=MIDI_SCRIPTING_DOC,
                    ),
                    CapabilityEvidence(
                        kind=live,
                        detail=current_handshake_detail,
                    ),
                ],
            ),
            CapabilityRecord(
                capability="bridge_read_only_boundary",
                status=boundary_status,
                access_path="MCP command allowlist plus independent in-process bridge lock",
                limitations=boundary_limits,
                evidence=[
                    CapabilityEvidence(
                        kind=fixture,
                        detail="Default bridge dispatch rejects a mixer write without changing fixture state.",
                    ),
                    CapabilityEvidence(
                        kind=live,
                        # With no handshake there is no running bridge to have
                        # reported anything, and "reported mode legacy_unknown"
                        # reads as though one did.
                        detail=(
                            f"Running bridge reported mode {connection.bridge_mode}."
                            if connection.connected
                            else "No running bridge answered, so no mode was reported."
                        ),
                    ),
                ],
            ),
            CapabilityRecord(
                capability="midi_sysex_bridge",
                status=(
                    CapabilityStatus.AVAILABLE
                    if connection.bridge_transport == "midi"
                    else (
                        CapabilityStatus.PARTIAL
                        if connection.connected
                        else CapabilityStatus.UNVALIDATED
                    )
                ),
                access_path="MIDI script OnSysEx/device.midiOutSysex over CoreMIDI IAC",
                limitations=["Local MIDI traffic is not authenticated in Phase 1."],
                evidence=[
                    CapabilityEvidence(
                        kind=official,
                        detail="Image-Line documents SysEx input and output for MIDI scripts.",
                        source_url=MIDI_SCRIPTING_DOC,
                    ),
                    CapabilityEvidence(
                        kind=live,
                        detail=f"Current observed bridge transport: {connection.bridge_transport}.",
                    ),
                ],
            ),
            CapabilityRecord(
                capability="project_transport_and_counts",
                status=(
                    CapabilityStatus.AVAILABLE
                    if project_probe
                    else CapabilityStatus.UNVALIDATED
                ),
                access_path="general, transport, mixer, channels and patterns getters",
                limitations=list(project_probe_limitations),
                evidence=[
                    CapabilityEvidence(
                        kind=official,
                        detail="The required getters are in the public MIDI scripting API.",
                        source_url=MIDI_SCRIPTING_DOC,
                    ),
                    CapabilityEvidence(
                        kind=live,
                        detail=(
                            "Current project.info read probe succeeded."
                            if project_probe
                            else "No successful current project.info probe."
                        ),
                    ),
                ],
            ),
            CapabilityRecord(
                capability="mixer_tracks_routing_and_loaded_effects",
                status=(
                    CapabilityStatus.PARTIAL
                    if mixer_probe
                    else CapabilityStatus.UNVALIDATED
                ),
                access_path="mixer and plugins getters",
                limitations=[
                    "Sidechain semantics are not fully classified by the public API.",
                    *mixer_probe_limitations,
                ],
                evidence=[
                    CapabilityEvidence(
                        kind=official,
                        detail="Mixer state, sends, effect slots and selection getters are documented.",
                        source_url=MIDI_SCRIPTING_DOC,
                    ),
                    CapabilityEvidence(
                        kind=fixture,
                        detail="Large mixer scans and routing reads pass deterministic fixtures.",
                    ),
                    CapabilityEvidence(
                        kind=live,
                        detail=(
                            "Current bounded mixer read probe succeeded."
                            if mixer_probe
                            else "No successful current mixer probe."
                        ),
                    ),
                ],
            ),
            CapabilityRecord(
                capability="plugin_parameter_inspection",
                status=(
                    CapabilityStatus.PARTIAL
                    if mixer_probe
                    else CapabilityStatus.UNVALIDATED
                ),
                access_path="plugins.getParamCount/getParamName/getParamValue/getParamValueString",
                limitations=[
                    "Display text is supported only by some plug-ins.",
                    "Vendor, exact version, format and units are not exposed.",
                    "Unprofiled parameters are never considered safe to modify.",
                    "No plug-in parameter was read to produce this record. The "
                    "status reflects a reachable, compatible bridge only; call "
                    "plugins_scan_parameters on a real slot to learn what this "
                    "plug-in actually exposes.",
                ],
                evidence=[
                    CapabilityEvidence(
                        kind=official,
                        detail="Normalized values are documented; display strings are explicitly partial.",
                        source_url=MIDI_SCRIPTING_DOC,
                    ),
                    CapabilityEvidence(kind=live, detail=current_handshake_detail),
                ],
            ),
            CapabilityRecord(
                capability="timeline_selection",
                status=(
                    CapabilityStatus.PARTIAL
                    if selection_probe
                    else CapabilityStatus.UNVALIDATED
                ),
                access_path="arrangement.selectionStart/selectionEnd",
                limitations=[
                    "Ordinary live results preserve raw endpoints and PPQ but do not interpret selection state, presence, units, or normalized ticks.",
                    "general.getRecPPB or the current meter cannot establish a time-signature denominator or markers across the selected span.",
                    "An inactive selection can retain a nonnegative raw start; it is not a selected-range boundary.",
                    "Endpoint inclusivity for rendering remains unvalidated.",
                    *selection_probe_limitations,
                ],
                evidence=[
                    CapabilityEvidence(
                        kind=official,
                        detail="Selection start and end getters are documented.",
                        source_url=MIDI_SCRIPTING_DOC,
                    ),
                    CapabilityEvidence(
                        kind=live,
                        detail=(
                            "Current raw selection read probe succeeded; semantic promotion "
                            "is disabled because meter and marker scope cannot be verified."
                            if selection_probe
                            else "No successful current raw selection probe."
                        ),
                    ),
                ],
            ),
            CapabilityRecord(
                capability="playlist_clip_enumeration",
                status=CapabilityStatus.UNAVAILABLE,
                limitations=["The public MIDI API exposes track metadata, not clip CRUD or source paths."],
                evidence=[
                    CapabilityEvidence(
                        kind=official,
                        detail="No playlist clip enumeration surface is present in the public reference.",
                        source_url=MIDI_SCRIPTING_DOC,
                    )
                ],
            ),
            CapabilityRecord(
                capability="automation_curve_inspection_and_crud",
                status=CapabilityStatus.UNAVAILABLE,
                limitations=["Low-level event recording is not automation-clip point inspection."],
                evidence=[
                    CapabilityEvidence(
                        kind=official,
                        detail="No automation clip or point CRUD is documented for MIDI scripts.",
                        source_url=MIDI_SCRIPTING_DOC,
                    )
                ],
            ),
            CapabilityRecord(
                capability="render_selected_sections_and_stems",
                status=CapabilityStatus.UNVALIDATED,
                access_path="saved-project CLI or narrow named-menu fallback",
                limitations=[
                    "There is no public MIDI scripting render function.",
                    "CLI rendering works from saved FLP files and does not expose every selection/stem option.",
                ],
                evidence=[
                    CapabilityEvidence(
                        kind=official,
                        detail="Image-Line documents export UI and saved-project command-line rendering.",
                        source_url=EXPORT_DOC,
                    )
                ],
            ),
            CapabilityRecord(
                capability="raw_audio_buffer_access",
                status=CapabilityStatus.UNAVAILABLE,
                limitations=["Audio analysis must operate on rendered or recorded local files."],
                evidence=[
                    CapabilityEvidence(
                        kind=official,
                        detail="No audio-buffer accessor exists in the public MIDI scripting reference.",
                        source_url=MIDI_SCRIPTING_DOC,
                    )
                ],
            ),
            CapabilityRecord(
                capability="plugin_insertion_removal_from_public_midi_script",
                status=CapabilityStatus.UNAVAILABLE,
                limitations=[
                    "FL Studio 2026 contains internal Gopher tooling for this, but it is not exposed to the public MIDI-script namespace."
                ],
                evidence=[
                    CapabilityEvidence(
                        kind=static,
                        detail="The FL 2026 bundle contains internal Gopher MCP tools absent from FL 2025.",
                    ),
                    CapabilityEvidence(
                        kind=static,
                        detail=(
                            "No plug-in insertion or removal command exists in "
                            "the bridge's command allowlist, and the public MIDI "
                            "scripting namespace exposes no addEffect/removeEffect "
                            "to reach for. This is a property of the surface, not "
                            "a probe of the running host."
                        ),
                    ),
                    CapabilityEvidence(
                        kind=official,
                        detail="The changelog confirms Gopher can control features and add plug-ins, not that third-party scripts can.",
                        source_url=CHANGELOG,
                    ),
                ],
            ),
        ]
        return CapabilitiesReport(
            generated_at=_now(), connection=connection, capabilities=records
        )

    def project_summary(self) -> ProjectSummary:
        connection = self._require_compatible()
        raw = self.gateway.call("project.info")
        dirty = _dirty(raw.get("unsaved_changes"))
        warnings = []
        if "project_title" not in raw:
            warnings.append(
                "The installed bridge protocol does not yet return the project title."
            )
        if connection.bridge_mode == "legacy_unknown":
            warnings.append(
                "The running bridge predates the independent read-only lock; the MCP "
                "allowlist is active, but install and reload bridge protocol 2."
            )
        elif connection.bridge_mode == "write_test":
            warnings.append(
                "The bridge has the verified write commands enabled; this "
                "read is still a read, but the write tools can change this project."
            )
        return ProjectSummary(
            observed_at=_now(),
            connection=connection,
            project_title=str(raw.get("project_title")) if raw.get("project_title") else None,
            project_author=str(raw.get("project_author")) if raw.get("project_author") else None,
            project_genre=str(raw.get("project_genre")) if raw.get("project_genre") else None,
            tempo_bpm=_float(raw.get("tempo_bpm")),
            ppq=_int(raw.get("ppq")),
            mixer_track_count=_int(raw.get("mixer_track_count")),
            channel_count=_int(raw.get("channel_count")),
            pattern_count=_int(raw.get("pattern_count")),
            playlist_track_count=_int(raw.get("playlist_track_count")),
            dirty_flag=dirty,
            dirty_state=_dirty_name(dirty),
            undo_history_position=_int(raw.get("undo_history_position")),
            undo_history_count=_int(raw.get("undo_history_count")),
            transport=TransportState(
                playing=_bool(raw.get("playing")),
                recording=_bool(raw.get("recording")),
                song_position_normalized=_float(raw.get("song_pos")),
                song_position_display=str(raw.get("song_pos_hint") or "") or None,
                song_length_ms=_int(raw.get("song_length_ms")),
                loop_mode=_int(raw.get("loop_mode")),
            ),
            warnings=warnings,
        )

    def transport_state(self) -> TransportState:
        return self.project_summary().transport

    def selected_range(self) -> SelectedRangeObservation:
        """Return raw selection data without unverifiable meter semantics."""
        self._require_compatible()
        before = self.gateway.call("project.info")
        raw = self.gateway.call("arrangement.selection")
        after = self.gateway.call("project.info")
        first_start = _strict_raw_int(raw.get("first_raw_start"), "first_raw_start")
        first_end = _strict_raw_int(raw.get("first_raw_end"), "first_raw_end")
        first_ppq = _strict_raw_int(raw.get("first_ppq"), "first_ppq")
        second_start = _strict_raw_int(
            raw.get("second_raw_start"), "second_raw_start"
        )
        second_end = _strict_raw_int(raw.get("second_raw_end"), "second_raw_end")
        second_ppq = _strict_raw_int(raw.get("second_ppq"), "second_ppq")
        if first_ppq <= 0 or second_ppq <= 0:
            raise ValueError("FL bridge returned a non-positive project PPQ")
        if (first_start, first_end, first_ppq) != (
            second_start,
            second_end,
            second_ppq,
        ):
            raise ValueError(
                "selection endpoints or PPQ changed within the bounded observation; retry"
            )
        warnings = _observation_warnings(before, after)
        warnings.extend(
            [
                "Raw selection endpoints and project PPQ are preserved, but selection state, presence, units, and normalized ticks remain unvalidated.",
                "general.getRecPPB or the current meter cannot establish a time-signature denominator or markers across the selected span; meter-independent selection semantics remain unvalidated.",
                "Selection endpoint inclusivity for export is unknown and this observation does not authorize rendering.",
                "Playlist timeline selection is distinct from selected clips and selected Playlist tracks.",
            ]
        )
        return SelectedRangeObservation(
            observed_at=_now(),
            project_dirty_flag=_dirty(after.get("unsaved_changes")),
            raw_start_time=second_start,
            raw_end_time=second_end,
            timebase_ppq=second_ppq,
            raw_start_display_hint=(
                None
                if second_start < 0
                else _optional_hint(raw.get("start_hint"), "start_hint")
            ),
            raw_end_display_hint=(
                None
                if second_end < 0
                else _optional_hint(raw.get("end_hint"), "end_hint")
            ),
            warnings=warnings,
        )

    def list_mixer_tracks(
        self,
        *,
        only_used: bool = False,
        include_peaks: bool = False,
        max_tracks: int | None = None,
    ) -> MixerTrackList:
        self._require_compatible()
        before = self.gateway.call("project.info")
        arguments: dict[str, Any] = {
            "only_used": only_used,
            "peaks": include_peaks,
        }
        if max_tracks is not None:
            arguments["max_tracks"] = max_tracks
        raw = self.gateway.call("mixer.list", **arguments)
        after = self.gateway.call("project.info")
        warnings = _observation_warnings(before, after)
        after_dirty = _dirty(after.get("unsaved_changes"))
        if only_used:
            warnings.append(
                "only_used is a conservative heuristic and can still omit a musically "
                "relevant default-state track; use only_used=false for authority."
            )
        total = _int(raw.get("track_count"))
        scanned = _int(raw.get("scanned"))
        if total is None:
            raise ValueError("FL bridge did not report the mixer track count")
        if scanned is None:
            scanned = total
        tracks = [_track(item) for item in raw.get("tracks") or []]
        partial = scanned < total
        if partial:
            warnings.append(f"Mixer observation stopped after {scanned} of {total} tracks.")
        return MixerTrackList(
            observed_at=_now(),
            project_dirty_flag=after_dirty,
            total_track_count=total,
            scanned_track_count=scanned,
            only_used=only_used,
            tracks=tracks,
            partial=partial,
            warnings=warnings,
        )

    def inspect_mixer_track(self, track_index: int) -> MixerTrackInspection:
        self._require_compatible()
        before = self.gateway.call("project.info")
        raw = self.gateway.call("mixer.track", track=track_index)
        after = self.gateway.call("project.info")
        warnings = _observation_warnings(before, after)
        after_dirty = _dirty(after.get("unsaved_changes"))
        routes = [
            MixerRoute(
                destination_track_index=int(item["to"]),
                destination_track_name=str(item.get("name") or ""),
                level_normalized=_float(item.get("level")),
            )
            for item in raw.get("routes") or []
        ]
        eq = raw.get("eq") or {}
        bands = [
            EqBandState(
                band_index=int(item["band"]),
                gain_normalized=_float(item.get("gain")),
                gain_db=_float(item.get("gain_db")),
                frequency_normalized=_float(item.get("freq")),
                frequency_hz=_float(item.get("freq_hz")),
                bandwidth_normalized=_float(item.get("bandwidth")),
            )
            for item in eq.get("bands") or []
        ]
        return MixerTrackInspection(
            observed_at=_now(),
            project_dirty_flag=after_dirty,
            track=_track(raw),
            routes=routes,
            builtin_eq=bands,
            warnings=warnings,
        )

    def scan_loaded_plugins(self, *, only_used: bool = False) -> LoadedPluginInventory:
        mixer = self.list_mixer_tracks(only_used=only_used)
        plugins = [plugin for track in mixer.tracks for plugin in track.plugins]
        return LoadedPluginInventory(
            observed_at=mixer.observed_at,
            project_dirty_flag=mixer.project_dirty_flag,
            plugins=plugins,
            warnings=list(mixer.warnings),
        )

    def plugin_parameters(
        self,
        *,
        track_index: int,
        slot_index: int,
        limit: int = 32,
        offset: int = 0,
        name_filter: str | None = None,
    ) -> PluginParameterPage:
        self._require_compatible()
        before = self.gateway.call("project.info")
        arguments: dict[str, Any] = {
            "track": track_index,
            "slot": slot_index,
            "limit": limit,
            "offset": offset,
            "skip_padding": False,
        }
        if name_filter:
            arguments["filter"] = name_filter
        raw = self.gateway.call("plugin.params", **arguments)
        after = self.gateway.call("project.info")
        total = _int(raw.get("param_count"))
        if total is None:
            raise ValueError("FL bridge did not report the plug-in parameter count")
        scanned = min(limit, max(0, total - offset))
        next_offset = offset + scanned if offset + scanned < total else None
        warnings = _observation_warnings(before, after) + [
            "Parameter values are read-only and unprofiled; indices are not a durable plug-in identity."
        ]
        parameters = []
        for item in raw.get("params") or []:
            name = str(item.get("name") or "")
            display = str(item.get("display") or "")
            padding = not name.strip() and display.strip() in {
                "",
                "0",
                "0.000000",
                "0.0000000",
            }
            parameters.append(
                PluginParameter(
                    index=int(item["index"]),
                    reported_name=name,
                    normalized_value=_float(item.get("value")),
                    display_text=display or None,
                    display_text_available=bool(display),
                    classification="padding_candidate" if padding else "reported",
                )
            )
        plugin = PluginSlotSummary(
            track_index=track_index,
            slot_index=slot_index,
            name=str(raw.get("plugin") or ""),
            reported_parameter_count=total,
        )
        return PluginParameterPage(
            observed_at=_now(),
            project_dirty_flag=_dirty(after.get("unsaved_changes")),
            plugin=plugin,
            reported_parameter_count=total,
            offset=offset,
            scanned_count=scanned,
            returned_count=len(parameters),
            has_more=next_offset is not None,
            next_offset=next_offset,
            parameters=parameters,
            warnings=warnings,
        )

    def scan_plugin_parameters(
        self,
        *,
        track_index: int,
        slot_index: int,
        start: int | None = None,
        end: int | None = None,
        max_indices: int | None = None,
        max_results: int | None = None,
    ) -> PluginParameterScan:
        """Walk a plug-in's whole index range inside FL and return what is real.

        `plugin_parameters` pages FL's raw range, which for a VST is a padded
        maximum: about a thousand round trips to see one VST3. This
        asks the bridge to do the walk on FL's thread and drop the padding
        before it answers, so one call returns the controls that exist.

        Every returned parameter is still `safe_to_modify=False`. Knowing a
        control is real is not the same as knowing what it does.
        """
        self._require_compatible()
        before = self.gateway.call("project.info")
        arguments: dict[str, Any] = {"track": track_index, "slot": slot_index}
        for key, value in (
            ("start", start),
            ("end", end),
            ("max_indices", max_indices),
            ("max_results", max_results),
        ):
            if value is not None:
                arguments[key] = value
        raw = self.gateway.call("plugin.scan_params", **arguments)
        after = self.gateway.call("project.info")

        reported = _int(raw.get("reported_count"))
        if reported is None:
            raise ValueError("FL bridge did not report the plug-in parameter count")

        parameters = []
        for item in raw.get("params") or []:
            display = str(item.get("display") or "")
            parameters.append(
                PluginParameter(
                    index=int(item["index"]),
                    reported_name=str(item.get("name") or ""),
                    normalized_value=_float(item.get("value")),
                    display_text=display or None,
                    display_text_available=bool(display),
                    classification="reported",
                )
            )

        truncated = bool(raw.get("truncated"))
        warnings = _observation_warnings(before, after) + [
            "Parameter values are read-only and unprofiled; indices are not a "
            "durable plug-in identity."
        ]
        if truncated:
            warnings.append(
                "This scan did not examine every index FL reports; the map is "
                "partial. Stopped by: %s." % (raw.get("truncated_by") or "unknown")
            )

        return PluginParameterScan(
            observed_at=_now(),
            project_dirty_flag=_dirty(after.get("unsaved_changes")),
            plugin=PluginSlotSummary(
                track_index=track_index,
                slot_index=slot_index,
                name=str(raw.get("plugin") or ""),
                reported_parameter_count=reported,
            ),
            reported_parameter_count=reported,
            scan_start=_int(raw.get("scan_start")) or 0,
            scan_end=_int(raw.get("scan_end")) or 0,
            examined_count=_int(raw.get("examined")) or 0,
            highest_index_examined=_int(raw.get("highest_index_examined")),
            real_count=_int(raw.get("real")) or 0,
            padding_skipped=_int(raw.get("padding_skipped")) or 0,
            truncated=truncated,
            truncated_by=raw.get("truncated_by") or None,
            parameters=parameters,
            warnings=warnings,
        )

    def capture(
        self,
        *,
        only_used: bool = False,
        parameter_limit: int = 16,
        max_plugins: int = 16,
    ) -> ReadOnlyInspectionReport:
        project = self.project_summary()
        mixer = self.list_mixer_tracks(only_used=only_used)
        previews = []
        warnings = list(project.warnings) + list(mixer.warnings)
        locations = [
            (track.index, plugin.slot_index)
            for track in mixer.tracks
            for plugin in track.plugins
        ]
        for track_index, slot_index in locations[:max_plugins]:
            try:
                previews.append(
                    self.plugin_parameters(
                        track_index=track_index,
                        slot_index=slot_index,
                        limit=parameter_limit,
                    )
                )
            except (BridgeError, ValueError) as exc:
                warnings.append(
                    f"Could not preview parameters for track {track_index}, slot {slot_index}: {exc}"
                )
        if len(locations) > max_plugins:
            warnings.append(
                f"Parameter previews were capped at {max_plugins} of {len(locations)} loaded plug-ins."
            )
        return ReadOnlyInspectionReport(
            observation_id=str(uuid.uuid4()),
            observed_at=_now(),
            project=project,
            mixer=mixer,
            parameter_previews=previews,
            warnings=warnings,
        )
