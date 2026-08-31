"""The MCP server for FL Studio 2026.  This is the default agent entry point.

Six surfaces, and nothing else is reachable from here:

* **Reads** over the live project, through the fail-closed inspector allowlist.
* **Measurements** of rendered audio files, because the FL API exposes no
  audio. Those tools read files the caller names, plus a bounded lookup over
  FL Studio's usual output and project folders; the discovery roots are fixed
  in code and cannot be chosen by an agent.
* **Verified state mutations** for transport, mixer tracks, global Channel Rack
  targets, the current pattern's step cells, and mixer-effect or generator
  parameters. Each reads FL back on a *later* idle tick and reports
  ``verified`` from that readback alone.
* **Session write-mode control**, which can expose or lock those exact
  mutations without restarting FL Studio. Enabling requires an explicit
  user-present confirmation and is independently verified by a new handshake.
* **Bounded live-note audition**, which reports note dispatch and release but
  never fabricates state verification.
* **Task-scoped Production Runs**, which validate a closed multi-stage plan,
  reuse the existing writers, and retain process-local receipts until the plan
  completes, blocks, or is stopped.

There is still no undo command, render, project save, caller-directed
filesystem search, playback-speed setter without a getter, or reflective FL
API escape hatch.

The project write tools apply and report; there is no confirmation round-trip
and no rollback ceremony. Where an FL undo request applies, whether a point actually
appeared is reported as ``undo_point_created`` rather than assumed; transient
transport and note actions truthfully report null. They are dispatchable only
while the bridge reports verified write mode; when it does not, they refuse locally with
:class:`~fl_studio_mcp.verified_writer.VerifiedWritesUnavailable`, which names
the user-confirmed mode tool, rather than surfacing a raw bridge rejection.
"""

from __future__ import annotations

import sys
from typing import Annotated, Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase
from mcp.types import ToolAnnotations
from pydantic import ConfigDict, Field

from . import __version__
from .advisory import (
    AudioComparison,
    AudioFileAnalysis,
    MaskingAnalysis,
    RecentAudioListing,
    analyze_audio_file,
    analyze_masking,
    compare_audio_files,
    find_recent_audio_files,
)
from .contracts import (
    CapabilitiesReport,
    ExpectedEqBandState,
    ExpectedMixerVolumeState,
    ExpectedPluginParameterState,
    LoadedPluginInventory,
    MixerTrackInspection,
    MixerTrackList,
    PluginParameterPage,
    PluginParameterScan,
    ProjectSummary,
    ReadOnlyInspectionReport,
    SelectedRangeObservation,
    TransportState,
    VerifiedMixerArmWrite,
    VerifiedMixerColorWrite,
    VerifiedMixerEqWrite,
    VerifiedMixerMuteWrite,
    VerifiedMixerNameWrite,
    VerifiedMixerPanWrite,
    VerifiedMixerSelectionWrite,
    VerifiedMixerSendLevelWrite,
    VerifiedMixerSendWrite,
    VerifiedMixerSoloWrite,
    VerifiedMixerStereoSeparationWrite,
    VerifiedMixerVolumeWrite,
    VerifiedMixerVolumeDbWrite,
    VerifiedPluginDisplayWrite,
    VerifiedPluginOptionWrite,
    VerifiedPluginParameterWrite,
    WriteModeChange,
)
from .creative import (
    PIANO_ROLL,
    ArrangementMarkerReceipt,
    AutomationRecordReceipt,
    CreativeNote,
    MidiExportReceipt,
    MidiTrackSpec,
    NoteSequence,
    PatternPreparation,
    PianoRollBridgeStatus,
    PianoRollDispatch,
    PianoRollTransform,
    SectionMarker,
    add_section_markers,
    compose_bassline as generate_bassline,
    compose_chord_progression as generate_chord_progression,
    compose_drums as generate_drums,
    compose_melody as generate_melody,
    export_type1_midi,
    prepare_empty_pattern,
    record_automation_value,
    transform_piano_roll,
    write_piano_roll_notes,
)
from .music_analysis import (
    AudioMusicAnalysis,
    MelodyTranscription,
    analyze_tempo_and_key,
    transcribe_monophonic,
)
from .readonly_inspector import ReadOnlyInspector
from .mixing import (
    MIX_PLANS,
    PEAK_WATCHES,
    FinishMixAssessment,
    GainStagePlanResult,
    MaskingRecommendationReport,
    MixDoctorReport,
    MixPlan,
    MixPlanApplication,
    MixTarget,
    PeakWatchReport,
    PluginCompatibilityReport,
    PluginProfileCatalog,
    ProcessingIntent,
    ProcessingIntentResolution,
    ReferenceRecommendationReport,
    create_gain_stage_plan,
    finish_mix_assessment,
    inspect_plugin_compatibility,
    list_plugin_profiles,
    masking_recommendations,
    reference_recommendations,
    resolve_processing_intent,
    run_mix_doctor,
)
from .performance import TrackBController, TrackBInspector
from .plugin_atlas_mcp import (
    AtlasGetProductRequest,
    AtlasInspectLoadedRequest,
    AtlasInspectLoadedResponse,
    AtlasProductResponse,
    AtlasRecommendationResponse,
    AtlasRecommendRequest,
    AtlasSearchRequest,
    AtlasSearchResponse,
    get_atlas_product,
    inspect_loaded_atlas,
    recommend_atlas,
    search_atlas,
)
from .production_runs import (
    PRODUCTION_RUNS,
    ProductionRunDelta,
    ProductionRunLookup,
    ProductionRunPlan,
    ProductionRunRequest,
    ProductionRunResult,
    ProductionRunValidation,
    validate_production_run,
)
from .sound_selection.executor import (
    SoundFeedbackResult,
    SoundPaletteLookup,
    SoundSelectionApplyResult,
)
from .sound_selection.history import SoundHistoryResetResult, SoundHistoryStatus
from .sound_selection.mcp import (
    sound_selection_apply as apply_sound_selection,
    sound_selection_create_variation as create_sound_selection_variation,
    sound_selection_get as get_sound_selection,
    sound_selection_history_reset as reset_sound_selection_history,
    sound_selection_history_status as get_sound_selection_history_status,
    sound_selection_inventory as get_sound_selection_inventory,
    sound_selection_plan as plan_sound_selection,
    sound_selection_record_feedback as record_sound_selection_feedback,
)
from .sound_selection.models import (
    DrumPadMap,
    SoundFeedbackRequest,
    SoundInventory,
    SoundPalettePlan,
    SoundPaletteVariationPlan,
    SoundSelectionRequest,
)
from .track_b_contracts import (
    ChannelList,
    EmptyPatternSearch,
    ExpectedChannelIdentityState,
    ExpectedChannelMixState,
    ExpectedChannelPitchState,
    ExpectedChannelRouteState,
    ExpectedChannelSelectionState,
    ExpectedChannelSoloState,
    ExpectedChannelTargetState,
    ExpectedLoopModeState,
    ExpectedMetronomeState,
    ExpectedPatternIdentityState,
    ExpectedPatternLengthState,
    ExpectedPatternSelectionState,
    ExpectedPlaylistTrackIdentityState,
    ExpectedPlaylistTrackState,
    ExpectedPlayingState,
    ExpectedPrecountState,
    ExpectedProjectHistoryState,
    ExpectedRecordingState,
    ExpectedSongPositionState,
    ExpectedStopState,
    ExpectedTempoState,
    ExpectedTimeSignatureState,
    LiveNoteDispatch,
    MAX_VERIFIED_STEP_COUNT,
    MAX_PATTERN_LENGTH_BEATS,
    MAX_PATTERN_NUMBER,
    PluginTarget,
    PatternList,
    PluginPresetCount,
    PluginPresetPage,
    PluginCurrentPreset,
    PluginPadMap,
    ExpectedPluginPresetState,
    PlaylistTrackList,
    ProjectHistoryObservation,
    StepCellUpdate,
    StepSequenceObservation,
    TargetedLoadedPluginInventory,
    TargetedPluginParameterPage,
    TargetedPluginParameterScan,
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
    VerifiedSongPositionWrite,
    VerifiedStepSequenceWrite,
    VerifiedStopWrite,
    VerifiedTargetedPluginDisplayWrite,
    VerifiedTargetedPluginOptionWrite,
    VerifiedTargetedPluginParameterWrite,
    VerifiedPluginPresetSelection,
    VerifiedTempoWrite,
    VerifiedTimeSignatureNumeratorWrite,
)
from .verified_writer import VerifiedWriter, WriteModeManager
from .workflows import (
    MAX_BATCH_OPERATIONS,
    BatchOperation,
    VerifiedBatchExecutor,
    VerifiedBatchResult,
)


# The upstream MCP SDK's generated function-argument models ignore unknown
# properties by default. Tighten the shared base before any tools are
# registered so misspelled or unexpected agent input fails closed. Tests pin
# this behavior because it is part of this server's safety boundary.
ArgModelBase.model_config = ConfigDict(
    arbitrary_types_allowed=True,
    extra="forbid",
)
ArgModelBase.model_rebuild(force=True)

SessionFingerprintArg = Annotated[
    str | None,
    Field(
        default=None,
        pattern=r"^[0-9a-f]{32}$",
        description=(
            "Optional bridge-lifetime fingerprint from a recent read. The write "
            "refuses if FL reloaded the bridge before mutation. This is a "
            "concurrency guard, not authentication or project identity."
        ),
    ),
]

# Sound Palette application is a workflow mutation whose service contract
# always requires a live session token. Keep the generic bridge-write alias
# optional for the lower-level setters that preserve their legacy call shape,
# but make this high-level public mutation fail at MCP argument validation
# rather than reaching the service with ``None``.
RequiredSoundSelectionSessionFingerprintArg = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{32}$",
        description=(
            "Required bridge-lifetime fingerprint from a recent live read. The "
            "palette application refuses if FL reloaded the bridge before mutation. "
            "This is a concurrency guard, not authentication or project identity."
        ),
    ),
]


INSTRUCTIONS = """\
PostFader 0.20 is a local FL Studio 2026 production copilot with 111 supported
tools and 8 live resources. It observes project, transport, mixer, Channel
Rack, loaded plug-ins, patterns, Playlist tracks, history, presets, and step
cells. Prefer the fl:// resources for initial context, then use focused reads
before deciding on a mutation.

The bridge starts read-only. Only call fl_set_write_mode(enabled=true,
confirm_user_present=true) after the present user explicitly asks to change the
open project. The authorization is session-only. Direct setters are bounded,
Master-protected, never automatically replayed after an ambiguous outcome, and
read FL back on a later idle tick. Treat verified=false as the headline: the
requested state was not proven. False or null undo evidence means Ctrl+Z may
not recover the change. PostFader never saves the project.

fl_apply_verified_batch performs one preflight and ordered direct operations
with per-item receipts. It is non-atomic: successful earlier items are not
rolled back. mix_create_plan and mix_apply_plan keep recommendation and action
separate and apply a stored plan at most once. Peak watches and mix plans live
only in this MCP process.

Autonomy is task-scoped, not a persistent PostFader mode. The user does not
need a special command. When the user asks to create, continue, finish,
transform, arrange, remix, produce, mix, or otherwise change the project, the
connected AI may translate that request into one bounded Production Run. Keep
the user's scope and preservation constraints in the run request. For a clear
request to make changes, postfader_execute_run enables the existing write
boundary once internally; do not ask for a separate mode transition between
run operations. Use lower-level tools for precise one-off changes and a
Production Run for multi-stage, outcome-oriented work.

When the user asks only for ideas, options, analysis, or a plan, use read-only
inspection, postfader_validate_run, or a plan_only run and do not submit
project mutations. Production Runs stop on a real capability, setup, session,
scope, or verification blocker. Use postfader_continue_run only after a
conversational follow-up and postfader_stop_run to prevent future operations;
neither rewrites completed receipts or undoes earlier changes. Runs are
bounded and process-local. Never claim a requested result completed when FL
cannot expose or verify a required operation.

Mix Doctor, gain staging, reference matching, masking recommendations,
processing intents, plug-in profiles, and finish assessment use actual decoded
bounces where audio evidence is required. Recommendations are bounded policy,
not proof of artistic quality. The server cannot hear FL's live output; the
user must export candidate audio before bounce analysis.

Composition tools generate deterministic chords, melody, bass, and drums.
midi_export_type1 writes a local MIDI file, reopens it, and verifies its event
content. Tempo/key estimation and monophonic transcription read caller-selected
audio files and do not mutate FL.

Sound Selection is task-scoped and needs no persistent mode. When the user
delegates instrument, preset, drum-kit, or palette decisions, translate their
direction, preferences, exclusions, locked roles, continuity, and novelty
policy into a SoundSelectionRequest. Use sound_selection_plan for read-only
ideas and sound_selection_apply only after a clear request authorizes project
changes and includes the current 32-character lowercase session_fingerprint
from a recent live read.
That fingerprint is required by the apply tool's public schema. User direction
is the strongest input; balanced planning preserves
core sounds within a song and uses bounded local recency only to distinguish
similarly suitable choices. Plan and apply a palette before complete-song
Production Runs write notes, then reference its generator roles and drum map.
Use sound_selection_create_variation for later sections rather than replacing
anchors without a request. Do not ask for confirmation between role changes in
one authorized run. Atlas-only products are recommendations, never executable
assignments, and no preset choice is claimed as heard audio.

plugins_list_presets and plugins_get_current_preset expose bounded exact
identity; fl_select_plugin_preset navigates only within a bounded search and
requires later-idle-tick readback. Duplicate names require an index. Use
plugins_inspect_pad_map before non-General-MIDI drum writing. Loop Starter is
an explicit, separate loop-based source and its reroll remains dispatch-only;
never substitute it for an instrument-based request.

Piano Roll mutations use FL's separate .pyscript runtime. First prepare the
bridge, manually run Postfader Apply once, and confirm that step. Automatic
calls verify the target channel and pattern, but hotkey dispatch is not note
readback: application_verified remains false. Section-marker names are read
back, but marker times are not. Automation helpers verify the controlled value
while explicitly leaving automation-point existence unknown.

Plug-in insertion/removal/reordering, per-slot bypass/wet control, Playlist
clip CRUD, live audio buffers, rendering, project save, playback speed, and a
generic raw FL API escape hatch are unavailable. A send must exist before its
level can be set. Unprofiled plug-in parameters remain unsafe by default;
prefer displayed-value or exact-option tools when their meaning is known.

Plugin Atlas is a bundled, offline knowledge layer. Use
plugins_atlas_search, plugins_atlas_get_product, and plugins_atlas_recommend
for static product, technique, limitation, adapter, and stock-alternative
knowledge; these tools do not contact FL Studio and do not claim ownership,
installation, or loaded state. Use plugins_atlas_inspect_loaded when a live
inventory match is needed. It reads the same target-aware Track B inventory as
plugins_scan_loaded_plugins, keeps mixer-effect and global Channel Rack
generator identities separate, and reports a product-name match as name_only:
name-only matching is never control proof. Atlas records and adapters do not
authorize plug-in insertion or parameter writes.

fl_get_selected_range intentionally leaves selection semantics and render
inclusivity unknown. Step writes require the latest grid digest and preserve
the published bounded call budget. This server requires FL Studio 26.1.3 build
5336 or newer and MIDI scripting API 44 or newer.
"""

READ_ONLY = ToolAnnotations(
    title="Read FL Studio state",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

# Measuring a file on disk touches no external system and cannot change one,
# so the audio tools are closed-world. A fixed file measures the same way every
# time; a directory listing does not, which is the one distinction below.
LOCAL_READ_ONLY = ToolAnnotations(
    title="Measure rendered audio",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

LOCAL_READ_ONLY_VOLATILE = ToolAnnotations(
    title="List recent audio bounces",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

# The write tools. Honest hints:
#
# * readOnlyHint=False, because these change the user's open project.
# * destructiveHint=True, because these overwrite live project state. Each call
#   reports undo evidence; persistent writes request an FL undo point, while
#   transient transport actions truthfully report null. An undo point is not
#   guaranteed and does not make the mutation non-destructive.
# * idempotentHint=False, even though targets are absolute: repeating a command
#   can add undo history, and display/option searches can perform transient
#   parameter writes. BridgeClient therefore never replays mutation outcomes.
# * openWorldHint=True, because the outcome depends on a live FL Studio.
MUTATING = ToolAnnotations(
    title="Change FL Studio state",
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

# This changes no project value, but it grants or revokes access to destructive
# tools. Mark it destructive so MCP clients can put their approval UI in front
# of the capability transition. It is an absolute, session-only state, so
# repeating the same request is idempotent.
WRITE_MODE_CONTROL = ToolAnnotations(
    title="Enable or disable FL Studio writes",
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)

# Live-note audition sends one bounded note-on/off pair. It changes no saved
# project state and has no authoritative state getter, so its result is an
# explicit dispatch receipt rather than a fabricated verified write.
EPHEMERAL_MUTATING = ToolAnnotations(
    title="Audition a Channel Rack note",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

WORKFLOW_STATE = ToolAnnotations(
    title="Manage a process-local workflow",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

FILE_MUTATING = ToolAnnotations(
    title="Write a local creative artifact",
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)

mcp = MCPServer(
    name="postfader-fl-studio-mcp",
    version=__version__,
    instructions=INSTRUCTIONS,
)


async def _run(method_name: str, **arguments):
    def invoke():
        inspector = ReadOnlyInspector()
        return getattr(inspector, method_name)(**arguments)

    return await anyio.to_thread.run_sync(invoke)


async def _measure(function, *positional, **keyword):
    """Run one blocking audio measurement off the event loop."""

    def invoke():
        return function(*positional, **keyword)

    return await anyio.to_thread.run_sync(invoke)


async def _mix(function, *positional, **keyword):
    """Run a blocking production workflow off the MCP event loop."""

    def invoke():
        return function(*positional, **keyword)

    return await anyio.to_thread.run_sync(invoke)


async def _write(method_name: str, **arguments):
    """Apply one verified write off the event loop.

    Every refusal reaches the agent as a raised error rather than as a
    successful-looking result: VerifiedWritesUnavailable when the live bridge
    session has writes disabled, IncompatibleFLStudio when the handshake is
    wrong, ValueError for a value this layer rejected before the bridge saw it.
    A write that FL accepted and ignored is not an error -- it returns normally
    with verified=false.
    """

    def invoke():
        writer = VerifiedWriter()
        return getattr(writer, method_name)(**arguments)

    return await anyio.to_thread.run_sync(invoke)


async def _set_write_mode(**arguments):
    """Change only the live bridge capability state, off the event loop."""

    def invoke():
        return WriteModeManager().set_write_mode(**arguments)

    return await anyio.to_thread.run_sync(invoke)


async def _performance_read(method_name: str, **arguments):
    def invoke():
        return getattr(TrackBInspector(), method_name)(**arguments)

    return await anyio.to_thread.run_sync(invoke)


async def _performance_write(method_name: str, **arguments):
    def invoke():
        return getattr(TrackBController(), method_name)(**arguments)

    return await anyio.to_thread.run_sync(invoke)


async def _apply_batch(**arguments):
    """Run one ordered verified batch off the event loop."""

    def invoke():
        return VerifiedBatchExecutor().apply(**arguments)

    return await anyio.to_thread.run_sync(invoke)


@mcp.resource(
    "fl://capabilities",
    name="fl-capabilities",
    title="FL Studio capabilities",
    description=(
        "Live verified capability report for the connected FL Studio bridge."
    ),
    mime_type="application/json",
)
async def resource_capabilities() -> CapabilitiesReport:
    """Read the same typed capability report exposed by fl_get_capabilities."""

    return await _run("capabilities")


@mcp.resource(
    "fl://status",
    name="fl-status",
    title="FL Studio session status",
    description=(
        "Compact live connection, project, transport, and write-mode context."
    ),
    mime_type="application/json",
)
async def resource_status() -> dict[str, object]:
    """Return high-signal session context without requiring several tool calls."""

    capabilities = await _run("capabilities")
    project = await _run("project_summary")
    transport = await _run("transport_state")
    return {
        "connection": project.connection,
        "project_title": project.project_title,
        "dirty_flag": project.dirty_flag,
        "dirty_state": project.dirty_state,
        "transport": transport,
        "verified_writes_enabled": (
            capabilities.connection.verified_writes_enabled
        ),
        "bridge_mode": capabilities.connection.bridge_mode,
        "session_fingerprint": capabilities.connection.session_fingerprint,
    }


@mcp.resource(
    "fl://project",
    name="fl-project",
    title="FL Studio project",
    description="Live typed summary of the currently open FL Studio project.",
    mime_type="application/json",
)
async def resource_project() -> ProjectSummary:
    """Read current project metadata and counts."""

    return await _run("project_summary")


@mcp.resource(
    "fl://transport",
    name="fl-transport",
    title="FL Studio transport",
    description="Live playback, recording, loop, position, and tempo state.",
    mime_type="application/json",
)
async def resource_transport() -> TransportState:
    """Read the authoritative transport observation."""

    return await _run("transport_state")


@mcp.resource(
    "fl://mixer",
    name="fl-mixer",
    title="FL Studio mixer",
    description="Live bounded mixer-track inventory without instantaneous peaks.",
    mime_type="application/json",
)
async def resource_mixer() -> MixerTrackList:
    """Read the complete bounded mixer inventory using the normal inspector."""

    return await _run(
        "list_mixer_tracks",
        only_used=False,
        include_peaks=False,
        max_tracks=None,
    )


@mcp.resource(
    "fl://channels",
    name="fl-channels",
    title="FL Studio Channel Rack",
    description="Live global Channel Rack inventory with stable target identities.",
    mime_type="application/json",
)
async def resource_channels() -> ChannelList:
    """Read all global channels through the Track B inspection boundary."""

    return await _performance_read("list_channels")


@mcp.resource(
    "fl://patterns",
    name="fl-patterns",
    title="FL Studio patterns",
    description="Live pattern inventory, current selection, identity, and length.",
    mime_type="application/json",
)
async def resource_patterns() -> PatternList:
    """Read the current bounded pattern inventory."""

    return await _performance_read("list_patterns")


@mcp.resource(
    "fl://plugins",
    name="fl-plugins",
    title="FL Studio loaded plug-ins",
    description="Live inventory of loaded mixer effects and Channel Rack generators.",
    mime_type="application/json",
)
async def resource_plugins() -> TargetedLoadedPluginInventory:
    """Read loaded effects and generators without changing their parameters."""

    return await _performance_read("scan_loaded_plugins", only_used=False)


@mcp.tool(
    name="fl_get_capabilities",
    annotations=READ_ONLY.model_copy(update={"title": "Get verified FL capabilities"}),
)
async def fl_get_capabilities() -> CapabilitiesReport:
    """Report direct, partial, unavailable, and unvalidated integration paths."""
    return await _run("capabilities")


@mcp.tool(
    name="fl_get_project_summary",
    annotations=READ_ONLY.model_copy(update={"title": "Get project summary"}),
)
async def fl_get_project_summary() -> ProjectSummary:
    """Read project metadata, counts, dirty state, version, and transport state."""
    return await _run("project_summary")


@mcp.tool(
    name="fl_get_transport_state",
    annotations=READ_ONLY.model_copy(update={"title": "Get transport state"}),
)
async def fl_get_transport_state() -> TransportState:
    """Read playback, recording, loop mode, position, and song length."""
    return await _run("transport_state")


@mcp.tool(
    name="fl_get_selected_range",
    annotations=READ_ONLY.model_copy(
        update={"title": "Get raw Playlist timeline selection"}
    ),
)
async def fl_get_selected_range() -> SelectedRangeObservation:
    """Read raw endpoints and PPQ without claiming meter or rendering semantics."""
    return await _run("selected_range")


@mcp.tool(
    name="fl_list_mixer_tracks",
    annotations=READ_ONLY.model_copy(update={"title": "List mixer tracks"}),
)
async def fl_list_mixer_tracks(
    only_used: Annotated[
        bool,
        Field(
            description="Apply a conservative used-track heuristic. False is the authoritative default."
        ),
    ] = False,
    include_peaks: Annotated[
        bool,
        Field(description="Include instantaneous meter values; these are not audio analysis."),
    ] = False,
    max_tracks: Annotated[
        int | None,
        Field(
            default=None,
            description="Optional early page limit for a large mixer scan.",
            ge=1,
            le=500,
        ),
    ] = None,
) -> MixerTrackList:
    """List mixer tracks, current levels, selection state, and loaded effects."""
    return await _run(
        "list_mixer_tracks",
        only_used=only_used,
        include_peaks=include_peaks,
        max_tracks=max_tracks,
    )


@mcp.tool(
    name="fl_inspect_mixer_track",
    annotations=READ_ONLY.model_copy(update={"title": "Inspect one mixer track"}),
)
async def fl_inspect_mixer_track(
    track_index: Annotated[
        int,
        Field(description="Zero-based mixer index. Index 0 is always Master.", ge=0),
    ],
) -> MixerTrackInspection:
    """Read one track's state, effects, built-in EQ, and outgoing routes."""
    return await _run("inspect_mixer_track", track_index=track_index)


@mcp.tool(
    name="plugins_scan_loaded_plugins",
    annotations=READ_ONLY.model_copy(update={"title": "Scan loaded effects"}),
)
async def plugins_scan_loaded_plugins(
    only_used: Annotated[
        bool,
        Field(description="Apply the conservative used-track heuristic."),
    ] = False,
    include_channel_generators: Annotated[
        bool,
        Field(
            description=(
                "Also include Channel Rack generators with explicit "
                "channel_generator targets. False preserves the 0.11 "
                "mixer-effect-only response contract."
            )
        ),
    ] = False,
) -> LoadedPluginInventory | TargetedLoadedPluginInventory:
    """Inventory loaded plug-ins without inserting or changing anything."""
    if include_channel_generators:
        return await _performance_read("scan_loaded_plugins", only_used=only_used)
    return await _run("scan_loaded_plugins", only_used=only_used)


@mcp.tool(
    name="plugins_inspect_parameter_map",
    annotations=READ_ONLY.model_copy(update={"title": "Inspect plug-in parameters"}),
)
async def plugins_inspect_parameter_map(
    track_index: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Legacy zero-based mixer track index. Supply it together with "
                "slot_index, or use target, never both."
            ),
            ge=0,
        ),
    ] = None,
    slot_index: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Legacy zero-based effect slot (0 through 9). Supply it with "
                "track_index, or use target, never both."
            ),
            ge=0,
            le=9,
        ),
    ] = None,
    target: Annotated[
        PluginTarget | None,
        Field(
            default=None,
            description=(
                "Explicit mixer_effect or global channel_generator target. "
                "Mutually exclusive with legacy track_index/slot_index."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Number of parameter indices to scan in this page.", ge=1, le=128),
    ] = 32,
    offset: Annotated[
        int,
        Field(description="First parameter index in this page.", ge=0),
    ] = 0,
    name_filter: Annotated[
        str | None,
        Field(default=None, description="Optional case-insensitive name substring."),
    ] = None,
) -> PluginParameterPage | TargetedPluginParameterPage:
    """Read a bounded page of exposed parameters; never marks unknown controls safe."""
    if target is not None:
        return await _performance_read(
            "plugin_parameters",
            target=target,
            track_index=track_index,
            slot_index=slot_index,
            limit=limit,
            offset=offset,
            name_filter=name_filter,
        )
    if track_index is None or slot_index is None:
        raise ValueError(
            "target or both legacy track_index and slot_index must be supplied"
        )
    return await _run(
        "plugin_parameters",
        track_index=track_index,
        slot_index=slot_index,
        limit=limit,
        offset=offset,
        name_filter=name_filter,
    )


@mcp.tool(
    name="plugins_scan_parameters",
    annotations=READ_ONLY.model_copy(update={"title": "Scan a plug-in's real parameters"}),
)
async def plugins_scan_parameters(
    track_index: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Legacy zero-based mixer track index. Supply it together with "
                "slot_index, or use target, never both."
            ),
            ge=0,
        ),
    ] = None,
    slot_index: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Legacy zero-based effect slot (0 through 9). Supply it with "
                "track_index, or use target, never both."
            ),
            ge=0,
            le=9,
        ),
    ] = None,
    target: Annotated[
        PluginTarget | None,
        Field(
            default=None,
            description=(
                "Explicit mixer_effect or global channel_generator target. "
                "Mutually exclusive with legacy track_index/slot_index."
            ),
        ),
    ] = None,
    start: Annotated[
        int | None,
        Field(default=None, description="First index to examine. Defaults to 0.", ge=0),
    ] = None,
    end: Annotated[
        int | None,
        Field(
            default=None,
            description="Exclusive last index. Defaults to FL's reported count.",
            ge=0,
        ),
    ] = None,
    max_indices: Annotated[
        int | None,
        Field(
            default=None,
            description="Stop after examining this many indices.",
            ge=1,
            le=8192,
        ),
    ] = None,
    max_results: Annotated[
        int | None,
        Field(default=None, description="Stop after collecting this many real controls.", ge=1),
    ] = None,
) -> PluginParameterScan | TargetedPluginParameterScan:
    """De-pad a whole plug-in in one call; prefer this over paging a VST.

    FL reports a padded maximum rather than a parameter count for VST plug-ins
    -- often thousands of slots for a VST3 -- with the real controls sparse
    inside it.
    Paging that with `plugins_inspect_parameter_map` is about a thousand round
    trips. This walks the range inside FL and returns only what is real, with
    each control's display string, which is what actually identifies it.

    Check `truncated` before treating the result as the whole plug-in.
    """
    if target is not None:
        return await _performance_read(
            "scan_plugin_parameters",
            target=target,
            track_index=track_index,
            slot_index=slot_index,
            start=start,
            end=end,
            max_indices=max_indices,
            max_results=max_results,
        )
    if track_index is None or slot_index is None:
        raise ValueError(
            "target or both legacy track_index and slot_index must be supplied"
        )
    return await _run(
        "scan_plugin_parameters",
        track_index=track_index,
        slot_index=slot_index,
        start=start,
        end=end,
        max_indices=max_indices,
        max_results=max_results,
    )


@mcp.tool(
    name="plugins_atlas_search",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Search Plugin Atlas"}),
)
async def plugins_atlas_search(
    request: AtlasSearchRequest,
) -> AtlasSearchResponse:
    """Search bundled static plug-in knowledge without contacting FL Studio."""
    return await _mix(search_atlas, request)


@mcp.tool(
    name="plugins_atlas_get_product",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Get Plugin Atlas product"}),
)
async def plugins_atlas_get_product(
    request: AtlasGetProductRequest,
) -> AtlasProductResponse:
    """Read one static Atlas product and its related descriptive records."""
    return await _mix(get_atlas_product, request)


@mcp.tool(
    name="plugins_atlas_recommend",
    annotations=LOCAL_READ_ONLY.model_copy(
        update={"title": "Recommend from Plugin Atlas"}
    ),
)
async def plugins_atlas_recommend(
    request: AtlasRecommendRequest,
) -> AtlasRecommendationResponse:
    """Rank static plug-in choices or stock alternatives without changing FL."""
    return await _mix(recommend_atlas, request)


@mcp.tool(
    name="plugins_atlas_inspect_loaded",
    annotations=READ_ONLY.model_copy(
        update={"title": "Match loaded plug-ins to Plugin Atlas"}
    ),
)
async def plugins_atlas_inspect_loaded(
    request: AtlasInspectLoadedRequest,
) -> AtlasInspectLoadedResponse:
    """Match the target-aware live Track B inventory to static Atlas knowledge."""
    return await _mix(inspect_loaded_atlas, request)


@mcp.tool(
    name="copilot_capture_readonly_inspection",
    annotations=READ_ONLY.model_copy(update={"title": "Capture read-only inspection"}),
)
async def copilot_capture_readonly_inspection(
    only_used: Annotated[
        bool,
        Field(description="Apply the conservative used-track heuristic."),
    ] = False,
    parameter_limit: Annotated[
        int,
        Field(description="Maximum parameter indices previewed per plug-in.", ge=1, le=64),
    ] = 16,
    max_plugins: Annotated[
        int,
        Field(description="Maximum loaded plug-ins whose parameters are previewed.", ge=1, le=64),
    ] = 16,
) -> ReadOnlyInspectionReport:
    """Capture a compact project, mixer, and effect inspection report."""
    return await _run(
        "capture",
        only_used=only_used,
        parameter_limit=parameter_limit,
        max_plugins=max_plugins,
    )


# ---------------------------------------------------------------------------
# runtime write-mode control and verified writes
#
# Each tool applies the change and reports; there is no confirmation
# round-trip. The returned model always carries `verified`, which the bridge
# decided by reading FL back on a later idle tick, plus a
# `verification_summary` and -- when unverified -- a leading warning. None of
# these tools raises because a write went unverified: that is a real outcome
# about the user's project and it is reported, not hidden behind an exception.
# ---------------------------------------------------------------------------


@mcp.tool(
    name="fl_set_write_mode",
    annotations=WRITE_MODE_CONTROL,
)
async def fl_set_write_mode(
    enabled: Annotated[
        bool,
        Field(
            description=(
                "Absolute session write state. True exposes only the bounded "
                "verified write tools; false locks them again."
            )
        ),
    ],
    confirm_user_present: Annotated[
        bool,
        Field(
            description=(
                "Must be true to enable writes, after the present user explicitly "
                "requested the capability change. Not required to disable writes."
            )
        ),
    ] = False,
) -> WriteModeChange:
    """Enable or disable writes for this bridge session without restarting FL.

    This changes no project value and never persists the setting. Enabling is
    refused unless `confirm_user_present` is literally true. The result is
    verified with a fresh bridge handshake before it reports success.
    """
    return await _set_write_mode(
        enabled=enabled,
        confirm_user_present=confirm_user_present,
    )


@mcp.tool(
    name="fl_set_mixer_volume",
    annotations=MUTATING.model_copy(update={"title": "Set a mixer track's volume"}),
)
async def fl_set_mixer_volume(
    track_index: Annotated[
        int,
        Field(
            description="Zero-based mixer index. Index 0 is Master and is refused unless allow_master is true.",
            ge=0,
        ),
    ],
    volume_normalized: Annotated[
        float,
        Field(
            description="Fader position, 0.0 silent to 1.0 maximum. 0.8 is FL Studio's 0 dB default.",
            ge=0.0,
            le=1.0,
        ),
    ],
    allow_master: Annotated[
        bool,
        Field(description="Deliberately target the master bus at index 0."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        float | None,
        Field(
            default=None,
            description="Optional expected current fader position; refuse if it changed.",
            ge=0.0,
            le=1.0,
        ),
    ] = None,
) -> VerifiedMixerVolumeWrite:
    """Set one mixer fader and report the readback FL gave on a later tick."""
    return await _write(
        "set_mixer_volume",
        track_index=track_index,
        volume_normalized=volume_normalized,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_mixer_volume_db",
    annotations=MUTATING.model_copy(update={"title": "Set a mixer fader in dB"}),
)
async def fl_set_mixer_volume_db(
    track_index: Annotated[
        int,
        Field(description="Zero-based mixer index; Master requires allow_master.", ge=0),
    ],
    volume_db: Annotated[
        float,
        Field(description="Target fader readback in dB.", ge=-60.0, le=6.0),
    ],
    tolerance_db: Annotated[
        float,
        Field(description="Maximum accepted dB readback error.", ge=0.01, le=1.0),
    ] = 0.1,
    allow_master: Annotated[
        bool, Field(description="Deliberately target Master at mixer index 0.")
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedMixerVolumeState | None,
        Field(default=None, description="Optional expected normalized and/or dB state."),
    ] = None,
) -> VerifiedMixerVolumeDbWrite:
    """Search FL's fader curve and prove the requested dB value on a later tick."""
    return await _write(
        "set_mixer_volume_db",
        track_index=track_index,
        volume_db=volume_db,
        tolerance_db=tolerance_db,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_mixer_pan",
    annotations=MUTATING.model_copy(update={"title": "Set a mixer track's pan"}),
)
async def fl_set_mixer_pan(
    track_index: Annotated[
        int,
        Field(
            description="Zero-based mixer index. Index 0 is Master and is refused unless allow_master is true.",
            ge=0,
        ),
    ],
    pan: Annotated[
        float,
        Field(
            description="Pan position, -1.0 hard left through 0.0 centre to 1.0 hard right.",
            ge=-1.0,
            le=1.0,
        ),
    ],
    allow_master: Annotated[
        bool,
        Field(description="Deliberately target the master bus at index 0."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        float | None,
        Field(
            default=None,
            description="Optional expected current pan; refuse if it changed.",
            ge=-1.0,
            le=1.0,
        ),
    ] = None,
) -> VerifiedMixerPanWrite:
    """Set one mixer pan and report the readback FL gave on a later tick."""
    return await _write(
        "set_mixer_pan",
        track_index=track_index,
        pan=pan,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_mixer_mute",
    annotations=MUTATING.model_copy(update={"title": "Mute or unmute a mixer track"}),
)
async def fl_set_mixer_mute(
    track_index: Annotated[
        int,
        Field(
            description="Zero-based mixer index. Index 0 is Master and is refused unless allow_master is true.",
            ge=0,
        ),
    ],
    muted: Annotated[
        bool,
        Field(
            description="The wanted state: true mutes, false unmutes. This is stated, never toggled."
        ),
    ],
    allow_master: Annotated[
        bool,
        Field(description="Deliberately target the master bus at index 0."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        bool | None,
        Field(
            default=None,
            description="Optional expected current mute state; refuse if it changed.",
        ),
    ] = None,
) -> VerifiedMixerMuteWrite:
    """Set one track's mute state and report the readback FL gave on a later tick."""
    return await _write(
        "set_mixer_mute",
        track_index=track_index,
        muted=muted,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_mixer_solo",
    annotations=MUTATING.model_copy(update={"title": "Solo or unsolo a mixer track"}),
)
async def fl_set_mixer_solo(
    track_index: Annotated[
        int,
        Field(description="Zero-based mixer index. Index 0 is Master.", ge=0),
    ],
    soloed: Annotated[
        bool,
        Field(description="The absolute wanted solo state; never a toggle."),
    ],
    allow_master: Annotated[
        bool,
        Field(description="Required to target mixer track 0."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        bool | None,
        Field(default=None, description="Optional expected current solo state."),
    ] = None,
) -> VerifiedMixerSoloWrite:
    """Set one mixer track's solo state and verify it on a later FL tick."""
    return await _write(
        "set_mixer_solo",
        track_index=track_index,
        soloed=soloed,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_mixer_arm",
    annotations=MUTATING.model_copy(update={"title": "Arm or disarm a mixer track"}),
)
async def fl_set_mixer_arm(
    track_index: Annotated[
        int,
        Field(description="Zero-based mixer index. Index 0 is Master.", ge=0),
    ],
    armed: Annotated[
        bool,
        Field(description="The absolute wanted recording-arm state."),
    ],
    allow_master: Annotated[
        bool,
        Field(description="Required to target mixer track 0."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        bool | None,
        Field(default=None, description="Optional expected current arm state."),
    ] = None,
) -> VerifiedMixerArmWrite:
    """Set recording arm with one bounded toggle and later-tick readback."""
    return await _write(
        "set_mixer_arm",
        track_index=track_index,
        armed=armed,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_mixer_color",
    annotations=MUTATING.model_copy(update={"title": "Set a mixer track color"}),
)
async def fl_set_mixer_color(
    track_index: Annotated[
        int,
        Field(description="Zero-based mixer index. Index 0 is Master.", ge=0),
    ],
    color: Annotated[
        int,
        Field(
            description="FL color word as unsigned 0xAABBGGRR integer.",
            ge=0,
            le=0xFFFFFFFF,
        ),
    ],
    allow_master: Annotated[
        bool,
        Field(description="Required to target mixer track 0."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        int | None,
        Field(
            default=None,
            description="Optional expected current FL color word.",
            ge=0,
            le=0xFFFFFFFF,
        ),
    ] = None,
) -> VerifiedMixerColorWrite:
    """Set a mixer color, accepting FL-owned differences in the high byte."""
    return await _write(
        "set_mixer_color",
        track_index=track_index,
        color=color,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_mixer_stereo_separation",
    annotations=MUTATING.model_copy(
        update={"title": "Set mixer stereo separation"}
    ),
)
async def fl_set_mixer_stereo_separation(
    track_index: Annotated[
        int,
        Field(description="Zero-based mixer index. Index 0 is Master.", ge=0),
    ],
    stereo_separation: Annotated[
        float,
        Field(description="FL stereo-separation value from -1.0 to 1.0.", ge=-1.0, le=1.0),
    ],
    allow_master: Annotated[
        bool,
        Field(description="Required to target mixer track 0."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        float | None,
        Field(
            default=None,
            description="Optional expected current stereo-separation value.",
            ge=-1.0,
            le=1.0,
        ),
    ] = None,
) -> VerifiedMixerStereoSeparationWrite:
    """Set stereo separation and report FL's later-tick readback."""
    return await _write(
        "set_mixer_stereo_separation",
        track_index=track_index,
        stereo_separation=stereo_separation,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_select_mixer_track",
    annotations=MUTATING.model_copy(update={"title": "Select a mixer track"}),
)
async def fl_select_mixer_track(
    track_index: Annotated[
        int,
        Field(description="Zero-based mixer index to make active.", ge=0),
    ],
    allow_master: Annotated[
        bool,
        Field(description="Required to select mixer track 0 (Master)."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        int | None,
        Field(default=None, description="Optional expected active track index.", ge=0),
    ] = None,
) -> VerifiedMixerSelectionWrite:
    """Make one mixer track active and verify the active-track getter."""
    return await _write(
        "select_mixer_track",
        track_index=track_index,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_track_eq",
    annotations=MUTATING.model_copy(update={"title": "Set a built-in EQ band"}),
)
async def fl_set_track_eq(
    track_index: Annotated[
        int,
        Field(
            description="Zero-based mixer index. Index 0 is Master and is refused unless allow_master is true.",
            ge=0,
        ),
    ],
    band_index: Annotated[
        int,
        Field(
            description="Which band of the track's built-in three-band EQ: 0 low, 1 mid, 2 high.",
            ge=0,
            le=2,
        ),
    ],
    gain_normalized: Annotated[
        float | None,
        Field(
            default=None,
            description="Band gain, normalized 0.0 to 1.0; 0.5 is flat. Omit to leave the gain alone.",
            ge=0.0,
            le=1.0,
        ),
    ] = None,
    frequency_normalized: Annotated[
        float | None,
        Field(
            default=None,
            description="Band centre frequency, normalized 0.0 to 1.0. Omit to leave the frequency alone.",
            ge=0.0,
            le=1.0,
        ),
    ] = None,
    allow_master: Annotated[
        bool,
        Field(description="Deliberately target the master bus at index 0."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedEqBandState | None,
        Field(
            default=None,
            description="Optional expected gain and/or frequency; refuse if any supplied field changed.",
        ),
    ] = None,
) -> VerifiedMixerEqWrite:
    """Set gain and/or frequency on one built-in EQ band; at least one is required."""
    return await _write(
        "set_mixer_eq",
        track_index=track_index,
        band_index=band_index,
        gain_normalized=gain_normalized,
        frequency_normalized=frequency_normalized,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_mixer_name",
    annotations=MUTATING.model_copy(update={"title": "Name a mixer track"}),
)
async def fl_set_mixer_name(
    track_index: Annotated[
        int,
        Field(description="Zero-based mixer index. Index 0 is Master.", ge=0),
    ],
    name: Annotated[
        str,
        Field(description="New track name. Pass \"\" to restore FL's default."),
    ],
    allow_master: Annotated[
        bool,
        Field(description="Required to target mixer track 0."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        str | None,
        Field(
            default=None,
            max_length=64,
            description="Optional expected current name; refuse if it changed.",
        ),
    ] = None,
) -> VerifiedMixerNameWrite:
    """Rename one mixer track.

    An empty name is not a blank label: FL puts the track's default back
    ("Insert 8"), and the result says so via `restored_default`.
    """
    return await _write(
        "set_mixer_name",
        track_index=track_index,
        name=name,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_mixer_send",
    annotations=MUTATING.model_copy(update={"title": "Create or remove a send"}),
)
async def fl_set_mixer_send(
    track_index: Annotated[
        int,
        Field(description="Zero-based index of the sending track.", ge=0),
    ],
    destination_track_index: Annotated[
        int,
        Field(description="Zero-based index of the receiving track.", ge=0),
    ],
    enabled: Annotated[
        bool,
        Field(description="True to create the send, False to tear it down."),
    ],
    allow_master: Annotated[
        bool,
        Field(description="Required only to send FROM track 0."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        bool | None,
        Field(
            default=None,
            description="Optional expected current route state; refuse if it changed.",
        ),
    ] = None,
) -> VerifiedMixerSendWrite:
    """Route one mixer track to another, or stop routing it there.

    A stated state, never a toggle. Sending *to* Master needs no flag; only
    sending *from* Master does. Set the amount with `fl_set_mixer_send_level`
    afterwards -- this call only decides whether the route exists.
    """
    return await _write(
        "set_mixer_send",
        track_index=track_index,
        destination_track_index=destination_track_index,
        enabled=enabled,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_mixer_send_level",
    annotations=MUTATING.model_copy(update={"title": "Set a send level"}),
)
async def fl_set_mixer_send_level(
    track_index: Annotated[
        int,
        Field(description="Zero-based index of the sending track.", ge=0),
    ],
    destination_track_index: Annotated[
        int,
        Field(description="Zero-based index of the receiving track.", ge=0),
    ],
    level_normalized: Annotated[
        float,
        Field(description="Send amount, 0..1. 0.8 is unity.", ge=0.0, le=1.0),
    ],
    allow_master: Annotated[
        bool,
        Field(description="Required only to send FROM track 0."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        float | None,
        Field(
            default=None,
            description="Optional expected current send amount; refuse if it changed.",
            ge=0.0,
            le=1.0,
        ),
    ] = None,
) -> VerifiedMixerSendLevelWrite:
    """Set how much of one track reaches another. 0.8 is unity, as on the fader.

    The send must already exist; create it with `fl_set_mixer_send` first. FL
    raises rather than reporting a level for a route that is not active, so
    this is refused outright rather than written and reported unverified.
    """
    return await _write(
        "set_mixer_send_level",
        track_index=track_index,
        destination_track_index=destination_track_index,
        level_normalized=level_normalized,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_plugin_param_display",
    annotations=MUTATING.model_copy(
        update={"title": "Set a plug-in parameter in its own units"}
    ),
)
async def fl_set_plugin_param_display(
    parameter: Annotated[
        int | str,
        Field(
            description=(
                "Parameter index, or text matched against parameter names AND "
                "display strings (many real third-party controls have no name)."

            )
        ),
    ],
    target_value: Annotated[
        float,
        Field(
            description=(
                "The number the plug-in displays: 20 for '20 ms', -18 for "
                "'-18.0 dB', 4000 for '4.0kHz'."
            )
        ),
    ],
    track_index: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Legacy zero-based mixer index. Supply it with slot_index, or "
                "use target, never both."
            ),
            ge=0,
        ),
    ] = None,
    slot_index: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Legacy zero-based effect slot 0 through 9. Supply it with "
                "track_index, or use target, never both."
            ),
            ge=0,
            le=9,
        ),
    ] = None,
    target: Annotated[
        PluginTarget | None,
        Field(
            default=None,
            description=(
                "Explicit mixer_effect or global channel_generator target. "
                "Mutually exclusive with legacy track_index/slot_index."
            ),
        ),
    ] = None,
    tolerance: Annotated[
        float | None,
        Field(
            default=None,
            description="How close counts. Defaults to 2% of the target, floor 0.01.",
            ge=0.0,
        ),
    ] = None,
    allow_master: Annotated[
        bool,
        Field(description="Required to target mixer track 0."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedPluginParameterState | None,
        Field(
            default=None,
            description="Optional expected normalized value and/or exact display text; refuse if any supplied field changed.",
        ),
    ] = None,
) -> VerifiedPluginDisplayWrite | VerifiedTargetedPluginDisplayWrite:
    """Set a plug-in parameter using the units it displays, not a 0..1 guess.

    Prefer this over `fl_set_plugin_param` for anything with real units.
    Normalized 0..1 has no published mapping to ms, dB or Hz, and the curve
    differs per control; this searches the control until its own readback
    reports the number you asked for, so no curve is ever assumed.

    Address the parameter by name where it has one ("Attack"), or by
    what it displays where it does not ("Auto mode"). Run
    `plugins_scan_parameters` first to see both.

    Controls whose display is pure text -- "Chromatic", "Low Male" -- are
    enumerations with no number to search on and are refused. Use
    `fl_set_plugin_param_option`, which sets them by their option text and
    also reports every option the control accepts.
    """
    if target is not None:
        return await _performance_write(
            "set_plugin_parameter_display",
            target=target,
            track_index=track_index,
            slot_index=slot_index,
            parameter=parameter,
            target_value=target_value,
            tolerance=tolerance,
            allow_master=allow_master,
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
    if track_index is None or slot_index is None:
        raise ValueError(
            "target or both legacy track_index and slot_index must be supplied"
        )
    return await _write(
        "set_plugin_parameter_display",
        track_index=track_index,
        slot_index=slot_index,
        parameter=parameter,
        target_value=target_value,
        tolerance=tolerance,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_plugin_param_option",
    annotations=MUTATING.model_copy(
        update={"title": "Set an enumerated plug-in parameter"}
    ),
)
async def fl_set_plugin_param_option(
    parameter: Annotated[
        int | str,
        Field(description="Parameter index, or text matched against names and displays."),
    ],
    option: Annotated[
        str,
        Field(
            description=(
                "The exact option text to land on, e.g. 'A', 'Major', "
                "'Low Male'."
            )
        ),
    ],
    track_index: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Legacy zero-based mixer index. Supply it with slot_index, or "
                "use target, never both."
            ),
            ge=0,
        ),
    ] = None,
    slot_index: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Legacy zero-based effect slot 0 through 9. Supply it with "
                "track_index, or use target, never both."
            ),
            ge=0,
            le=9,
        ),
    ] = None,
    target: Annotated[
        PluginTarget | None,
        Field(
            default=None,
            description=(
                "Explicit mixer_effect or global channel_generator target. "
                "Mutually exclusive with legacy track_index/slot_index."
            ),
        ),
    ] = None,
    sweep_steps: Annotated[
        int,
        Field(
            description="Sweep resolution. Raise it only if an option is being missed.",
            ge=2,
            le=256,
        ),
    ] = 64,
    allow_master: Annotated[
        bool,
        Field(description="Required to target mixer track 0."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedPluginParameterState | None,
        Field(
            default=None,
            description="Optional expected normalized value and/or exact display text; refuse if any supplied field changed.",
        ),
    ] = None,
) -> VerifiedPluginOptionWrite | VerifiedTargetedPluginOptionWrite:
    """Set a parameter that shows words rather than numbers: Key, Scale, Input Type.

    Use this where `fl_set_plugin_param_display` refuses. That tool searches on
    a number, and an enumeration has none.

    **This moves the control while it looks.** FL cannot report a control's
    options, so the only way to find them is to walk the parameter across its
    range and read what it displays. The requested label must exactly match an
    option, ignoring case. If it does not exist, the original value is restored
    before the error, and the error lists every option that was found.

    The result carries `options` -- the whole enumeration, in order -- so one
    call is also how you discover what a control accepts.
    """
    if target is not None:
        return await _performance_write(
            "set_plugin_parameter_option",
            target=target,
            track_index=track_index,
            slot_index=slot_index,
            parameter=parameter,
            option=option,
            sweep_steps=sweep_steps,
            allow_master=allow_master,
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
    if track_index is None or slot_index is None:
        raise ValueError(
            "target or both legacy track_index and slot_index must be supplied"
        )
    return await _write(
        "set_plugin_parameter_option",
        track_index=track_index,
        slot_index=slot_index,
        parameter=parameter,
        option=option,
        sweep_steps=sweep_steps,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_plugin_param",
    annotations=MUTATING.model_copy(update={"title": "Set one plug-in parameter"}),
)
async def fl_set_plugin_param(
    parameter_index: Annotated[
        int,
        Field(
            description="Parameter index as reported by plugins_inspect_parameter_map. Nothing here knows what the control does.",
            ge=0,
        ),
    ],
    normalized_value: Annotated[
        float,
        Field(description="Parameter value, normalized 0.0 to 1.0.", ge=0.0, le=1.0),
    ],
    track_index: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Legacy zero-based mixer index. Supply it with slot_index, or "
                "use target, never both."
            ),
            ge=0,
        ),
    ] = None,
    slot_index: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Legacy zero-based effect slot 0 through 9. Supply it with "
                "track_index, or use target, never both."
            ),
            ge=0,
            le=9,
        ),
    ] = None,
    target: Annotated[
        PluginTarget | None,
        Field(
            default=None,
            description=(
                "Explicit mixer_effect or global channel_generator target. "
                "Mutually exclusive with legacy track_index/slot_index."
            ),
        ),
    ] = None,
    allow_master: Annotated[
        bool,
        Field(description="Deliberately target the master bus at index 0."),
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedPluginParameterState | None,
        Field(
            default=None,
            description="Optional expected normalized value and/or exact display text; refuse if any supplied field changed.",
        ),
    ] = None,
) -> VerifiedPluginParameterWrite | VerifiedTargetedPluginParameterWrite:
    """Set one plug-in parameter; verified from FL's display string changing."""
    if target is not None:
        return await _performance_write(
            "set_plugin_parameter",
            target=target,
            track_index=track_index,
            slot_index=slot_index,
            parameter_index=parameter_index,
            normalized_value=normalized_value,
            allow_master=allow_master,
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
    if track_index is None or slot_index is None:
        raise ValueError(
            "target or both legacy track_index and slot_index must be supplied"
        )
    return await _write(
        "set_plugin_parameter",
        track_index=track_index,
        slot_index=slot_index,
        parameter_index=parameter_index,
        normalized_value=normalized_value,
        allow_master=allow_master,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


# ---------------------------------------------------------------------------
# transport, Channel Rack, and current-pattern performance surface
# ---------------------------------------------------------------------------


@mcp.tool(
    name="fl_apply_verified_batch",
    annotations=MUTATING.model_copy(update={"title": "Apply a verified write batch"}),
)
async def fl_apply_verified_batch(
    operations: Annotated[
        list[BatchOperation],
        Field(
            description=(
                "Ordered absolute writes. Operation IDs and written fields must be "
                "unique; every attempted item gets its own later-tick receipt."
            ),
            min_length=1,
            max_length=MAX_BATCH_OPERATIONS,
        ),
    ],
    stop_on_unverified: Annotated[
        bool,
        Field(description="Skip remaining items after the first unverified receipt."),
    ] = True,
    session_fingerprint: SessionFingerprintArg = None,
) -> VerifiedBatchResult:
    """Apply a closed-union batch after one session preflight, without replay."""
    return await _apply_batch(
        operations=operations,
        stop_on_unverified=stop_on_unverified,
        session_fingerprint=session_fingerprint,
    )


@mcp.tool(
    name="fl_set_playing",
    annotations=MUTATING.model_copy(update={"title": "Set playback state"}),
)
async def fl_set_playing(
    playing: Annotated[bool, Field(description="Absolute playing state; never a toggle.")],
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedPlayingState | None,
        Field(default=None, description="Optional expected current playing state."),
    ] = None,
) -> VerifiedPlayingWrite:
    """Set playback to an absolute state and verify it on a later FL idle tick."""
    return await _performance_write(
        "set_playing", playing=playing, session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_stop",
    annotations=MUTATING.model_copy(update={"title": "Stop and rewind playback"}),
)
async def fl_stop(
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedStopState | None,
        Field(default=None, description="Optional expected playing and/or position state."),
    ] = None,
) -> VerifiedStopWrite:
    """Stop playback, set normalized position to zero, and verify both fields."""
    return await _performance_write(
        "stop", session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_song_position",
    annotations=MUTATING.model_copy(update={"title": "Set the song position"}),
)
async def fl_set_song_position(
    position_normalized: Annotated[
        float, Field(description="Absolute normalized playhead position.", ge=0.0, le=1.0)
    ],
    tolerance: Annotated[
        float, Field(description="Maximum normalized readback error.", ge=0.0, le=0.05)
    ] = 0.0001,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedSongPositionState | None,
        Field(default=None, description="Optional expected current normalized position."),
    ] = None,
) -> VerifiedSongPositionWrite:
    """Set a stopped transport's absolute normalized playhead position."""
    return await _performance_write(
        "set_song_position", position_normalized=position_normalized,
        tolerance=tolerance, session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_loop_mode",
    annotations=MUTATING.model_copy(update={"title": "Set the transport loop mode"}),
)
async def fl_set_loop_mode(
    loop_mode: Annotated[
        str, Field(description="Absolute loop mode: 'pattern' or 'song'.", pattern=r"^(pattern|song)$")
    ],
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedLoopModeState | None,
        Field(default=None, description="Optional expected current loop mode."),
    ] = None,
) -> VerifiedLoopModeWrite:
    """Set Pattern or Song loop mode without exposing FL's toggle-only API."""
    return await _performance_write(
        "set_loop_mode", loop_mode=loop_mode,
        session_fingerprint=session_fingerprint, expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_tempo",
    annotations=MUTATING.model_copy(update={"title": "Set the project tempo"}),
)
async def fl_set_tempo(
    tempo_bpm: Annotated[
        float, Field(description="Absolute project tempo in BPM.", ge=10.0, le=522.0)
    ],
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedTempoState | None,
        Field(default=None, description="Optional expected current tempo in BPM."),
    ] = None,
) -> VerifiedTempoWrite:
    """Set project tempo while stopped and verify BPM on a later FL idle tick."""
    return await _performance_write(
        "set_tempo", tempo_bpm=tempo_bpm,
        session_fingerprint=session_fingerprint, expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_recording",
    annotations=MUTATING.model_copy(update={"title": "Set recording arm state"}),
)
async def fl_set_recording(
    recording: Annotated[
        bool, Field(description="Absolute transport recording-arm state.")
    ],
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedRecordingState | None,
        Field(default=None, description="Optional expected recording state."),
    ] = None,
) -> VerifiedRecordingWrite:
    """Set recording absolutely; FL's toggle is dispatched at most once."""
    return await _performance_write(
        "set_recording",
        recording=recording,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_metronome",
    annotations=MUTATING.model_copy(update={"title": "Set metronome state"}),
)
async def fl_set_metronome(
    enabled: Annotated[bool, Field(description="Absolute metronome state.")],
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedMetronomeState | None,
        Field(default=None, description="Optional expected metronome state."),
    ] = None,
) -> VerifiedMetronomeWrite:
    """Set the metronome absolutely and prove the later UI state."""
    return await _performance_write(
        "set_metronome",
        enabled=enabled,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_precount",
    annotations=MUTATING.model_copy(update={"title": "Set recording precount"}),
)
async def fl_set_precount(
    enabled: Annotated[
        bool, Field(description="Absolute countdown-before-recording state.")
    ],
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedPrecountState | None,
        Field(default=None, description="Optional expected precount state."),
    ] = None,
) -> VerifiedPrecountWrite:
    """Set recording precount absolutely and verify it on a later FL tick."""
    return await _performance_write(
        "set_precount",
        enabled=enabled,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_time_signature_numerator",
    annotations=MUTATING.model_copy(
        update={"title": "Set time-signature numerator"}
    ),
)
async def fl_set_time_signature_numerator(
    numerator: Annotated[
        int,
        Field(
            description="Beats per bar. FL exposes no denominator getter.",
            ge=1,
            le=32,
        ),
    ],
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedTimeSignatureState | None,
        Field(default=None, description="Optional expected current numerator."),
    ] = None,
) -> VerifiedTimeSignatureNumeratorWrite:
    """Set and prove beats per bar from FL's PPB/PPQ getter pair."""
    return await _performance_write(
        "set_time_signature_numerator",
        numerator=numerator,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_get_project_history",
    annotations=READ_ONLY.model_copy(update={"title": "Read project undo history"}),
)
async def fl_get_project_history() -> ProjectHistoryObservation:
    """Read undo/redo bounds, current history position, hint, and dirty state."""
    return await _performance_read("project_history")


@mcp.tool(
    name="fl_undo",
    annotations=MUTATING.model_copy(update={"title": "Undo one project change"}),
)
async def fl_undo(
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedProjectHistoryState | None,
        Field(default=None, description="Optional history position/count/dirty guard."),
    ] = None,
) -> VerifiedProjectHistoryMove:
    """Move to the previous absolute undo-history position and verify it."""
    return await _performance_write(
        "undo",
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_redo",
    annotations=MUTATING.model_copy(update={"title": "Redo one project change"}),
)
async def fl_redo(
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedProjectHistoryState | None,
        Field(default=None, description="Optional history position/count/dirty guard."),
    ] = None,
) -> VerifiedProjectHistoryMove:
    """Move to the next absolute undo-history position and verify it."""
    return await _performance_write(
        "redo",
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_get_plugin_preset_count",
    annotations=READ_ONLY.model_copy(update={"title": "Read plug-in preset count"}),
)
async def fl_get_plugin_preset_count(
    target: Annotated[
        PluginTarget,
        Field(description="Explicit mixer effect or global channel-generator target."),
    ],
) -> PluginPresetCount:
    """Read FL's authoritative preset count for one loaded plug-in."""
    return await _performance_read("plugin_preset_count", target=target)


@mcp.tool(
    name="plugins_list_presets",
    annotations=READ_ONLY.model_copy(update={"title": "List plug-in presets"}),
)
async def plugins_list_presets(
    target: Annotated[
        PluginTarget,
        Field(description="Explicit mixer effect or global channel-generator target."),
    ],
    start: Annotated[int, Field(ge=0, description="First preset index to inspect.")] = 0,
    limit: Annotated[
        int,
        Field(ge=1, le=256, description="Bounded number of preset names in this page."),
    ] = 64,
    include_current: Annotated[
        bool,
        Field(description="Also report FL's current preset identity."),
    ] = True,
    include_empty_names: Annotated[
        bool,
        Field(description="Retain blank preset-name rows in the returned page."),
    ] = False,
) -> PluginPresetPage:
    """Read one deterministic preset page without changing the plug-in."""
    return await _performance_read(
        "list_plugin_presets",
        target=target,
        start=start,
        limit=limit,
        include_current=include_current,
        include_empty_names=include_empty_names,
    )


@mcp.tool(
    name="plugins_get_current_preset",
    annotations=READ_ONLY.model_copy(update={"title": "Read current plug-in preset"}),
)
async def plugins_get_current_preset(
    target: Annotated[
        PluginTarget,
        Field(description="Explicit mixer effect or global channel-generator target."),
    ],
) -> PluginCurrentPreset:
    """Read FL's current preset name and an index only when it is unique."""
    return await _performance_read("get_plugin_current_preset", target=target)


@mcp.tool(
    name="plugins_inspect_pad_map",
    annotations=READ_ONLY.model_copy(update={"title": "Inspect a plug-in pad map"}),
)
async def plugins_inspect_pad_map(
    target: Annotated[
        PluginTarget,
        Field(description="Explicit mixer effect or global channel-generator target."),
    ],
) -> PluginPadMap:
    """Read generic pad, MIDI-note, color, empty, and mute observations."""
    return await _performance_read("inspect_plugin_pad_map", target=target)


@mcp.tool(
    name="fl_select_plugin_preset",
    annotations=MUTATING.model_copy(update={"title": "Select an exact plug-in preset"}),
)
async def fl_select_plugin_preset(
    target: Annotated[
        PluginTarget,
        Field(description="Explicit mixer effect or global channel-generator target."),
    ],
    preset_name: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=256, description="Exact reported preset name."),
    ] = None,
    preset_index: Annotated[
        int | None,
        Field(default=None, ge=0, le=999_999, description="Exact reported preset index."),
    ] = None,
    expected_current: Annotated[
        ExpectedPluginPresetState | None,
        Field(default=None, description="Optional stale-read guard for the current preset."),
    ] = None,
    session_fingerprint: SessionFingerprintArg = None,
    target_fingerprint: Annotated[
        str | None,
        Field(default=None, pattern=r"^[0-9a-f]{64}$", description="Observed target-identity guard."),
    ] = None,
    max_navigation_steps: Annotated[
        int,
        Field(default=64, ge=0, le=256, description="Bound on next/previous navigation."),
    ] = 64,
    settle_tick_limit: Annotated[
        int,
        Field(default=1, ge=1, le=8, description="Later idle ticks allowed for plug-in settling."),
    ] = 1,
) -> VerifiedPluginPresetSelection:
    """Navigate to an exact preset and require later-idle-tick identity readback."""
    return await _performance_write(
        "select_plugin_preset",
        target=target,
        preset_name=preset_name,
        preset_index=preset_index,
        expected_current=expected_current,
        session_fingerprint=session_fingerprint,
        target_fingerprint=target_fingerprint,
        max_navigation_steps=max_navigation_steps,
        settle_tick_limit=settle_tick_limit,
    )


@mcp.tool(
    name="fl_list_channels",
    annotations=READ_ONLY.model_copy(update={"title": "List Channel Rack channels"}),
)
async def fl_list_channels() -> ChannelList:
    """List globally addressed channels, mix state, routing, and generator identity."""
    return await _performance_read("list_channels")


@mcp.tool(
    name="fl_list_patterns",
    annotations=READ_ONLY.model_copy(update={"title": "List project patterns"}),
)
async def fl_list_patterns() -> PatternList:
    """List pattern identity, length, current state, and empty/default status."""
    return await _performance_read("list_patterns")


@mcp.tool(
    name="fl_find_empty_pattern",
    annotations=READ_ONLY.model_copy(update={"title": "Find an empty pattern"}),
)
async def fl_find_empty_pattern(
    start_pattern_number: Annotated[
        int,
        Field(description="First pattern number to inspect.", ge=1, le=MAX_PATTERN_NUMBER),
    ] = 1,
) -> EmptyPatternSearch:
    """Find the first default-empty pattern without changing the current pattern."""
    return await _performance_read(
        "find_empty_pattern", start_pattern_number=start_pattern_number
    )


@mcp.tool(
    name="fl_select_pattern",
    annotations=MUTATING.model_copy(update={"title": "Select a current pattern"}),
)
async def fl_select_pattern(
    pattern_number: Annotated[
        int,
        Field(description="Pattern number to make current.", ge=1, le=MAX_PATTERN_NUMBER),
    ],
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedPatternSelectionState | None,
        Field(default=None, description="Optional expected current pattern guard."),
    ] = None,
) -> VerifiedPatternSelectionWrite:
    """Select one pattern and verify FL's current-pattern getter."""
    return await _performance_write(
        "select_pattern",
        pattern_number=pattern_number,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_pattern_identity",
    annotations=MUTATING.model_copy(update={"title": "Name or color a pattern"}),
)
async def fl_set_pattern_identity(
    pattern_number: Annotated[
        int,
        Field(description="Pattern number to edit.", ge=1, le=MAX_PATTERN_NUMBER),
    ],
    name: Annotated[
        str | None,
        Field(default=None, description="Absolute pattern name.", max_length=64),
    ] = None,
    color: Annotated[
        int | None,
        Field(
            default=None,
            description="Absolute unsigned FL color word.",
            ge=0,
            le=0xFFFFFFFF,
        ),
    ] = None,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedPatternIdentityState | None,
        Field(default=None, description="Optional expected pattern name/color."),
    ] = None,
) -> VerifiedPatternIdentityWrite:
    """Set pattern name and/or color with per-field later-tick proof."""
    return await _performance_write(
        "set_pattern_identity",
        pattern_number=pattern_number,
        name=name,
        color=color,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_pattern_length",
    annotations=MUTATING.model_copy(update={"title": "Set pattern length"}),
)
async def fl_set_pattern_length(
    pattern_number: Annotated[
        int,
        Field(description="Pattern number to edit.", ge=1, le=MAX_PATTERN_NUMBER),
    ],
    length_beats: Annotated[
        int,
        Field(
            description="Absolute pattern length in beats.",
            ge=1,
            le=MAX_PATTERN_LENGTH_BEATS,
        ),
    ],
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedPatternLengthState | None,
        Field(default=None, description="Optional expected current length."),
    ] = None,
) -> VerifiedPatternLengthWrite:
    """Set pattern length using Image-Line's API 39+ getter/setter pair."""
    return await _performance_write(
        "set_pattern_length",
        pattern_number=pattern_number,
        length_beats=length_beats,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_list_playlist_tracks",
    annotations=READ_ONLY.model_copy(update={"title": "List Playlist tracks"}),
)
async def fl_list_playlist_tracks() -> PlaylistTrackList:
    """List every one-based Playlist track and its controllable state."""
    return await _performance_read("list_playlist_tracks")


@mcp.tool(
    name="fl_set_playlist_track_identity",
    annotations=MUTATING.model_copy(update={"title": "Name or color a Playlist track"}),
)
async def fl_set_playlist_track_identity(
    track_index: Annotated[
        int,
        Field(description="One-based Playlist track index.", ge=1),
    ],
    name: Annotated[
        str | None,
        Field(default=None, description="Absolute Playlist track name.", max_length=64),
    ] = None,
    color: Annotated[
        int | None,
        Field(
            default=None,
            description="Absolute unsigned FL color word.",
            ge=0,
            le=0xFFFFFFFF,
        ),
    ] = None,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedPlaylistTrackIdentityState | None,
        Field(default=None, description="Optional expected track name/color."),
    ] = None,
) -> VerifiedPlaylistTrackIdentityWrite:
    """Set Playlist name and/or color with per-field later-tick proof."""
    return await _performance_write(
        "set_playlist_track_identity",
        track_index=track_index,
        name=name,
        color=color,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_playlist_track_state",
    annotations=MUTATING.model_copy(update={"title": "Set Playlist track states"}),
)
async def fl_set_playlist_track_state(
    track_index: Annotated[
        int,
        Field(description="One-based Playlist track index.", ge=1),
    ],
    muted: Annotated[
        bool | None,
        Field(default=None, description="Absolute mute state."),
    ] = None,
    soloed: Annotated[
        bool | None,
        Field(default=None, description="Absolute solo state."),
    ] = None,
    selected: Annotated[
        bool | None,
        Field(default=None, description="Absolute selection state."),
    ] = None,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedPlaylistTrackState | None,
        Field(default=None, description="Optional expected mute/solo/selection state."),
    ] = None,
) -> VerifiedPlaylistTrackStateWrite:
    """Set Playlist states; toggle-only selection is dispatched at most once."""
    return await _performance_write(
        "set_playlist_track_state",
        track_index=track_index,
        muted=muted,
        soloed=soloed,
        selected=selected,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_channel_mix",
    annotations=MUTATING.model_copy(update={"title": "Set Channel Rack mix fields"}),
)
async def fl_set_channel_mix(
    channel_index: Annotated[int, Field(description="Global channel index.", ge=0)],
    volume_normalized: Annotated[
        float | None, Field(default=None, description="Absolute channel volume.", ge=0.0, le=1.0)
    ] = None,
    pan: Annotated[
        float | None, Field(default=None, description="Absolute channel pan.", ge=-1.0, le=1.0)
    ] = None,
    muted: Annotated[
        bool | None, Field(default=None, description="Absolute channel mute state.")
    ] = None,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedChannelMixState | None,
        Field(default=None, description="Optional guarded channel fingerprint and/or mix fields."),
    ] = None,
) -> VerifiedChannelMixWrite:
    """Set channel volume, pan, and/or mute with per-field readback proof."""
    return await _performance_write(
        "set_channel_mix", channel_index=channel_index,
        volume_normalized=volume_normalized, pan=pan, muted=muted,
        session_fingerprint=session_fingerprint, expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_channel_solo",
    annotations=MUTATING.model_copy(update={"title": "Solo or unsolo a channel"}),
)
async def fl_set_channel_solo(
    channel_index: Annotated[int, Field(description="Global channel index.", ge=0)],
    soloed: Annotated[
        bool,
        Field(description="Absolute wanted solo state; never a toggle."),
    ],
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedChannelSoloState | None,
        Field(default=None, description="Optional fingerprint and/or solo-state guard."),
    ] = None,
) -> VerifiedChannelSoloWrite:
    """Set a global channel solo state and verify it on a later FL tick."""
    return await _performance_write(
        "set_channel_solo",
        channel_index=channel_index,
        soloed=soloed,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_channel_pitch",
    annotations=MUTATING.model_copy(update={"title": "Set Channel Rack pitch"}),
)
async def fl_set_channel_pitch(
    channel_index: Annotated[int, Field(description="Global channel index.", ge=0)],
    pitch_normalized: Annotated[
        float,
        Field(
            description="Absolute FL channel pitch from -1.0 to 1.0.",
            ge=-1.0,
            le=1.0,
        ),
    ],
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedChannelPitchState | None,
        Field(default=None, description="Optional fingerprint and/or pitch guard."),
    ] = None,
) -> VerifiedChannelPitchWrite:
    """Set normalized channel pitch and report normalized/semitone readback."""
    return await _performance_write(
        "set_channel_pitch",
        channel_index=channel_index,
        pitch_normalized=pitch_normalized,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_select_channel",
    annotations=MUTATING.model_copy(update={"title": "Select one Channel Rack channel"}),
)
async def fl_select_channel(
    channel_index: Annotated[int, Field(description="Global channel index.", ge=0)],
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedChannelSelectionState | None,
        Field(default=None, description="Optional exact selected-channel list guard."),
    ] = None,
) -> VerifiedChannelSelectionWrite:
    """Select one global channel exclusively and verify the complete selection."""
    return await _performance_write(
        "select_channel",
        channel_index=channel_index,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_set_channel_identity",
    annotations=MUTATING.model_copy(update={"title": "Set Channel Rack identity fields"}),
)
async def fl_set_channel_identity(
    channel_index: Annotated[int, Field(description="Global channel index.", ge=0)],
    name: Annotated[
        str | None, Field(default=None, description="Absolute channel name.", max_length=64)
    ] = None,
    color: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Absolute FL 0x--BBGGRR color word. FL owns the high byte, so "
                "write verification compares the low 24 color bits."
            ),
            ge=0,
            le=0xFFFFFFFF,
        ),
    ] = None,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedChannelIdentityState | None,
        Field(default=None, description="Optional guarded channel fingerprint/name/color."),
    ] = None,
) -> VerifiedChannelIdentityWrite:
    """Set a channel's name and/or color with per-field readback proof."""
    return await _performance_write(
        "set_channel_identity", channel_index=channel_index, name=name, color=color,
        session_fingerprint=session_fingerprint, expected_before=expected_before,
    )


@mcp.tool(
    name="fl_route_channel_to_mixer",
    annotations=MUTATING.model_copy(update={"title": "Route a channel to the mixer"}),
)
async def fl_route_channel_to_mixer(
    channel_index: Annotated[int, Field(description="Global channel index.", ge=0)],
    mixer_destination: Annotated[
        int, Field(description="Absolute mixer destination; -1 leaves it unassigned.", ge=-1)
    ],
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedChannelRouteState | None,
        Field(default=None, description="Optional guarded channel fingerprint/destination."),
    ] = None,
) -> VerifiedChannelRouteWrite:
    """Set one global channel's absolute mixer destination and verify it."""
    return await _performance_write(
        "route_channel_to_mixer", channel_index=channel_index,
        mixer_destination=mixer_destination, session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="fl_get_step_sequence",
    annotations=READ_ONLY.model_copy(update={"title": "Read a step sequence"}),
)
async def fl_get_step_sequence(
    pattern_number: Annotated[int, Field(description="Explicit current pattern number.", ge=1)],
    channel_index: Annotated[int, Field(description="Global channel index.", ge=0)],
) -> StepSequenceObservation:
    """Read an explicit current-pattern/channel grid and its conflict digest."""
    return await _performance_read(
        "get_step_sequence", pattern_number=pattern_number, channel_index=channel_index,
    )


@mcp.tool(
    name="fl_set_step_sequence",
    annotations=MUTATING.model_copy(update={"title": "Set absolute step cells"}),
)
async def fl_set_step_sequence(
    pattern_number: Annotated[int, Field(description="Explicit current pattern number.", ge=1)],
    channel_index: Annotated[int, Field(description="Global channel index.", ge=0)],
    expected_digest: Annotated[
        str, Field(description="Required digest from fl_get_step_sequence.", pattern=r"^[0-9a-f]{64}$")
    ],
    updates: Annotated[
        list[StepCellUpdate], Field(
            description="Unique absolute cell states.",
            min_length=1,
            max_length=MAX_VERIFIED_STEP_COUNT,
        )
    ],
    session_fingerprint: SessionFingerprintArg = None,
) -> VerifiedStepSequenceWrite:
    """Set absolute current-pattern cells only if the observed grid digest still matches."""
    return await _performance_write(
        "set_step_sequence", pattern_number=pattern_number,
        channel_index=channel_index, expected_digest=expected_digest,
        updates=updates, session_fingerprint=session_fingerprint,
    )


@mcp.tool(
    name="fl_trigger_note",
    annotations=EPHEMERAL_MUTATING,
)
async def fl_trigger_note(
    channel_index: Annotated[int, Field(description="Global channel index.", ge=0)],
    note: Annotated[int, Field(description="MIDI note number.", ge=0, le=127)],
    velocity: Annotated[int, Field(description="MIDI note-on velocity.", ge=1, le=127)],
    duration_ms: Annotated[
        int, Field(description="Bounded audition duration in milliseconds.", ge=20, le=5000)
    ] = 250,
    midi_channel: Annotated[
        int, Field(description="FL MIDI channel override; -1 uses the default.", ge=-1, le=15)
    ] = -1,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedChannelTargetState | None,
        Field(default=None, description="Optional observation-scoped channel fingerprint guard."),
    ] = None,
) -> LiveNoteDispatch:
    """Audition a global channel with a bounded note-on/off dispatch receipt."""
    return await _performance_write(
        "trigger_note", channel_index=channel_index, note=note, velocity=velocity,
        duration_ms=duration_ms, midi_channel=midi_channel,
        session_fingerprint=session_fingerprint, expected_before=expected_before,
    )


@mcp.tool(
    name="audio_analyze_file",
    annotations=LOCAL_READ_ONLY.model_copy(
        update={"title": "Measure a rendered audio file"}
    ),
)
async def audio_analyze_file(
    path: Annotated[
        str,
        Field(description="Absolute path to an existing audio file bounced from FL Studio."),
    ],
    include_pitch: Annotated[
        bool,
        Field(
            description="Also run the monophonic pitch tracker; useful for a lead vocal stem, unreliable for a full mix."
        ),
    ] = False,
    max_seconds: Annotated[
        float | None,
        Field(
            default=None,
            description="Optional shorter analysis bound in seconds; the default reads up to 600.",
            ge=1.0,
            le=600.0,
        ),
    ] = None,
) -> AudioFileAnalysis:
    """Measure level, spectrum, dynamics, stereo, and optional pitch of one render."""
    return await _measure(
        analyze_audio_file, path, include_pitch=include_pitch, max_seconds=max_seconds
    )


@mcp.tool(
    name="audio_compare_files",
    annotations=LOCAL_READ_ONLY.model_copy(
        update={"title": "Compare two rendered files"}
    ),
)
async def audio_compare_files(
    reference_path: Annotated[
        str,
        Field(description="Absolute path to the reference render."),
    ],
    candidate_path: Annotated[
        str,
        Field(description="Absolute path to the candidate render; reported as the target."),
    ],
    max_seconds: Annotated[
        float | None,
        Field(
            default=None,
            description="Optional shorter analysis bound in seconds; the default reads up to 600.",
            ge=1.0,
            le=600.0,
        ),
    ] = None,
) -> AudioComparison:
    """Measure band deltas over the aligned, loudness-matched common overlap."""
    return await _measure(
        compare_audio_files, reference_path, candidate_path, max_seconds=max_seconds
    )


@mcp.tool(
    name="audio_analyze_masking",
    annotations=LOCAL_READ_ONLY.model_copy(
        update={"title": "Measure vocal/instrument overlap"}
    ),
)
async def audio_analyze_masking(
    vocal_path: Annotated[
        str,
        Field(description="Absolute path to the vocal render."),
    ],
    instrument_path: Annotated[
        str,
        Field(
            description="Absolute path to the instrumental render of the same section, rendered sample-synchronously."
        ),
    ],
    max_seconds: Annotated[
        float | None,
        Field(
            default=None,
            description="Optional shorter analysis bound in seconds; the default reads up to 600.",
            ge=1.0,
            le=600.0,
        ),
    ] = None,
) -> MaskingAnalysis:
    """Measure per-band spectral overlap and vocal-minus-instrument level margins."""
    return await _measure(
        analyze_masking, vocal_path, instrument_path, max_seconds=max_seconds
    )


@mcp.tool(
    name="audio_find_recent_bounces",
    annotations=LOCAL_READ_ONLY_VOLATILE,
)
async def audio_find_recent_bounces(
    limit: Annotated[
        int,
        Field(description="Maximum number of files to return, newest first.", ge=1, le=200),
    ] = 20,
) -> RecentAudioListing:
    """List the newest audio files in FL Studio's Rendered, Audio, and Projects folders."""
    return await _measure(find_recent_audio_files, limit)


# ---------------------------------------------------------------------------
# Production-copilot workflows
# ---------------------------------------------------------------------------


@mcp.tool(
    name="mix_doctor",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Diagnose a bounced mix"}),
)
async def mix_doctor(
    candidate_path: Annotated[str, Field(description="Absolute path to the candidate bounce.")],
    target: Annotated[MixTarget, Field(description="Technical review target.")] = "balanced",
    reference_path: Annotated[str | None, Field(default=None, description="Optional absolute reference path.")] = None,
    vocal_path: Annotated[str | None, Field(default=None, description="Optional synchronized vocal stem.")] = None,
    instrumental_path: Annotated[str | None, Field(default=None, description="Optional synchronized instrumental stem.")] = None,
    max_seconds: Annotated[float | None, Field(default=None, ge=1.0, le=600.0)] = None,
) -> MixDoctorReport:
    """Diagnose a real bounce with explicit policy thresholds and no mutation."""
    return await _mix(
        run_mix_doctor,
        candidate_path,
        target=target,
        reference_path=reference_path,
        vocal_path=vocal_path,
        instrumental_path=instrumental_path,
        max_seconds=max_seconds,
    )


@mcp.tool(
    name="mix_reference_recommendations",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Recommend from a real reference comparison"}),
)
async def mix_reference_recommendations(
    reference_path: Annotated[str, Field(description="Absolute path to the reference audio.")],
    candidate_path: Annotated[str, Field(description="Absolute path to the candidate bounce.")],
    max_seconds: Annotated[float | None, Field(default=None, ge=1.0, le=600.0)] = None,
) -> ReferenceRecommendationReport:
    """Return bounded tonal review ranges only when alignment/readiness passes."""
    return await _mix(
        reference_recommendations,
        reference_path,
        candidate_path,
        max_seconds=max_seconds,
    )


@mcp.tool(
    name="mix_masking_recommendations",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Recommend masking remediation"}),
)
async def mix_masking_recommendations(
    vocal_path: Annotated[str, Field(description="Absolute synchronized vocal stem path.")],
    instrumental_path: Annotated[str, Field(description="Absolute synchronized instrumental stem path.")],
    max_seconds: Annotated[float | None, Field(default=None, ge=1.0, le=600.0)] = None,
) -> MaskingRecommendationReport:
    """Recommend bounded dynamic remediation from sample-synchronous stems."""
    return await _mix(
        masking_recommendations,
        vocal_path,
        instrumental_path,
        max_seconds=max_seconds,
    )


@mcp.tool(
    name="mix_start_peak_watch",
    annotations=WORKFLOW_STATE.model_copy(update={"title": "Start persistent mixer peak watch"}),
)
async def mix_start_peak_watch(
    duration_seconds: Annotated[float, Field(description="Watch duration.", ge=1.0, le=3600.0)] = 180.0,
    interval_ms: Annotated[int, Field(description="Sampling interval.", ge=250, le=5000)] = 500,
    only_used: Annotated[bool, Field(description="Retain active/custom-named tracks plus Master.")] = True,
    max_tracks: Annotated[int, Field(description="Maximum mixer indices scanned.", ge=1, le=126)] = 126,
) -> PeakWatchReport:
    """Start a process-persistent sampled peak watch and return its first frame."""
    return await _mix(
        PEAK_WATCHES.start,
        duration_seconds=duration_seconds,
        interval_ms=interval_ms,
        only_used=only_used,
        max_tracks=max_tracks,
    )


@mcp.tool(
    name="mix_get_peak_watch",
    annotations=READ_ONLY.model_copy(update={"title": "Read a mixer peak watch"}),
)
async def mix_get_peak_watch(
    watch_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
) -> PeakWatchReport:
    """Read cumulative sampled peaks without stopping the watch."""
    return await _mix(PEAK_WATCHES.get, watch_id)


@mcp.tool(
    name="mix_stop_peak_watch",
    annotations=WORKFLOW_STATE.model_copy(update={"title": "Stop a mixer peak watch"}),
)
async def mix_stop_peak_watch(
    watch_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
) -> PeakWatchReport:
    """Stop one process-local watch and return its final aggregate."""
    return await _mix(PEAK_WATCHES.stop, watch_id)


@mcp.tool(
    name="mix_create_gain_stage_plan",
    annotations=WORKFLOW_STATE.model_copy(update={"title": "Create a gain-staging plan"}),
)
async def mix_create_gain_stage_plan(
    watch_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
    target_peak_dbfs: Annotated[float, Field(ge=-30.0, le=-3.0)] = -12.0,
    max_adjustment_db: Annotated[float, Field(ge=0.5, le=24.0)] = 12.0,
    allow_master: Annotated[bool, Field(description="Explicitly include Master in the proposed plan.")] = False,
) -> GainStagePlanResult:
    """Create, but do not apply, dB-fader changes from a peak watch."""
    return await _mix(
        create_gain_stage_plan,
        watch_id,
        target_peak_dbfs=target_peak_dbfs,
        max_adjustment_db=max_adjustment_db,
        allow_master=allow_master,
    )


@mcp.tool(
    name="mix_list_plugin_profiles",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "List plug-in and recipe profiles"}),
)
async def mix_list_plugin_profiles(
    category: Annotated[str | None, Field(default=None, description="Optional exact profile category.")] = None,
) -> PluginProfileCatalog:
    """List bundled parameter-role adapters and processing recipes."""
    return await _mix(list_plugin_profiles, category)


@mcp.tool(
    name="mix_inspect_plugin_compatibility",
    annotations=READ_ONLY.model_copy(update={"title": "Match loaded plug-ins to profiles"}),
)
async def mix_inspect_plugin_compatibility(
    only_used: Annotated[bool, Field(description="Filter conservatively to used mixer tracks.")] = True,
) -> PluginCompatibilityReport:
    """Report which loaded effects have known parameter-role adapters."""
    return await _mix(inspect_plugin_compatibility, only_used=only_used)


@mcp.tool(
    name="mix_resolve_processing_intent",
    annotations=READ_ONLY.model_copy(update={"title": "Resolve a processing intent"}),
)
async def mix_resolve_processing_intent(
    intent: Annotated[ProcessingIntent, Field(description="Outcome-level processing intent.")],
    track_index: Annotated[int, Field(description="Mixer track to inspect.", ge=0)],
    strength: Annotated[float, Field(description="Reviewed artistic strength hint.", ge=0.0, le=1.0)] = 0.5,
) -> ProcessingIntentResolution:
    """Map an intent to loaded profiled controls without applying settings."""
    return await _mix(
        resolve_processing_intent,
        intent,
        track_index=track_index,
        strength=strength,
    )


@mcp.tool(
    name="mix_create_plan",
    annotations=WORKFLOW_STATE.model_copy(update={"title": "Create a reviewable mix plan"}),
)
async def mix_create_plan(
    title: Annotated[str, Field(min_length=1, max_length=128)],
    operations: Annotated[list[BatchOperation], Field(min_length=1, max_length=32)],
    rationale: Annotated[list[str] | None, Field(default=None, max_length=32)] = None,
    session_fingerprint: SessionFingerprintArg = None,
) -> MixPlan:
    """Store a session-bound closed-union plan; no project value changes."""
    return await _mix(
        MIX_PLANS.create,
        title=title,
        operations=operations,
        rationale=rationale,
        session_fingerprint=session_fingerprint,
    )


@mcp.tool(
    name="mix_get_plan",
    annotations=READ_ONLY.model_copy(update={"title": "Read a mix plan"}),
)
async def mix_get_plan(
    plan_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
) -> MixPlan:
    """Read one process-local reviewable plan."""
    return await _mix(MIX_PLANS.get, plan_id)


@mcp.tool(
    name="mix_apply_plan",
    annotations=MUTATING.model_copy(update={"title": "Apply a reviewed mix plan"}),
)
async def mix_apply_plan(
    plan_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
    stop_on_unverified: Annotated[bool, Field(description="Skip remaining plan items after unverified proof.")] = True,
) -> MixPlanApplication:
    """Apply a plan once through the verified batch kernel."""
    return await _mix(
        MIX_PLANS.apply,
        plan_id,
        stop_on_unverified=stop_on_unverified,
    )


@mcp.tool(
    name="mix_finish_assessment",
    annotations=READ_ONLY.model_copy(update={"title": "Assess the finish-mix workflow"}),
)
async def mix_finish_assessment(
    candidate_path: Annotated[str, Field(description="Absolute candidate bounce path.")],
    target: Annotated[MixTarget, Field(description="Technical review target.")] = "balanced",
    reference_path: Annotated[str | None, Field(default=None)] = None,
    vocal_path: Annotated[str | None, Field(default=None)] = None,
    instrumental_path: Annotated[str | None, Field(default=None)] = None,
    max_seconds: Annotated[float | None, Field(default=None, ge=1.0, le=600.0)] = None,
) -> FinishMixAssessment:
    """Run the end-to-end read-only finish assessment and stop at user export."""
    return await _mix(
        finish_mix_assessment,
        candidate_path,
        target=target,
        reference_path=reference_path,
        vocal_path=vocal_path,
        instrumental_path=instrumental_path,
        max_seconds=max_seconds,
    )


# ---------------------------------------------------------------------------
# Sound Selection: live inventory, deterministic palettes, and local history
# ---------------------------------------------------------------------------


@mcp.tool(
    name="sound_selection_inventory",
    annotations=READ_ONLY.model_copy(update={"title": "Inventory available sounds"}),
)
async def sound_selection_inventory(
    request: Annotated[
        SoundSelectionRequest | None,
        Field(default=None, description="Optional structured direction used to include the relevant target pool."),
    ] = None,
    only_used: Annotated[
        bool,
        Field(description="Limit mixer observations to used tracks; generators remain included."),
    ] = False,
    include_effects: Annotated[
        bool | None,
        Field(default=None, description="Include loaded effects; defaults from the request."),
    ] = None,
    preset_start: Annotated[int, Field(ge=0, description="First preset index per target.")] = 0,
    preset_limit: Annotated[
        int,
        Field(ge=1, le=256, description="Maximum preset names per loaded target."),
    ] = 64,
    include_current: Annotated[bool, Field(description="Read current preset identities.")] = True,
    include_empty_names: Annotated[bool, Field(description="Retain blank preset names.")] = False,
    include_pad_maps: Annotated[bool, Field(description="Inspect generic generator pad maps.")] = True,
    include_atlas: Annotated[bool, Field(description="Enrich loaded observations with local Plugin Atlas metadata.")] = True,
) -> SoundInventory:
    """Read a compact loaded sound pool; Atlas-only products remain recommendations."""
    return await _mix(
        get_sound_selection_inventory,
        request,
        only_used=only_used,
        include_effects=include_effects,
        preset_start=preset_start,
        preset_limit=preset_limit,
        include_current=include_current,
        include_empty_names=include_empty_names,
        include_pad_maps=include_pad_maps,
        include_atlas=include_atlas,
    )


@mcp.tool(
    name="sound_selection_plan",
    annotations=READ_ONLY.model_copy(update={"title": "Plan a coherent sound palette"}),
)
async def sound_selection_plan(
    request: Annotated[
        SoundSelectionRequest,
        Field(description="Task-scoped roles, direction, preferences, exclusions, continuity, and history policy."),
    ],
) -> SoundPalettePlan:
    """Choose deterministic loaded-target assignments without changing FL or history."""
    return await _mix(plan_sound_selection, request)


@mcp.tool(
    name="sound_selection_get",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Get a Sound Palette"}),
)
async def sound_selection_get(
    palette_id: Annotated[
        str,
        Field(min_length=1, max_length=128, description="Process-local palette identifier."),
    ],
) -> SoundPaletteLookup:
    """Look up one process-local palette without treating expiry as a server error."""
    return await _mix(get_sound_selection, palette_id)


@mcp.tool(
    name="sound_selection_create_variation",
    annotations=READ_ONLY.model_copy(update={"title": "Plan a Sound Palette variation"}),
)
async def sound_selection_create_variation(
    palette_id: Annotated[str, Field(min_length=1, max_length=128)],
    request: Annotated[
        SoundSelectionRequest,
        Field(description="Section-specific direction; anchors remain preserved by default."),
    ],
    section: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=128, description="Section receiving the delta."),
    ] = None,
    replace_roles: Annotated[
        tuple[str, ...],
        Field(default=(), max_length=128, description="Roles explicitly allowed to replace."),
    ] = (),
) -> SoundPaletteVariationPlan:
    """Return a section delta instead of replacing the existing palette."""
    return await _mix(
        create_sound_selection_variation,
        palette_id,
        request,
        section,
        replace_roles,
    )


@mcp.tool(
    name="sound_selection_apply",
    annotations=MUTATING.model_copy(update={"title": "Apply a Sound Palette"}),
)
async def sound_selection_apply(
    palette: Annotated[
        SoundPalettePlan | SoundPaletteVariationPlan | str,
        Field(
            description=(
                "A validated palette plan, section variation, or its "
                "process-local palette ID."
            )
        ),
    ],
    session_fingerprint: RequiredSoundSelectionSessionFingerprintArg,
    authorized_to_modify: Annotated[
        bool,
        Field(description="True only when the current user explicitly authorized these project changes."),
    ],
    role_ids: Annotated[
        tuple[str, ...],
        Field(default=(), max_length=128, description="Optional bounded subset of palette roles."),
    ] = (),
    max_navigation_steps: Annotated[int, Field(default=64, ge=0, le=256)] = 64,
    settle_tick_limit: Annotated[int, Field(default=1, ge=1, le=8)] = 1,
    persist_history: Annotated[
        bool | None,
        Field(default=None, description="Override this palette's task-scoped history policy."),
    ] = None,
) -> SoundSelectionApplyResult:
    """Apply exact presets in deterministic order and stop on unknown or unverified outcomes."""
    return await _mix(
        apply_sound_selection,
        palette,
        session_fingerprint,
        authorized_to_modify,
        role_ids=role_ids,
        max_navigation_steps=max_navigation_steps,
        settle_tick_limit=settle_tick_limit,
        persist_history=persist_history,
    )


@mcp.tool(
    name="sound_selection_record_feedback",
    annotations=WORKFLOW_STATE.model_copy(
        update={
            "title": "Record explicit Sound Selection feedback",
            "open_world_hint": False,
        }
    ),
)
async def sound_selection_record_feedback(
    request: Annotated[
        SoundFeedbackRequest,
        Field(description="Explicit accepted, rejected, or neutral palette feedback."),
    ],
) -> SoundFeedbackResult:
    """Update bounded local ranking feedback; silence is never inferred."""
    return await _mix(record_sound_selection_feedback, request)


@mcp.tool(
    name="sound_selection_history_status",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Inspect Sound Selection history"}),
)
async def sound_selection_history_status() -> SoundHistoryStatus:
    """Report the local history path, health, schema, and bounded record counts."""
    return await _mix(get_sound_selection_history_status)


@mcp.tool(
    name="sound_selection_history_reset",
    annotations=WORKFLOW_STATE.model_copy(
        update={
            "title": "Reset Sound Selection history",
            "destructive_hint": True,
            "idempotent_hint": True,
            "open_world_hint": False,
        }
    ),
)
async def sound_selection_history_reset(
    confirm: Annotated[
        bool,
        Field(description="Must be true after the user explicitly requested local history deletion."),
    ],
) -> SoundHistoryResetResult:
    """Explicitly remove bounded local selection history; project state is unchanged."""
    return await _mix(reset_sound_selection_history, confirm)


# ---------------------------------------------------------------------------
# Task-scoped Production Runs
# ---------------------------------------------------------------------------


@mcp.tool(
    name="postfader_validate_run",
    annotations=READ_ONLY.model_copy(update={"title": "Validate a Production Run"}),
)
async def postfader_validate_run(
    request: Annotated[
        ProductionRunRequest,
        Field(
            description=(
                "Task-scoped objective, scope, preservation rules, allowed changes, "
                "completion target, and authorization inferred from the user's request."
            )
        ),
    ],
    plan: Annotated[
        ProductionRunPlan,
        Field(description="Closed ordered Production Run plan to validate without mutation."),
    ],
) -> ProductionRunValidation:
    """Validate a bounded Production Run and current capabilities without changing FL."""
    return await _mix(validate_production_run, request, plan)


@mcp.tool(
    name="postfader_execute_run",
    annotations=MUTATING.model_copy(update={"title": "Execute a Production Run"}),
)
async def postfader_execute_run(
    request: Annotated[
        ProductionRunRequest,
        Field(
            description=(
                "Task-scoped request. Mutating plans require authorized_to_modify=true "
                "because the present user explicitly asked to change the project."
            )
        ),
    ],
    plan: Annotated[
        ProductionRunPlan,
        Field(description="Closed bounded plan to validate completely, then execute in order."),
    ],
) -> ProductionRunResult:
    """Create and execute one task-scoped run until its plan completes or blocks."""
    return await _mix(PRODUCTION_RUNS.execute, request, plan)


@mcp.tool(
    name="postfader_get_run",
    annotations=READ_ONLY.model_copy(update={"title": "Get a Production Run"}),
)
async def postfader_get_run(
    run_id: Annotated[
        str,
        Field(
            pattern=r"^[0-9a-f]{32}$",
            description="Process-local Production Run identifier.",
        ),
    ],
) -> ProductionRunLookup:
    """Read current process-local run state and its truthful operation receipts."""
    return await _mix(PRODUCTION_RUNS.get, run_id)


@mcp.tool(
    name="postfader_continue_run",
    annotations=MUTATING.model_copy(update={"title": "Continue a Production Run"}),
)
async def postfader_continue_run(
    run_id: Annotated[
        str,
        Field(
            pattern=r"^[0-9a-f]{32}$",
            description="Process-local Production Run identifier.",
        ),
    ],
    delta: Annotated[
        ProductionRunDelta,
        Field(
            description=(
                "Append operations or replace only the unexecuted remainder; an optional "
                "updated request may narrow scope or change task policy."
            )
        ),
    ],
) -> ProductionRunResult:
    """Continue or replace only a run's unexecuted remainder after a follow-up."""
    return await _mix(PRODUCTION_RUNS.continue_run, run_id, delta)


@mcp.tool(
    name="postfader_stop_run",
    annotations=WORKFLOW_STATE.model_copy(update={"title": "Stop a Production Run"}),
)
async def postfader_stop_run(
    run_id: Annotated[
        str,
        Field(
            pattern=r"^[0-9a-f]{32}$",
            description="Process-local Production Run identifier.",
        ),
    ],
) -> ProductionRunResult:
    """Stop future run operations without undoing completed project changes."""
    return await _mix(PRODUCTION_RUNS.stop, run_id)


# ---------------------------------------------------------------------------
# Creative pack: composition, Piano Roll, MIDI, analysis, and arrangement
# ---------------------------------------------------------------------------


@mcp.tool(
    name="piano_roll_bridge",
    annotations=WORKFLOW_STATE.model_copy(update={"title": "Prepare or inspect the Piano Roll bridge"}),
)
async def piano_roll_bridge(
    action: Annotated[
        Literal["status", "prepare", "confirm"],
        Field(description="Status only, write the bootstrap script, or confirm the user ran it once."),
    ] = "status",
    confirm_user_ran_script: Annotated[
        bool,
        Field(description="Required only for action='confirm', after the user manually ran Postfader Apply."),
    ] = False,
) -> PianoRollBridgeStatus:
    """Manage the one-time-per-process arm for FL's separate Piano Roll script runtime."""
    return await _mix(
        PIANO_ROLL.bridge_action,
        action,
        confirm_user_ran_script=confirm_user_ran_script,
    )


@mcp.tool(
    name="piano_roll_write_notes",
    annotations=MUTATING.model_copy(update={"title": "Write notes through FL's Piano Roll script"}),
)
async def piano_roll_write_notes(
    notes: Annotated[
        list[CreativeNote],
        Field(min_length=1, max_length=2048, description="Bounded notes in quarter-note beat units."),
    ],
    channel_index: Annotated[int, Field(ge=0, description="Global Channel Rack target index.")],
    pattern_number: Annotated[int, Field(ge=1, le=999, description="Pattern to select before triggering the script.")],
    mode: Annotated[
        Literal["append", "replace"],
        Field(description="Append to the score or clear all notes before adding these notes."),
    ] = "append",
    auto_trigger: Annotated[
        bool,
        Field(description="Send FL's run-last-Piano-Roll-script shortcut after verified target selection."),
    ] = True,
) -> PianoRollDispatch:
    """Generate a typed Piano Roll script, select its target, and report hotkey dispatch honestly."""
    return await _mix(
        write_piano_roll_notes,
        notes,
        channel_index=channel_index,
        pattern_number=pattern_number,
        mode=mode,
        auto_trigger=auto_trigger,
    )


@mcp.tool(
    name="piano_roll_transform",
    annotations=MUTATING.model_copy(update={"title": "Transform live Piano Roll notes"}),
)
async def piano_roll_transform(
    request: Annotated[PianoRollTransform, Field(description="Closed transform request read by FL's live score script.")],
    channel_index: Annotated[int, Field(ge=0, description="Global Channel Rack target index.")],
    pattern_number: Annotated[int, Field(ge=1, le=999, description="Pattern to select before triggering the script.")],
    auto_trigger: Annotated[bool, Field(description="Automatically send the run-last-script shortcut.")] = True,
) -> PianoRollDispatch:
    """Quantize, transpose, humanize, duplicate, delete, or clear selected/all notes."""
    return await _mix(
        transform_piano_roll,
        request,
        channel_index=channel_index,
        pattern_number=pattern_number,
        auto_trigger=auto_trigger,
    )


@mcp.tool(
    name="compose_chord_progression",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Compose a voice-led chord progression"}),
)
async def compose_chord_progression(
    progression: Annotated[list[str], Field(min_length=1, max_length=64, description="Roman chords such as I, vi, IV, V, or V7.")],
    root: Annotated[str, Field(description="Tonic note name, for example C, F#, or Bb.")] = "C",
    collection: Annotated[str, Field(description="Bundled scale/mode/raga name, or custom.")] = "major",
    custom_intervals: Annotated[list[int] | None, Field(default=None, max_length=12)] = None,
    beats_per_chord: Annotated[float, Field(ge=0.125, le=32.0)] = 4.0,
    octave: Annotated[int, Field(ge=0, le=8)] = 4,
    voicing: Annotated[Literal["close", "open", "drop2"], Field(description="Deterministic voicing strategy.")] = "close",
    velocity: Annotated[float, Field(ge=0.0, le=1.0)] = 0.78,
    tempo_bpm: Annotated[float, Field(ge=10.0, le=522.0)] = 120.0,
) -> NoteSequence:
    """Generate voice-led triads/sevenths without changing FL."""
    return await _mix(
        generate_chord_progression,
        progression,
        root=root,
        collection=collection,
        custom_intervals=custom_intervals,
        beats_per_chord=beats_per_chord,
        octave=octave,
        voicing=voicing,
        velocity=velocity,
        tempo_bpm=tempo_bpm,
    )


@mcp.tool(
    name="compose_melody",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Compose a deterministic melody"}),
)
async def compose_melody(
    root: Annotated[str, Field(description="Tonic note name.")] = "C",
    collection: Annotated[str, Field(description="Bundled scale/mode/raga name, or custom.")] = "major",
    custom_intervals: Annotated[list[int] | None, Field(default=None, max_length=12)] = None,
    bars: Annotated[int, Field(ge=1, le=64)] = 4,
    beats_per_bar: Annotated[int, Field(ge=1, le=16)] = 4,
    density: Annotated[float, Field(ge=0.05, le=1.0)] = 0.65,
    register_low: Annotated[int, Field(ge=0, le=130)] = 60,
    register_high: Annotated[int, Field(ge=1, le=131)] = 84,
    contour: Annotated[Literal["balanced", "rising", "falling", "arch", "wave"], Field()] = "balanced",
    seed: Annotated[int, Field(description="Deterministic variation seed.")] = 0,
    tempo_bpm: Annotated[float, Field(ge=10.0, le=522.0)] = 120.0,
) -> NoteSequence:
    """Generate a bounded scale-aware melody without changing FL."""
    return await _mix(
        generate_melody,
        root=root,
        collection=collection,
        custom_intervals=custom_intervals,
        bars=bars,
        beats_per_bar=beats_per_bar,
        density=density,
        register_low=register_low,
        register_high=register_high,
        contour=contour,
        seed=seed,
        tempo_bpm=tempo_bpm,
    )


@mcp.tool(
    name="compose_bassline",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Compose a progression-aware bassline"}),
)
async def compose_bassline(
    progression: Annotated[list[str], Field(min_length=1, max_length=64)],
    root: Annotated[str, Field(description="Tonic note name.")] = "C",
    collection: Annotated[str, Field(description="Bundled scale/mode/raga name, or custom.")] = "major",
    custom_intervals: Annotated[list[int] | None, Field(default=None, max_length=12)] = None,
    beats_per_chord: Annotated[float, Field(ge=0.5, le=32.0)] = 4.0,
    octave: Annotated[int, Field(ge=0, le=7)] = 2,
    style: Annotated[Literal["roots", "eighths", "octaves", "walking"], Field()] = "roots",
    seed: Annotated[int, Field(description="Deterministic variation seed.")] = 0,
    tempo_bpm: Annotated[float, Field(ge=10.0, le=522.0)] = 120.0,
) -> NoteSequence:
    """Generate a bounded bass part from Roman harmony without changing FL."""
    return await _mix(
        generate_bassline,
        progression,
        root=root,
        collection=collection,
        custom_intervals=custom_intervals,
        beats_per_chord=beats_per_chord,
        octave=octave,
        style=style,
        seed=seed,
        tempo_bpm=tempo_bpm,
    )


@mcp.tool(
    name="compose_drums",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Compose a deterministic drum part"}),
)
async def compose_drums(
    style: Annotated[Literal["house", "hiphop", "trap", "pop", "dnb"], Field()] = "house",
    bars: Annotated[int, Field(ge=1, le=64)] = 4,
    beats_per_bar: Annotated[int, Field(ge=1, le=16)] = 4,
    seed: Annotated[int, Field(description="Deterministic variation seed.")] = 0,
    swing: Annotated[float, Field(ge=0.0, le=0.49, description="Delay offbeat eighths in beats.")] = 0.0,
    tempo_bpm: Annotated[float, Field(ge=10.0, le=522.0)] = 120.0,
    drum_map: Annotated[
        DrumPadMap | None,
        Field(default=None, description="Selected semantic drum map; omit for explicit General MIDI fallback."),
    ] = None,
) -> NoteSequence:
    """Generate mapped kick/snare/hat patterns without changing FL."""
    return await _mix(
        generate_drums,
        style=style,
        bars=bars,
        beats_per_bar=beats_per_bar,
        seed=seed,
        swing=swing,
        tempo_bpm=tempo_bpm,
        drum_map=drum_map,
    )


@mcp.tool(
    name="midi_export_type1",
    annotations=FILE_MUTATING.model_copy(update={"title": "Export and verify a Type-1 MIDI file"}),
)
async def midi_export_type1(
    path: Annotated[str, Field(description="Absolute .mid/.midi output path whose parent already exists.")],
    tracks: Annotated[list[MidiTrackSpec], Field(min_length=1, max_length=32)],
    tempo_bpm: Annotated[float, Field(ge=10.0, le=522.0)] = 120.0,
    ppq: Annotated[int, Field(ge=24, le=9600)] = 480,
    numerator: Annotated[int, Field(ge=1, le=32)] = 4,
    denominator: Annotated[Literal[1, 2, 4, 8, 16, 32], Field()] = 4,
    overwrite: Annotated[bool, Field(description="Explicitly allow atomic replacement of an existing file.")] = False,
) -> MidiExportReceipt:
    """Write a standard Type-1 file, reopen it, parse it, and verify its digest/events."""
    return await _mix(
        export_type1_midi,
        path,
        tracks,
        tempo_bpm=tempo_bpm,
        ppq=ppq,
        numerator=numerator,
        denominator=denominator,
        overwrite=overwrite,
    )


@mcp.tool(
    name="audio_estimate_tempo_and_key",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Estimate tempo and musical key"}),
)
async def audio_estimate_tempo_and_key(
    path: Annotated[str, Field(description="Absolute path to a decoded audio file.")],
    max_seconds: Annotated[float | None, Field(default=300.0, ge=1.0, le=600.0)] = 300.0,
) -> AudioMusicAnalysis:
    """Estimate periodic tempo and global major/minor key with ranked ambiguity."""
    return await _measure(analyze_tempo_and_key, path, max_seconds=max_seconds)


@mcp.tool(
    name="audio_transcribe_melody",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Transcribe a monophonic melody"}),
)
async def audio_transcribe_melody(
    path: Annotated[str, Field(description="Absolute path to one isolated pitched source.")],
    tempo_bpm: Annotated[float | None, Field(default=None, ge=10.0, le=522.0)] = None,
    fmin_hz: Annotated[float, Field(ge=30.0, le=3999.0)] = 55.0,
    fmax_hz: Annotated[float, Field(ge=31.0, le=4000.0)] = 1760.0,
    minimum_note_seconds: Annotated[float, Field(ge=0.03, le=2.0)] = 0.08,
    quantize_grid_beats: Annotated[float | None, Field(default=0.25, ge=0.03125, le=4.0)] = 0.25,
    max_seconds: Annotated[float | None, Field(default=180.0, ge=1.0, le=300.0)] = 180.0,
) -> MelodyTranscription:
    """Extract a reviewable note sequence from monophonic audio; it does not mutate FL."""
    return await _measure(
        transcribe_monophonic,
        path,
        tempo_bpm=tempo_bpm,
        fmin_hz=fmin_hz,
        fmax_hz=fmax_hz,
        minimum_note_seconds=minimum_note_seconds,
        quantize_grid_beats=quantize_grid_beats,
        max_seconds=max_seconds,
    )


@mcp.tool(
    name="arrangement_prepare_pattern",
    annotations=MUTATING.model_copy(update={"title": "Prepare a verified empty pattern"}),
)
async def arrangement_prepare_pattern(
    name: Annotated[str, Field(min_length=1, max_length=64)],
    length_beats: Annotated[int, Field(ge=1, le=4096)] = 16,
    color: Annotated[int | None, Field(default=None, ge=0, le=0xFFFFFFFF)] = None,
    start_pattern_number: Annotated[int, Field(ge=1, le=999)] = 1,
) -> PatternPreparation:
    """Find an FL-reported empty pattern, select it, name/color it, and set length."""
    return await _mix(
        prepare_empty_pattern,
        name=name,
        length_beats=length_beats,
        color=color,
        start_pattern_number=start_pattern_number,
    )


@mcp.tool(
    name="arrangement_add_section_markers",
    annotations=MUTATING.model_copy(update={"title": "Add section markers to the arrangement"}),
)
async def arrangement_add_section_markers(
    markers: Annotated[list[SectionMarker], Field(min_length=1, max_length=32)],
) -> ArrangementMarkerReceipt:
    """Add bar/beat section markers; name readback is available, marker-time readback is not."""
    return await _mix(add_section_markers, markers)


@mcp.tool(
    name="automation_record_value",
    annotations=MUTATING.model_copy(update={"title": "Record one public REC-event automation value"}),
)
async def automation_record_value(
    target_kind: Annotated[Literal["mixer", "channel"], Field(description="Automation target namespace.")],
    target_index: Annotated[int, Field(ge=0)],
    property: Annotated[Literal["volume", "pan", "stereo_separation"], Field(description="Channel targets support volume/pan; mixer also supports stereo separation.")],
    value_normalized: Annotated[float, Field(ge=0.0, le=1.0)],
    allow_master: Annotated[bool, Field(description="Explicitly permit mixer target 0.")] = False,
    expected_before: Annotated[float | None, Field(default=None, ge=0.0, le=1.0)] = None,
) -> AutomationRecordReceipt:
    """Dispatch one REC_MIDIController value while playback and recording are active."""
    return await _mix(
        record_automation_value,
        target_kind=target_kind,
        target_index=target_index,
        property=property,
        value_normalized=value_normalized,
        allow_master=allow_master,
        expected_before=expected_before,
    )


USAGE = """\
PostFader - unofficial local MCP server for FL Studio 2026

Usage:
  fl-studio-mcp              Serve the Model Context Protocol over stdio.
  fl-studio-mcp --help       Show this message.
  fl-studio-mcp --version    Print the version.

This command speaks MCP on stdin/stdout and is meant to be launched by an MCP
client, not run interactively -- on its own it will appear to hang while it
waits for a client. Register it using absolute interpreter and checkout paths.
From a source checkout, generate a Codex command, Codex TOML, or Claude JSON:

  python scripts/generate_mcp_config.py --help

The generator keeps automatic local-file mode read-only by default. Select
--transport midi and provide --midi-port only after configuring the same exact
virtual endpoint in FL Studio. PostFader never installs a virtual MIDI driver.

Writes start off. Ask the connected AI client to enable write mode for the
current session; explicit user-present confirmation is required and FL Studio
does not need to restart.

Use postfader-doctor (or scripts/doctor.py from a checkout) for setup evidence.
The supervised acceptance harnesses and native Windows bootstrap live in the
source repository: https://github.com/synopsys0/postfader-fl-studio-mcp
"""


def main(argv: list[str] | None = None) -> int:
    """Entry point for the console command.

    Running with no arguments is the supported mode and starts the stdio
    server. Arguments are handled here only so that a person checking their
    install with --help or --version gets an answer instead of a process that
    silently waits forever for MCP traffic on a terminal that will never send
    any.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        mcp.run(transport="stdio")
        return 0
    if len(args) == 1 and args[0] in {"-h", "--help", "help"}:
        print(USAGE, end="")
        return 0
    if len(args) == 1 and args[0] in {"-V", "--version", "version"}:
        print(__version__)
        return 0
    print(USAGE, end="", file=sys.stderr)
    print(
        "\nerror: unrecognised argument(s): %s" % " ".join(args),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
