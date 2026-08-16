"""High-level Track B gateways and controllers for FL Studio 2026.

The bridge commands are intentionally narrow and split into a read gateway and
a non-replayable performance gateway.  Nothing in this module registers MCP
tools or changes the installed bridge; it is the isolated Python integration
surface for the 0.12 work.

Five transport mutations are exposed.  Playback speed is intentionally omitted
because FL provides no authoritative getter with which to perform the required
later-idle-tick verification.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Protocol, cast

from pydantic import TypeAdapter

from .bridge_client import BridgeError, get_client
from .contracts import ConnectionInfo
from .readonly_inspector import IncompatibleFLStudio, connection_from_ping
from .track_b_contracts import (
    FL_COLOR_WORD_MAX,
    MAX_CHANNEL_NAME_LENGTH,
    MAX_PATTERN_LENGTH_BEATS,
    MAX_PATTERN_NAME_LENGTH,
    MAX_PATTERN_NUMBER,
    MAX_PLAYLIST_TRACK_NAME_LENGTH,
    MAX_STEP_COUNT,
    MAX_VERIFIED_STEP_COUNT,
    PLAYBACK_SPEED_OMISSION_REASON,
    ChannelGeneratorIdentity,
    ChannelIdentitySnapshot,
    ChannelList,
    ChannelMixSnapshot,
    ChannelPitchSnapshot,
    ChannelRouteSnapshot,
    ChannelSoloSnapshot,
    ChannelSummary,
    ExpectedChannelIdentityState,
    ExpectedChannelMixState,
    ExpectedChannelPitchState,
    ExpectedChannelRouteState,
    ExpectedChannelSelectionState,
    ExpectedChannelSoloState,
    ExpectedChannelTargetState,
    ExpectedLoopModeState,
    ExpectedMetronomeState,
    ExpectedPrecountState,
    ExpectedProjectHistoryState,
    ExpectedRecordingState,
    ExpectedTimeSignatureState,
    EmptyPatternSearch,
    ExpectedPatternIdentityState,
    ExpectedPatternLengthState,
    ExpectedPatternSelectionState,
    ExpectedPlaylistTrackIdentityState,
    ExpectedPlaylistTrackState,
    ExpectedPlayingState,
    ExpectedPluginParameterState,
    ExpectedSongPositionState,
    ExpectedStopState,
    ExpectedTempoState,
    LiveNoteDispatch,
    LoopMode,
    MixerEffectTarget,
    NormalizedPluginTarget,
    PluginParameterSnapshot,
    PatternIdentitySnapshot,
    PatternList,
    PatternSummary,
    PluginPresetCount,
    PluginTarget,
    ChannelGeneratorTarget,
    StepCellUpdate,
    StepCellVerification,
    StepSequenceObservation,
    StepSequenceSnapshot,
    TargetedLoadedPluginInventory,
    TargetedPluginParameter,
    TargetedPluginParameterPage,
    TargetedPluginParameterScan,
    TargetedPluginSummary,
    TrackBContract,
    PlaylistTrackIdentitySnapshot,
    PlaylistTrackList,
    PlaylistTrackStateSnapshot,
    PlaylistTrackSummary,
    ProjectHistoryObservation,
    ProjectHistorySnapshot,
    TimeSignatureSnapshot,
    VerifiedChannelIdentityWrite,
    VerifiedChannelMixWrite,
    VerifiedChannelPitchWrite,
    VerifiedChannelRouteWrite,
    VerifiedChannelSelectionWrite,
    VerifiedChannelSoloWrite,
    VerifiedLoopModeWrite,
    VerifiedMetronomeWrite,
    VerifiedPatternIdentityWrite,
    VerifiedPatternLengthWrite,
    VerifiedPatternSelectionWrite,
    VerifiedPlaylistTrackIdentityWrite,
    VerifiedPlaylistTrackStateWrite,
    VerifiedPlayingWrite,
    VerifiedPrecountWrite,
    VerifiedProjectHistoryMove,
    VerifiedRecordingWrite,
    VerifiedTargetedPluginDisplayWrite,
    VerifiedTargetedPluginOptionWrite,
    VerifiedTargetedPluginParameterWrite,
    VerifiedSongPositionWrite,
    VerifiedStepSequenceWrite,
    VerifiedStopWrite,
    VerifiedTempoWrite,
    VerifiedTimeSignatureNumeratorWrite,
    _compute_channel_fingerprint_raw,
    compute_channel_fingerprint,
    compute_step_sequence_digest,
    fl_colors_equivalent,
    model_payload,
    normalize_fl_color,
)


TRACK_B_READ_COMMANDS = frozenset(
    {
        "channels.list",
        "mixer.list",
        "plugin.params",
        "plugin.scan_params",
        "project.info",
        "project.history",
        "plugin.preset_count",
        "sequencer.get",
        "patterns.list",
        "patterns.find_empty",
        "playlist.list",
    }
)
TRACK_B_MUTATION_COMMANDS = frozenset(
    {
        "transport.set_playing",
        "transport.stop",
        "transport.set_song_position",
        "transport.set_loop_mode",
        "transport.set_tempo",
        "transport.set_recording",
        "transport.set_metronome",
        "transport.set_precount",
        "project.set_time_signature_numerator",
        "project.undo",
        "project.redo",
        "channel.set_mix",
        "channel.set_solo",
        "channel.set_pitch",
        "channel.select",
        "pattern.select",
        "pattern.set_identity",
        "pattern.set_length",
        "playlist.set_identity",
        "playlist.set_state",
        "channel.set_identity",
        "channel.route_to_mixer",
        "plugin.set_param",
        "plugin.set_param_display",
        "plugin.set_param_option",
        "sequencer.set",
        "channel.trigger_note",
    }
)
TRACK_B_MCP_TOOL_NAMES = frozenset(
    {
        "fl_set_playing",
        "fl_stop",
        "fl_set_song_position",
        "fl_set_loop_mode",
        "fl_set_tempo",
        "fl_set_recording",
        "fl_set_metronome",
        "fl_set_precount",
        "fl_set_time_signature_numerator",
        "fl_get_project_history",
        "fl_undo",
        "fl_redo",
        "fl_get_plugin_preset_count",
        "fl_list_channels",
        "fl_set_channel_mix",
        "fl_set_channel_solo",
        "fl_set_channel_pitch",
        "fl_select_channel",
        "fl_set_channel_identity",
        "fl_route_channel_to_mixer",
        "fl_get_step_sequence",
        "fl_set_step_sequence",
        "fl_trigger_note",
        "fl_list_patterns",
        "fl_find_empty_pattern",
        "fl_select_pattern",
        "fl_set_pattern_identity",
        "fl_set_pattern_length",
        "fl_list_playlist_tracks",
        "fl_set_playlist_track_identity",
        "fl_set_playlist_track_state",
    }
)
TARGET_AWARE_EXISTING_PLUGIN_TOOLS = frozenset(
    {
        "plugins_inspect_parameter_map",
        "plugins_scan_parameters",
        "plugins_scan_loaded_plugins",
        "fl_set_plugin_param",
        "fl_set_plugin_param_display",
        "fl_set_plugin_param_option",
    }
)

# FL's transport display and bridge verification both resolve tempo to three
# decimal places.  Keep every controller-side tempo proof on that same boundary
# so a bridge-verified readback cannot be rejected by a stricter facade.
TEMPO_READBACK_TOLERANCE = 1e-3
PLUGIN_PARAMETER_READBACK_TOLERANCE = 1e-4

UNVERIFIED_WARNING = (
    "UNVERIFIED: FL Studio accepted this mutation, but later-idle-tick readback "
    "did not show every requested absolute state. Nothing was replayed, retried "
    "by the client, or rolled back. Re-read the target before continuing."
)

CURRENT_PATTERN_ONLY_WARNING = (
    "FL's grid-bit API addresses only the current pattern. This observation "
    "verified that the explicitly requested pattern remained current before "
    "and after the read; the bridge must refuse a mismatch rather than switch it."
)

WRITES_DISABLED_HELP = (
    "This FL Studio bridge cannot apply Track B mutations: it reports "
    "bridge_mode={mode!r} and verified_writes_enabled={enabled!r}. Ask the "
    "connected AI client to call fl_set_write_mode with enabled=true and "
    "confirm_user_present=true after the user explicitly requests write access."
)


class BridgeLike(Protocol):
    @property
    def transport(self) -> str: ...

    def ping(self) -> dict[str, Any]: ...

    def call(self, cmd: str, **args: Any) -> dict[str, Any]: ...


class TrackBBoundaryViolation(RuntimeError):
    """A command attempted to cross a Track B allowlist boundary."""


class TrackBMutationsUnavailable(RuntimeError):
    """The live bridge cannot safely dispatch the Track B mutation surface."""


_PLUGIN_TARGET_ADAPTER = TypeAdapter(PluginTarget)


def normalize_plugin_target(
    *,
    target: PluginTarget | dict[str, Any] | None = None,
    track_index: int | None = None,
    slot_index: int | None = None,
    allow_master: bool = False,
) -> NormalizedPluginTarget:
    """Normalize a discriminated target or the legacy effect pair, never both.

    The legacy path preserves the public ``track_index`` plus ``slot_index``
    shape and its ``slot_index >= 0`` rule.  Generator callers must use the
    explicit target form; they resolve to FL's documented ``slotIndex=-1`` and
    global channel addressing without weakening the mixer-effect schema.
    """

    if type(allow_master) is not bool:
        raise ValueError("allow_master must be true or false")
    legacy_present = track_index is not None or slot_index is not None
    if target is not None and legacy_present:
        raise ValueError(
            "pass either target or the legacy track_index/slot_index pair, not both"
        )
    if target is not None:
        if allow_master:
            raise ValueError(
                "allow_master belongs inside MixerEffectTarget when target is used"
            )
        parsed = _PLUGIN_TARGET_ADAPTER.validate_python(target, strict=True)
        if isinstance(parsed, MixerEffectTarget):
            return NormalizedPluginTarget(
                kind="mixer_effect",
                bridge_index=parsed.track_index,
                track_index=parsed.track_index,
                slot_index=parsed.slot_index,
                use_global_index=False,
                allow_master=parsed.allow_master,
            )
        if not isinstance(parsed, ChannelGeneratorTarget):  # pragma: no cover
            raise ValueError("unknown plug-in target kind")
        return NormalizedPluginTarget(
            kind="channel_generator",
            bridge_index=parsed.channel_index,
            channel_index=parsed.channel_index,
            slot_index=-1,
            use_global_index=True,
            allow_master=False,
        )
    if track_index is None or slot_index is None:
        raise ValueError(
            "target or both legacy track_index and slot_index must be supplied"
        )
    track = _strict_int(track_index, "track_index", low=0)
    slot = _strict_int(slot_index, "slot_index", low=0, high=9)
    if track == 0 and not allow_master:
        raise ValueError(
            "mixer track 0 (Master) requires allow_master=true; no caller may "
            "infer that the master bus was intended"
        )
    return NormalizedPluginTarget(
        kind="mixer_effect",
        bridge_index=track,
        track_index=track,
        slot_index=slot,
        use_global_index=False,
        allow_master=allow_master,
    )


def _plugin_bridge_arguments(target: NormalizedPluginTarget) -> dict[str, Any]:
    """Return one explicit bridge shape while retaining legacy effect keys."""

    common: dict[str, Any] = {
        "target_kind": target.kind,
        "slot": target.slot_index,
        "use_global_index": target.use_global_index,
    }
    if target.kind == "mixer_effect":
        common.update(
            track=target.bridge_index,
            allow_master=target.allow_master,
        )
    else:
        common.update(channel=target.bridge_index, index_scope="global")
    return common


def _public_plugin_target(target: NormalizedPluginTarget) -> PluginTarget:
    if target.kind == "mixer_effect":
        assert target.track_index is not None
        return MixerEffectTarget(
            track_index=target.track_index,
            slot_index=target.slot_index,
            allow_master=target.allow_master,
        )
    assert target.channel_index is not None
    return ChannelGeneratorTarget(channel_index=target.channel_index)


def _echoed_plugin_target(
    payload: dict[str, Any], requested: NormalizedPluginTarget
) -> None:
    kind = payload.get("target_kind")
    # Protocol-2 effect replies predate target_kind/use_global_index. Preserve
    # compatibility for that path only; a generator must explicitly prove the
    # global addressing mode because a grouped index can name another channel.
    if kind is None and requested.kind == "mixer_effect":
        kind = "mixer_effect"
    if kind != requested.kind:
        raise ValueError(
            f"FL bridge reported target_kind={kind!r}, expected {requested.kind!r}"
        )
    field = "track" if requested.kind == "mixer_effect" else "channel"
    _echoed_index(payload, field, requested.bridge_index)
    slot = payload.get("slot")
    if type(slot) is not int or slot != requested.slot_index:
        raise ValueError(
            f"FL bridge reported slot={slot!r}, expected {requested.slot_index}"
        )
    use_global = payload.get("use_global_index")
    if use_global is None and requested.kind == "mixer_effect":
        use_global = False
    if type(use_global) is not bool or use_global != requested.use_global_index:
        raise ValueError("FL bridge did not echo the requested plug-in index scope")
    if (
        requested.kind == "channel_generator"
        and payload.get("index_scope") != "global"
    ):
        raise ValueError("FL bridge did not echo global generator index scope")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _strict_int(value: Any, label: str, *, low: int, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < low or (high is not None and value > high):
        bound = f"{low}..{high}" if high is not None else f"{low} or greater"
        raise ValueError(f"{label} must be {bound} (got {value})")
    return value


def _number(value: Any, label: str, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if number < low or number > high:
        raise ValueError(f"{label} must be within {low:g}..{high:g} (got {number:g})")
    return number


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _optional_int(value: Any) -> int | None:
    return value if type(value) is int else None


def _bridge_color(value: Any, label: str) -> int | None:
    """Normalize FL's signed color getter to the public unsigned word."""

    try:
        return normalize_fl_color(value)
    except ValueError as exc:
        raise ValueError(
            f"FL bridge returned a malformed {label} color"
        ) from exc


def _optional_bool(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _strict_bool(payload: dict[str, Any], field: str) -> bool:
    value = payload.get(field)
    if type(value) is not bool:
        raise ValueError(
            f"FL bridge did not report {field!r} as a boolean; the outcome is unknown"
        )
    return value


def _echoed_index(
    payload: dict[str, Any], field: str, requested: int, *, low: int = 0
) -> int:
    value = payload.get(field)
    if type(value) is not int or value < low or value != requested:
        raise ValueError(
            f"FL bridge reported {field}={value!r} when {requested} was requested"
        )
    return value


def _require_global_index_scope(payload: dict[str, Any], label: str) -> None:
    """Require an explicit global Channel Rack address in every bridge reply."""

    if payload.get("index_scope") != "global":
        raise ValueError(
            f"FL bridge did not explicitly report global channel indices for {label}"
        )


def _dirty(value: Any) -> int | None:
    return value if type(value) is int and value in (0, 1, 2) else None


def _command_matches(payload: dict[str, Any], command: str) -> None:
    reported = payload.get("command")
    if reported != command:
        raise ValueError(
            f"FL bridge replied to {command!r} with command={reported!r}"
        )


def _session_fingerprint(payload: dict[str, Any]) -> str | None:
    value = payload.get("session_fingerprint")
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 32 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError("FL bridge returned a malformed session fingerprint")
    return value


def _precondition_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_fingerprint": _session_fingerprint(payload),
        "session_precondition_applied": _strict_bool(
            payload, "session_precondition_applied"
        ),
        "expected_before_applied": _strict_bool(
            payload, "expected_before_applied"
        ),
    }


def _mutation_arguments(
    *,
    session_fingerprint: str | None,
    expected_before: TrackBContract | None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    if session_fingerprint is not None:
        # Validate without importing Track A's mutable contract definitions.
        if len(session_fingerprint) != 32 or any(
            char not in "0123456789abcdef" for char in session_fingerprint
        ):
            raise ValueError("session_fingerprint must be 32 lowercase hex characters")
        arguments["session_fingerprint"] = session_fingerprint
    expected = model_payload(expected_before)
    if expected is not None:
        arguments["expected_before"] = expected
    return arguments


def _summary(verified: bool, success: str, failure: str) -> tuple[str, list[str]]:
    return (success, []) if verified else (failure, [UNVERIFIED_WARNING])


def _warnings(payload: dict[str, Any], additional: list[str]) -> list[str]:
    raw = payload.get("warnings")
    bridge_warnings = (
        [str(item) for item in raw]
        if isinstance(raw, list)
        else []
    )
    return additional + bridge_warnings


def _loop_mode(value: Any, label: str) -> LoopMode | None:
    if value is None:
        return None
    if value not in {"pattern", "song"}:
        raise ValueError(f"FL bridge returned invalid {label}={value!r}")
    return cast(LoopMode, value)


class TrackBReadGateway:
    """Narrow adapter for idempotent Track B observations."""

    ALLOWED_COMMANDS = TRACK_B_READ_COMMANDS

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
            raise TrackBBoundaryViolation(
                f"bridge operation {command!r} is not in the Track B read allowlist"
            )
        result = self._client.call(command, **arguments)
        if not isinstance(result, dict):
            raise ValueError(f"FL bridge returned a malformed reply to {command!r}")
        return result


class TrackBMutationGateway:
    """Adapter for commands whose ambiguous outcomes must never be replayed."""

    ALLOWED_COMMANDS = TRACK_B_MUTATION_COMMANDS

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
            raise TrackBBoundaryViolation(
                f"bridge operation {command!r} is not in the Track B mutation allowlist"
            )
        result = self._client.call(command, **arguments)
        if not isinstance(result, dict):
            raise ValueError(f"FL bridge returned a malformed reply to {command!r}")
        return result


class _ConnectionController:
    def __init__(self, gateway: TrackBReadGateway | TrackBMutationGateway):
        self.gateway = gateway

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

    def _require_writable(
        self, session_fingerprint: str | None = None
    ) -> ConnectionInfo:
        connection = self._require_compatible()
        if not connection.verified_writes_enabled:
            raise TrackBMutationsUnavailable(
                WRITES_DISABLED_HELP.format(
                    mode=connection.bridge_mode,
                    enabled=connection.verified_writes_enabled,
                )
            )
        if not connection.bridge_provenance_verified:
            raise TrackBMutationsUnavailable(
                "Track B mutations require a running bridge whose source SHA-256 "
                "matches the packaged bridge; install and reload it before writing."
            )
        if session_fingerprint is not None:
            if (
                not isinstance(session_fingerprint, str)
                or len(session_fingerprint) != 32
                or any(char not in "0123456789abcdef" for char in session_fingerprint)
            ):
                raise ValueError(
                    "session_fingerprint must be 32 lowercase hex characters"
                )
            if connection.session_fingerprint != session_fingerprint:
                raise TrackBMutationsUnavailable(
                    "session precondition failed before dispatch; re-read live FL "
                    "Studio state before mutating it"
                )
        return connection


class TrackBInspector(_ConnectionController):
    """Typed Channel Rack and current-pattern sequencer observations."""

    def __init__(self, gateway: TrackBReadGateway | None = None):
        super().__init__(gateway or TrackBReadGateway())

    def list_channels(self) -> ChannelList:
        connection = self._require_compatible()
        raw = self.gateway.call("channels.list", global_count=True)
        command = raw.get("command")
        if command not in (None, "channels.list"):
            raise ValueError("FL bridge returned the wrong channel-list command")
        rows = raw.get("channels")
        if not isinstance(rows, list):
            raise ValueError("FL bridge returned malformed channels")
        channels: list[ChannelSummary] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("FL bridge returned a malformed channel row")
            index = _strict_int(row.get("index"), "channel index", low=0)
            _require_global_index_scope(row, "channel list")
            name = str(row.get("name") or "")
            if len(name) > 256:
                raise ValueError("FL bridge returned an oversized channel name")
            type_code = _optional_int(row.get("type"))
            kind_value = row.get("type_name", "unknown")
            allowed_kinds = {
                "sampler",
                "hybrid",
                "generator_plugin",
                "layer",
                "audio_clip",
                "automation_clip",
                "lightning",
                "unknown",
            }
            kind = str(kind_value) if kind_value in allowed_kinds else "unknown"
            raw_color = row.get("color")
            color = _bridge_color(raw_color, "channel")
            destination = _optional_int(row.get("mixer_track"))
            plugin_name = row.get("plugin")
            generator = None
            if plugin_name not in (None, ""):
                generator = ChannelGeneratorIdentity(
                    name=str(plugin_name),
                    reported_parameter_count=_optional_int(
                        row.get("reported_parameter_count")
                    ),
                )
            fingerprint = row.get("channel_fingerprint")
            computed = compute_channel_fingerprint(
                channel_index=index,
                name=name,
                channel_type_code=type_code,
                color=color,
                mixer_destination=destination,
                generator_name=None if generator is None else generator.name,
            )
            # A bridge installed immediately before this fix may have hashed
            # FL's negative signed spelling even though both spellings carry
            # the same 32 bits. Accept only that exactly recomputed legacy
            # fingerprint, then publish the canonical unsigned fingerprint.
            reported_spelling = _compute_channel_fingerprint_raw(
                channel_index=index,
                name=name,
                channel_type_code=type_code,
                color=raw_color,
                mixer_destination=destination,
                generator_name=None if generator is None else generator.name,
            )
            if fingerprint is not None and fingerprint not in {
                computed,
                reported_spelling,
            }:
                raise ValueError(
                    f"bridge returned a contradictory fingerprint for channel {index}"
                )
            channels.append(
                ChannelSummary(
                    channel_index=index,
                    name=name,
                    channel_type_code=type_code,
                    channel_type=cast(Any, kind),
                    color=color,
                    volume_normalized=_optional_float(row.get("volume")),
                    pan=_optional_float(row.get("pan")),
                    pitch_normalized=_optional_float(row.get("pitch")),
                    pitch_semitones=_optional_float(row.get("pitch_semitones")),
                    pitch_range_semitones=_optional_float(row.get("pitch_range")),
                    muted=_optional_bool(row.get("muted")),
                    soloed=_optional_bool(row.get("solo")),
                    selected=_optional_bool(row.get("selected")),
                    mixer_destination=destination,
                    generator=generator,
                    channel_fingerprint=computed,
                )
            )
        total = _strict_int(
            raw.get("channel_count", len(channels)), "channel_count", low=0
        )
        partial = raw.get("partial", len(channels) < total)
        if type(partial) is not bool:
            raise ValueError("FL bridge returned malformed partial flag")
        return ChannelList(
            observed_at=_now(),
            project_dirty_flag=_dirty(raw.get("unsaved_changes")),
            total_channel_count=total,
            scanned_channel_count=len(channels),
            channels=channels,
            partial=partial,
            warnings=_warnings(
                raw,
                list(connection.warnings) + [
                    "Channel identity is observation-scoped because FL exposes no "
                    "durable Channel Rack UUID; use the fingerprint only as a "
                    "same-session mutation guard."
                ],
            ),
        )

    def list_patterns(self) -> PatternList:
        connection = self._require_compatible()
        raw = self.gateway.call("patterns.list")
        if raw.get("command") not in (None, "patterns.list"):
            raise ValueError("FL bridge returned the wrong pattern-list command")
        current = _strict_int(
            raw.get("current_pattern"),
            "current_pattern",
            low=1,
            high=MAX_PATTERN_NUMBER,
        )
        count = _strict_int(
            raw.get("pattern_count"),
            "pattern_count",
            low=0,
            high=MAX_PATTERN_NUMBER,
        )
        maximum = _strict_int(
            raw.get("pattern_max"),
            "pattern_max",
            low=1,
            high=MAX_PATTERN_NUMBER,
        )
        rows = raw.get("patterns")
        if not isinstance(rows, list) or len(rows) != count:
            raise ValueError("FL bridge returned malformed pattern rows")
        parsed: list[PatternSummary] = []
        for expected_number, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise ValueError("FL bridge returned a malformed pattern row")
            number = _strict_int(
                row.get("pattern"),
                "pattern number",
                low=1,
                high=MAX_PATTERN_NUMBER,
            )
            if number != expected_number:
                raise ValueError("FL bridge returned non-canonical pattern ordering")
            name = row.get("name")
            if not isinstance(name, str) or len(name) > MAX_PATTERN_NAME_LENGTH:
                raise ValueError("FL bridge returned a malformed pattern name")
            length = _optional_int(row.get("length"))
            parsed.append(
                PatternSummary(
                    pattern_number=number,
                    name=name,
                    color=_bridge_color(row.get("color"), "pattern"),
                    length_beats=length,
                    current=_strict_bool(row, "current"),
                    selected_in_picker=_optional_bool(row.get("selected")),
                    default_empty=_optional_bool(row.get("default")),
                )
            )
        return PatternList(
            observed_at=_now(),
            current_pattern_number=current,
            reported_pattern_count=count,
            maximum_pattern_number=maximum,
            patterns=parsed,
            project_dirty_flag=_dirty(raw.get("unsaved_changes")),
            warnings=_warnings(raw, list(connection.warnings)),
        )

    def find_empty_pattern(self, *, start_pattern_number: int = 1) -> EmptyPatternSearch:
        start = _strict_int(
            start_pattern_number,
            "start_pattern_number",
            low=1,
            high=MAX_PATTERN_NUMBER,
        )
        connection = self._require_compatible()
        raw = self.gateway.call("patterns.find_empty", start=start)
        if raw.get("command") not in (None, "patterns.find_empty"):
            raise ValueError("FL bridge returned the wrong empty-pattern command")
        echoed = _strict_int(
            raw.get("start"), "start", low=1, high=MAX_PATTERN_NUMBER
        )
        if echoed != start:
            raise ValueError("FL bridge searched from a different pattern number")
        found_raw = raw.get("empty_pattern")
        found = (
            None
            if found_raw is None
            else _strict_int(
                found_raw,
                "empty_pattern",
                low=start,
                high=MAX_PATTERN_NUMBER,
            )
        )
        scanned = _strict_int(
            raw.get("scanned"), "scanned", low=0, high=MAX_PATTERN_NUMBER
        )
        unchanged = _strict_bool(raw, "current_pattern_unchanged")
        return EmptyPatternSearch(
            observed_at=_now(),
            start_pattern_number=start,
            empty_pattern_number=found,
            scanned_pattern_count=scanned,
            current_pattern_unchanged=unchanged,
            project_dirty_flag=_dirty(raw.get("unsaved_changes")),
            warnings=_warnings(raw, list(connection.warnings)),
        )

    def list_playlist_tracks(self) -> PlaylistTrackList:
        connection = self._require_compatible()
        raw = self.gateway.call("playlist.list")
        if raw.get("command") not in (None, "playlist.list"):
            raise ValueError("FL bridge returned the wrong Playlist-list command")
        count = _strict_int(raw.get("track_count"), "track_count", low=0)
        rows = raw.get("tracks")
        if not isinstance(rows, list) or len(rows) != count:
            raise ValueError("FL bridge returned malformed Playlist rows")
        parsed: list[PlaylistTrackSummary] = []
        for expected_index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise ValueError("FL bridge returned a malformed Playlist row")
            index = _strict_int(row.get("track"), "track", low=1)
            if index != expected_index:
                raise ValueError("FL bridge returned non-canonical Playlist ordering")
            name = row.get("name")
            if not isinstance(name, str) or len(name) > MAX_PLAYLIST_TRACK_NAME_LENGTH:
                raise ValueError("FL bridge returned a malformed Playlist track name")
            parsed.append(
                PlaylistTrackSummary(
                    track_index=index,
                    name=name,
                    color=_bridge_color(row.get("color"), "Playlist track"),
                    muted=_optional_bool(row.get("muted")),
                    soloed=_optional_bool(row.get("solo")),
                    selected=_optional_bool(row.get("selected")),
                    activity_level=_optional_float(row.get("activity")),
                )
            )
        return PlaylistTrackList(
            observed_at=_now(),
            total_track_count=count,
            tracks=parsed,
            project_dirty_flag=_dirty(raw.get("unsaved_changes")),
            warnings=_warnings(raw, list(connection.warnings)),
        )

    def project_history(self) -> ProjectHistoryObservation:
        connection = self._require_compatible()
        raw = self.gateway.call("project.history")
        _command_matches(raw, "project.history")
        return ProjectHistoryObservation(
            observed_at=_now(),
            history=_project_history_snapshot(raw),
            warnings=_warnings(
                raw,
                list(connection.warnings)
                + [
                    "Undo-history positions are live project-session coordinates, "
                    "not durable identifiers; re-read before moving history."
                ],
            ),
        )

    def plugin_preset_count(
        self,
        *,
        target: PluginTarget | dict[str, Any] | None = None,
        track_index: int | None = None,
        slot_index: int | None = None,
        allow_master: bool = False,
    ) -> PluginPresetCount:
        resolved = normalize_plugin_target(
            target=target,
            track_index=track_index,
            slot_index=slot_index,
            allow_master=allow_master,
        )
        connection = self._require_compatible()
        raw = self.gateway.call(
            "plugin.preset_count", **_plugin_bridge_arguments(resolved)
        )
        _command_matches(raw, "plugin.preset_count")
        _echoed_plugin_target(raw, resolved)
        count = _strict_int(raw.get("preset_count"), "preset_count", low=0)
        parameter_count = _strict_int(
            raw.get("param_count"), "reported parameter count", low=0
        )
        return PluginPresetCount(
            observed_at=_now(),
            plugin=_targeted_plugin_summary(raw, resolved, parameter_count),
            preset_count=count,
            project_dirty_flag=_dirty(raw.get("unsaved_changes")),
            warnings=_warnings(
                raw,
                list(connection.warnings)
                + [
                    "FL exposes a preset count but no authoritative current-preset "
                    "index/name getter, so stable next/previous mutations are omitted."
                ],
            ),
        )

    def scan_loaded_plugins(
        self, *, only_used: bool = False
    ) -> TargetedLoadedPluginInventory:
        """List mixer effects and every Channel Rack generator in one inventory."""

        if type(only_used) is not bool:
            raise ValueError("only_used must be true or false")
        connection = self._require_compatible()
        before = self.gateway.call("project.info")
        mixer_raw = self.gateway.call(
            "mixer.list", only_used=only_used, peaks=False
        )
        channels_raw = self.gateway.call("channels.list", global_count=True)
        after = self.gateway.call("project.info")
        plugins: list[TargetedPluginSummary] = []
        tracks = mixer_raw.get("tracks")
        if not isinstance(tracks, list):
            raise ValueError("FL bridge returned malformed mixer tracks")
        for track_row in tracks:
            if not isinstance(track_row, dict):
                raise ValueError("FL bridge returned a malformed mixer row")
            track = _strict_int(track_row.get("index"), "track index", low=0)
            slots = track_row.get("plugins")
            if not isinstance(slots, list):
                raise ValueError("FL bridge returned malformed effect slots")
            for slot_row in slots:
                if not isinstance(slot_row, dict):
                    raise ValueError("FL bridge returned a malformed effect slot")
                slot = _strict_int(
                    slot_row.get("slot"), "slot index", low=0, high=9
                )
                plugins.append(
                    TargetedPluginSummary(
                        target=MixerEffectTarget(
                            track_index=track,
                            slot_index=slot,
                            allow_master=track == 0,
                        ),
                        name=str(slot_row.get("name") or ""),
                        user_name=(
                            None
                            if slot_row.get("user_name") in (None, "")
                            else str(slot_row["user_name"])
                        ),
                        reported_parameter_count=_optional_int(
                            slot_row.get("param_count")
                        ),
                        mix_level_normalized=_optional_float(
                            slot_row.get("mix_level")
                        ),
                    )
                )
        channel_rows = channels_raw.get("channels")
        if not isinstance(channel_rows, list):
            raise ValueError("FL bridge returned malformed channel generators")
        for row in channel_rows:
            if not isinstance(row, dict):
                raise ValueError("FL bridge returned a malformed channel row")
            _require_global_index_scope(row, "generator inventory")
            plugin_name = row.get("plugin")
            if plugin_name in (None, ""):
                continue
            channel = _strict_int(row.get("index"), "channel index", low=0)
            plugins.append(
                TargetedPluginSummary(
                    target=ChannelGeneratorTarget(channel_index=channel),
                    name=str(plugin_name),
                    user_name=(
                        None
                        if row.get("plugin_user_name") in (None, "")
                        else str(row["plugin_user_name"])
                    ),
                    reported_parameter_count=_optional_int(
                        row.get("reported_parameter_count")
                    ),
                )
            )
        warnings = list(connection.warnings) + _project_observation_warnings(
            before, after
        )
        warnings.append(
            "Generator targets use global Channel Rack indices and slotIndex=-1; "
            "mixer-effect slots remain constrained to 0..9."
        )
        if only_used:
            warnings.append(
                "only_used filters mixer tracks; every Channel Rack row is a "
                "loaded generator and is retained."
            )
        return TargetedLoadedPluginInventory(
            observed_at=_now(),
            project_dirty_flag=_dirty(after.get("unsaved_changes")),
            plugins=plugins,
            warnings=warnings,
        )

    def plugin_parameters(
        self,
        *,
        target: PluginTarget | dict[str, Any] | None = None,
        track_index: int | None = None,
        slot_index: int | None = None,
        allow_master: bool = False,
        limit: int = 32,
        offset: int = 0,
        name_filter: str | None = None,
    ) -> TargetedPluginParameterPage:
        resolved = normalize_plugin_target(
            target=target,
            track_index=track_index,
            slot_index=slot_index,
            allow_master=allow_master,
        )
        page_limit = _strict_int(limit, "limit", low=1, high=512)
        page_offset = _strict_int(offset, "offset", low=0)
        if name_filter is not None:
            if not isinstance(name_filter, str):
                raise ValueError("name_filter must be text")
            name_filter = name_filter.strip()
            if len(name_filter) > 256:
                raise ValueError("name_filter must be at most 256 characters")
        connection = self._require_compatible()
        before = self.gateway.call("project.info")
        arguments = _plugin_bridge_arguments(resolved)
        arguments.update(
            limit=page_limit,
            offset=page_offset,
            skip_padding=False,
        )
        if name_filter:
            arguments["filter"] = name_filter
        raw = self.gateway.call("plugin.params", **arguments)
        if raw.get("command") not in (None, "plugin.params"):
            raise ValueError("FL bridge returned the wrong plug-in page command")
        _echoed_plugin_target(raw, resolved)
        after = self.gateway.call("project.info")
        total = _strict_int(
            raw.get("param_count"), "reported parameter count", low=0
        )
        scanned = min(page_limit, max(0, total - page_offset))
        next_offset = page_offset + scanned if page_offset + scanned < total else None
        parameters = _targeted_parameters(raw.get("params"), padding_candidates=True)
        warnings = list(connection.warnings) + _project_observation_warnings(
            before, after
        )
        warnings.append(
            "Parameter values are read-only and unprofiled; indices are not a "
            "durable plug-in identity."
        )
        return TargetedPluginParameterPage(
            observed_at=_now(),
            project_dirty_flag=_dirty(after.get("unsaved_changes")),
            plugin=_targeted_plugin_summary(raw, resolved, total),
            reported_parameter_count=total,
            offset=page_offset,
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
        target: PluginTarget | dict[str, Any] | None = None,
        track_index: int | None = None,
        slot_index: int | None = None,
        allow_master: bool = False,
        start: int | None = None,
        end: int | None = None,
        max_indices: int | None = None,
        max_results: int | None = None,
    ) -> TargetedPluginParameterScan:
        resolved = normalize_plugin_target(
            target=target,
            track_index=track_index,
            slot_index=slot_index,
            allow_master=allow_master,
        )
        bounds: dict[str, int] = {}
        if start is not None:
            bounds["start"] = _strict_int(start, "start", low=0)
        if end is not None:
            bounds["end"] = _strict_int(end, "end", low=0)
        if start is not None and end is not None and end < start:
            raise ValueError("end must be greater than or equal to start")
        if max_indices is not None:
            bounds["max_indices"] = _strict_int(
                max_indices, "max_indices", low=1, high=8192
            )
        if max_results is not None:
            bounds["max_results"] = _strict_int(
                max_results, "max_results", low=1, high=8192
            )
        connection = self._require_compatible()
        before = self.gateway.call("project.info")
        arguments = _plugin_bridge_arguments(resolved)
        arguments.update(bounds)
        raw = self.gateway.call("plugin.scan_params", **arguments)
        _command_matches(raw, "plugin.scan_params")
        _echoed_plugin_target(raw, resolved)
        after = self.gateway.call("project.info")
        reported = _strict_int(
            raw.get("reported_count"), "reported parameter count", low=0
        )
        parameters = _targeted_parameters(raw.get("params"), padding_candidates=False)
        truncated = raw.get("truncated")
        if type(truncated) is not bool:
            raise ValueError("FL bridge returned malformed truncated flag")
        stopped = raw.get("truncated_by")
        if stopped not in (None, "", "max_indices", "max_results", "start", "end"):
            raise ValueError("FL bridge returned an unknown scan truncation reason")
        warnings = list(connection.warnings) + _project_observation_warnings(
            before, after
        )
        warnings.append(
            "Parameter values are read-only and unprofiled; indices are not a "
            "durable plug-in identity."
        )
        if truncated:
            warnings.append(
                f"This parameter map is partial; stopped by {stopped or 'unknown'}."
            )
        return TargetedPluginParameterScan(
            observed_at=_now(),
            project_dirty_flag=_dirty(after.get("unsaved_changes")),
            plugin=_targeted_plugin_summary(raw, resolved, reported),
            reported_parameter_count=reported,
            scan_start=_strict_int(raw.get("scan_start"), "scan_start", low=0),
            scan_end=_strict_int(raw.get("scan_end"), "scan_end", low=0),
            examined_count=_strict_int(raw.get("examined"), "examined", low=0),
            highest_index_examined=(
                None
                if raw.get("highest_index_examined") is None
                else _strict_int(
                    raw.get("highest_index_examined"),
                    "highest_index_examined",
                    low=0,
                )
            ),
            real_count=len(parameters),
            padding_skipped=_strict_int(
                raw.get("padding_skipped"), "padding_skipped", low=0
            ),
            truncated=truncated,
            truncated_by=None if stopped in (None, "") else cast(Any, stopped),
            parameters=parameters,
            warnings=warnings,
        )

    def get_step_sequence(
        self, *, pattern_number: int, channel_index: int
    ) -> StepSequenceObservation:
        pattern = _strict_int(pattern_number, "pattern_number", low=1)
        channel = _strict_int(channel_index, "channel_index", low=0)
        connection = self._require_compatible()
        raw = self.gateway.call(
            "sequencer.get",
            pattern=pattern,
            channel=channel,
            index_scope="global",
        )
        _command_matches(raw, "sequencer.get")
        snapshot = _step_snapshot(raw, pattern=pattern, channel=channel)
        return StepSequenceObservation(
            **snapshot.model_dump(),
            observed_at=_now(),
            project_dirty_flag=_dirty(raw.get("unsaved_changes")),
            warnings=_warnings(
                raw, list(connection.warnings) + [CURRENT_PATTERN_ONLY_WARNING]
            ),
        )


class TrackBController(_ConnectionController):
    """Typed controller for readback-verified Track B mutations."""

    def __init__(self, gateway: TrackBMutationGateway | None = None):
        super().__init__(gateway or TrackBMutationGateway())

    def _call(
        self,
        command: str,
        arguments: dict[str, Any],
        *,
        session_fingerprint: str | None,
        expected_before: TrackBContract | None,
    ) -> dict[str, Any]:
        connection = self._require_writable(session_fingerprint)
        arguments.update(
            _mutation_arguments(
                session_fingerprint=session_fingerprint,
                expected_before=expected_before,
            )
        )
        raw = self.gateway.call(command, **arguments)
        _command_matches(raw, command)
        echoed = _session_fingerprint(raw)
        if echoed is None or echoed != connection.session_fingerprint:
            raise ValueError(
                "FL bridge session changed between the pre-write handshake and "
                "the mutation reply"
            )
        if _strict_bool(raw, "session_precondition_applied") != (
            session_fingerprint is not None
        ):
            raise ValueError(
                "FL bridge reported contradictory session-precondition metadata"
            )
        if command != "sequencer.set" and _strict_bool(
            raw, "expected_before_applied"
        ) != (expected_before is not None):
            raise ValueError(
                "FL bridge reported contradictory expected-before metadata"
            )
        return raw

    def set_playing(
        self,
        *,
        playing: bool,
        session_fingerprint: str | None = None,
        expected_before: ExpectedPlayingState | None = None,
    ) -> VerifiedPlayingWrite:
        if type(playing) is not bool:
            raise ValueError("playing must be true or false; this is an absolute state")
        raw = self._call(
            "transport.set_playing",
            {"playing": playing},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        verified = _strict_bool(raw, "verified")
        after = _optional_bool(raw.get("after"))
        _require_verified_value(
            reported=verified,
            matches=after is playing,
            label="playing state",
        )
        summary, warnings = _summary(
            verified,
            f"FL read playing={after!r} on a later idle tick, matching the request.",
            f"FL read playing={after!r}, not the requested {playing!r}.",
        )
        return VerifiedPlayingWrite(
            applied_at=_now(),
            verified=verified,
            verification_summary=summary,
            requested_playing=playing,
            before_playing=_optional_bool(raw.get("before")),
            after_playing=after,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def stop(
        self,
        *,
        session_fingerprint: str | None = None,
        expected_before: ExpectedStopState | None = None,
    ) -> VerifiedStopWrite:
        raw = self._call(
            "transport.stop",
            {"playing": False, "position": 0.0},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        fields = _verified_fields(raw)
        playing_verified = _strict_field_bool(fields, "playing")
        position_verified = _strict_field_bool(fields, "position")
        verified = _strict_bool(raw, "verified")
        after = raw.get("after") if isinstance(raw.get("after"), dict) else {}
        before = raw.get("before") if isinstance(raw.get("before"), dict) else {}
        after_playing = _optional_bool(after.get("playing"))
        after_position = _optional_float(after.get("position"))
        _require_verified_value(
            reported=playing_verified,
            matches=after_playing is False,
            label="stopped playing state",
        )
        _require_verified_value(
            reported=position_verified,
            matches=after_position is not None and abs(after_position) <= 1e-4,
            label="rewound song position",
        )
        summary, warnings = _summary(
            verified,
            "FL read the transport stopped at normalized position 0 on a later tick.",
            "FL did not read back both stopped playback and normalized position 0.",
        )
        return VerifiedStopWrite(
            applied_at=_now(),
            verified=verified,
            verification_summary=summary,
            before_playing=_optional_bool(before.get("playing")),
            after_playing=after_playing,
            before_song_position_normalized=_optional_float(before.get("position")),
            after_song_position_normalized=after_position,
            playing_verified=playing_verified,
            position_verified=position_verified,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_song_position(
        self,
        *,
        position_normalized: float,
        tolerance: float = 0.0001,
        session_fingerprint: str | None = None,
        expected_before: ExpectedSongPositionState | None = None,
    ) -> VerifiedSongPositionWrite:
        position = _number(
            position_normalized, "position_normalized", low=0.0, high=1.0
        )
        tolerance_value = _number(tolerance, "tolerance", low=0.0, high=0.05)
        raw = self._call(
            "transport.set_song_position",
            {"position": position, "tolerance": tolerance_value},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        verified = _strict_bool(raw, "verified")
        after = _optional_float(raw.get("after"))
        _require_verified_value(
            reported=verified,
            matches=(
                after is not None and abs(after - position) <= tolerance_value
            ),
            label="song position",
        )
        summary, warnings = _summary(
            verified,
            f"FL read the playhead back at normalized position {after!r}.",
            f"FL read the playhead back at {after!r}, outside the requested tolerance.",
        )
        return VerifiedSongPositionWrite(
            applied_at=_now(),
            verified=verified,
            verification_summary=summary,
            requested_song_position_normalized=position,
            tolerance=tolerance_value,
            before_song_position_normalized=_optional_float(raw.get("before")),
            after_song_position_normalized=after,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_loop_mode(
        self,
        *,
        loop_mode: LoopMode,
        session_fingerprint: str | None = None,
        expected_before: ExpectedLoopModeState | None = None,
    ) -> VerifiedLoopModeWrite:
        if loop_mode not in {"pattern", "song"}:
            raise ValueError("loop_mode must be 'pattern' or 'song'")
        raw = self._call(
            "transport.set_loop_mode",
            {"loop_mode": loop_mode},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        verified = _strict_bool(raw, "verified")
        after = _loop_mode(raw.get("after"), "after")
        _require_verified_value(
            reported=verified,
            matches=after == loop_mode,
            label="loop mode",
        )
        summary, warnings = _summary(
            verified,
            f"FL read loop mode back as {after!r} on a later idle tick.",
            f"FL read loop mode back as {after!r}, not {loop_mode!r}.",
        )
        return VerifiedLoopModeWrite(
            applied_at=_now(),
            verified=verified,
            verification_summary=summary,
            requested_loop_mode=loop_mode,
            before_loop_mode=_loop_mode(raw.get("before"), "before"),
            after_loop_mode=after,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_tempo(
        self,
        *,
        tempo_bpm: float,
        session_fingerprint: str | None = None,
        expected_before: ExpectedTempoState | None = None,
    ) -> VerifiedTempoWrite:
        tempo = _number(tempo_bpm, "tempo_bpm", low=10.0, high=522.0)
        raw = self._call(
            "transport.set_tempo",
            {"tempo_bpm": tempo},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        verified = _strict_bool(raw, "verified")
        after = _optional_float(raw.get("after"))
        _require_verified_value(
            reported=verified,
            matches=(
                after is not None
                and abs(after - tempo) <= TEMPO_READBACK_TOLERANCE
            ),
            label="tempo",
        )
        summary, warnings = _summary(
            verified,
            f"FL read tempo back at {after!r} BPM on a later idle tick.",
            f"FL read tempo back at {after!r} BPM, not {tempo!r} BPM.",
        )
        return VerifiedTempoWrite(
            applied_at=_now(),
            verified=verified,
            verification_summary=summary,
            requested_tempo_bpm=tempo,
            before_tempo_bpm=_optional_float(raw.get("before")),
            after_tempo_bpm=after,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_recording(
        self,
        *,
        recording: bool,
        session_fingerprint: str | None = None,
        expected_before: ExpectedRecordingState | None = None,
    ) -> VerifiedRecordingWrite:
        if type(recording) is not bool:
            raise ValueError("recording must be true or false")
        raw = self._call(
            "transport.set_recording",
            {"recording": recording},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        verified = _strict_bool(raw, "verified")
        after = _optional_bool(raw.get("after"))
        _require_verified_value(
            reported=verified,
            matches=after is recording,
            label="recording state",
        )
        summary, warnings = _summary(
            verified,
            f"FL read recording={after!r} on a later idle tick.",
            f"FL read recording={after!r}, not {recording!r}.",
        )
        return VerifiedRecordingWrite(
            applied_at=_now(),
            requested_recording=recording,
            before_recording=_optional_bool(raw.get("before")),
            after_recording=after,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def _set_transport_option(
        self,
        *,
        command: str,
        enabled: bool,
        session_fingerprint: str | None,
        expected_before: TrackBContract | None,
    ) -> tuple[dict[str, Any], bool, bool | None, str, list[str]]:
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false")
        raw = self._call(
            command,
            {"enabled": enabled},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        verified = _strict_bool(raw, "verified")
        after = _optional_bool(raw.get("after"))
        _require_verified_value(
            reported=verified,
            matches=after is enabled,
            label=command,
        )
        summary, warnings = _summary(
            verified,
            f"FL read enabled={after!r} for {command} on a later idle tick.",
            f"FL read enabled={after!r} for {command}, not {enabled!r}.",
        )
        return raw, verified, after, summary, warnings

    def set_metronome(
        self,
        *,
        enabled: bool,
        session_fingerprint: str | None = None,
        expected_before: ExpectedMetronomeState | None = None,
    ) -> VerifiedMetronomeWrite:
        raw, verified, after, summary, warnings = self._set_transport_option(
            command="transport.set_metronome",
            enabled=enabled,
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        return VerifiedMetronomeWrite(
            applied_at=_now(),
            requested_enabled=enabled,
            before_enabled=_optional_bool(raw.get("before")),
            after_enabled=after,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_precount(
        self,
        *,
        enabled: bool,
        session_fingerprint: str | None = None,
        expected_before: ExpectedPrecountState | None = None,
    ) -> VerifiedPrecountWrite:
        raw, verified, after, summary, warnings = self._set_transport_option(
            command="transport.set_precount",
            enabled=enabled,
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        return VerifiedPrecountWrite(
            applied_at=_now(),
            requested_enabled=enabled,
            before_enabled=_optional_bool(raw.get("before")),
            after_enabled=after,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_time_signature_numerator(
        self,
        *,
        numerator: int,
        session_fingerprint: str | None = None,
        expected_before: ExpectedTimeSignatureState | None = None,
    ) -> VerifiedTimeSignatureNumeratorWrite:
        wanted = _strict_int(numerator, "numerator", low=1, high=32)
        raw = self._call(
            "project.set_time_signature_numerator",
            {"numerator": wanted},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        before = _time_signature_snapshot(raw.get("before"))
        after = _time_signature_snapshot(raw.get("after"))
        verified = _strict_bool(raw, "verified")
        _require_verified_value(
            reported=verified,
            matches=after.numerator == wanted,
            label="time-signature numerator",
        )
        summary, warnings = _summary(
            verified,
            f"FL read a {wanted}-beat bar from getRecPPB/getRecPPQ on a later tick.",
            "FL did not read back the requested time-signature numerator.",
        )
        warnings.append(
            "FL exposes no denominator getter; this stable tool changes and proves "
            "the numerator only."
        )
        return VerifiedTimeSignatureNumeratorWrite(
            applied_at=_now(),
            requested_numerator=wanted,
            before=before,
            after=after,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def _move_project_history(
        self,
        *,
        direction: str,
        session_fingerprint: str | None,
        expected_before: ExpectedProjectHistoryState | None,
    ) -> VerifiedProjectHistoryMove:
        if direction not in {"undo", "redo"}:
            raise ValueError("history direction must be undo or redo")
        command = "project." + direction
        raw = self._call(
            command,
            {},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        before = _project_history_snapshot(raw.get("before"))
        after = _project_history_snapshot(raw.get("after"))
        requested = _strict_int(
            raw.get("requested_position"), "requested_position", low=0
        )
        verified = _strict_bool(raw, "verified")
        _require_verified_value(
            reported=verified,
            matches=after.position == requested,
            label="undo-history position",
        )
        summary, warnings = _summary(
            verified,
            f"FL moved {direction} to absolute history position {requested}.",
            f"FL did not read back history position {requested} after {direction}.",
        )
        return VerifiedProjectHistoryMove(
            bridge_command=cast(Any, command),
            applied_at=_now(),
            direction=cast(Any, direction),
            requested_position=requested,
            before=before,
            after=after,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def undo(
        self,
        *,
        session_fingerprint: str | None = None,
        expected_before: ExpectedProjectHistoryState | None = None,
    ) -> VerifiedProjectHistoryMove:
        return self._move_project_history(
            direction="undo",
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )

    def redo(
        self,
        *,
        session_fingerprint: str | None = None,
        expected_before: ExpectedProjectHistoryState | None = None,
    ) -> VerifiedProjectHistoryMove:
        return self._move_project_history(
            direction="redo",
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )

    def set_channel_mix(
        self,
        *,
        channel_index: int,
        volume_normalized: float | None = None,
        pan: float | None = None,
        muted: bool | None = None,
        session_fingerprint: str | None = None,
        expected_before: ExpectedChannelMixState | None = None,
    ) -> VerifiedChannelMixWrite:
        channel = _strict_int(channel_index, "channel_index", low=0)
        volume = (
            None
            if volume_normalized is None
            else _number(volume_normalized, "volume_normalized", low=0.0, high=1.0)
        )
        pan_value = None if pan is None else _number(pan, "pan", low=-1.0, high=1.0)
        if muted is not None and type(muted) is not bool:
            raise ValueError("muted must be true or false")
        if volume is None and pan_value is None and muted is None:
            raise ValueError("volume_normalized, pan, muted, or a combination is required")
        arguments: dict[str, Any] = {
            "channel": channel,
            "index_scope": "global",
        }
        if volume is not None:
            arguments["volume"] = volume
        if pan_value is not None:
            arguments["pan"] = pan_value
        if muted is not None:
            arguments["muted"] = muted
        raw = self._call(
            "channel.set_mix",
            arguments,
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        _echoed_index(raw, "channel", channel)
        _require_global_index_scope(raw, "channel mix mutation")
        fields = _verified_fields(raw)
        volume_verified = None if volume is None else _strict_field_bool(fields, "volume")
        pan_verified = None if pan_value is None else _strict_field_bool(fields, "pan")
        mute_verified = None if muted is None else _strict_field_bool(fields, "muted")
        verified = _strict_bool(raw, "verified")
        before = _channel_mix_snapshot(raw.get("before"))
        after = _channel_mix_snapshot(raw.get("after"))
        proofs = [
            proof
            for requested, proof in (
                (volume, volume_verified),
                (pan_value, pan_verified),
                (muted, mute_verified),
            )
            if requested is not None
        ]
        _require_aggregate_verified(
            reported=verified,
            expected=all(proof is True for proof in proofs),
            label="channel mix aggregate",
        )
        if volume is not None:
            _require_verified_value(
                reported=volume_verified is True,
                matches=(
                    after.volume_normalized is not None
                    and abs(after.volume_normalized - volume) <= 1e-4
                ),
                label="channel volume",
            )
        if pan_value is not None:
            _require_verified_value(
                reported=pan_verified is True,
                matches=after.pan is not None and abs(after.pan - pan_value) <= 1e-4,
                label="channel pan",
            )
        if muted is not None:
            _require_verified_value(
                reported=mute_verified is True,
                matches=after.muted is muted,
                label="channel mute",
            )
        summary, warnings = _summary(
            verified,
            "FL read every requested channel-mix field back on a later idle tick.",
            "At least one requested channel-mix field did not read back as requested.",
        )
        return VerifiedChannelMixWrite(
            applied_at=_now(),
            channel_index=channel,
            verified=verified,
            verification_summary=summary,
            requested_volume_normalized=volume,
            requested_pan=pan_value,
            requested_muted=muted,
            before=before,
            after=after,
            volume_verified=volume_verified,
            pan_verified=pan_verified,
            mute_verified=mute_verified,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_channel_solo(
        self,
        *,
        channel_index: int,
        soloed: bool,
        session_fingerprint: str | None = None,
        expected_before: ExpectedChannelSoloState | None = None,
    ) -> VerifiedChannelSoloWrite:
        channel = _strict_int(channel_index, "channel_index", low=0)
        if type(soloed) is not bool:
            raise ValueError("soloed must be true or false")
        raw = self._call(
            "channel.set_solo",
            {"channel": channel, "soloed": soloed, "index_scope": "global"},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        _echoed_index(raw, "channel", channel)
        _require_global_index_scope(raw, "channel solo mutation")
        verified = _strict_bool(raw, "verified")
        before = _channel_solo_snapshot(raw.get("before"))
        after = _channel_solo_snapshot(raw.get("after"))
        _require_verified_value(
            reported=verified,
            matches=after.soloed is soloed,
            label="channel solo",
        )
        summary, warnings = _summary(
            verified,
            "FL read the requested channel solo state back on a later idle tick.",
            "FL did not read the requested channel solo state back.",
        )
        return VerifiedChannelSoloWrite(
            applied_at=_now(),
            channel_index=channel,
            requested_soloed=soloed,
            before=before,
            after=after,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_channel_pitch(
        self,
        *,
        channel_index: int,
        pitch_normalized: float,
        session_fingerprint: str | None = None,
        expected_before: ExpectedChannelPitchState | None = None,
    ) -> VerifiedChannelPitchWrite:
        channel = _strict_int(channel_index, "channel_index", low=0)
        pitch = _number(
            pitch_normalized, "pitch_normalized", low=-1.0, high=1.0
        )
        raw = self._call(
            "channel.set_pitch",
            {"channel": channel, "pitch": pitch, "index_scope": "global"},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        _echoed_index(raw, "channel", channel)
        _require_global_index_scope(raw, "channel pitch mutation")
        verified = _strict_bool(raw, "verified")
        before = _channel_pitch_snapshot(raw.get("before"))
        after = _channel_pitch_snapshot(raw.get("after"))
        _require_verified_value(
            reported=verified,
            matches=(
                after.pitch_normalized is not None
                and abs(after.pitch_normalized - pitch) <= 1e-4
            ),
            label="channel pitch",
        )
        summary, warnings = _summary(
            verified,
            "FL read channel pitch back on a later idle tick at the requested value.",
            "FL did not read channel pitch back at the requested value.",
        )
        return VerifiedChannelPitchWrite(
            applied_at=_now(),
            channel_index=channel,
            requested_pitch_normalized=pitch,
            before=before,
            after=after,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def select_channel(
        self,
        *,
        channel_index: int,
        session_fingerprint: str | None = None,
        expected_before: ExpectedChannelSelectionState | None = None,
    ) -> VerifiedChannelSelectionWrite:
        channel = _strict_int(channel_index, "channel_index", low=0)
        raw = self._call(
            "channel.select",
            {"channel": channel, "index_scope": "global", "exclusive": True},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        _echoed_index(raw, "channel", channel)
        _require_global_index_scope(raw, "channel selection mutation")
        if raw.get("exclusive") is not True:
            raise ValueError("FL bridge did not report exclusive channel selection")
        before = _selected_channel_indices(raw.get("before"), "before selection")
        after = _selected_channel_indices(raw.get("after"), "after selection")
        verified = _strict_bool(raw, "verified")
        _require_verified_value(
            reported=verified,
            matches=after == [channel],
            label="exclusive channel selection",
        )
        summary, warnings = _summary(
            verified,
            f"FL reported channel {channel} as the sole selected channel.",
            f"FL did not report channel {channel} as the sole selected channel.",
        )
        return VerifiedChannelSelectionWrite(
            applied_at=_now(),
            channel_index=channel,
            before_selected_channel_indices=before,
            after_selected_channel_indices=after,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_channel_identity(
        self,
        *,
        channel_index: int,
        name: str | None = None,
        color: int | None = None,
        session_fingerprint: str | None = None,
        expected_before: ExpectedChannelIdentityState | None = None,
    ) -> VerifiedChannelIdentityWrite:
        channel = _strict_int(channel_index, "channel_index", low=0)
        if name is not None:
            if not isinstance(name, str):
                raise ValueError("name must be text")
            if len(name) > MAX_CHANNEL_NAME_LENGTH:
                raise ValueError(
                    f"name must be at most {MAX_CHANNEL_NAME_LENGTH} characters"
                )
        color_value = (
            None
            if color is None
            else _strict_int(color, "color", low=0, high=FL_COLOR_WORD_MAX)
        )
        if name is None and color_value is None:
            raise ValueError("name, color, or both is required")
        arguments: dict[str, Any] = {
            "channel": channel,
            "index_scope": "global",
        }
        if name is not None:
            arguments["name"] = name
        if color_value is not None:
            arguments["color"] = color_value
        raw = self._call(
            "channel.set_identity",
            arguments,
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        _echoed_index(raw, "channel", channel)
        _require_global_index_scope(raw, "channel identity mutation")
        fields = _verified_fields(raw)
        name_verified = None if name is None else _strict_field_bool(fields, "name")
        color_verified = None if color_value is None else _strict_field_bool(fields, "color")
        verified = _strict_bool(raw, "verified")
        before = _channel_identity_snapshot(raw.get("before"))
        after = _channel_identity_snapshot(raw.get("after"))
        proofs = [
            proof
            for requested, proof in (
                (name, name_verified),
                (color_value, color_verified),
            )
            if requested is not None
        ]
        _require_aggregate_verified(
            reported=verified,
            expected=all(proof is True for proof in proofs),
            label="channel identity aggregate",
        )
        if name is not None:
            _require_verified_value(
                reported=name_verified is True,
                matches=after.name == name,
                label="channel name",
            )
        if color_value is not None:
            _require_verified_value(
                reported=color_verified is True,
                matches=fl_colors_equivalent(after.color, color_value),
                label="channel color",
            )
        summary, warnings = _summary(
            verified,
            "FL read every requested channel-identity field back on a later tick.",
            "At least one requested channel-identity field did not read back as requested.",
        )
        return VerifiedChannelIdentityWrite(
            applied_at=_now(),
            channel_index=channel,
            verified=verified,
            verification_summary=summary,
            requested_name=name,
            requested_color=color_value,
            before=before,
            after=after,
            name_verified=name_verified,
            color_verified=color_verified,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def route_channel_to_mixer(
        self,
        *,
        channel_index: int,
        mixer_destination: int,
        session_fingerprint: str | None = None,
        expected_before: ExpectedChannelRouteState | None = None,
    ) -> VerifiedChannelRouteWrite:
        channel = _strict_int(channel_index, "channel_index", low=0)
        destination = _strict_int(
            mixer_destination, "mixer_destination", low=-1
        )
        raw = self._call(
            "channel.route_to_mixer",
            {
                "channel": channel,
                "destination": destination,
                "index_scope": "global",
            },
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        _echoed_index(raw, "channel", channel)
        _require_global_index_scope(raw, "channel routing mutation")
        verified = _strict_bool(raw, "verified")
        before = _channel_route_snapshot(raw.get("before"))
        after = _channel_route_snapshot(raw.get("after"))
        _require_verified_value(
            reported=verified,
            matches=after.mixer_destination == destination,
            label="channel routing",
        )
        summary, warnings = _summary(
            verified,
            f"FL read channel routing back at mixer destination {destination}.",
            f"FL did not read channel routing back at destination {destination}.",
        )
        return VerifiedChannelRouteWrite(
            applied_at=_now(),
            channel_index=channel,
            verified=verified,
            verification_summary=summary,
            requested_mixer_destination=destination,
            before=before,
            after=after,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def select_pattern(
        self,
        *,
        pattern_number: int,
        session_fingerprint: str | None = None,
        expected_before: ExpectedPatternSelectionState | None = None,
    ) -> VerifiedPatternSelectionWrite:
        pattern = _strict_int(
            pattern_number,
            "pattern_number",
            low=1,
            high=MAX_PATTERN_NUMBER,
        )
        raw = self._call(
            "pattern.select",
            {"pattern": pattern},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        requested = _strict_int(
            raw.get("requested"), "requested pattern", low=1, high=MAX_PATTERN_NUMBER
        )
        if requested != pattern:
            raise ValueError("FL bridge selected a different pattern than requested")
        verified = _strict_bool(raw, "verified")
        before = _optional_int(raw.get("before"))
        after = _optional_int(raw.get("after"))
        _require_verified_value(
            reported=verified,
            matches=after == pattern,
            label="pattern selection",
        )
        summary, warnings = _summary(
            verified,
            f"FL reported pattern {pattern} current on a later idle tick.",
            f"FL did not report pattern {pattern} current after selection.",
        )
        return VerifiedPatternSelectionWrite(
            applied_at=_now(),
            requested_pattern_number=pattern,
            before_pattern_number=before,
            after_pattern_number=after,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_pattern_identity(
        self,
        *,
        pattern_number: int,
        name: str | None = None,
        color: int | None = None,
        session_fingerprint: str | None = None,
        expected_before: ExpectedPatternIdentityState | None = None,
    ) -> VerifiedPatternIdentityWrite:
        pattern = _strict_int(
            pattern_number,
            "pattern_number",
            low=1,
            high=MAX_PATTERN_NUMBER,
        )
        if name is not None:
            if not isinstance(name, str):
                raise ValueError("name must be text")
            if len(name) > MAX_PATTERN_NAME_LENGTH:
                raise ValueError(
                    f"name must be at most {MAX_PATTERN_NAME_LENGTH} characters"
                )
        color_value = (
            None
            if color is None
            else _strict_int(color, "color", low=0, high=FL_COLOR_WORD_MAX)
        )
        if name is None and color_value is None:
            raise ValueError("name, color, or both is required")
        arguments: dict[str, Any] = {"pattern": pattern}
        if name is not None:
            arguments["name"] = name
        if color_value is not None:
            arguments["color"] = color_value
        raw = self._call(
            "pattern.set_identity",
            arguments,
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        _echoed_index(raw, "pattern", pattern, low=1)
        fields = _verified_fields(raw)
        name_verified = None if name is None else _strict_field_bool(fields, "name")
        color_verified = (
            None if color_value is None else _strict_field_bool(fields, "color")
        )
        verified = _strict_bool(raw, "verified")
        before = _pattern_identity_snapshot(raw.get("before"))
        after = _pattern_identity_snapshot(raw.get("after"))
        proofs = [
            proof
            for requested, proof in (
                (name, name_verified),
                (color_value, color_verified),
            )
            if requested is not None
        ]
        _require_aggregate_verified(
            reported=verified,
            expected=all(proof is True for proof in proofs),
            label="pattern identity aggregate",
        )
        if name is not None:
            _require_verified_value(
                reported=name_verified is True,
                matches=after.name == name,
                label="pattern name",
            )
        if color_value is not None:
            _require_verified_value(
                reported=color_verified is True,
                matches=fl_colors_equivalent(after.color, color_value),
                label="pattern color",
            )
        summary, warnings = _summary(
            verified,
            "FL read every requested pattern identity field back on a later tick.",
            "At least one requested pattern identity field did not read back.",
        )
        return VerifiedPatternIdentityWrite(
            applied_at=_now(),
            pattern_number=pattern,
            requested_name=name,
            requested_color=color_value,
            before=before,
            after=after,
            name_verified=name_verified,
            color_verified=color_verified,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_pattern_length(
        self,
        *,
        pattern_number: int,
        length_beats: int,
        session_fingerprint: str | None = None,
        expected_before: ExpectedPatternLengthState | None = None,
    ) -> VerifiedPatternLengthWrite:
        pattern = _strict_int(
            pattern_number,
            "pattern_number",
            low=1,
            high=MAX_PATTERN_NUMBER,
        )
        length = _strict_int(
            length_beats,
            "length_beats",
            low=1,
            high=MAX_PATTERN_LENGTH_BEATS,
        )
        raw = self._call(
            "pattern.set_length",
            {"pattern": pattern, "length": length},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        _echoed_index(raw, "pattern", pattern, low=1)
        verified = _strict_bool(raw, "verified")
        before = _optional_int(raw.get("before"))
        after = _optional_int(raw.get("after"))
        _require_verified_value(
            reported=verified,
            matches=after == length,
            label="pattern length",
        )
        summary, warnings = _summary(
            verified,
            f"FL read pattern {pattern} back at {length} beats.",
            f"FL did not read pattern {pattern} back at {length} beats.",
        )
        return VerifiedPatternLengthWrite(
            applied_at=_now(),
            pattern_number=pattern,
            requested_length_beats=length,
            before_length_beats=before,
            after_length_beats=after,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_playlist_track_identity(
        self,
        *,
        track_index: int,
        name: str | None = None,
        color: int | None = None,
        session_fingerprint: str | None = None,
        expected_before: ExpectedPlaylistTrackIdentityState | None = None,
    ) -> VerifiedPlaylistTrackIdentityWrite:
        track = _strict_int(track_index, "track_index", low=1)
        if name is not None:
            if not isinstance(name, str):
                raise ValueError("name must be text")
            if len(name) > MAX_PLAYLIST_TRACK_NAME_LENGTH:
                raise ValueError(
                    f"name must be at most {MAX_PLAYLIST_TRACK_NAME_LENGTH} characters"
                )
        color_value = (
            None
            if color is None
            else _strict_int(color, "color", low=0, high=FL_COLOR_WORD_MAX)
        )
        if name is None and color_value is None:
            raise ValueError("name, color, or both is required")
        arguments: dict[str, Any] = {"track": track}
        if name is not None:
            arguments["name"] = name
        if color_value is not None:
            arguments["color"] = color_value
        raw = self._call(
            "playlist.set_identity",
            arguments,
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        _echoed_index(raw, "track", track, low=1)
        fields = _verified_fields(raw)
        name_verified = None if name is None else _strict_field_bool(fields, "name")
        color_verified = (
            None if color_value is None else _strict_field_bool(fields, "color")
        )
        verified = _strict_bool(raw, "verified")
        before = _playlist_identity_snapshot(raw.get("before"))
        after = _playlist_identity_snapshot(raw.get("after"))
        proofs = [proof for proof in (name_verified, color_verified) if proof is not None]
        _require_aggregate_verified(
            reported=verified,
            expected=all(proof is True for proof in proofs),
            label="Playlist identity aggregate",
        )
        if name is not None:
            _require_verified_value(
                reported=name_verified is True,
                matches=after.name == name,
                label="Playlist track name",
            )
        if color_value is not None:
            _require_verified_value(
                reported=color_verified is True,
                matches=fl_colors_equivalent(after.color, color_value),
                label="Playlist track color",
            )
        summary, warnings = _summary(
            verified,
            "FL read every requested Playlist identity field back on a later tick.",
            "At least one requested Playlist identity field did not read back.",
        )
        return VerifiedPlaylistTrackIdentityWrite(
            applied_at=_now(),
            track_index=track,
            requested_name=name,
            requested_color=color_value,
            before=before,
            after=after,
            name_verified=name_verified,
            color_verified=color_verified,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_playlist_track_state(
        self,
        *,
        track_index: int,
        muted: bool | None = None,
        soloed: bool | None = None,
        selected: bool | None = None,
        session_fingerprint: str | None = None,
        expected_before: ExpectedPlaylistTrackState | None = None,
    ) -> VerifiedPlaylistTrackStateWrite:
        track = _strict_int(track_index, "track_index", low=1)
        requested: dict[str, bool] = {}
        for field, value in (
            ("muted", muted),
            ("soloed", soloed),
            ("selected", selected),
        ):
            if value is not None:
                if type(value) is not bool:
                    raise ValueError(f"{field} must be true or false")
                requested[field] = value
        if not requested:
            raise ValueError("muted, soloed, selected, or a combination is required")
        raw = self._call(
            "playlist.set_state",
            {"track": track, **requested},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        _echoed_index(raw, "track", track, low=1)
        fields = _verified_fields(raw)
        mute_verified = (
            None if muted is None else _strict_field_bool(fields, "muted")
        )
        solo_verified = (
            None if soloed is None else _strict_field_bool(fields, "soloed")
        )
        selection_verified = (
            None if selected is None else _strict_field_bool(fields, "selected")
        )
        verified = _strict_bool(raw, "verified")
        before = _playlist_state_snapshot(raw.get("before"))
        after = _playlist_state_snapshot(raw.get("after"))
        proofs = [
            proof
            for proof in (mute_verified, solo_verified, selection_verified)
            if proof is not None
        ]
        _require_aggregate_verified(
            reported=verified,
            expected=all(proof is True for proof in proofs),
            label="Playlist state aggregate",
        )
        for wanted, observed, proof, label in (
            (muted, after.muted, mute_verified, "Playlist mute"),
            (soloed, after.soloed, solo_verified, "Playlist solo"),
            (selected, after.selected, selection_verified, "Playlist selection"),
        ):
            if wanted is not None:
                _require_verified_value(
                    reported=proof is True,
                    matches=observed is wanted,
                    label=label,
                )
        summary, warnings = _summary(
            verified,
            "FL read every requested Playlist state back on a later idle tick.",
            "At least one requested Playlist state did not read back.",
        )
        return VerifiedPlaylistTrackStateWrite(
            applied_at=_now(),
            track_index=track,
            requested_muted=muted,
            requested_soloed=soloed,
            requested_selected=selected,
            before=before,
            after=after,
            mute_verified=mute_verified,
            solo_verified=solo_verified,
            selection_verified=selection_verified,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_plugin_parameter(
        self,
        *,
        parameter_index: int,
        normalized_value: float,
        target: PluginTarget | dict[str, Any] | None = None,
        track_index: int | None = None,
        slot_index: int | None = None,
        allow_master: bool = False,
        session_fingerprint: str | None = None,
        expected_before: ExpectedPluginParameterState | None = None,
    ) -> VerifiedTargetedPluginParameterWrite:
        resolved = normalize_plugin_target(
            target=target,
            track_index=track_index,
            slot_index=slot_index,
            allow_master=allow_master,
        )
        parameter = _strict_int(parameter_index, "parameter_index", low=0)
        value = _number(
            normalized_value, "normalized_value", low=0.0, high=1.0
        )
        arguments = _plugin_bridge_arguments(resolved)
        arguments.update(index=parameter, value=value)
        raw = self._call(
            "plugin.set_param",
            arguments,
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        _echoed_plugin_target(raw, resolved)
        if _echoed_index(raw, "index", parameter) != parameter:
            raise AssertionError("unreachable")
        verified = _strict_bool(raw, "verified")
        display_changed = _strict_bool(raw, "display_changed")
        reads_at_value = _strict_bool(raw, "reads_at_value")
        before = _plugin_parameter_snapshot(raw.get("before"))
        after = _plugin_parameter_snapshot(raw.get("after"))
        observed_display_changed = before.display_text != after.display_text
        observed_reads_at_value = (
            after.normalized_value is not None
            and abs(after.normalized_value - value)
            <= PLUGIN_PARAMETER_READBACK_TOLERANCE
        )
        if display_changed != observed_display_changed:
            raise ValueError(
                "FL bridge reported contradictory plug-in display-change proof"
            )
        if reads_at_value != observed_reads_at_value:
            raise ValueError(
                "FL bridge reported contradictory plug-in numeric readback proof"
            )
        proof = (
            "value_readback"
            if reads_at_value
            else "display_change_only"
            if display_changed
            else "none"
        )
        reported_proof = raw.get("verification_basis", proof)
        if reported_proof != proof:
            raise ValueError("FL bridge returned contradictory plug-in proof fields")
        _require_aggregate_verified(
            reported=verified,
            expected=reads_at_value or display_changed,
            label="plug-in parameter aggregate",
        )
        summary, warnings = _summary(
            verified,
            (
                "FL read the requested normalized value back on a later tick."
                if reads_at_value
                else "FL's displayed parameter value changed on a later tick."
            ),
            "Neither numeric readback nor displayed-value change proved the write.",
        )
        return VerifiedTargetedPluginParameterWrite(
            applied_at=_now(),
            target=_public_plugin_target(resolved),
            parameter_index=parameter,
            plugin_name=str(raw.get("plugin") or ""),
            parameter_name=str(raw.get("name") or ""),
            requested_normalized_value=value,
            before=before,
            after=after,
            display_changed=display_changed,
            reads_at_requested_value=reads_at_value,
            verification_basis_detail=cast(Any, proof),
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_plugin_parameter_display(
        self,
        *,
        parameter: int | str,
        target_value: float,
        target: PluginTarget | dict[str, Any] | None = None,
        track_index: int | None = None,
        slot_index: int | None = None,
        allow_master: bool = False,
        tolerance: float | None = None,
        session_fingerprint: str | None = None,
        expected_before: ExpectedPluginParameterState | None = None,
    ) -> VerifiedTargetedPluginDisplayWrite:
        resolved = normalize_plugin_target(
            target=target,
            track_index=track_index,
            slot_index=slot_index,
            allow_master=allow_master,
        )
        selector = _plugin_parameter_selector(parameter)
        displayed = _number(target_value, "target_value", low=-1e6, high=1e6)
        tolerance_value = (
            None
            if tolerance is None
            else _number(tolerance, "tolerance", low=0.0, high=1e6)
        )
        arguments = _plugin_bridge_arguments(resolved)
        arguments.update(param=selector, target=displayed)
        if tolerance_value is not None:
            arguments["tolerance"] = tolerance_value
        raw = self._call(
            "plugin.set_param_display",
            arguments,
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        _echoed_plugin_target(raw, resolved)
        verified = _strict_bool(raw, "verified")
        before = _plugin_parameter_snapshot(raw.get("before"))
        after = _plugin_parameter_snapshot(raw.get("after"))
        landed = _optional_float(raw.get("landed_on"))
        reported_tolerance = _optional_float(raw.get("tolerance"))
        expected_tolerance = (
            max(0.01, abs(displayed) * 0.02)
            if tolerance_value is None
            else tolerance_value
        )
        if (
            reported_tolerance is None
            or reported_tolerance < 0.0
            or abs(reported_tolerance - expected_tolerance) > 1e-12
        ):
            raise ValueError(
                "FL bridge returned a contradictory plug-in display tolerance"
            )
        after_display_value = _first_displayed_number(after.display_text)
        landed_matches = (
            landed is not None
            and abs(landed - displayed) <= reported_tolerance
        )
        later_display_matches = (
            after_display_value is not None
            and abs(after_display_value - displayed) <= reported_tolerance
        )
        _require_aggregate_verified(
            reported=verified,
            expected=landed_matches and later_display_matches,
            label="plug-in displayed-value aggregate",
        )
        summary, warnings = _summary(
            verified,
            f"FL's later readback landed at displayed value {landed!r}.",
            f"FL's later readback did not land on displayed value {displayed!r}.",
        )
        return VerifiedTargetedPluginDisplayWrite(
            applied_at=_now(),
            target=_public_plugin_target(resolved),
            parameter_index=_strict_int(raw.get("index"), "parameter index", low=0),
            plugin_name=(
                None if raw.get("plugin") is None else str(raw.get("plugin"))
            ),
            parameter_name=(
                None if raw.get("name") is None else str(raw.get("name"))
            ),
            matched_on=_plugin_match_kind(raw.get("matched_on")),
            matched_text=(
                None
                if raw.get("matched_text") is None
                else str(raw.get("matched_text"))
            ),
            requested_value=displayed,
            tolerance=reported_tolerance,
            landed_value=landed,
            normalized_value=_optional_float(raw.get("normalised")),
            before=before,
            after=after,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_plugin_parameter_option(
        self,
        *,
        parameter: int | str,
        option: str,
        target: PluginTarget | dict[str, Any] | None = None,
        track_index: int | None = None,
        slot_index: int | None = None,
        allow_master: bool = False,
        sweep_steps: int = 64,
        session_fingerprint: str | None = None,
        expected_before: ExpectedPluginParameterState | None = None,
    ) -> VerifiedTargetedPluginOptionWrite:
        resolved = normalize_plugin_target(
            target=target,
            track_index=track_index,
            slot_index=slot_index,
            allow_master=allow_master,
        )
        selector = _plugin_parameter_selector(parameter)
        if not isinstance(option, str) or not option.strip():
            raise ValueError("option must be non-empty text")
        selected_option = option.strip()
        if len(selected_option) > 256:
            raise ValueError("option must be at most 256 characters")
        steps = _strict_int(sweep_steps, "sweep_steps", low=2, high=256)
        arguments = _plugin_bridge_arguments(resolved)
        arguments.update(param=selector, option=selected_option, steps=steps)
        raw = self._call(
            "plugin.set_param_option",
            arguments,
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        _echoed_plugin_target(raw, resolved)
        verified = _strict_bool(raw, "verified")
        before = _plugin_parameter_snapshot(raw.get("before"))
        after = _plugin_parameter_snapshot(raw.get("after"))
        selected_raw = raw.get("selected")
        if (
            not isinstance(selected_raw, str)
            or not selected_raw
            or len(selected_raw) > 256
        ):
            raise ValueError("FL bridge returned a malformed selected option")
        landed_option = selected_raw
        options_raw = raw.get("options")
        if (
            not isinstance(options_raw, list)
            or any(not isinstance(item, str) for item in options_raw)
        ):
            raise ValueError("FL bridge returned malformed enumerated options")
        options = cast(list[str], options_raw)
        if landed_option.casefold() != selected_option.casefold():
            raise ValueError(
                "FL bridge selected an option that does not exactly match the request"
            )
        if landed_option not in options:
            raise ValueError(
                "FL bridge selected an option absent from its enumerated options"
            )
        later_display_matches = (
            after.display_text is not None
            and after.display_text.lower() == landed_option.lower()
        )
        _require_aggregate_verified(
            reported=verified,
            expected=later_display_matches,
            label="plug-in option aggregate",
        )
        summary, warnings = _summary(
            verified,
            f"FL read the selected option back as {landed_option!r}.",
            f"FL did not read option {selected_option!r} back after the sweep.",
        )
        return VerifiedTargetedPluginOptionWrite(
            applied_at=_now(),
            target=_public_plugin_target(resolved),
            parameter_index=_strict_int(raw.get("index"), "parameter index", low=0),
            plugin_name=(
                None if raw.get("plugin") is None else str(raw.get("plugin"))
            ),
            parameter_name=(
                None if raw.get("name") is None else str(raw.get("name"))
            ),
            matched_on=_plugin_match_kind(raw.get("matched_on")),
            matched_text=(
                None
                if raw.get("matched_text") is None
                else str(raw.get("matched_text"))
            ),
            requested_option=selected_option,
            selected_option=landed_option,
            normalized_value=_optional_float(raw.get("normalised")),
            sweep_steps=_strict_int(raw.get("steps", steps), "steps", low=2, high=256),
            options=options,
            before=before,
            after=after,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings),
            **_precondition_fields(raw),
        )

    def set_step_sequence(
        self,
        *,
        pattern_number: int,
        channel_index: int,
        expected_digest: str,
        updates: list[StepCellUpdate | dict[str, Any]],
        session_fingerprint: str | None = None,
    ) -> VerifiedStepSequenceWrite:
        pattern = _strict_int(pattern_number, "pattern_number", low=1)
        channel = _strict_int(channel_index, "channel_index", low=0)
        if not isinstance(expected_digest, str) or len(expected_digest) != 64 or any(
            char not in "0123456789abcdef" for char in expected_digest
        ):
            raise ValueError("expected_digest must be 64 lowercase hex characters")
        if (
            not isinstance(updates, list)
            or not updates
            or len(updates) > MAX_VERIFIED_STEP_COUNT
        ):
            raise ValueError(
                f"updates must contain 1..{MAX_VERIFIED_STEP_COUNT} cells"
            )
        parsed = [
            value if isinstance(value, StepCellUpdate) else StepCellUpdate.model_validate(value)
            for value in updates
        ]
        indices = [value.step_index for value in parsed]
        if len(set(indices)) != len(indices):
            raise ValueError("updates must not contain duplicate step indices")
        raw = self._call(
            "sequencer.set",
            {
                "pattern": pattern,
                "channel": channel,
                "index_scope": "global",
                "expected_digest": expected_digest,
                "updates": [value.model_dump() for value in parsed],
            },
            session_fingerprint=session_fingerprint,
            # Digest is mandatory and command-specific, not duplicated in the
            # generic Track A expected_before envelope.
            expected_before=None,
        )
        _echoed_index(raw, "pattern", pattern, low=1)
        _echoed_index(raw, "channel", channel)
        _require_global_index_scope(raw, "step-sequence mutation")
        if raw.get("expected_digest") != expected_digest:
            raise ValueError("FL bridge did not echo the required grid digest")
        before = _step_snapshot(raw.get("before"), pattern=pattern, channel=channel)
        after = _step_snapshot(raw.get("after"), pattern=pattern, channel=channel)
        if before.digest != expected_digest:
            raise ValueError(
                "FL bridge mutated against a before-grid other than expected_digest"
            )
        cells_raw = raw.get("verified_cells")
        if not isinstance(cells_raw, list) or len(cells_raw) != len(parsed):
            raise ValueError("FL bridge returned malformed per-cell verification")
        proofs: list[StepCellVerification] = []
        for requested, item in zip(parsed, cells_raw, strict=True):
            if not isinstance(item, dict):
                raise ValueError("FL bridge returned malformed cell verification")
            if item.get("step_index") != requested.step_index:
                raise ValueError("FL bridge returned cell proofs out of order")
            if item.get("requested_enabled") != requested.enabled:
                raise ValueError("FL bridge returned a contradictory requested cell")
            after_enabled = _optional_bool(item.get("after_enabled"))
            proof_verified = _strict_bool(item, "verified")
            if requested.step_index >= after.step_count:
                raise ValueError(
                    "FL bridge returned a step proof outside its after-grid"
                )
            grid_value = after.cells[requested.step_index]
            if after_enabled is not grid_value:
                raise ValueError(
                    "FL bridge returned a cell proof that contradicts the after-grid"
                )
            _require_verified_value(
                reported=proof_verified,
                matches=after_enabled is requested.enabled,
                label=f"step cell {requested.step_index}",
            )
            proofs.append(
                StepCellVerification(
                    step_index=requested.step_index,
                    requested_enabled=requested.enabled,
                    after_enabled=after_enabled,
                    verified=proof_verified,
                )
            )
        verified = _strict_bool(raw, "verified")
        summary, warnings = _summary(
            verified,
            "FL read every requested absolute step state back after the mutation.",
            "At least one requested step did not read back in its absolute state.",
        )
        preconditions = _precondition_fields(raw)
        if not preconditions["expected_before_applied"]:
            raise ValueError("FL bridge did not report applying the required grid digest")
        return VerifiedStepSequenceWrite(
            applied_at=_now(),
            pattern_number=pattern,
            channel_index=channel,
            expected_digest=expected_digest,
            requested_updates=parsed,
            before=before,
            after=after,
            cells_verified=proofs,
            verified=verified,
            verification_summary=summary,
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            warnings=_warnings(raw, warnings + [CURRENT_PATTERN_ONLY_WARNING]),
            **preconditions,
        )

    def trigger_note(
        self,
        *,
        channel_index: int,
        note: int,
        velocity: int,
        duration_ms: int = 250,
        midi_channel: int = -1,
        session_fingerprint: str | None = None,
        expected_before: ExpectedChannelTargetState | None = None,
    ) -> LiveNoteDispatch:
        channel = _strict_int(channel_index, "channel_index", low=0)
        note_value = _strict_int(note, "note", low=0, high=127)
        velocity_value = _strict_int(velocity, "velocity", low=1, high=127)
        duration = _strict_int(duration_ms, "duration_ms", low=20, high=5000)
        midi_channel_value = _strict_int(
            midi_channel, "midi_channel", low=-1, high=15
        )
        raw = self._call(
            "channel.trigger_note",
            {
                "channel": channel,
                "index_scope": "global",
                "note": note_value,
                "velocity": velocity_value,
                "duration_ms": duration,
                "midi_channel": midi_channel_value,
            },
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        _echoed_index(raw, "channel", channel)
        _require_global_index_scope(raw, "live-note dispatch")
        if _echoed_index(raw, "note", note_value) != note_value:
            raise AssertionError("unreachable")
        if _echoed_index(raw, "velocity", velocity_value, low=1) != velocity_value:
            raise AssertionError("unreachable")
        if _echoed_index(raw, "duration_ms", duration, low=20) != duration:
            raise AssertionError("unreachable")
        if _echoed_index(raw, "midi_channel", midi_channel_value, low=-1) != midi_channel_value:
            raise AssertionError("unreachable")
        dispatched = _strict_bool(raw, "dispatched")
        note_off = _strict_bool(raw, "note_off_sent")
        if note_off and not dispatched:
            raise ValueError(
                "FL bridge reported note-off dispatch without first dispatching note-on"
            )
        preconditions = _precondition_fields(raw)
        warnings = [] if note_off else [
            "FL did not confirm note-off dispatch; stop auditioning and reload the "
            "bridge if the channel remains sounding."
        ]
        warnings.append(
            "This receipt proves only note-on/note-off dispatch. FL exposes no "
            "authoritative live-note state or audible-output readback."
        )
        return LiveNoteDispatch(
            dispatched_at=_now(),
            channel_index=channel,
            note=note_value,
            velocity=velocity_value,
            duration_ms=duration,
            midi_channel=midi_channel_value,
            dispatched=dispatched,
            note_off_sent=note_off,
            warnings=_warnings(raw, warnings),
            **preconditions,
        )


def _verified_fields(payload: dict[str, Any]) -> dict[str, Any]:
    fields = payload.get("verified_fields")
    if not isinstance(fields, dict):
        raise ValueError("FL bridge did not report per-field verification")
    return fields


def _strict_field_bool(fields: dict[str, Any], field: str) -> bool:
    value = fields.get(field)
    if type(value) is not bool:
        raise ValueError(f"FL bridge did not verify requested field {field!r}")
    return value


def _require_aggregate_verified(
    *, reported: bool, expected: bool, label: str
) -> None:
    if reported != expected:
        raise ValueError(
            f"FL bridge reported contradictory {label} verification; "
            "the later-tick readback does not support verified=true"
        )


def _require_verified_value(
    *, reported: bool, matches: bool, label: str
) -> None:
    if reported and not matches:
        raise ValueError(
            f"FL bridge reported {label} verified, but its later-tick "
            "readback does not match the requested value"
        )


def _snapshot_payload(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"FL bridge returned malformed {label}")
    return raw


def _project_observation_warnings(
    before: dict[str, Any], after: dict[str, Any]
) -> list[str]:
    warnings = [
        "This is a non-atomic live observation; concurrent user or plug-in "
        "changes can produce a torn view."
    ]
    token_fields = (
        "unsaved_changes",
        "undo_history_position",
        "undo_history_count",
    )
    if tuple(before.get(key) for key in token_fields) != tuple(
        after.get(key) for key in token_fields
    ):
        warnings.append(
            "The public dirty/undo consistency token changed during observation; retry."
        )
    return warnings


def _project_history_snapshot(raw: Any) -> ProjectHistorySnapshot:
    payload = _snapshot_payload(raw, "project-history snapshot")
    position = _strict_int(payload.get("position"), "history position", low=0)
    count = _strict_int(payload.get("count"), "history count", low=0)
    last = _strict_int(
        payload.get("last_position"), "last history position", low=0
    )
    hint = payload.get("level_hint")
    if not isinstance(hint, str) or len(hint) > 512:
        raise ValueError("FL bridge returned a malformed undo-level hint")
    return ProjectHistorySnapshot(
        position=position,
        count=count,
        last_position=last,
        level_hint=hint,
        project_dirty_flag=_dirty(payload.get("project_dirty_flag")),
        can_undo=_strict_bool(payload, "can_undo"),
        can_redo=_strict_bool(payload, "can_redo"),
    )


def _time_signature_snapshot(raw: Any) -> TimeSignatureSnapshot:
    payload = _snapshot_payload(raw, "time-signature snapshot")
    numerator = _strict_int(payload.get("numerator"), "numerator", low=1, high=32)
    ppq = _strict_int(payload.get("ppq"), "PPQ", low=1)
    pulses = _strict_int(payload.get("pulses_per_bar"), "pulses_per_bar", low=1)
    if pulses != ppq * numerator:
        raise ValueError("FL bridge returned contradictory time-signature evidence")
    if payload.get("denominator_available") is not False:
        raise ValueError("FL bridge claimed an unavailable denominator readback")
    return TimeSignatureSnapshot(
        numerator=numerator,
        ppq=ppq,
        pulses_per_bar=pulses,
    )


def _targeted_parameters(
    raw: Any, *, padding_candidates: bool
) -> list[TargetedPluginParameter]:
    if not isinstance(raw, list):
        raise ValueError("FL bridge returned malformed plug-in parameters")
    parameters: list[TargetedPluginParameter] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("FL bridge returned a malformed plug-in parameter")
        index = _strict_int(item.get("index"), "parameter index", low=0)
        name = str(item.get("name") or "")
        display = str(item.get("display") or "")
        if len(name) > 256 or len(display) > 256:
            raise ValueError("FL bridge returned oversized plug-in parameter text")
        padding = padding_candidates and not name.strip() and display.strip() in {
            "",
            "0",
            "0.000000",
            "0.0000000",
        }
        parameters.append(
            TargetedPluginParameter(
                index=index,
                reported_name=name,
                normalized_value=_optional_float(item.get("value")),
                display_text=display or None,
                display_text_available=bool(display),
                classification="padding_candidate" if padding else "reported",
            )
        )
    return parameters


def _targeted_plugin_summary(
    payload: dict[str, Any], target: NormalizedPluginTarget, count: int
) -> TargetedPluginSummary:
    user_name = payload.get("plugin_user_name")
    if user_name in (None, ""):
        user_name = None
    else:
        user_name = str(user_name)
    return TargetedPluginSummary(
        target=_public_plugin_target(target),
        name=str(payload.get("plugin") or ""),
        user_name=user_name,
        reported_parameter_count=count,
        mix_level_normalized=(
            _optional_float(payload.get("mix_level"))
            if target.kind == "mixer_effect"
            else None
        ),
    )


def _plugin_parameter_snapshot(raw: Any) -> PluginParameterSnapshot:
    value = _snapshot_payload(raw, "plug-in parameter snapshot")
    display_raw = value.get("display")
    display = None if display_raw in (None, "") else str(display_raw)
    if display is not None and len(display) > 256:
        raise ValueError("FL bridge returned oversized parameter display text")
    return PluginParameterSnapshot(
        normalized_value=_optional_float(value.get("value")),
        display_text=display,
        display_text_available=display is not None,
    )


def _first_displayed_number(value: str | None) -> float | None:
    """Mirror FL's display search parser for a later-tick proof cross-check."""

    if value is None:
        return None
    number = ""
    seen_digit = False
    for character in value:
        if character.isdigit():
            number += character
            seen_digit = True
        elif character in "-+" and not number:
            number += character
        elif character == "." and seen_digit and "." not in number:
            number += character
        elif seen_digit:
            break
        else:
            number = ""
    try:
        parsed = float(number)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _plugin_parameter_selector(value: Any) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("parameter must be an index or a name")
    if isinstance(value, int):
        return _strict_int(value, "parameter", low=0)
    selected = value.strip()
    if not selected:
        raise ValueError("parameter name must not be empty")
    if len(selected) > 256:
        raise ValueError("parameter name must be at most 256 characters")
    return selected


def _plugin_match_kind(value: Any) -> Any:
    allowed = {
        "index",
        "name",
        "display",
        "name_substring",
        "display_substring",
    }
    if value not in allowed:
        raise ValueError("FL bridge returned an invalid plug-in match kind")
    return value


def _channel_mix_snapshot(raw: Any) -> ChannelMixSnapshot:
    value = _snapshot_payload(raw, "channel mix snapshot")
    return ChannelMixSnapshot(
        volume_normalized=_optional_float(value.get("volume")),
        pan=_optional_float(value.get("pan")),
        muted=_optional_bool(value.get("muted")),
        channel_fingerprint=value.get("channel_fingerprint"),
    )


def _channel_identity_snapshot(raw: Any) -> ChannelIdentitySnapshot:
    value = _snapshot_payload(raw, "channel identity snapshot")
    name = value.get("name")
    if name is not None and not isinstance(name, str):
        raise ValueError("FL bridge returned a non-text channel name")
    return ChannelIdentitySnapshot(
        name=name,
        color=_bridge_color(value.get("color"), "channel identity"),
        channel_fingerprint=value.get("channel_fingerprint"),
    )


def _channel_route_snapshot(raw: Any) -> ChannelRouteSnapshot:
    value = _snapshot_payload(raw, "channel route snapshot")
    return ChannelRouteSnapshot(
        mixer_destination=_optional_int(value.get("mixer_destination")),
        channel_fingerprint=value.get("channel_fingerprint"),
    )


def _channel_solo_snapshot(raw: Any) -> ChannelSoloSnapshot:
    value = _snapshot_payload(raw, "channel solo snapshot")
    return ChannelSoloSnapshot(
        soloed=_optional_bool(value.get("soloed")),
        channel_fingerprint=value.get("channel_fingerprint"),
    )


def _channel_pitch_snapshot(raw: Any) -> ChannelPitchSnapshot:
    value = _snapshot_payload(raw, "channel pitch snapshot")
    return ChannelPitchSnapshot(
        pitch_normalized=_optional_float(value.get("pitch")),
        pitch_semitones=_optional_float(value.get("pitch_semitones")),
        pitch_range_semitones=_optional_float(value.get("pitch_range")),
        channel_fingerprint=value.get("channel_fingerprint"),
    )


def _selected_channel_indices(raw: Any, label: str) -> list[int]:
    if not isinstance(raw, list):
        raise ValueError(f"FL bridge returned malformed {label}")
    values: list[int] = []
    for value in raw:
        if type(value) is not int or value < 0:
            raise ValueError(f"FL bridge returned malformed {label}")
        values.append(value)
    if values != sorted(set(values)):
        raise ValueError(f"FL bridge returned non-canonical {label}")
    return values


def _pattern_identity_snapshot(raw: Any) -> PatternIdentitySnapshot:
    value = _snapshot_payload(raw, "pattern identity snapshot")
    name = value.get("name")
    if name is not None and (
        not isinstance(name, str) or len(name) > MAX_PATTERN_NAME_LENGTH
    ):
        raise ValueError("FL bridge returned a malformed pattern name")
    return PatternIdentitySnapshot(
        name=name,
        color=_bridge_color(value.get("color"), "pattern"),
    )


def _playlist_identity_snapshot(raw: Any) -> PlaylistTrackIdentitySnapshot:
    value = _snapshot_payload(raw, "Playlist identity snapshot")
    name = value.get("name")
    if name is not None and (
        not isinstance(name, str) or len(name) > MAX_PLAYLIST_TRACK_NAME_LENGTH
    ):
        raise ValueError("FL bridge returned a malformed Playlist track name")
    return PlaylistTrackIdentitySnapshot(
        name=name,
        color=_bridge_color(value.get("color"), "Playlist track"),
    )


def _playlist_state_snapshot(raw: Any) -> PlaylistTrackStateSnapshot:
    value = _snapshot_payload(raw, "Playlist state snapshot")
    return PlaylistTrackStateSnapshot(
        muted=_optional_bool(value.get("muted")),
        soloed=_optional_bool(value.get("soloed")),
        selected=_optional_bool(value.get("selected")),
    )


def _step_snapshot(
    raw: Any, *, pattern: int, channel: int
) -> StepSequenceSnapshot:
    value = _snapshot_payload(raw, "step sequence snapshot")
    _echoed_index(value, "pattern", pattern, low=1)
    _echoed_index(value, "channel", channel)
    _require_global_index_scope(value, "step sequence")
    current = _strict_int(
        value.get("current_pattern"), "current_pattern", low=1
    )
    if current != pattern:
        raise ValueError(
            "step-grid APIs are current-pattern-only and the requested pattern "
            "was not current; nothing may switch patterns implicitly"
        )
    step_count = _strict_int(value.get("step_count"), "step_count", low=1, high=MAX_STEP_COUNT)
    cells_raw = value.get("cells")
    if not isinstance(cells_raw, list) or len(cells_raw) != step_count:
        raise ValueError("FL bridge returned a malformed absolute step grid")
    if any(type(cell) is not bool for cell in cells_raw):
        raise ValueError("FL bridge returned non-boolean grid cells")
    cells = cast(list[bool], cells_raw)
    digest = compute_step_sequence_digest(
        pattern_number=pattern,
        channel_index=channel,
        step_count=step_count,
        cells=cells,
    )
    reported_digest = value.get("digest")
    if reported_digest != digest:
        raise ValueError("FL bridge returned a step-grid digest mismatch")
    if value.get("digest_algorithm", "sha256-canonical-json-v1") != "sha256-canonical-json-v1":
        raise ValueError("FL bridge returned an unsupported grid digest algorithm")
    if value.get("grid_resolution", "sixteenth_note") != "sixteenth_note":
        raise ValueError("FL bridge returned an unsupported step-grid resolution")
    return StepSequenceSnapshot(
        pattern_number=pattern,
        current_pattern_number=current,
        channel_index=channel,
        step_count=step_count,
        cells=cells,
        digest=digest,
    )


__all__ = [
    "ChannelGeneratorTarget",
    "MixerEffectTarget",
    "NormalizedPluginTarget",
    "PluginTarget",
    "PLAYBACK_SPEED_OMISSION_REASON",
    "TEMPO_READBACK_TOLERANCE",
    "TRACK_B_MUTATION_COMMANDS",
    "TRACK_B_MCP_TOOL_NAMES",
    "TRACK_B_READ_COMMANDS",
    "TARGET_AWARE_EXISTING_PLUGIN_TOOLS",
    "TrackBBoundaryViolation",
    "TrackBController",
    "TrackBInspector",
    "TrackBMutationGateway",
    "TrackBMutationsUnavailable",
    "TrackBReadGateway",
    "normalize_plugin_target",
]
