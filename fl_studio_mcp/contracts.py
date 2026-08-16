"""Strict, agent-facing contracts for the FL Studio copilot.

The original prototype models intentionally accept almost any bridge payload.
That is useful while exploring an API, but it is too permissive for an agent
boundary.  The models in this module are the stable, fail-closed contracts used
by the read-only inspection surface.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


SCHEMA_VERSION = "1.0"


class ContractModel(BaseModel):
    """Base model for data returned to an MCP client."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CapabilityStatus(str, Enum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    UNVALIDATED = "unvalidated"


class EvidenceKind(str, Enum):
    OFFICIAL_DOCUMENTATION = "official_documentation"
    LIVE_READ_ONLY_TEST = "live_read_only_test"
    FIXTURE_TEST = "fixture_test"
    LOCAL_STATIC_INSPECTION = "local_static_inspection"


class CapabilityEvidence(ContractModel):
    kind: EvidenceKind
    detail: str
    source_url: str | None = None


class CapabilityRecord(ContractModel):
    capability: str
    status: CapabilityStatus
    access_path: str | None = None
    mutates_project: bool = False
    limitations: list[str] = Field(default_factory=list)
    evidence: list[CapabilityEvidence] = Field(default_factory=list)


class ConnectionInfo(ContractModel):
    connected: bool
    compatible: bool
    compatibility_reason: str
    program_title: str | None = None
    fl_app_version: str | None = None
    fl_build: int | None = None
    midi_scripting_api_version: int | None = None
    bridge_protocol_version: int | None = None
    bridge_transport: Literal["tcp", "files", "midi", "none", "unknown"] = "unknown"
    bridge_mode: Literal["read_only", "write_test", "legacy_unknown"] = "legacy_unknown"
    bridge_read_only_enforced: bool = False
    # True only when the running bridge itself says it will dispatch the
    # verified write commands.  The bridge is the sole authority: this mirrors
    # its ping, and no client-side flag can turn it on.
    verified_writes_enabled: bool = False
    runtime_write_mode_control: bool = False
    write_mode_origin: Literal[
        "disabled", "startup_environment", "runtime_request", "legacy_unknown"
    ] = "legacy_unknown"
    startup_write_mode_enabled: bool | None = None
    bridge_source_sha256: str | None = None
    expected_bridge_source_sha256: str | None = None
    bridge_provenance: Literal[
        "matching", "missing", "malformed", "mismatched", "unavailable"
    ] = "unavailable"
    bridge_provenance_verified: bool = False
    # Generated when FL loads the bridge and stable only for that bridge
    # lifetime. Callers may pass it back to a write as an optional stale-
    # session precondition.
    session_fingerprint: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class CapabilitiesReport(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    generated_at: datetime
    connection: ConnectionInfo
    capabilities: list[CapabilityRecord]


class TransportState(ContractModel):
    playing: bool | None = None
    recording: bool | None = None
    metronome_enabled: bool | None = None
    precount_enabled: bool | None = None
    time_signature_numerator: int | None = Field(default=None, ge=1, le=32)
    tempo_bpm: float | None = Field(default=None, ge=0.0)
    song_position_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    song_position_display: str | None = None
    song_length_ms: int | None = Field(default=None, ge=0)
    loop_mode: int | None = None


class SelectedRangeObservation(ContractModel):
    """Raw-only selection observation with semantic promotion disabled."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    project_dirty_flag: Literal[0, 1, 2] | None = None
    raw_start_time: StrictInt = Field(
        ge=-9_007_199_254_740_991, le=9_007_199_254_740_991
    )
    raw_end_time: StrictInt = Field(
        ge=-9_007_199_254_740_991, le=9_007_199_254_740_991
    )
    raw_time_unit: Literal["unknown"] = "unknown"
    selection_state: Literal["unknown"] = "unknown"
    selection_presence: Literal["unknown"] = "unknown"
    interpretation_status: Literal["unvalidated"] = "unvalidated"
    semantic_scope: None = None
    timebase_ppq: StrictInt | None = Field(
        default=None, gt=0, le=9_007_199_254_740_991
    )
    start_ticks: None = None
    end_ticks: None = None
    duration_ticks: None = None
    range_order: None = None
    inactive_start_semantics: Literal["unknown"] = "unknown"
    render_endpoint_inclusivity: Literal["unknown"] = "unknown"
    raw_start_display_hint: str | None = Field(default=None, max_length=128)
    raw_end_display_hint: str | None = Field(default=None, max_length=128)
    repeated_read_consistent: Literal[True] = True
    safe_for_rendering: Literal[False] = False
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)

class ProjectSummary(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    connection: ConnectionInfo
    project_title: str | None = None
    project_author: str | None = None
    project_genre: str | None = None
    tempo_bpm: float | None = Field(default=None, gt=0.0)
    ppq: int | None = Field(default=None, gt=0)
    mixer_track_count: int | None = Field(default=None, ge=0)
    channel_count: int | None = Field(default=None, ge=0)
    pattern_count: int | None = Field(default=None, ge=0)
    playlist_track_count: int | None = Field(default=None, ge=0)
    dirty_flag: Literal[0, 1, 2] | None = None
    dirty_state: Literal["clean", "dirty", "autosave_dirty", "unknown"] = "unknown"
    undo_history_position: int | None = Field(default=None, ge=0)
    undo_history_count: int | None = Field(default=None, ge=0)
    transport: TransportState
    warnings: list[str] = Field(default_factory=list)


class PluginSlotSummary(ContractModel):
    track_index: int = Field(ge=0)
    slot_index: int = Field(ge=0, le=9)
    name: str
    user_name: str | None = None
    reported_parameter_count: int | None = Field(default=None, ge=0)
    mix_level_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    exact_version: None = Field(
        default=None,
        description="The public MIDI scripting API does not expose plug-in versions.",
    )


class MixerTrackSummary(ContractModel):
    index: int = Field(ge=0)
    name: str
    volume_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    volume_db: float | None = None
    pan: float | None = Field(default=None, ge=-1.0, le=1.0)
    stereo_separation: float | None = Field(default=None, ge=-1.0, le=1.0)
    muted: bool | None = None
    soloed: bool | None = None
    armed: bool | None = None
    selected: bool | None = None
    track_enabled: bool | None = None
    effect_slots_enabled: bool | None = None
    polarity_reversed: bool | None = None
    channels_swapped: bool | None = None
    color_rgba: int | None = None
    peak_left: float | None = None
    peak_right: float | None = None
    plugins: list[PluginSlotSummary] = Field(default_factory=list)


class MixerTrackList(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    project_dirty_flag: Literal[0, 1, 2] | None = None
    total_track_count: int = Field(ge=0)
    scanned_track_count: int = Field(ge=0)
    only_used: bool
    tracks: list[MixerTrackSummary]
    partial: bool = False
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


class MixerRoute(ContractModel):
    destination_track_index: int = Field(ge=0)
    destination_track_name: str
    level_normalized: float | None = Field(default=None, ge=0.0, le=1.0)


class EqBandState(ContractModel):
    band_index: int = Field(ge=0)
    gain_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    gain_db: float | None = None
    frequency_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    frequency_hz: float | None = Field(default=None, gt=0.0)
    bandwidth_normalized: float | None = Field(default=None, ge=0.0, le=1.0)


class MixerTrackInspection(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    project_dirty_flag: Literal[0, 1, 2] | None = None
    track: MixerTrackSummary
    routes: list[MixerRoute] = Field(default_factory=list)
    builtin_eq: list[EqBandState] = Field(default_factory=list)
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


class PluginParameter(ContractModel):
    index: int = Field(ge=0)
    reported_name: str
    normalized_value: float | None = Field(default=None, ge=0.0, le=1.0)
    display_text: str | None = None
    display_text_available: bool
    classification: Literal["reported", "padding_candidate"] = "reported"
    profile_status: Literal["unprofiled_read_only"] = "unprofiled_read_only"
    safe_to_modify: Literal[False] = False


class PluginParameterPage(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    project_dirty_flag: Literal[0, 1, 2] | None = None
    plugin: PluginSlotSummary
    reported_parameter_count: int = Field(ge=0)
    offset: int = Field(ge=0)
    scanned_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    has_more: bool
    next_offset: int | None = Field(default=None, ge=0)
    parameters: list[PluginParameter]
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


class PluginParameterScan(ContractModel):
    """A whole plug-in's real parameter map, de-padded inside FL.

    `PluginParameterPage` is one page of FL's raw index range. This is the
    result of walking that range on FL's own thread and dropping the padding
    before it goes on the wire, so `parameters` holds controls that exist
    rather than a slice that may be mostly empty. `reported_parameter_count`
    is still FL's padded maximum, often thousands of slots for a VST3, and
    `real_count` is what survived the padding rule.

    Check `truncated` before treating this as the whole plug-in. A scan
    stopped by a bound says so, and `truncated_by` names the bound.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    project_dirty_flag: Literal[0, 1, 2] | None = None
    plugin: PluginSlotSummary
    reported_parameter_count: int = Field(ge=0)
    scan_start: int = Field(ge=0)
    scan_end: int = Field(ge=0)
    examined_count: int = Field(ge=0)
    highest_index_examined: int | None = Field(default=None, ge=0)
    real_count: int = Field(ge=0)
    padding_skipped: int = Field(ge=0)
    truncated: bool
    truncated_by: Literal["max_indices", "max_results", "start", "end"] | None = None
    parameters: list[PluginParameter]
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


class LoadedPluginInventory(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    project_dirty_flag: Literal[0, 1, 2] | None = None
    plugins: list[PluginSlotSummary]
    observation_atomic: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# verified write contracts
#
# One model per write command.  Three rules shape all of them:
#
# * ``verified`` is the only success claim, and it always comes from reading FL
#   back on a later idle tick.  A write FL accepted and then ignored is
#   ``verified=false`` with an explanatory ``verification_summary`` and a
#   leading warning; it is a real outcome, never an exception.
# * Requested values are bounded here because this layer validated them before
#   the bridge ever saw them.  Observed before/after readings are deliberately
#   unbounded: rejecting a surprising readback would raise on a write that
#   already landed and destroy the only report of it.
# * The models are frozen.  A write report is a record of something that
#   already happened to the user's project, so nothing downstream may edit it.
# ---------------------------------------------------------------------------


class WriteModeChange(ContractModel):
    """Verified capability transition for one loaded bridge session."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    changed_at: datetime
    bridge_command: Literal["session.set_write_mode"] = "session.set_write_mode"
    requested_enabled: bool
    before_enabled: bool
    after_enabled: bool
    changed: bool
    verified: Literal[True] = True
    verification_basis: Literal["post_transition_bridge_handshake"] = (
        "post_transition_bridge_handshake"
    )
    bridge_mode: Literal["read_only", "write_test"]
    runtime_write_mode_control: Literal[True] = True
    write_mode_origin: Literal[
        "disabled", "startup_environment", "runtime_request"
    ]
    confirmation_required: bool
    confirmation_applied: bool
    session_fingerprint: str = Field(pattern=r"^[0-9a-f]{32}$")
    session_precondition_applied: Literal[True] = True
    session_only: Literal[True] = True
    startup_default_enabled: bool
    project_saved: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_transition(self) -> "WriteModeChange":
        if self.after_enabled != self.requested_enabled:
            raise ValueError("after_enabled must match requested_enabled")
        if self.changed != (self.before_enabled != self.after_enabled):
            raise ValueError("changed must describe the before/after transition")
        expected_mode = "write_test" if self.after_enabled else "read_only"
        if self.bridge_mode != expected_mode:
            raise ValueError("bridge_mode contradicts after_enabled")
        expected_origin = "runtime_request" if self.after_enabled else "disabled"
        if self.write_mode_origin != expected_origin:
            raise ValueError("write_mode_origin contradicts the runtime transition")
        if self.confirmation_required != self.requested_enabled:
            raise ValueError("confirmation_required contradicts requested_enabled")
        if self.confirmation_applied != self.requested_enabled:
            raise ValueError("confirmation_applied contradicts requested_enabled")
        return self


class VerifiedWrite(ContractModel):
    """Common shape shared by every verified write result."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    applied_at: datetime
    bridge_command: str
    track_index: int = Field(ge=0)
    targeted_master: bool = False
    verified: bool
    verification_summary: str
    verification_basis: Literal["readback_on_a_later_fl_idle_tick"] = (
        "readback_on_a_later_fl_idle_tick"
    )
    # Observed, never asserted. FL's saveUndo reports nothing and raises
    # nothing when it does not take, so the bridge watches the undo history
    # across the call instead. True means a point demonstrably appeared, False
    # that one demonstrably did not, and null that FL would not say -- which
    # is not the same as success. Undo is the whole safety net for this
    # surface, so a caller must be able to tell these apart.
    undo_point_created: bool | None = None
    project_saved: Literal[False] = False
    session_fingerprint: str | None = None
    session_precondition_applied: bool = False
    expected_before_applied: bool = False
    warnings: list[str] = Field(default_factory=list)


class VerifiedMixerVolumeWrite(VerifiedWrite):
    bridge_command: Literal["mixer.set_volume"] = "mixer.set_volume"
    requested_volume_normalized: float = Field(ge=0.0, le=1.0)
    before_volume_normalized: float | None = None
    after_volume_normalized: float | None = None
    before_volume_db: float | None = None
    after_volume_db: float | None = None


class ExpectedMixerVolumeState(ContractModel):
    volume_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    volume_db: float | None = Field(default=None, ge=-200.0, le=12.0)

    @model_validator(mode="after")
    def require_one_field(self) -> "ExpectedMixerVolumeState":
        if self.volume_normalized is None and self.volume_db is None:
            raise ValueError("expected mixer volume needs normalized and/or dB state")
        return self


class VerifiedMixerVolumeDbWrite(VerifiedWrite):
    bridge_command: Literal["mixer.set_volume_db"] = "mixer.set_volume_db"
    requested_volume_db: float = Field(ge=-60.0, le=6.0)
    tolerance_db: float = Field(gt=0.0, le=1.0)
    before_volume_normalized: float | None = None
    after_volume_normalized: float | None = None
    before_volume_db: float | None = None
    after_volume_db: float | None = None
    search_iterations: int = Field(ge=0, le=20)


class VerifiedMixerPanWrite(VerifiedWrite):
    bridge_command: Literal["mixer.set_pan"] = "mixer.set_pan"
    requested_pan: float = Field(ge=-1.0, le=1.0)
    before_pan: float | None = None
    after_pan: float | None = None


class VerifiedMixerMuteWrite(VerifiedWrite):
    bridge_command: Literal["mixer.set_mute"] = "mixer.set_mute"
    requested_muted: bool
    before_muted: bool | None = None
    after_muted: bool | None = None


class VerifiedMixerSoloWrite(VerifiedWrite):
    bridge_command: Literal["mixer.set_solo"] = "mixer.set_solo"
    requested_soloed: bool
    before_soloed: bool | None = None
    after_soloed: bool | None = None


class VerifiedMixerArmWrite(VerifiedWrite):
    bridge_command: Literal["mixer.set_arm"] = "mixer.set_arm"
    requested_armed: bool
    before_armed: bool | None = None
    after_armed: bool | None = None


class VerifiedMixerColorWrite(VerifiedWrite):
    bridge_command: Literal["mixer.set_color"] = "mixer.set_color"
    requested_color: int = Field(ge=0, le=0xFFFFFFFF)
    before_color: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    after_color: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)


class VerifiedMixerStereoSeparationWrite(VerifiedWrite):
    bridge_command: Literal["mixer.set_stereo_separation"] = (
        "mixer.set_stereo_separation"
    )
    requested_stereo_separation: float = Field(ge=-1.0, le=1.0)
    before_stereo_separation: float | None = Field(default=None, ge=-1.0, le=1.0)
    after_stereo_separation: float | None = Field(default=None, ge=-1.0, le=1.0)


class VerifiedMixerSelectionWrite(VerifiedWrite):
    bridge_command: Literal["mixer.select_track"] = "mixer.select_track"
    requested_active_track_index: int = Field(ge=0)
    before_active_track_index: int | None = Field(default=None, ge=0)
    after_active_track_index: int | None = Field(default=None, ge=0)


class VerifiedPluginDisplayWrite(VerifiedWrite):
    """One plug-in parameter set in the units the plug-in itself displays."""

    bridge_command: Literal["plugin.set_param_display"] = "plugin.set_param_display"
    slot_index: int = Field(ge=0, le=9)
    parameter_index: int = Field(ge=0)
    plugin_name: str | None = None
    parameter_name: str | None = None
    # How the requested parameter was found. Real third-party controls often
    # have no name, so a display match is an ordinary outcome, not a fallback.
    matched_on: Literal[
        "index", "name", "display", "name_substring", "display_substring"
    ]
    matched_text: str | None = None
    requested_value: float
    tolerance: float = Field(ge=0.0)
    landed_value: float | None = None
    normalized_value: float | None = Field(default=None, ge=0.0, le=1.0)
    before: PluginParameterObservation
    after: PluginParameterObservation


class VerifiedPluginOptionWrite(VerifiedWrite):
    """One enumerated plug-in parameter set to a named option."""

    bridge_command: Literal["plugin.set_param_option"] = "plugin.set_param_option"
    slot_index: int = Field(ge=0, le=9)
    parameter_index: int = Field(ge=0)
    plugin_name: str | None = None
    parameter_name: str | None = None
    matched_on: Literal[
        "index", "name", "display", "name_substring", "display_substring"
    ]
    matched_text: str | None = None
    requested_option: str
    selected_option: str | None = None
    normalized_value: float | None = Field(default=None, ge=0.0, le=1.0)
    sweep_steps: int = Field(ge=2)
    # Every option the sweep found, in order. FL cannot report an enumeration,
    # so this is the only way to learn what a control accepts -- and getting it
    # required moving the control.
    options: list[str] = Field(default_factory=list)
    before: PluginParameterObservation
    after: PluginParameterObservation


class VerifiedMixerNameWrite(VerifiedWrite):
    bridge_command: Literal["mixer.set_name"] = "mixer.set_name"
    requested_name: str
    before_name: str | None = None
    after_name: str | None = None
    # True when the request was the empty string. FL answers that by putting
    # the track's default back ("Insert 8"), not by leaving the label blank,
    # so ``after_name`` will not echo what was asked for.
    restored_default: bool = False


class VerifiedMixerSendWrite(VerifiedWrite):
    """One send created or torn down between two mixer tracks."""

    bridge_command: Literal["mixer.set_send"] = "mixer.set_send"
    destination_track_index: int = Field(ge=0)
    requested_enabled: bool
    before_enabled: bool | None = None
    after_enabled: bool | None = None
    # Null once the route is gone: FL raises rather than reporting a level for
    # an inactive route, so there is no honest zero to put here.
    level_normalized: float | None = Field(default=None, ge=0.0, le=1.0)


class VerifiedMixerSendLevelWrite(VerifiedWrite):
    bridge_command: Literal["mixer.set_send_level"] = "mixer.set_send_level"
    destination_track_index: int = Field(ge=0)
    requested_level_normalized: float = Field(ge=0.0, le=1.0)
    before_level_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    after_level_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    send_active: bool | None = None


class EqBandObservation(ContractModel):
    """One built-in EQ band as FL reported it around a write."""

    model_config = ConfigDict(frozen=True)

    gain_normalized: float | None = None
    gain_db: float | None = None
    frequency_normalized: float | None = None
    frequency_hz: float | None = None


class VerifiedMixerEqWrite(VerifiedWrite):
    bridge_command: Literal["mixer.set_eq"] = "mixer.set_eq"
    band_index: int = Field(ge=0)
    requested_gain_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    requested_frequency_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    before: EqBandObservation
    after: EqBandObservation
    # Null when that field was not part of the request; ``verified`` above is
    # the conjunction of the fields that were.
    gain_verified: bool | None = None
    frequency_verified: bool | None = None


class PluginParameterObservation(ContractModel):
    """One plug-in parameter as FL reported it around a write."""

    model_config = ConfigDict(frozen=True)

    normalized_value: float | None = None
    display_text: str | None = None
    display_text_available: bool


class ExpectedEqBandState(ContractModel):
    """Optional before-state guard for a built-in mixer EQ write."""

    gain_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    frequency_normalized: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def require_one_field(self) -> "ExpectedEqBandState":
        if self.gain_normalized is None and self.frequency_normalized is None:
            raise ValueError(
                "expected_before needs gain_normalized, frequency_normalized, or both"
            )
        return self


class ExpectedPluginParameterState(ContractModel):
    """Optional before-state guard for a plug-in parameter write."""

    normalized_value: float | None = Field(default=None, ge=0.0, le=1.0)
    display_text: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def require_one_field(self) -> "ExpectedPluginParameterState":
        if self.normalized_value is None and self.display_text is None:
            raise ValueError(
                "expected_before needs normalized_value, display_text, or both"
            )
        return self


PluginVerificationBasis = Literal[
    "value_readback", "display_change_only", "none"
]


class VerifiedPluginParameterWrite(VerifiedWrite):
    # How the outcome was established. "value_readback" confirms the control
    # landed on the requested value. "display_change_only" proves it moved but
    # not where it moved to, because FL's getParamValue keeps returning the
    # previous number and cannot cross-check the destination; prefer
    # fl_set_plugin_param_display when the destination has to be guaranteed.
    verification_basis_detail: PluginVerificationBasis
    bridge_command: Literal["plugin.set_param"] = "plugin.set_param"
    slot_index: int = Field(ge=0, le=9)
    parameter_index: int = Field(ge=0)
    plugin_name: str
    parameter_name: str
    requested_normalized_value: float = Field(ge=0.0, le=1.0)
    before: PluginParameterObservation
    after: PluginParameterObservation
    # The display string changing is the only positive proof a parameter moved;
    # FL's numeric readback can lag a whole operation behind a write that did
    # land.  A parameter that merely reads at the request counts as verified
    # only because that is the "it was already there" case.
    display_changed: bool
    reads_at_requested_value: bool


class ReadOnlyInspectionReport(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observation_id: str
    observed_at: datetime
    mode: Literal["read_only"] = "read_only"
    observation_atomic: Literal[False] = False
    project: ProjectSummary
    mixer: MixerTrackList
    parameter_previews: list[PluginParameterPage]
    warnings: list[str] = Field(default_factory=list)
    prohibited_operations: list[str] = Field(
        default_factory=lambda: [
            "project writes",
            "transport control",
            "plug-in insertion or removal",
            "parameter changes",
            "automation writes",
            "rendering",
            "generic FL API calls",
        ]
    )
