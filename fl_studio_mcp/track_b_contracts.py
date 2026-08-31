"""Strict contracts for the 0.12 Track B performance surface.

This module is intentionally isolated from :mod:`fl_studio_mcp.contracts` while
Track A is landing.  It imports only the stable base contract and schema version
and does not alter the existing mixer/plugin JSON surface.

Playback speed is deliberately absent.  FL Studio documents a setter but no
authoritative getter, so it cannot meet this project's later-idle-tick readback
rule.  ``PLAYBACK_SPEED_OMISSION_REASON`` is exported so the integration layer
can expose that limitation without accidentally presenting an unverified tool.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, model_validator

from .contracts import ContractModel, SCHEMA_VERSION


PLAYBACK_SPEED_OMISSION_REASON = (
    "FL Studio exposes transport.setPlaybackSpeed but no authoritative playback "
    "speed getter, so a later-idle-tick readback-verified setter cannot be "
    "implemented honestly."
)

SESSION_FINGERPRINT_PATTERN = r"^[0-9a-f]{32}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
MAX_CHANNEL_NAME_LENGTH = 64
MAX_PATTERN_NAME_LENGTH = 64
MAX_PLAYLIST_TRACK_NAME_LENGTH = 64
MAX_PATTERN_NUMBER = 999
MAX_PATTERN_LENGTH_BEATS = 4096
FL_COLOR_WORD_MAX = 0xFFFFFFFF
FL_COLOR_RGB_MASK = 0x00FFFFFF
FL_COLOR_SIGNED_MIN = -(1 << 31)
MAX_STEP_COUNT = 512
MAX_VERIFIED_STEP_COUNT = 256
SEQUENCER_WRITE_CALL_BUDGET = 320
SEQUENCER_WRITE_FIXED_CALLS = 8
STEP_GRID_RESOLUTION = "sixteenth_note"
STEP_DIGEST_ALGORITHM = "sha256-canonical-json-v1"
MAX_PLUGIN_PRESET_COUNT = 1_000_000
MAX_PLUGIN_PRESET_PAGE_SIZE = 256
MAX_PLUGIN_PRESET_NAME_LENGTH = 256
MAX_PLUGIN_PAD_COUNT = 512
MAX_PRESET_NAVIGATION_STEPS = 256
MAX_PRESET_SETTLE_TICKS = 8

LoopMode = Literal["pattern", "song"]
ChannelKind = Literal[
    "sampler",
    "hybrid",
    "generator_plugin",
    "layer",
    "audio_clip",
    "automation_clip",
    "lightning",
    "unknown",
]


def normalize_fl_color(value: int | None) -> int | None:
    """Return FL's signed or unsigned 32-bit color word as unsigned.

    FL's Python getter can expose the same 32 bits as a negative signed
    integer even though JSON and the public contract use ``0..0xffffffff``.
    Preserve the bit pattern rather than treating the sign as color data or
    dropping FL's unused high byte.
    """

    if value is None:
        return None
    if type(value) is not int:
        raise ValueError("FL color must be an integer")
    if value < FL_COLOR_SIGNED_MIN or value > FL_COLOR_WORD_MAX:
        raise ValueError("FL color must fit one signed or unsigned 32-bit word")
    return value & FL_COLOR_WORD_MAX


def fl_color_rgb(value: int | None) -> int | None:
    """Return the controllable ``0x00BBGGRR`` portion of an FL color word.

    FL Studio owns the high byte: a setter request such as ``0x0055AA`` can
    legitimately read back as ``0xFF0055AA``.  Observations retain the exact
    unsigned 32-bit word, while write guards and proof compare these RGB bits.
    """

    word = normalize_fl_color(value)
    return None if word is None else word & FL_COLOR_RGB_MASK


def fl_colors_equivalent(left: int | None, right: int | None) -> bool:
    """Whether two FL color words select the same controllable RGB value."""

    return fl_color_rgb(left) == fl_color_rgb(right)


class TrackBContract(ContractModel):
    """Strict and immutable input/output record for the Track B boundary."""

    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, strict=True, frozen=True
    )


class MixerEffectTarget(TrackBContract):
    """One mixer effect; the slot remains strictly zero through nine."""

    kind: Literal["mixer_effect"] = "mixer_effect"
    track_index: int = Field(ge=0)
    slot_index: int = Field(ge=0, le=9)
    allow_master: bool = False

    @model_validator(mode="after")
    def protect_master(self) -> "MixerEffectTarget":
        if self.track_index == 0 and not self.allow_master:
            raise ValueError(
                "mixer track 0 (Master) requires allow_master=true; no caller "
                "may infer that the master bus was intended"
            )
        return self


class ChannelGeneratorTarget(TrackBContract):
    """A channel's generator plug-in, addressed by a global channel index."""

    kind: Literal["channel_generator"] = "channel_generator"
    channel_index: int = Field(ge=0)
    index_scope: Literal["global"] = "global"


PluginTarget = Annotated[
    MixerEffectTarget | ChannelGeneratorTarget,
    Field(discriminator="kind"),
]


class NormalizedPluginTarget(TrackBContract):
    """Unambiguous FL plug-in addressing after legacy/target normalization."""

    kind: Literal["mixer_effect", "channel_generator"]
    bridge_index: int = Field(ge=0)
    track_index: int | None = Field(default=None, ge=0)
    channel_index: int | None = Field(default=None, ge=0)
    slot_index: int = Field(ge=-1, le=9)
    use_global_index: bool
    allow_master: bool = False

    @model_validator(mode="after")
    def validate_resolution(self) -> "NormalizedPluginTarget":
        if self.kind == "mixer_effect":
            if (
                self.track_index != self.bridge_index
                or self.channel_index is not None
                or self.slot_index < 0
                or self.use_global_index
            ):
                raise ValueError("mixer-effect resolution is internally inconsistent")
            if self.track_index == 0 and not self.allow_master:
                raise ValueError("mixer track 0 requires allow_master=true")
        elif (
            self.channel_index != self.bridge_index
            or self.track_index is not None
            or self.slot_index != -1
            or not self.use_global_index
            or self.allow_master
        ):
            raise ValueError("channel-generator resolution is internally inconsistent")
        return self


class TrackBVerifiedMutation(TrackBContract):
    """Target-neutral proof fields shared by readback-verified mutations."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    applied_at: datetime
    bridge_command: str
    verified: bool
    verification_summary: str = Field(min_length=1)
    verification_basis: Literal["readback_on_a_later_fl_idle_tick"] = (
        "readback_on_a_later_fl_idle_tick"
    )
    undo_point_created: bool | None = None
    project_saved: Literal[False] = False
    session_fingerprint: str | None = Field(
        default=None, pattern=SESSION_FINGERPRINT_PATTERN
    )
    session_precondition_applied: bool = False
    expected_before_applied: bool = False
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class ExpectedPlayingState(TrackBContract):
    playing: bool


class ExpectedStopState(TrackBContract):
    playing: bool | None = None
    song_position_normalized: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def require_one_field(self) -> "ExpectedStopState":
        if self.playing is None and self.song_position_normalized is None:
            raise ValueError(
                "expected_before needs playing, song_position_normalized, or both"
            )
        return self


class ExpectedSongPositionState(TrackBContract):
    song_position_normalized: float = Field(ge=0.0, le=1.0)


class ExpectedLoopModeState(TrackBContract):
    loop_mode: LoopMode


class ExpectedTempoState(TrackBContract):
    tempo_bpm: float = Field(ge=10.0, le=522.0)


class ExpectedRecordingState(TrackBContract):
    recording: bool


class ExpectedMetronomeState(TrackBContract):
    enabled: bool


class ExpectedPrecountState(TrackBContract):
    enabled: bool


class ExpectedTimeSignatureState(TrackBContract):
    numerator: int = Field(ge=1, le=32)


class ExpectedProjectHistoryState(TrackBContract):
    position: int | None = Field(default=None, ge=0)
    count: int | None = Field(default=None, ge=0)
    project_dirty_flag: Literal[0, 1, 2] | None = None

    @model_validator(mode="after")
    def require_one_field(self) -> "ExpectedProjectHistoryState":
        if (
            self.position is None
            and self.count is None
            and self.project_dirty_flag is None
        ):
            raise ValueError("expected_before needs a history position, count, or dirty flag")
        return self


class VerifiedPlayingWrite(TrackBVerifiedMutation):
    bridge_command: Literal["transport.set_playing"] = "transport.set_playing"
    requested_playing: bool
    before_playing: bool | None = None
    after_playing: bool | None = None


class VerifiedStopWrite(TrackBVerifiedMutation):
    bridge_command: Literal["transport.stop"] = "transport.stop"
    requested_playing: Literal[False] = False
    requested_song_position_normalized: float = Field(default=0.0, ge=0.0, le=0.0)
    before_playing: bool | None = None
    after_playing: bool | None = None
    before_song_position_normalized: float | None = None
    after_song_position_normalized: float | None = None
    playing_verified: bool
    position_verified: bool

    @model_validator(mode="after")
    def aggregate_fields(self) -> "VerifiedStopWrite":
        if self.verified != (self.playing_verified and self.position_verified):
            raise ValueError("verified must be the AND of the two stop-state fields")
        return self


class VerifiedSongPositionWrite(TrackBVerifiedMutation):
    bridge_command: Literal["transport.set_song_position"] = (
        "transport.set_song_position"
    )
    requested_song_position_normalized: float = Field(ge=0.0, le=1.0)
    tolerance: float = Field(ge=0.0, le=0.05)
    before_song_position_normalized: float | None = None
    after_song_position_normalized: float | None = None


class VerifiedLoopModeWrite(TrackBVerifiedMutation):
    bridge_command: Literal["transport.set_loop_mode"] = "transport.set_loop_mode"
    requested_loop_mode: LoopMode
    before_loop_mode: LoopMode | None = None
    after_loop_mode: LoopMode | None = None


class VerifiedTempoWrite(TrackBVerifiedMutation):
    bridge_command: Literal["transport.set_tempo"] = "transport.set_tempo"
    requested_tempo_bpm: float = Field(ge=10.0, le=522.0)
    before_tempo_bpm: float | None = None
    after_tempo_bpm: float | None = None


class VerifiedRecordingWrite(TrackBVerifiedMutation):
    bridge_command: Literal["transport.set_recording"] = "transport.set_recording"
    requested_recording: bool
    before_recording: bool | None = None
    after_recording: bool | None = None


class VerifiedMetronomeWrite(TrackBVerifiedMutation):
    bridge_command: Literal["transport.set_metronome"] = "transport.set_metronome"
    requested_enabled: bool
    before_enabled: bool | None = None
    after_enabled: bool | None = None


class VerifiedPrecountWrite(TrackBVerifiedMutation):
    bridge_command: Literal["transport.set_precount"] = "transport.set_precount"
    requested_enabled: bool
    before_enabled: bool | None = None
    after_enabled: bool | None = None


class TimeSignatureSnapshot(TrackBContract):
    numerator: int | None = Field(default=None, ge=1, le=32)
    ppq: int | None = Field(default=None, ge=1)
    pulses_per_bar: int | None = Field(default=None, ge=1)
    denominator_available: Literal[False] = False


class VerifiedTimeSignatureNumeratorWrite(TrackBVerifiedMutation):
    bridge_command: Literal["project.set_time_signature_numerator"] = (
        "project.set_time_signature_numerator"
    )
    requested_numerator: int = Field(ge=1, le=32)
    before: TimeSignatureSnapshot
    after: TimeSignatureSnapshot


class ProjectHistorySnapshot(TrackBContract):
    position: int = Field(ge=0)
    count: int = Field(ge=0)
    last_position: int = Field(ge=0)
    level_hint: str = Field(max_length=512)
    project_dirty_flag: Literal[0, 1, 2] | None = None
    can_undo: bool
    can_redo: bool

    @model_validator(mode="after")
    def validate_bounds(self) -> "ProjectHistorySnapshot":
        if self.position > self.count or self.last_position > self.count:
            raise ValueError("undo-history position/last/count bounds are inconsistent")
        if self.can_undo != (self.position > 1):
            raise ValueError("can_undo contradicts the history position")
        if self.can_redo != (self.position < self.count):
            raise ValueError("can_redo contradicts the history count")
        return self


class ProjectHistoryObservation(TrackBContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    history: ProjectHistorySnapshot
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


class VerifiedProjectHistoryMove(TrackBVerifiedMutation):
    bridge_command: Literal["project.undo", "project.redo"]
    direction: Literal["undo", "redo"]
    requested_position: int = Field(ge=0)
    before: ProjectHistorySnapshot
    after: ProjectHistorySnapshot

    @model_validator(mode="after")
    def direction_matches_command(self) -> "VerifiedProjectHistoryMove":
        if self.bridge_command != "project." + self.direction:
            raise ValueError("history direction contradicts bridge command")
        if self.verified != (self.after.position == self.requested_position):
            raise ValueError("verified must reflect the requested history position")
        return self


# ---------------------------------------------------------------------------
# Channel Rack
# ---------------------------------------------------------------------------


class ChannelGeneratorIdentity(TrackBContract):
    name: str = Field(min_length=1, max_length=256)
    reported_parameter_count: int | None = Field(default=None, ge=0)
    exact_version: None = Field(
        default=None,
        description="The public MIDI scripting API does not expose plug-in versions.",
    )


def _compute_channel_fingerprint_raw(
    *,
    channel_index: int,
    name: str,
    channel_type_code: int | None,
    color: int | None,
    mixer_destination: int | None,
    generator_name: str | None,
) -> str:
    """Hash one already-selected spelling of the channel identity."""

    material = {
        "channel_index": channel_index,
        "channel_type_code": channel_type_code,
        "color": color,
        "generator_name": generator_name,
        "mixer_destination": mixer_destination,
        "name": name,
        "scope": "global",
    }
    encoded = json.dumps(
        material, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def compute_channel_fingerprint(
    *,
    channel_index: int,
    name: str,
    channel_type_code: int | None,
    color: int | None,
    mixer_destination: int | None,
    generator_name: str | None,
) -> str:
    """Return the canonical observation-scoped channel identity token.

    FL exposes no durable Channel Rack UUID.  This digest therefore detects a
    changed/reordered target within a bridge session; it must never be treated
    as a permanent project identifier. Signed and unsigned spellings of the
    same FL color word intentionally produce the same fingerprint.
    """

    return _compute_channel_fingerprint_raw(
        channel_index=channel_index,
        name=name,
        channel_type_code=channel_type_code,
        color=normalize_fl_color(color),
        mixer_destination=mixer_destination,
        generator_name=generator_name,
    )


class ChannelSummary(TrackBContract):
    channel_index: int = Field(ge=0)
    index_scope: Literal["global"] = "global"
    name: str = Field(max_length=256)
    channel_type_code: int | None = None
    channel_type: ChannelKind = "unknown"
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)
    volume_normalized: float | None = None
    pan: float | None = None
    pitch_normalized: float | None = Field(default=None, ge=-1.0, le=1.0)
    pitch_semitones: float | None = None
    pitch_range_semitones: float | None = Field(default=None, ge=0.0)
    muted: bool | None = None
    soloed: bool | None = None
    selected: bool | None = None
    mixer_destination: int | None = Field(default=None, ge=-1)
    generator: ChannelGeneratorIdentity | None = None
    channel_fingerprint: str = Field(pattern=SHA256_PATTERN)
    identity_scope: Literal["observation_scoped_not_durable"] = (
        "observation_scoped_not_durable"
    )

    @model_validator(mode="after")
    def fingerprint_matches_identity(self) -> "ChannelSummary":
        expected = compute_channel_fingerprint(
            channel_index=self.channel_index,
            name=self.name,
            channel_type_code=self.channel_type_code,
            color=self.color,
            mixer_destination=self.mixer_destination,
            generator_name=None if self.generator is None else self.generator.name,
        )
        if self.channel_fingerprint != expected:
            raise ValueError("channel_fingerprint does not match the channel identity")
        return self


class ChannelList(TrackBContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    project_dirty_flag: Literal[0, 1, 2] | None = None
    total_channel_count: int = Field(ge=0)
    scanned_channel_count: int = Field(ge=0)
    channels: list[ChannelSummary]
    partial: bool = False
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def counts_match_payload(self) -> "ChannelList":
        if self.scanned_channel_count != len(self.channels):
            raise ValueError("scanned_channel_count must equal the channel list length")
        if self.scanned_channel_count > self.total_channel_count:
            raise ValueError("scanned_channel_count cannot exceed total_channel_count")
        return self


# ---------------------------------------------------------------------------
# Target-aware plug-ins (mixer effects and channel generators)
# ---------------------------------------------------------------------------


class TargetedPluginSummary(TrackBContract):
    target: PluginTarget
    name: str = Field(max_length=256)
    user_name: str | None = Field(default=None, max_length=256)
    target_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    reported_parameter_count: int | None = Field(default=None, ge=0)
    mix_level_normalized: float | None = None
    exact_version: None = Field(
        default=None,
        description="The public MIDI scripting API does not expose plug-in versions.",
    )

    @model_validator(mode="after")
    def generator_has_no_mixer_wet_level(self) -> "TargetedPluginSummary":
        if (
            isinstance(self.target, ChannelGeneratorTarget)
            and self.mix_level_normalized is not None
        ):
            raise ValueError("a channel generator has no mixer-slot wet level")
        return self


class TargetedPluginParameter(TrackBContract):
    index: int = Field(ge=0)
    reported_name: str = Field(max_length=256)
    normalized_value: float | None = None
    display_text: str | None = Field(default=None, max_length=256)
    display_text_available: bool
    classification: Literal["reported", "padding_candidate"] = "reported"
    profile_status: Literal["unprofiled_read_only"] = "unprofiled_read_only"
    safe_to_modify: Literal[False] = False

    @model_validator(mode="after")
    def display_availability_matches(self) -> "TargetedPluginParameter":
        if self.display_text_available != (self.display_text is not None):
            raise ValueError("display_text_available contradicts display_text")
        return self


class TargetedPluginParameterPage(TrackBContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    project_dirty_flag: Literal[0, 1, 2] | None = None
    plugin: TargetedPluginSummary
    reported_parameter_count: int = Field(ge=0)
    offset: int = Field(ge=0)
    scanned_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    has_more: bool
    next_offset: int | None = Field(default=None, ge=0)
    parameters: list[TargetedPluginParameter]
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def counts_match(self) -> "TargetedPluginParameterPage":
        if self.returned_count != len(self.parameters):
            raise ValueError("returned_count must equal the parameter list length")
        return self


class TargetedPluginParameterScan(TrackBContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    project_dirty_flag: Literal[0, 1, 2] | None = None
    plugin: TargetedPluginSummary
    reported_parameter_count: int = Field(ge=0)
    scan_start: int = Field(ge=0)
    scan_end: int = Field(ge=0)
    examined_count: int = Field(ge=0)
    highest_index_examined: int | None = Field(default=None, ge=0)
    real_count: int = Field(ge=0)
    padding_skipped: int = Field(ge=0)
    truncated: bool
    truncated_by: Literal["max_indices", "max_results", "start", "end"] | None = None
    parameters: list[TargetedPluginParameter]
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def real_count_matches(self) -> "TargetedPluginParameterScan":
        if self.real_count != len(self.parameters):
            raise ValueError("real_count must equal the de-padded parameter length")
        return self


class TargetedLoadedPluginInventory(TrackBContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    project_dirty_flag: Literal[0, 1, 2] | None = None
    plugins: list[TargetedPluginSummary]
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


class PluginPresetCount(TrackBContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    plugin: TargetedPluginSummary
    preset_count: int = Field(ge=0)
    project_dirty_flag: Literal[0, 1, 2] | None = None
    warnings: list[str] = Field(default_factory=list)


PresetIdentityStatus = Literal[
    "stable",
    "blank",
    "ambiguous",
    "unsupported",
    "unresolved",
    "not_requested",
]


class PluginPresetState(TrackBContract):
    """One current-preset observation, kept separate from product identity."""

    name: str | None = Field(default=None, max_length=MAX_PLUGIN_PRESET_NAME_LENGTH)
    index: int | None = Field(default=None, ge=0)
    identity_status: PresetIdentityStatus

    @model_validator(mode="after")
    def status_matches_identity(self) -> "PluginPresetState":
        if self.identity_status == "stable" and not self.name:
            raise ValueError("stable preset identity requires a non-empty name")
        if self.identity_status == "blank":
            if self.name != "" or self.index is not None:
                raise ValueError("blank preset identity must have an empty name and no index")
        if self.identity_status in {"unsupported", "not_requested"}:
            if self.name is not None or self.index is not None:
                raise ValueError(
                    "unavailable preset identities cannot carry a name or index"
                )
        if self.identity_status == "unresolved" and self.index is not None:
            raise ValueError("an unresolved preset identity cannot carry an index")
        if self.identity_status == "ambiguous" and not self.name:
            raise ValueError("ambiguous preset identity requires a reported name")
        if self.index is not None and self.identity_status != "stable":
            raise ValueError("a resolved preset index requires stable identity")
        return self


class PluginPresetRecord(TrackBContract):
    """A bounded, deterministic preset page row."""

    index: int = Field(ge=0, le=MAX_PLUGIN_PRESET_COUNT - 1)
    name: str = Field(max_length=MAX_PLUGIN_PRESET_NAME_LENGTH)
    is_current: bool = False


class PluginPresetPage(TrackBContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    plugin: TargetedPluginSummary
    preset_count: int = Field(ge=0, le=MAX_PLUGIN_PRESET_COUNT)
    start: int = Field(ge=0, le=MAX_PLUGIN_PRESET_COUNT)
    limit: int = Field(ge=1, le=MAX_PLUGIN_PRESET_PAGE_SIZE)
    scanned_count: int = Field(ge=0, le=MAX_PLUGIN_PRESET_COUNT)
    returned_count: int = Field(ge=0, le=MAX_PLUGIN_PRESET_PAGE_SIZE)
    has_more: bool
    next_start: int | None = Field(default=None, ge=0, le=MAX_PLUGIN_PRESET_COUNT)
    presets: list[PluginPresetRecord] = Field(max_length=MAX_PLUGIN_PRESET_PAGE_SIZE)
    current_preset_name: str | None = Field(
        default=None, max_length=MAX_PLUGIN_PRESET_NAME_LENGTH
    )
    current_preset_index: int | None = Field(
        default=None, ge=0, le=MAX_PLUGIN_PRESET_COUNT - 1
    )
    current_preset_status: PresetIdentityStatus
    duplicate_names: list[str] = Field(max_length=MAX_PLUGIN_PRESET_PAGE_SIZE)
    blank_name_indices: list[int] = Field(max_length=MAX_PLUGIN_PRESET_PAGE_SIZE)
    partial: bool = False
    truncated: bool = False
    truncated_by: Literal["max_enumeration", "unsupported", "page"] | None = None
    session_fingerprint: str | None = Field(
        default=None, pattern=SESSION_FINGERPRINT_PATTERN
    )
    project_dirty_flag: Literal[0, 1, 2] | None = None
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def page_counts_and_identity_agree(self) -> "PluginPresetPage":
        if self.returned_count != len(self.presets):
            raise ValueError("returned_count must equal the preset page length")
        indexes = [row.index for row in self.presets]
        rows_by_index = {row.index: row for row in self.presets}
        if indexes != sorted(set(indexes)):
            raise ValueError("preset rows must be ordered and have unique indexes")
        if any(
            row.index < self.start
            or row.index >= self.start + self.limit
            or row.index >= self.preset_count
            for row in self.presets
        ):
            raise ValueError("preset rows must fall inside the requested page")
        if sum(row.is_current for row in self.presets) > 1:
            raise ValueError("a preset page cannot mark multiple rows current")
        if self.start > self.preset_count:
            raise ValueError("preset page start cannot exceed preset_count")
        if self.scanned_count > self.preset_count:
            raise ValueError("scanned_count cannot exceed preset_count")
        if self.returned_count > self.scanned_count:
            raise ValueError("returned_count cannot exceed scanned_count")
        if self.current_preset_index is not None:
            if self.current_preset_status != "stable" or not self.current_preset_name:
                raise ValueError("current_preset_index requires a stable current name")
            if self.current_preset_index >= self.preset_count:
                raise ValueError("current_preset_index cannot exceed preset_count")
        if self.current_preset_status == "stable" and not self.current_preset_name:
            raise ValueError("stable current preset status requires a name")
        if self.current_preset_status == "blank" and self.current_preset_name != "":
            raise ValueError("blank current preset status requires an empty name")
        if self.current_preset_status in {"unsupported", "not_requested"}:
            if self.current_preset_name is not None or self.current_preset_index is not None:
                raise ValueError("unresolved current preset cannot carry identity fields")
        if self.current_preset_status == "unresolved" and self.current_preset_index is not None:
            raise ValueError("an unresolved current preset cannot carry an index")
        # Validate the status/name/index tuple through the same state contract
        # used by current-preset and mutation receipts. This also rejects an
        # ambiguous or blank status that carries an identity field it cannot
        # truthfully support.
        PluginPresetState(
            name=self.current_preset_name,
            index=self.current_preset_index,
            identity_status=self.current_preset_status,
        )
        expected_more = self.start + self.limit < self.preset_count
        if self.has_more != expected_more and not self.truncated:
            raise ValueError("has_more contradicts preset_count, start, and limit")
        if self.has_more:
            if self.next_start != self.start + self.limit:
                raise ValueError("next_start must equal start + limit when has_more is true")
        elif self.next_start is not None:
            raise ValueError("next_start is only valid when has_more is true")
        if self.duplicate_names != sorted(set(self.duplicate_names)):
            raise ValueError("duplicate_names must be sorted and unique")
        if self.blank_name_indices != sorted(set(self.blank_name_indices)):
            raise ValueError("blank_name_indices must be sorted and unique")
        if any(
            index < self.start
            or index >= self.start + self.limit
            or index >= self.preset_count
            for index in self.blank_name_indices
        ):
            raise ValueError("blank_name_indices must fall inside the requested page")
        expected_duplicates = sorted(
            {
                row.name
                for row in self.presets
                if row.name
                and sum(other.name == row.name for other in self.presets) > 1
            }
        )
        if self.duplicate_names != expected_duplicates:
            raise ValueError("duplicate_names does not match the preset page")

        row_blank_indices = {
            row.index for row in self.presets if row.name == ""
        }
        if not row_blank_indices.issubset(self.blank_name_indices):
            raise ValueError(
                "blank_name_indices must include every returned blank-name row"
            )
        if any(
            index in rows_by_index and rows_by_index[index].name != ""
            for index in self.blank_name_indices
        ):
            raise ValueError(
                "blank_name_indices cannot identify a non-blank preset row"
            )

        current_rows = [row for row in self.presets if row.is_current]
        if current_rows:
            if (
                self.current_preset_status != "stable"
                or self.current_preset_name is None
                or self.current_preset_index is None
            ):
                raise ValueError(
                    "is_current requires a stable current name and index"
                )
            current_row = current_rows[0]
            if (
                current_row.index != self.current_preset_index
                or current_row.name != self.current_preset_name
            ):
                raise ValueError(
                    "is_current must identify the reported current preset"
                )
        elif (
            self.current_preset_status == "stable"
            and self.current_preset_index is not None
            and self.current_preset_index in rows_by_index
        ):
            current_row = rows_by_index[self.current_preset_index]
            if current_row.name != self.current_preset_name:
                raise ValueError(
                    "current_preset_index does not match the page row name"
                )
            raise ValueError(
                "the page row at current_preset_index must be marked is_current"
            )
        return self


class PluginCurrentPreset(TrackBContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    plugin: TargetedPluginSummary
    preset_count: int = Field(ge=0, le=MAX_PLUGIN_PRESET_COUNT)
    current_preset_name: str | None = Field(
        default=None, max_length=MAX_PLUGIN_PRESET_NAME_LENGTH
    )
    current_preset_index: int | None = Field(
        default=None, ge=0, le=MAX_PLUGIN_PRESET_COUNT - 1
    )
    current_preset_status: PresetIdentityStatus
    session_fingerprint: str | None = Field(
        default=None, pattern=SESSION_FINGERPRINT_PATTERN
    )
    project_dirty_flag: Literal[0, 1, 2] | None = None
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def identity_fields_match(self) -> "PluginCurrentPreset":
        PluginPresetState(
            name=self.current_preset_name,
            index=self.current_preset_index,
            identity_status=self.current_preset_status,
        )
        return self

    @property
    def current(self) -> PluginPresetState:
        return PluginPresetState(
            name=self.current_preset_name,
            index=self.current_preset_index,
            identity_status=self.current_preset_status,
        )


class PluginPad(TrackBContract):
    pad_index: int = Field(ge=0, le=MAX_PLUGIN_PAD_COUNT - 1)
    semitone: int | None = Field(default=None, ge=0, le=127)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)
    empty: bool | None = None
    muted: bool | None = None
    semitone_name: str | None = Field(
        default=None, max_length=MAX_PLUGIN_PRESET_NAME_LENGTH
    )


class PluginPadMap(TrackBContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    plugin: TargetedPluginSummary
    pad_count: int = Field(ge=0, le=MAX_PLUGIN_PAD_COUNT)
    pads: list[PluginPad] = Field(max_length=MAX_PLUGIN_PAD_COUNT)
    complete: bool
    session_fingerprint: str | None = Field(
        default=None, pattern=SESSION_FINGERPRINT_PATTERN
    )
    project_dirty_flag: Literal[0, 1, 2] | None = None
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def pads_match_count(self) -> "PluginPadMap":
        expected = list(range(len(self.pads)))
        if [pad.pad_index for pad in self.pads] != expected:
            raise ValueError("pad rows must be contiguous and ordered from zero")
        if self.complete and len(self.pads) != self.pad_count:
            raise ValueError("complete pad maps must contain every reported pad")
        if len(self.pads) > self.pad_count:
            raise ValueError("pad rows cannot exceed pad_count")
        return self


class ExpectedPluginPresetState(TrackBContract):
    """Optional stale-read guard for a preset selection."""

    name: str | None = Field(default=None, max_length=MAX_PLUGIN_PRESET_NAME_LENGTH)
    index: int | None = Field(default=None, ge=0, le=MAX_PLUGIN_PRESET_COUNT - 1)

    @model_validator(mode="after")
    def require_one_preset_field(self) -> "ExpectedPluginPresetState":
        if self.name is None and self.index is None:
            raise ValueError("expected current preset needs name, index, or both")
        return self


class VerifiedPluginPresetSelection(TrackBVerifiedMutation):
    bridge_command: Literal["plugin.select_preset"] = "plugin.select_preset"
    target: PluginTarget
    plugin: TargetedPluginSummary
    requested_preset_name: str | None = Field(
        default=None, max_length=MAX_PLUGIN_PRESET_NAME_LENGTH
    )
    requested_preset_index: int | None = Field(
        default=None, ge=0, le=MAX_PLUGIN_PRESET_COUNT - 1
    )
    before: PluginPresetState
    after: PluginPresetState
    outcome: Literal["verified", "not_reached", "unknown"]
    navigation_direction: Literal["none", "next", "prev", "fallback_next"]
    navigation_steps: int = Field(ge=0, le=MAX_PRESET_NAVIGATION_STEPS)
    max_navigation_steps: int = Field(ge=0, le=MAX_PRESET_NAVIGATION_STEPS)
    settle_tick_limit: int = Field(ge=1, le=MAX_PRESET_SETTLE_TICKS)
    target_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def selection_proof_is_truthful(self) -> "VerifiedPluginPresetSelection":
        if self.requested_preset_name is None and self.requested_preset_index is None:
            raise ValueError("preset selection needs a name, index, or both")
        if self.outcome == "verified":
            if not self.verified or self.after.identity_status != "stable":
                raise ValueError("verified preset selection needs stable readback")
            if (
                self.requested_preset_name is not None
                and self.after.name != self.requested_preset_name
            ):
                raise ValueError("verified preset name does not match the request")
            if (
                self.requested_preset_index is not None
                and self.after.index != self.requested_preset_index
            ):
                raise ValueError("verified preset index does not match the request")
        elif self.verified:
            raise ValueError("unverified preset outcomes cannot set verified=true")
        if self.navigation_steps > self.max_navigation_steps:
            raise ValueError("navigation_steps cannot exceed max_navigation_steps")
        if self.navigation_direction == "none" and self.navigation_steps != 0:
            raise ValueError("navigation_direction='none' requires zero navigation steps")
        return self


class LoopStarterRerollDispatch(TrackBContract):
    """One explicit Loop Starter reroll; identity is intentionally unverified."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    bridge_command: Literal["channels.rerollLoopStarterLoop"] = (
        "channels.rerollLoopStarterLoop"
    )
    channel_index: int = Field(ge=0)
    index_scope: Literal["global"] = "global"
    before_channel_fingerprint: str | None = Field(
        default=None, pattern=SHA256_PATTERN
    )
    after_channel_fingerprint: str | None = Field(
        default=None, pattern=SHA256_PATTERN
    )
    dispatched: bool
    identity_verified: Literal[False] = False
    verified: Literal[False] = False
    verification_basis: Literal["dispatch_only_no_identity_readback"] = (
        "dispatch_only_no_identity_readback"
    )
    undo_point_created: bool | None = None
    project_saved: Literal[False] = False
    session_fingerprint: str | None = Field(
        default=None, pattern=SESSION_FINGERPRINT_PATTERN
    )
    session_precondition_applied: bool = False
    expected_before_applied: bool = False
    warnings: list[str] = Field(default_factory=list)


class PluginParameterSnapshot(TrackBContract):
    normalized_value: float | None = None
    display_text: str | None = Field(default=None, max_length=256)
    display_text_available: bool

    @model_validator(mode="after")
    def display_availability_matches(self) -> "PluginParameterSnapshot":
        if self.display_text_available != (self.display_text is not None):
            raise ValueError("display_text_available contradicts display_text")
        return self


class ExpectedPluginParameterState(TrackBContract):
    normalized_value: float | None = Field(default=None, ge=0.0, le=1.0)
    display_text: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def require_one_field(self) -> "ExpectedPluginParameterState":
        if self.normalized_value is None and self.display_text is None:
            raise ValueError(
                "expected_before needs normalized_value, display_text, or both"
            )
        return self


PluginMatchKind = Literal[
    "index", "name", "display", "name_substring", "display_substring"
]
PluginProofKind = Literal["value_readback", "display_change_only", "none"]


class VerifiedTargetedPluginParameterWrite(TrackBVerifiedMutation):
    bridge_command: Literal["plugin.set_param"] = "plugin.set_param"
    target: PluginTarget
    parameter_index: int = Field(ge=0)
    plugin_name: str = Field(max_length=256)
    parameter_name: str = Field(max_length=256)
    requested_normalized_value: float = Field(ge=0.0, le=1.0)
    before: PluginParameterSnapshot
    after: PluginParameterSnapshot
    display_changed: bool
    reads_at_requested_value: bool
    verification_basis_detail: PluginProofKind

    @model_validator(mode="after")
    def proof_fields_agree(self) -> "VerifiedTargetedPluginParameterWrite":
        expected = (
            "value_readback"
            if self.reads_at_requested_value
            else "display_change_only"
            if self.display_changed
            else "none"
        )
        if self.verification_basis_detail != expected:
            raise ValueError("plug-in verification basis contradicts its evidence")
        if self.verified != (expected != "none"):
            raise ValueError("verified contradicts plug-in readback evidence")
        return self


class VerifiedTargetedPluginDisplayWrite(TrackBVerifiedMutation):
    bridge_command: Literal["plugin.set_param_display"] = "plugin.set_param_display"
    target: PluginTarget
    parameter_index: int = Field(ge=0)
    plugin_name: str | None = Field(default=None, max_length=256)
    parameter_name: str | None = Field(default=None, max_length=256)
    matched_on: PluginMatchKind
    matched_text: str | None = Field(default=None, max_length=256)
    requested_value: float
    tolerance: float = Field(ge=0.0, le=1e6)
    landed_value: float | None = None
    normalized_value: float | None = None
    before: PluginParameterSnapshot
    after: PluginParameterSnapshot


class VerifiedTargetedPluginOptionWrite(TrackBVerifiedMutation):
    bridge_command: Literal["plugin.set_param_option"] = "plugin.set_param_option"
    target: PluginTarget
    parameter_index: int = Field(ge=0)
    plugin_name: str | None = Field(default=None, max_length=256)
    parameter_name: str | None = Field(default=None, max_length=256)
    matched_on: PluginMatchKind
    matched_text: str | None = Field(default=None, max_length=256)
    requested_option: str = Field(min_length=1, max_length=256)
    selected_option: str | None = Field(default=None, max_length=256)
    normalized_value: float | None = None
    sweep_steps: int = Field(ge=2, le=256)
    options: list[str] = Field(default_factory=list)
    before: PluginParameterSnapshot
    after: PluginParameterSnapshot


class ExpectedChannelMixState(TrackBContract):
    channel_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    volume_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    pan: float | None = Field(default=None, ge=-1.0, le=1.0)
    muted: bool | None = None

    @model_validator(mode="after")
    def require_one_field(self) -> "ExpectedChannelMixState":
        if all(
            value is None
            for value in (
                self.channel_fingerprint,
                self.volume_normalized,
                self.pan,
                self.muted,
            )
        ):
            raise ValueError("expected_before needs at least one channel mix field")
        return self


class ExpectedChannelIdentityState(TrackBContract):
    channel_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    name: str | None = Field(default=None, max_length=MAX_CHANNEL_NAME_LENGTH)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)

    @model_validator(mode="after")
    def require_one_field(self) -> "ExpectedChannelIdentityState":
        if self.channel_fingerprint is None and self.name is None and self.color is None:
            raise ValueError("expected_before needs at least one channel identity field")
        return self


class ExpectedChannelRouteState(TrackBContract):
    channel_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    mixer_destination: int | None = Field(default=None, ge=-1)

    @model_validator(mode="after")
    def require_one_field(self) -> "ExpectedChannelRouteState":
        if self.channel_fingerprint is None and self.mixer_destination is None:
            raise ValueError("expected_before needs a fingerprint and/or destination")
        return self


class ExpectedChannelSoloState(TrackBContract):
    channel_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    soloed: bool | None = None

    @model_validator(mode="after")
    def require_one_field(self) -> "ExpectedChannelSoloState":
        if self.channel_fingerprint is None and self.soloed is None:
            raise ValueError("expected_before needs a fingerprint and/or solo state")
        return self


class ExpectedChannelPitchState(TrackBContract):
    channel_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    pitch_normalized: float | None = Field(default=None, ge=-1.0, le=1.0)

    @model_validator(mode="after")
    def require_one_field(self) -> "ExpectedChannelPitchState":
        if self.channel_fingerprint is None and self.pitch_normalized is None:
            raise ValueError("expected_before needs a fingerprint and/or pitch")
        return self


class ExpectedChannelSelectionState(TrackBContract):
    selected_channel_indices: list[int]

    @model_validator(mode="after")
    def require_canonical_indices(self) -> "ExpectedChannelSelectionState":
        values = self.selected_channel_indices
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("selected channel indices must be non-negative integers")
        if values != sorted(set(values)):
            raise ValueError("selected channel indices must be sorted and unique")
        return self


class ExpectedChannelTargetState(TrackBContract):
    """Strong observation-scoped guard for an event aimed at one channel."""

    channel_fingerprint: str = Field(pattern=SHA256_PATTERN)


class ChannelMixSnapshot(TrackBContract):
    volume_normalized: float | None = None
    pan: float | None = None
    muted: bool | None = None
    channel_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)


class ChannelIdentitySnapshot(TrackBContract):
    name: str | None = None
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)
    channel_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)


class ChannelRouteSnapshot(TrackBContract):
    mixer_destination: int | None = None
    channel_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)


class ChannelSoloSnapshot(TrackBContract):
    soloed: bool | None = None
    channel_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)


class ChannelPitchSnapshot(TrackBContract):
    pitch_normalized: float | None = Field(default=None, ge=-1.0, le=1.0)
    pitch_semitones: float | None = None
    pitch_range_semitones: float | None = Field(default=None, ge=0.0)
    channel_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)


class VerifiedChannelMixWrite(TrackBVerifiedMutation):
    bridge_command: Literal["channel.set_mix"] = "channel.set_mix"
    channel_index: int = Field(ge=0)
    index_scope: Literal["global"] = "global"
    requested_volume_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    requested_pan: float | None = Field(default=None, ge=-1.0, le=1.0)
    requested_muted: bool | None = None
    before: ChannelMixSnapshot
    after: ChannelMixSnapshot
    volume_verified: bool | None = None
    pan_verified: bool | None = None
    mute_verified: bool | None = None

    @model_validator(mode="after")
    def verify_requested_fields(self) -> "VerifiedChannelMixWrite":
        pairs = (
            (self.requested_volume_normalized, self.volume_verified),
            (self.requested_pan, self.pan_verified),
            (self.requested_muted, self.mute_verified),
        )
        if all(requested is None for requested, _ in pairs):
            raise ValueError("at least one channel mix field must be requested")
        if any((requested is None) != (proof is None) for requested, proof in pairs):
            raise ValueError("only requested channel mix fields may have proof flags")
        aggregate = all(bool(proof) for requested, proof in pairs if requested is not None)
        if self.verified != aggregate:
            raise ValueError("verified must be the AND of requested channel mix fields")
        return self


class VerifiedChannelIdentityWrite(TrackBVerifiedMutation):
    bridge_command: Literal["channel.set_identity"] = "channel.set_identity"
    channel_index: int = Field(ge=0)
    index_scope: Literal["global"] = "global"
    requested_name: str | None = Field(default=None, max_length=MAX_CHANNEL_NAME_LENGTH)
    requested_color: int | None = Field(
        default=None, ge=0, le=FL_COLOR_WORD_MAX
    )
    before: ChannelIdentitySnapshot
    after: ChannelIdentitySnapshot
    name_verified: bool | None = None
    color_verified: bool | None = None

    @model_validator(mode="after")
    def verify_requested_fields(self) -> "VerifiedChannelIdentityWrite":
        pairs = (
            (self.requested_name, self.name_verified),
            (self.requested_color, self.color_verified),
        )
        if all(requested is None for requested, _ in pairs):
            raise ValueError("name, color, or both must be requested")
        if any((requested is None) != (proof is None) for requested, proof in pairs):
            raise ValueError("only requested identity fields may have proof flags")
        aggregate = all(bool(proof) for requested, proof in pairs if requested is not None)
        if self.verified != aggregate:
            raise ValueError("verified must be the AND of requested identity fields")
        return self


class VerifiedChannelRouteWrite(TrackBVerifiedMutation):
    bridge_command: Literal["channel.route_to_mixer"] = "channel.route_to_mixer"
    channel_index: int = Field(ge=0)
    index_scope: Literal["global"] = "global"
    requested_mixer_destination: int = Field(ge=-1)
    before: ChannelRouteSnapshot
    after: ChannelRouteSnapshot


class VerifiedChannelSoloWrite(TrackBVerifiedMutation):
    bridge_command: Literal["channel.set_solo"] = "channel.set_solo"
    channel_index: int = Field(ge=0)
    index_scope: Literal["global"] = "global"
    requested_soloed: bool
    before: ChannelSoloSnapshot
    after: ChannelSoloSnapshot


class VerifiedChannelPitchWrite(TrackBVerifiedMutation):
    bridge_command: Literal["channel.set_pitch"] = "channel.set_pitch"
    channel_index: int = Field(ge=0)
    index_scope: Literal["global"] = "global"
    requested_pitch_normalized: float = Field(ge=-1.0, le=1.0)
    before: ChannelPitchSnapshot
    after: ChannelPitchSnapshot


class VerifiedChannelSelectionWrite(TrackBVerifiedMutation):
    bridge_command: Literal["channel.select"] = "channel.select"
    channel_index: int = Field(ge=0)
    index_scope: Literal["global"] = "global"
    exclusive: Literal[True] = True
    before_selected_channel_indices: list[int]
    after_selected_channel_indices: list[int]

    @model_validator(mode="after")
    def verify_exclusive_selection(self) -> "VerifiedChannelSelectionWrite":
        if self.verified != (self.after_selected_channel_indices == [self.channel_index]):
            raise ValueError("verified must reflect exclusive channel selection")
        return self


# ---------------------------------------------------------------------------
# Patterns and Playlist tracks
# ---------------------------------------------------------------------------


class PatternSummary(TrackBContract):
    pattern_number: int = Field(ge=1, le=MAX_PATTERN_NUMBER)
    name: str = Field(max_length=MAX_PATTERN_NAME_LENGTH)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)
    length_beats: int | None = Field(
        default=None, ge=1, le=MAX_PATTERN_LENGTH_BEATS
    )
    current: bool
    selected_in_picker: bool | None = None
    default_empty: bool | None = None


class PatternList(TrackBContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    current_pattern_number: int = Field(ge=1, le=MAX_PATTERN_NUMBER)
    reported_pattern_count: int = Field(ge=0, le=MAX_PATTERN_NUMBER)
    maximum_pattern_number: int = Field(ge=1, le=MAX_PATTERN_NUMBER)
    patterns: list[PatternSummary]
    project_dirty_flag: Literal[0, 1, 2] | None = None
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


class EmptyPatternSearch(TrackBContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    start_pattern_number: int = Field(ge=1, le=MAX_PATTERN_NUMBER)
    empty_pattern_number: int | None = Field(
        default=None, ge=1, le=MAX_PATTERN_NUMBER
    )
    scanned_pattern_count: int = Field(ge=0, le=MAX_PATTERN_NUMBER)
    current_pattern_unchanged: bool
    project_dirty_flag: Literal[0, 1, 2] | None = None
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


class ExpectedPatternSelectionState(TrackBContract):
    current_pattern_number: int = Field(ge=1, le=MAX_PATTERN_NUMBER)


class ExpectedPatternIdentityState(TrackBContract):
    name: str | None = Field(default=None, max_length=MAX_PATTERN_NAME_LENGTH)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)

    @model_validator(mode="after")
    def require_one_field(self) -> "ExpectedPatternIdentityState":
        if self.name is None and self.color is None:
            raise ValueError("expected_before needs a pattern name and/or color")
        return self


class ExpectedPatternLengthState(TrackBContract):
    length_beats: int = Field(ge=1, le=MAX_PATTERN_LENGTH_BEATS)


class PatternIdentitySnapshot(TrackBContract):
    name: str | None = Field(default=None, max_length=MAX_PATTERN_NAME_LENGTH)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)


class VerifiedPatternSelectionWrite(TrackBVerifiedMutation):
    bridge_command: Literal["pattern.select"] = "pattern.select"
    requested_pattern_number: int = Field(ge=1, le=MAX_PATTERN_NUMBER)
    before_pattern_number: int | None = Field(
        default=None, ge=1, le=MAX_PATTERN_NUMBER
    )
    after_pattern_number: int | None = Field(
        default=None, ge=1, le=MAX_PATTERN_NUMBER
    )


class VerifiedPatternIdentityWrite(TrackBVerifiedMutation):
    bridge_command: Literal["pattern.set_identity"] = "pattern.set_identity"
    pattern_number: int = Field(ge=1, le=MAX_PATTERN_NUMBER)
    requested_name: str | None = Field(
        default=None, max_length=MAX_PATTERN_NAME_LENGTH
    )
    requested_color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)
    before: PatternIdentitySnapshot
    after: PatternIdentitySnapshot
    name_verified: bool | None = None
    color_verified: bool | None = None

    @model_validator(mode="after")
    def verify_requested_fields(self) -> "VerifiedPatternIdentityWrite":
        pairs = (
            (self.requested_name, self.name_verified),
            (self.requested_color, self.color_verified),
        )
        if all(requested is None for requested, _ in pairs):
            raise ValueError("name, color, or both must be requested")
        if any((requested is None) != (proof is None) for requested, proof in pairs):
            raise ValueError("only requested pattern fields may have proof flags")
        expected = all(bool(proof) for requested, proof in pairs if requested is not None)
        if self.verified != expected:
            raise ValueError("verified must be the AND of requested pattern fields")
        return self


class VerifiedPatternLengthWrite(TrackBVerifiedMutation):
    bridge_command: Literal["pattern.set_length"] = "pattern.set_length"
    pattern_number: int = Field(ge=1, le=MAX_PATTERN_NUMBER)
    requested_length_beats: int = Field(ge=1, le=MAX_PATTERN_LENGTH_BEATS)
    before_length_beats: int | None = Field(
        default=None, ge=1, le=MAX_PATTERN_LENGTH_BEATS
    )
    after_length_beats: int | None = Field(
        default=None, ge=1, le=MAX_PATTERN_LENGTH_BEATS
    )


class PlaylistTrackSummary(TrackBContract):
    track_index: int = Field(ge=1)
    name: str = Field(max_length=MAX_PLAYLIST_TRACK_NAME_LENGTH)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)
    muted: bool | None = None
    soloed: bool | None = None
    selected: bool | None = None
    activity_level: float | None = None


class PlaylistTrackList(TrackBContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    total_track_count: int = Field(ge=0)
    tracks: list[PlaylistTrackSummary]
    project_dirty_flag: Literal[0, 1, 2] | None = None
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


class ExpectedPlaylistTrackIdentityState(TrackBContract):
    name: str | None = Field(default=None, max_length=MAX_PLAYLIST_TRACK_NAME_LENGTH)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)

    @model_validator(mode="after")
    def require_one_field(self) -> "ExpectedPlaylistTrackIdentityState":
        if self.name is None and self.color is None:
            raise ValueError("expected_before needs a Playlist name and/or color")
        return self


class ExpectedPlaylistTrackState(TrackBContract):
    muted: bool | None = None
    soloed: bool | None = None
    selected: bool | None = None

    @model_validator(mode="after")
    def require_one_field(self) -> "ExpectedPlaylistTrackState":
        if self.muted is None and self.soloed is None and self.selected is None:
            raise ValueError("expected_before needs a Playlist track state")
        return self


class PlaylistTrackIdentitySnapshot(TrackBContract):
    name: str | None = Field(default=None, max_length=MAX_PLAYLIST_TRACK_NAME_LENGTH)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)


class PlaylistTrackStateSnapshot(TrackBContract):
    muted: bool | None = None
    soloed: bool | None = None
    selected: bool | None = None


class VerifiedPlaylistTrackIdentityWrite(TrackBVerifiedMutation):
    bridge_command: Literal["playlist.set_identity"] = "playlist.set_identity"
    track_index: int = Field(ge=1)
    requested_name: str | None = Field(
        default=None, max_length=MAX_PLAYLIST_TRACK_NAME_LENGTH
    )
    requested_color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)
    before: PlaylistTrackIdentitySnapshot
    after: PlaylistTrackIdentitySnapshot
    name_verified: bool | None = None
    color_verified: bool | None = None

    @model_validator(mode="after")
    def verify_requested_fields(self) -> "VerifiedPlaylistTrackIdentityWrite":
        pairs = (
            (self.requested_name, self.name_verified),
            (self.requested_color, self.color_verified),
        )
        if all(requested is None for requested, _ in pairs):
            raise ValueError("name, color, or both must be requested")
        if any((requested is None) != (proof is None) for requested, proof in pairs):
            raise ValueError("only requested Playlist identity fields may have proof")
        expected = all(bool(proof) for requested, proof in pairs if requested is not None)
        if self.verified != expected:
            raise ValueError("verified must be the AND of requested identity fields")
        return self


class VerifiedPlaylistTrackStateWrite(TrackBVerifiedMutation):
    bridge_command: Literal["playlist.set_state"] = "playlist.set_state"
    track_index: int = Field(ge=1)
    requested_muted: bool | None = None
    requested_soloed: bool | None = None
    requested_selected: bool | None = None
    before: PlaylistTrackStateSnapshot
    after: PlaylistTrackStateSnapshot
    mute_verified: bool | None = None
    solo_verified: bool | None = None
    selection_verified: bool | None = None

    @model_validator(mode="after")
    def verify_requested_fields(self) -> "VerifiedPlaylistTrackStateWrite":
        pairs = (
            (self.requested_muted, self.mute_verified),
            (self.requested_soloed, self.solo_verified),
            (self.requested_selected, self.selection_verified),
        )
        if all(requested is None for requested, _ in pairs):
            raise ValueError("mute, solo, selection, or a combination is required")
        if any((requested is None) != (proof is None) for requested, proof in pairs):
            raise ValueError("only requested Playlist state fields may have proof")
        expected = all(bool(proof) for requested, proof in pairs if requested is not None)
        if self.verified != expected:
            raise ValueError("verified must be the AND of requested Playlist states")
        return self


# ---------------------------------------------------------------------------
# Step sequencer
# ---------------------------------------------------------------------------


def compute_step_sequence_digest(
    *, pattern_number: int, channel_index: int, step_count: int, cells: list[bool]
) -> str:
    """Compute the bridge/client canonical conflict-detection digest."""

    material = {
        "cells": [1 if value else 0 for value in cells],
        "channel_index": channel_index,
        "grid_resolution": STEP_GRID_RESOLUTION,
        "pattern_number": pattern_number,
        "step_count": step_count,
    }
    encoded = json.dumps(
        material, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class StepCellUpdate(TrackBContract):
    step_index: int = Field(ge=0, lt=MAX_STEP_COUNT)
    enabled: bool


class StepSequenceSnapshot(TrackBContract):
    pattern_number: int = Field(ge=1)
    current_pattern_number: int = Field(ge=1)
    channel_index: int = Field(ge=0)
    index_scope: Literal["global"] = "global"
    step_count: int = Field(ge=1, le=MAX_STEP_COUNT)
    grid_resolution: Literal["sixteenth_note"] = STEP_GRID_RESOLUTION
    cells: list[bool]
    digest_algorithm: Literal["sha256-canonical-json-v1"] = STEP_DIGEST_ALGORITHM
    digest: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_current_pattern_and_digest(self) -> "StepSequenceSnapshot":
        if self.pattern_number != self.current_pattern_number:
            raise ValueError(
                "step-grid APIs are current-pattern-only; requested and current "
                "pattern numbers must match"
            )
        if len(self.cells) != self.step_count:
            raise ValueError("cells length must equal step_count")
        expected = compute_step_sequence_digest(
            pattern_number=self.pattern_number,
            channel_index=self.channel_index,
            step_count=self.step_count,
            cells=self.cells,
        )
        if self.digest != expected:
            raise ValueError("step sequence digest does not match the absolute grid")
        return self


class StepSequenceObservation(StepSequenceSnapshot):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    project_dirty_flag: Literal[0, 1, 2] | None = None
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


class StepCellVerification(TrackBContract):
    step_index: int = Field(ge=0, lt=MAX_STEP_COUNT)
    requested_enabled: bool
    after_enabled: bool | None = None
    verified: bool


class VerifiedStepSequenceWrite(TrackBVerifiedMutation):
    bridge_command: Literal["sequencer.set"] = "sequencer.set"
    pattern_number: int = Field(ge=1)
    channel_index: int = Field(ge=0)
    index_scope: Literal["global"] = "global"
    expected_digest: str = Field(pattern=SHA256_PATTERN)
    requested_updates: list[StepCellUpdate] = Field(
        min_length=1, max_length=MAX_VERIFIED_STEP_COUNT
    )
    before: StepSequenceSnapshot
    after: StepSequenceSnapshot
    cells_verified: list[StepCellVerification] = Field(
        min_length=1, max_length=MAX_VERIFIED_STEP_COUNT
    )

    @model_validator(mode="after")
    def validate_batch_proof(self) -> "VerifiedStepSequenceWrite":
        requested = [cell.step_index for cell in self.requested_updates]
        proven = [cell.step_index for cell in self.cells_verified]
        if len(set(requested)) != len(requested):
            raise ValueError("requested step indices must be unique")
        if requested != proven:
            raise ValueError("cells_verified must match requested updates in order")
        if self.before.digest != self.expected_digest:
            raise ValueError("before digest must equal the required expected digest")
        if self.before.step_count > MAX_VERIFIED_STEP_COUNT:
            raise ValueError(
                "verified step writes require a grid of at most "
                f"{MAX_VERIFIED_STEP_COUNT} cells"
            )
        atomic_calls = (
            self.before.step_count
            + len(self.requested_updates)
            + SEQUENCER_WRITE_FIXED_CALLS
        )
        if atomic_calls > SEQUENCER_WRITE_CALL_BUDGET:
            raise ValueError(
                "verified step write exceeds the "
                f"{SEQUENCER_WRITE_CALL_BUDGET}-call atomic budget"
            )
        if any(index >= self.before.step_count for index in requested):
            raise ValueError("requested step is outside the guarded before-grid")
        if self.verified != all(cell.verified for cell in self.cells_verified):
            raise ValueError("verified must be the AND of requested step cells")
        return self


# ---------------------------------------------------------------------------
# Dispatch-only live note
# ---------------------------------------------------------------------------


class LiveNoteDispatch(TrackBContract):
    """Audition receipt.  It intentionally makes no readback-success claim."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    dispatched_at: datetime
    bridge_command: Literal["channel.trigger_note"] = "channel.trigger_note"
    channel_index: int = Field(ge=0)
    index_scope: Literal["global"] = "global"
    note: int = Field(ge=0, le=127)
    velocity: int = Field(ge=1, le=127)
    duration_ms: int = Field(ge=20, le=5000)
    midi_channel: int = Field(ge=-1, le=15)
    dispatched: bool
    note_off_sent: bool
    verification_basis: Literal["dispatch_only_no_state_readback"] = (
        "dispatch_only_no_state_readback"
    )
    undo_point_created: None = None
    project_saved: Literal[False] = False
    session_fingerprint: str | None = Field(
        default=None, pattern=SESSION_FINGERPRINT_PATTERN
    )
    session_precondition_applied: bool = False
    expected_before_applied: bool = False
    warnings: list[str] = Field(default_factory=list)


TrackBResult = (
    VerifiedPlayingWrite
    | VerifiedStopWrite
    | VerifiedSongPositionWrite
    | VerifiedLoopModeWrite
    | VerifiedTempoWrite
    | ChannelList
    | VerifiedChannelMixWrite
    | VerifiedChannelSoloWrite
    | VerifiedChannelPitchWrite
    | VerifiedChannelSelectionWrite
    | PatternList
    | EmptyPatternSearch
    | VerifiedPatternSelectionWrite
    | VerifiedPatternIdentityWrite
    | VerifiedPatternLengthWrite
    | PlaylistTrackList
    | VerifiedPlaylistTrackIdentityWrite
    | VerifiedPlaylistTrackStateWrite
    | VerifiedChannelIdentityWrite
    | VerifiedChannelRouteWrite
    | TargetedPluginParameterPage
    | TargetedPluginParameterScan
    | TargetedLoadedPluginInventory
    | PluginPresetCount
    | PluginPresetPage
    | PluginCurrentPreset
    | PluginPadMap
    | VerifiedPluginPresetSelection
    | LoopStarterRerollDispatch
    | VerifiedTargetedPluginParameterWrite
    | VerifiedTargetedPluginDisplayWrite
    | VerifiedTargetedPluginOptionWrite
    | StepSequenceObservation
    | VerifiedStepSequenceWrite
    | LiveNoteDispatch
)


def model_payload(value: TrackBContract | None) -> dict[str, Any] | None:
    """Serialize a typed optional precondition without null placeholders."""

    return None if value is None else value.model_dump(exclude_none=True)
