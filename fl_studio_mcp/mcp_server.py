"""MCP entry point for FL Studio project control and production workflows.

Tools are named ``<area>_<verb>[_<object>]``. Each target has one read and one
setter; workflows plan without mutating and apply through one entry point.
Blocking bridge and audio work runs off the MCP event loop. Task authorization
flows through the workflow; typed receipts report applied, partial and unknown
outcomes. The bridge owns live capabilities and session/target checks.
"""

from __future__ import annotations

import sys
from typing import Annotated, Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase
from mcp.types import ToolAnnotations
from pydantic import ConfigDict, Field, WithJsonSchema

from . import __version__
from .advisory import (
    AudioFileAnalysis,
    RecentAudioListing,
    analyze_audio_file,
    find_recent_audio_files,
)
from .contracts import (
    CapabilitiesReport,
    MixerTrackInspection,
    MixerTrackList,
    ProjectSummary,
    SelectedRangeObservation,
    TransportState,
    WriteModeChange,
)
from .creation_review.mcp import (
    ReviewApplyRevisionRequest,
    ReviewAttachAssetsRequest,
    ReviewCompareRequest,
    ReviewDeleteResult,
    ReviewDeliveryExportRequest,
    ReviewDeliveryExportResult,
    ReviewEvaluateRequest,
    ReviewPlanRevisionRequest,
    ReviewSessionLookup,
    delivery_export_manifest as export_review_delivery_manifest,
    delivery_manifest as build_review_delivery_manifest,
    review_apply_revision as apply_creation_revision,
    review_attach_assets as attach_creation_review_assets,
    review_compare as compare_creation_revision,
    review_delete as delete_creation_review,
    review_evaluate as evaluate_creation_review,
    review_export_handoff as build_review_export_handoff,
    review_get as get_creation_review,
    review_plan_revision as plan_creation_revision,
    review_record_feedback as record_creation_review_feedback,
    review_start as start_creation_review,
    review_stop as stop_creation_review,
)
from .creation_review.models import (
    CreationEvaluationReport,
    CreationFeedback,
    DeliveryManifest,
    ExportHandoff,
    ReviewSession,
    ReviewSessionRequest,
    RevisionComparison,
    RevisionPass,
    RevisionPlan,
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
from .edits import (
    ExpectedChannelFields,
    ExpectedMixerTrackFields,
    ExpectedPatternFields,
    ExpectedPlaylistTrackFields,
    ExpectedTransportFields,
    MAX_MIXER_SENDS,
    ChannelEditResult,
    MixerEqBandChange,
    MixerSendChange,
    MixerTrackEditResult,
    PatternEditResult,
    PlaylistTrackEditResult,
    TransportEditResult,
    set_channel,
    set_mixer_track,
    set_pattern,
    set_playlist_track,
    set_transport,
)
from .music_analysis import (
    AudioMusicAnalysis,
    MelodyTranscription,
    analyze_tempo_and_key,
    transcribe_monophonic,
)
from .piano_roll import PianoRollNoteSnapshot, read_piano_roll_notes
from .plugin_loading import (
    PluginLoadRequest, PluginLoadResult, PluginMenuInventory,
    list_available_plugins, load_plugin,
)
from .saved_project_render import (
    SavedProjectRenderJob,
    SavedProjectRenderRequest,
    get_saved_project_render_jobs,
    shutdown_saved_project_render_jobs,
)
from .readonly_inspector import ReadOnlyInspector
from .mixing import (
    PEAK_WATCHES,
    GainStagePlan,
    MaskingRecommendationReport,
    MixDoctorReport,
    MixTarget,
    PeakWatchReport,
    ReferenceRecommendationReport,
    create_gain_stage_plan,
    masking_recommendations,
    reference_recommendations,
    run_mix_doctor,
)
from .performance import TrackBController, TrackBInspector
from .creation_pipeline.processing import ProcessingPlan, ProcessingRequest
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
    ApplyProcessingPlanOperation,
    PRODUCTION_RUNS,
    ProductionRunCheck,
    ProductionRunDelta,
    ProductionRunLookup,
    ProductionRunPlan,
    ProductionRunRequest,
    ProductionRunResult,
    ProductionRunSummary,
    ProductionScope,
    check_production_run,
    list_production_runs,
    plan_live_processing,
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
    ExpectedChannelTargetState,
    ExpectedPluginParameterState,
    ExpectedPluginPresetState,
    ExpectedProjectHistoryState,
    LiveNoteDispatch,
    MAX_VERIFIED_STEP_COUNT,
    MAX_PATTERN_LENGTH_BEATS,
    MAX_PATTERN_NUMBER,
    PluginTarget,
    PatternList,
    PluginPadMap,
    PluginPresetPage,
    PlaylistTrackList,
    ProjectHistoryObservation,
    StepCellUpdate,
    StepSequenceObservation,
    TargetedLoadedPluginInventory,
    TargetedPluginParameterScan,
    VerifiedPluginPresetSelection,
    VerifiedProjectHistoryMove,
    VerifiedStepSequenceWrite,
    VerifiedTargetedPluginDisplayWrite,
    VerifiedTargetedPluginOptionWrite,
    VerifiedTargetedPluginParameterWrite,
)
from .tool_schemas import (
    MAX_DESCRIBED_OPERATIONS,
    OperationCatalog,
    ProductionOperationName,
    compact_schema,
    describe_operations,
)
from .verified_writer import WriteModeManager
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
            "Optional session_fingerprint from a recent read. The call refuses after a "
            "bridge reload or a reported project load. This is a concurrency guard, "
            "not authentication or a durable project identity."
        ),
    ),
]

# Workflow applies (palettes, processing plans) always require a live session
# token. Keep the generic setter guard optional, but make these high-level
# mutations fail at MCP argument validation rather than reaching the service
# with ``None``.
RequiredSessionFingerprintArg = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{32}$",
        description=(
            "Required session_fingerprint from a recent live read. The call refuses "
            "after a bridge reload or a reported project load. This is a concurrency "
            "guard, not authentication or a durable project identity."
        ),
    ),
]

StopOnUnverifiedArg = Annotated[
    bool,
    Field(
        description=(
            "Skip the remaining writes after the first write whose readback did not "
            "verify. An unknown outcome always stops the sequence."
        )
    ),
]

MaxSecondsArg = Annotated[
    float | None,
    Field(
        default=None,
        ge=1.0,
        le=600.0,
        description="Optional shorter analysis bound in seconds; the default reads up to 600.",
    ),
]

RunIdArg = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{32}$",
        description="Production Run identifier from run_execute or run_list.",
    ),
]

ReviewSessionIdArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
        description="Review Session identifier from review_start.",
    ),
]

PluginTargetArg = Annotated[
    PluginTarget,
    Field(
        description=(
            'Loaded plug-in: {"kind": "mixer_effect", "track_index", "slot_index"} '
            '(Master also needs "allow_master": true) or {"kind": '
            '"channel_generator", "channel_index"}. Read targets with plugin_list_loaded.'
        )
    ),
]


INSTRUCTIONS = """\
PostFader is an FL Studio production connector. Tool names are
<area>_<verb>[_<object>]. Read before you change: project_get_summary,
mixer_list_tracks, channel_list, pattern_list, playlist_list_tracks and
plugin_list_loaded give focused context; prefer the fl:// resources for initial
context when the client exposes them. The connected AI makes creative
decisions; PostFader executes and reports FL state.

A request to create, edit, continue, finish, arrange, remix or mix authorizes
supported changes within that task. Preserve the user's stated constraints and
accepted material. Analysis and ideas alone do not authorize project changes.

Direct edits: each target has one setter that changes any of its fields in one
call (mixer_set_track, channel_set, pattern_set, playlist_set_track,
transport_set, plugin_set_parameter). Use project_apply_edits for ordered
edits across several targets. Enable session_set_write_mode(enabled=true,
confirm_user_present=true) once first; the user's request to edit is the
confirmation. Every write is read back on a later FL tick: check verified and
each receipt, and describe partial results accurately. Writes are ordered and
non-atomic; earlier verified writes remain if a later one fails. Never replay an
ambiguous write automatically. project_step_history undoes or redoes;
PostFader never saves the project.

Multi-stage work: use run_execute. It performs readiness checks and enables
writes internally once; do not enable write mode separately, repeat
authorization inside the run, or validate first unless the user wants a plan
reviewed (run_validate). Plan operations are listed by name only: call
run_describe_operations with the operations you will use to get their fields.
Proceed through warnings and supported alternatives within scope. Stop for a
missing capability, changed target, unmet setup dependency or unknown mutation
outcome, and report the blocked operation and a usable next step. run_continue
resumes after a follow-up (delta.mode="resume" continues a saved plan) and
keeps completed receipts; run_stop prevents future operations. Runs are saved
locally across MCP restarts; run_list rediscovers them. Interrupted writes with
unknown outcomes are never replayed.

Sounds: sound_plan_palette then sound_apply_palette (or the palette operations
inside a run). Apply takes the session_fingerprint from a live read. Keep user
preferences, exclusions, locked roles and continuity in the request. Select the
palette before writing notes; pass base_palette_id to plan a later section's
variation. Atlas supplies offline product knowledge; only a live inventory
establishes a loaded instrument or effect. Read presets and non-GM drum pad maps
(plugin_list_presets, plugin_get_pad_map) before addressing them. On macOS,
plugin_list_available and plugin_load add missing instruments or effects from
FL's Add menu; match exact menu names and use write mode like any setter.

Notes: piano_roll_read_notes inspects existing notes before composing around
them. compose_* tools generate parts offline; compose_export_midi writes a
checked Type-1 MIDI file. Piano Roll writing needs one setup: piano_roll_setup
action=prepare, the user runs Postfader Apply once in FL, then action=confirm.
Missing receipts mean an unknown outcome, not permission to retry. Step edits
use the latest digest from channel_get_steps.

Effects: processing_plan then processing_apply for focused loaded-effect work,
or the plan_processing/apply_processing_plan operations inside a run. Prefer
displayed values and exact options when the control's meaning is known;
unprofiled controls need runtime evidence. Target Master only when requested.

Audio: tools measure caller-selected exported files; FL's live output is not
available. render_start_job renders a saved .flp to a new WAV job;
render_get_job reports output and status. Review a draft with review_start,
review_attach_assets, review_evaluate, review_plan_revision and
review_apply_revision in the same authorized task; compare matching exports
afterwards. Measurements support decisions; they do not establish approval.

Current bridge limits: plug-in removal/reordering, per-slot bypass and wet
control, Playlist clip editing, live audio capture, live-project rendering,
project save and playback speed are unavailable. Explain these at the relevant
step and use supported handoffs. Requires FL Studio 26.1.3 build 5336 or newer
and MIDI scripting API 44 or newer.
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
    title="Manage a workflow",
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


class PostFaderServer(MCPServer):
    """Serve compact schemas; validation still uses the full models.

    Clients commonly load every tool definition into the model's context, so
    the listing advertises the reduced input and output schemas from
    ``tool_schemas``. Calls are validated by the SDK against each tool's
    complete argument model, and results are built from the complete models.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._advertised_tools: dict[str, dict[str, object]] = {}

    async def list_tools(self):
        tools = await super().list_tools()
        listed = []
        for tool in tools:
            update = self._advertised_tools.get(tool.name)
            if update is None:
                update = {"input_schema": compact_schema(tool.input_schema)}
                if tool.output_schema is not None:
                    update["output_schema"] = compact_schema(tool.output_schema)
                self._advertised_tools[tool.name] = update
            listed.append(tool.model_copy(update=update))
        return listed


mcp = PostFaderServer(
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


async def _edit(function, **arguments):
    """Apply one per-target setter off the event loop.

    Refusals before the first write (writes disabled, an incompatible bridge, a
    stale session, an argument this layer rejected) reach the agent as errors.
    After the preflight, every attempted write is reported in the result,
    including unverified and unknown outcomes.
    """

    def invoke():
        return function(**arguments)

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
    """Read the same typed capability report exposed by session_get_capabilities."""

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


# ---------------------------------------------------------------------------
# session: bridge capabilities and write mode
# ---------------------------------------------------------------------------


@mcp.tool(
    name="session_get_capabilities",
    annotations=READ_ONLY.model_copy(update={"title": "Get bridge capabilities"}),
)
async def session_get_capabilities() -> CapabilitiesReport:
    """Report which FL Studio integration paths work in this bridge session.

    Returns the connection (FL version, protocol, write mode, and the current
    session_fingerprint) and, for each capability, whether it is direct,
    partial, unavailable, or unvalidated. Read-only. Call it when a tool is
    refused or before planning work that depends on a capability; use
    project_get_summary for the open project's contents."""
    return await _run("capabilities")


@mcp.tool(
    name="session_set_write_mode",
    annotations=WRITE_MODE_CONTROL,
)
async def session_set_write_mode(
    enabled: Annotated[
        bool,
        Field(
            description=(
                "Absolute session write state. True unlocks the verified setters; "
                "false locks them again."
            )
        ),
    ],
    confirm_user_present: Annotated[
        bool,
        Field(
            description=(
                "True asserts that the user asked for project changes in this task. "
                "Required to enable; not needed to disable."
            )
        ),
    ] = False,
) -> WriteModeChange:
    """Turn write mode on or off for this bridge session without restarting FL.

    Setters (mixer_set_track, channel_set, plugin_set_parameter and the other
    tools that change the project directly) refuse until write mode is on.
    run_execute, run_continue, processing_apply, sound_apply_palette, and
    review_apply_revision enable it for their own work. Enabling requires
    confirm_user_present=true; the user's request to edit is that confirmation,
    so do not ask separately. The result is verified with a fresh handshake.
    Changes no project value, applies only to this session, and is never
    saved. Disable it when the task's edits are done."""
    return await _set_write_mode(
        enabled=enabled,
        confirm_user_present=confirm_user_present,
    )


# ---------------------------------------------------------------------------
# project: summary, undo history, and multi-target edits
# ---------------------------------------------------------------------------


@mcp.tool(
    name="project_get_summary",
    annotations=READ_ONLY.model_copy(update={"title": "Get project summary"}),
)
async def project_get_summary() -> ProjectSummary:
    """Read the open project's metadata, counts, dirty state, and transport.

    Returns the title, tempo, PPQ, mixer/channel/pattern/Playlist counts, the
    undo position, dirty state, the connection with its session_fingerprint,
    and the transport (playing, recording, loop mode, position, metronome,
    precount, time signature). Read-only. Use it for overall context and the
    transport state before transport_set; use the area list tools
    (mixer_list_tracks, channel_list, pattern_list, playlist_list_tracks) for
    target details."""
    return await _run("project_summary")


@mcp.tool(
    name="project_get_history",
    annotations=READ_ONLY.model_copy(update={"title": "Read project undo history"}),
)
async def project_get_history() -> ProjectHistoryObservation:
    """Read FL's undo history: position, count, next undo hint, and dirty state.

    Read-only. Call it before project_step_history to see what an undo or redo
    would move to, and pass its position, count, or dirty flag as that call's
    expected_before guard."""
    return await _performance_read("project_history")


@mcp.tool(
    name="project_step_history",
    annotations=MUTATING.model_copy(update={"title": "Undo or redo one step"}),
)
async def project_step_history(
    direction: Annotated[
        Literal["undo", "redo"],
        Field(description="undo moves one step back in FL's history; redo moves one step forward."),
    ],
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedProjectHistoryState | None,
        Field(
            default=None,
            description="Optional history position, count, and/or dirty flag from project_get_history; refuse if changed.",
        ),
    ] = None,
) -> VerifiedProjectHistoryMove:
    """Undo or redo one step of FL Studio's project history and verify the move.

    Requires write mode (session_set_write_mode). Moves exactly one step and
    reads FL's history position back on a later tick; the receipt reports the
    position before and after. FL's history covers every project change, not
    only PostFader's, so read project_get_history first and guard with
    expected_before. Does not save the project."""
    return await _performance_write(
        direction,
        session_fingerprint=session_fingerprint,
        expected_before=expected_before,
    )


@mcp.tool(
    name="project_apply_edits",
    annotations=MUTATING.model_copy(update={"title": "Apply ordered edits across targets"}),
)
async def project_apply_edits(
    operations: Annotated[
        list[BatchOperation],
        Field(
            description=(
                "Ordered absolute writes. Each needs a unique operation_id and an "
                "operation that selects its fields (mixer_volume_db, channel_mix, "
                "plugin_parameter, pattern_length, tempo, and others); no two items "
                "may write the same field. Mixer index 0 needs allow_master=true."
            ),
            min_length=1,
            max_length=MAX_BATCH_OPERATIONS,
            examples=[[
                {"operation_id": "level-1", "operation": "mixer_volume_db", "track_index": 1, "volume_db": -6.0},
                {"operation_id": "pan-2", "operation": "mixer_pan", "track_index": 2, "pan": 0.2},
            ]],
        ),
    ],
    stop_on_unverified: StopOnUnverifiedArg = True,
    session_fingerprint: SessionFingerprintArg = None,
) -> VerifiedBatchResult:
    """Apply ordered edits across several targets with one session check.

    Use it when one change spans different mixer tracks, channels, patterns,
    Playlist tracks, plug-in parameters, or the tempo, or to apply the
    operations from mixer_plan_gain_staging. For several fields of one target,
    the area setter (mixer_set_track, channel_set) is simpler; for multi-stage
    work, use run_execute. Requires write mode. Every attempted item gets its
    own later-tick receipt. The batch is non-atomic: earlier changes remain if a
    later item fails, and an unknown outcome stops it. Inspect each receipt and
    never replay an ambiguous batch. No rollback or project save is performed."""
    return await _apply_batch(
        operations=operations,
        stop_on_unverified=stop_on_unverified,
        session_fingerprint=session_fingerprint,
    )


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


@mcp.tool(
    name="transport_set",
    annotations=MUTATING.model_copy(update={"title": "Set transport and timing"}),
)
async def transport_set(
    stop: Annotated[
        bool,
        Field(description="Stop playback and rewind to the start, like FL's Stop button. Not combined with playing or position_normalized."),
    ] = False,
    playing: Annotated[
        bool | None,
        Field(default=None, description="Absolute playback state: true plays, false pauses in place."),
    ] = None,
    tempo_bpm: Annotated[
        float | None,
        Field(default=None, ge=10.0, le=522.0, description="Project tempo in BPM. FL needs playback stopped and recording off."),
    ] = None,
    time_signature_numerator: Annotated[
        int | None,
        Field(default=None, ge=1, le=32, description="Beats per bar. FL exposes no denominator."),
    ] = None,
    loop_mode: Annotated[
        Literal["pattern", "song"] | None,
        Field(default=None, description="Pattern or Song mode."),
    ] = None,
    metronome: Annotated[
        bool | None,
        Field(default=None, description="Absolute metronome state."),
    ] = None,
    precount: Annotated[
        bool | None,
        Field(default=None, description="Absolute count-in-before-recording state."),
    ] = None,
    recording: Annotated[
        bool | None,
        Field(default=None, description="Absolute transport record-arm state."),
    ] = None,
    position_normalized: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=1.0, description="Playhead position, 0 start to 1 end. FL needs playback stopped."),
    ] = None,
    position_tolerance: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=0.05, description="Maximum position readback error; defaults to 0.0001. Only with position_normalized."),
    ] = None,
    expected_before: Annotated[
        ExpectedTransportFields | None,
        Field(default=None, description="Optional current values from project_get_summary for the settings this call changes; refuse if any changed. Guards for unchanged settings are refused."),
    ] = None,
    session_fingerprint: SessionFingerprintArg = None,
    stop_on_unverified: StopOnUnverifiedArg = True,
) -> TransportEditResult:
    """Change playback, recording, tempo, loop mode, and other transport settings.

    Set only the fields to change; each is an absolute state, never a toggle.
    Requires write mode. Writes run in a fixed order so one call can stop,
    adjust, and restart: stop/pause and recording-off first; then tempo, time
    signature, loop mode, metronome, precount, and position; then recording-on;
    then playback last. Each write gets its own later-tick receipt, and
    verified is true only when all of them verified. Non-atomic: earlier writes
    remain if a later one fails. Read the current state with
    project_get_summary. Does not save the project."""
    return await _edit(
        set_transport,
        stop=stop,
        playing=playing,
        tempo_bpm=tempo_bpm,
        time_signature_numerator=time_signature_numerator,
        loop_mode=loop_mode,
        metronome=metronome,
        precount=precount,
        recording=recording,
        position_normalized=position_normalized,
        position_tolerance=position_tolerance,
        expected_before=expected_before,
        session_fingerprint=session_fingerprint,
        stop_on_unverified=stop_on_unverified,
    )


# ---------------------------------------------------------------------------
# mixer
# ---------------------------------------------------------------------------


@mcp.tool(
    name="mixer_list_tracks",
    annotations=READ_ONLY.model_copy(update={"title": "List mixer tracks"}),
)
async def mixer_list_tracks(
    only_used: Annotated[
        bool,
        Field(
            description="Apply a conservative used-track heuristic. False, the default, lists every track."
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
            description="Optional early page limit for a large mixer.",
            ge=1,
            le=500,
        ),
    ] = None,
) -> MixerTrackList:
    """List mixer tracks with names, levels, mute/solo state, and loaded effects.

    Read-only. Index 0 is Master. Use it to find track indices and effect slots
    before mixer_set_track or plugin tools; use mixer_get_track for one track's
    built-in EQ and outgoing sends, and mixer_start_peak_watch for levels over
    time."""
    return await _run(
        "list_mixer_tracks",
        only_used=only_used,
        include_peaks=include_peaks,
        max_tracks=max_tracks,
    )


@mcp.tool(
    name="mixer_get_track",
    annotations=READ_ONLY.model_copy(update={"title": "Get one mixer track"}),
)
async def mixer_get_track(
    track_index: Annotated[
        int,
        Field(description="Zero-based mixer index. Index 0 is always Master.", ge=0),
    ],
) -> MixerTrackInspection:
    """Read one mixer track's full state: level, pan, effects, EQ, and sends.

    Read-only. Returns the fader in normalized and dB form, pan, stereo
    separation, mute/solo/arm, name, color, effect slots, the built-in
    three-band EQ, and outgoing sends with their levels. Read it before
    mixer_set_track to supply current values as expected_before guards."""
    return await _run("inspect_mixer_track", track_index=track_index)


@mcp.tool(
    name="mixer_set_track",
    annotations=MUTATING.model_copy(update={"title": "Change a mixer track"}),
)
async def mixer_set_track(
    track_index: Annotated[
        int,
        Field(description="Zero-based mixer index. Index 0 is Master and needs allow_master=true.", ge=0),
    ],
    allow_master: Annotated[
        bool,
        Field(description="Deliberately target Master at index 0."),
    ] = False,
    name: Annotated[
        str | None,
        Field(default=None, max_length=64, description='Track name; "" restores FL\'s default name.'),
    ] = None,
    color: Annotated[
        int | None,
        Field(default=None, ge=0, le=0xFFFFFFFF, description="FL color word, unsigned 0xAABBGGRR; FL may change the high byte."),
    ] = None,
    volume_normalized: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=1.0, description="Fader position 0..1; 0.8 is 0 dB. Use volume_db for decibels."),
    ] = None,
    volume_db: Annotated[
        float | None,
        Field(default=None, ge=-60.0, le=6.0, description="Fader target in dB; found by searching FL's fader curve, which moves the fader while it searches."),
    ] = None,
    tolerance_db: Annotated[
        float | None,
        Field(default=None, ge=0.01, le=1.0, description="Maximum dB readback error; defaults to 0.1. Only with volume_db."),
    ] = None,
    pan: Annotated[
        float | None,
        Field(default=None, ge=-1.0, le=1.0, description="Pan from -1 hard left through 0 centre to 1 hard right."),
    ] = None,
    stereo_separation: Annotated[
        float | None,
        Field(default=None, ge=-1.0, le=1.0, description="FL stereo separation from -1 to 1."),
    ] = None,
    muted: Annotated[bool | None, Field(default=None, description="Absolute mute state.")] = None,
    soloed: Annotated[bool | None, Field(default=None, description="Absolute solo state.")] = None,
    armed: Annotated[bool | None, Field(default=None, description="Absolute recording-arm state.")] = None,
    eq: Annotated[
        tuple[MixerEqBandChange, ...],
        Field(default=(), max_length=3, description="Built-in three-band EQ changes, one entry per band."),
    ] = (),
    sends: Annotated[
        tuple[MixerSendChange, ...],
        Field(
            default=(),
            max_length=MAX_MIXER_SENDS,
            description="Sends to other tracks: create or remove a route with enabled, set its amount with level_normalized.",
        ),
    ] = (),
    select: Annotated[
        bool,
        Field(description="Also make this the active (selected) mixer track, after the other changes."),
    ] = False,
    expected_before: Annotated[
        ExpectedMixerTrackFields | None,
        Field(default=None, description="Optional current values from mixer_get_track for the fields this call changes; refuse if any changed. Guards for unchanged fields are refused."),
    ] = None,
    session_fingerprint: SessionFingerprintArg = None,
    stop_on_unverified: StopOnUnverifiedArg = True,
) -> MixerTrackEditResult:
    """Change any combination of one mixer track's settings in one call.

    Set only the fields to change: name, color, volume (normalized or dB), pan,
    stereo separation, mute, solo, record arm, built-in EQ bands, sends, and
    whether it is the active track. Requires write mode
    (session_set_write_mode). Writes run in that order, each read back on a
    later FL tick with its own receipt; verified is true only when every write
    verified. Non-atomic: earlier writes remain if a later one fails, and an
    unknown outcome stops the rest. Use plugin_set_parameter for effect
    parameters and project_apply_edits for several tracks. Does not save."""
    return await _edit(
        set_mixer_track,
        track_index=track_index,
        allow_master=allow_master,
        name=name,
        color=color,
        volume_normalized=volume_normalized,
        volume_db=volume_db,
        tolerance_db=tolerance_db,
        pan=pan,
        stereo_separation=stereo_separation,
        muted=muted,
        soloed=soloed,
        armed=armed,
        eq=eq,
        sends=sends,
        select=select,
        expected_before=expected_before,
        session_fingerprint=session_fingerprint,
        stop_on_unverified=stop_on_unverified,
    )


WatchIdArg = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{32}$", description="Peak watch identifier from mixer_start_peak_watch."),
]


@mcp.tool(
    name="mixer_start_peak_watch",
    annotations=WORKFLOW_STATE.model_copy(update={"title": "Start a mixer peak watch"}),
)
async def mixer_start_peak_watch(
    duration_seconds: Annotated[float, Field(description="How long to sample.", ge=1.0, le=3600.0)] = 180.0,
    interval_ms: Annotated[int, Field(description="Sampling interval in milliseconds.", ge=250, le=5000)] = 500,
    only_used: Annotated[bool, Field(description="Keep only active or custom-named tracks plus Master.")] = True,
    max_tracks: Annotated[int, Field(description="Maximum mixer indices scanned per sample.", ge=1, le=126)] = 126,
) -> PeakWatchReport:
    """Start sampling mixer peak meters while the user plays the song.

    Runs in the background in this MCP process and returns its first frame and
    a watch_id. Ask the user to play the section to measure, then read it with
    mixer_get_peak_watch or end it with mixer_stop_peak_watch. Changes no
    project state. Meter peaks are post-fader samples, not audio analysis; use
    the watch with mixer_plan_gain_staging to propose fader moves."""
    return await _mix(
        PEAK_WATCHES.start,
        duration_seconds=duration_seconds,
        interval_ms=interval_ms,
        only_used=only_used,
        max_tracks=max_tracks,
    )


@mcp.tool(
    name="mixer_get_peak_watch",
    annotations=READ_ONLY.model_copy(update={"title": "Read a mixer peak watch"}),
)
async def mixer_get_peak_watch(watch_id: WatchIdArg) -> PeakWatchReport:
    """Read a peak watch's status and cumulative per-track peaks so far.

    Read-only; the watch keeps running. Returns each track's maximum peak,
    clipping frame count, and last fader reading. Watches are process-local
    and end when the MCP server restarts."""
    return await _mix(PEAK_WATCHES.get, watch_id)


@mcp.tool(
    name="mixer_stop_peak_watch",
    annotations=WORKFLOW_STATE.model_copy(update={"title": "Stop a mixer peak watch"}),
)
async def mixer_stop_peak_watch(watch_id: WatchIdArg) -> PeakWatchReport:
    """Stop a peak watch early and return its final per-track aggregate.

    Changes no project state. A stopped watch can still be read and used by
    mixer_plan_gain_staging."""
    return await _mix(PEAK_WATCHES.stop, watch_id)


@mcp.tool(
    name="mixer_plan_gain_staging",
    annotations=READ_ONLY.model_copy(update={"title": "Plan gain staging from a peak watch"}),
)
async def mixer_plan_gain_staging(
    watch_id: WatchIdArg,
    target_peak_dbfs: Annotated[
        float, Field(ge=-30.0, le=-3.0, description="Peak level each track should reach, in dBFS.")
    ] = -12.0,
    max_adjustment_db: Annotated[
        float, Field(ge=0.5, le=24.0, description="Largest fader move proposed for any track, in dB.")
    ] = 12.0,
    allow_master: Annotated[bool, Field(description="Also propose a move for Master.")] = False,
) -> GainStagePlan:
    """Propose dB fader moves that bring each watched track to a target peak.

    Read-only: nothing is applied. Returns mixer_volume_db operations with
    expected_before guards from the watch, the watch's session_fingerprint, a
    rationale per track, and the tracks it skipped (muted, silent, Master, or
    already within 0.5 dB). Review them, then pass operations and
    session_fingerprint to project_apply_edits. Re-watch and re-bounce after
    applying."""
    return await _mix(
        create_gain_stage_plan,
        watch_id,
        target_peak_dbfs=target_peak_dbfs,
        max_adjustment_db=max_adjustment_db,
        allow_master=allow_master,
    )


# ---------------------------------------------------------------------------
# channel: Channel Rack channels, step sequencer, and audition
# ---------------------------------------------------------------------------


ChannelIndexArg = Annotated[
    int,
    Field(description="Global Channel Rack index from channel_list.", ge=0),
]


@mcp.tool(
    name="channel_list",
    annotations=READ_ONLY.model_copy(update={"title": "List Channel Rack channels"}),
)
async def channel_list() -> ChannelList:
    """List every Channel Rack channel with its mix, routing, and generator.

    Read-only. Each channel has a global channel_index, name, color, volume,
    pan, pitch, mute/solo/selection, mixer destination, generator identity, and
    a channel_fingerprint. Use the index for channel_set, channel_get_steps,
    piano_roll tools, and channel_generator plug-in targets; pass the
    fingerprint as channel_set's expected_before guard."""
    return await _performance_read("list_channels")


@mcp.tool(
    name="channel_set",
    annotations=MUTATING.model_copy(update={"title": "Change a Channel Rack channel"}),
)
async def channel_set(
    channel_index: ChannelIndexArg,
    name: Annotated[
        str | None,
        Field(default=None, max_length=64, description="Absolute channel name."),
    ] = None,
    color: Annotated[
        int | None,
        Field(default=None, ge=0, le=0xFFFFFFFF, description="FL 0x--BBGGRR color word; FL owns the high byte, so the low 24 bits are verified."),
    ] = None,
    mixer_destination: Annotated[
        int | None,
        Field(default=None, ge=-1, description="Mixer track this channel feeds; -1 leaves it unassigned."),
    ] = None,
    volume_normalized: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=1.0, description="Channel volume 0..1."),
    ] = None,
    pan: Annotated[
        float | None,
        Field(default=None, ge=-1.0, le=1.0, description="Channel pan from -1 left to 1 right."),
    ] = None,
    muted: Annotated[bool | None, Field(default=None, description="Absolute mute state.")] = None,
    soloed: Annotated[bool | None, Field(default=None, description="Absolute solo state.")] = None,
    pitch_normalized: Annotated[
        float | None,
        Field(default=None, ge=-1.0, le=1.0, description="Channel pitch knob from -1 to 1, not semitones; the receipt reports semitones."),
    ] = None,
    select: Annotated[
        bool,
        Field(description="Also make this the only selected channel, after the other changes."),
    ] = False,
    expected_before: Annotated[
        ExpectedChannelFields | None,
        Field(default=None, description="Optional channel_fingerprint and current values from channel_list for the fields this call changes; refuse if any changed. The fingerprint is checked up to the first name, color, or routing change."),
    ] = None,
    session_fingerprint: SessionFingerprintArg = None,
    stop_on_unverified: StopOnUnverifiedArg = True,
) -> ChannelEditResult:
    """Change any combination of one Channel Rack channel's settings in one call.

    Set only the fields to change: volume, pan, mute, solo, pitch, name and
    color, mixer routing, and exclusive selection. Requires write mode
    (session_set_write_mode). Writes run in that order, each read back on a
    later FL tick with its own receipt; verified is true only when every write
    verified. Non-atomic: earlier writes remain if a later one fails. Use
    plugin_set_parameter for the channel's instrument, channel_set_steps for
    its step grid, and piano_roll_write_notes for its notes. Does not save."""
    return await _edit(
        set_channel,
        channel_index=channel_index,
        name=name,
        color=color,
        mixer_destination=mixer_destination,
        volume_normalized=volume_normalized,
        pan=pan,
        muted=muted,
        soloed=soloed,
        pitch_normalized=pitch_normalized,
        select=select,
        expected_before=expected_before,
        session_fingerprint=session_fingerprint,
        stop_on_unverified=stop_on_unverified,
    )


@mcp.tool(
    name="channel_get_steps",
    annotations=READ_ONLY.model_copy(update={"title": "Read a channel's step grid"}),
)
async def channel_get_steps(
    pattern_number: Annotated[
        int,
        Field(description="Pattern to read; it must be FL's current pattern (see pattern_list).", ge=1),
    ],
    channel_index: ChannelIndexArg,
) -> StepSequenceObservation:
    """Read one channel's step-sequencer grid in the current pattern.

    Read-only. Returns each step's on/off state and a digest of the grid. Pass
    that digest as channel_set_steps' expected_digest so the edit refuses if
    the grid changed in between. The pattern must be the current one; select
    it first with pattern_set(select=true)."""
    return await _performance_read(
        "get_step_sequence", pattern_number=pattern_number, channel_index=channel_index,
    )


@mcp.tool(
    name="channel_set_steps",
    annotations=MUTATING.model_copy(update={"title": "Set step-sequencer cells"}),
)
async def channel_set_steps(
    pattern_number: Annotated[
        int,
        Field(description="FL's current pattern; the same value given to channel_get_steps.", ge=1),
    ],
    channel_index: ChannelIndexArg,
    expected_digest: Annotated[
        str,
        Field(description="Required digest from the latest channel_get_steps read of this grid.", pattern=r"^[0-9a-f]{64}$"),
    ],
    updates: Annotated[
        list[StepCellUpdate],
        Field(
            description="Absolute cell states, at most one per step index.",
            min_length=1,
            max_length=MAX_VERIFIED_STEP_COUNT,
        ),
    ],
    session_fingerprint: SessionFingerprintArg = None,
) -> VerifiedStepSequenceWrite:
    """Turn step-sequencer cells on or off for one channel in the current pattern.

    Requires write mode and a fresh channel_get_steps read: the edit refuses if
    the grid no longer matches expected_digest, so nothing is overwritten
    blindly. Each changed cell is read back on a later tick and reported. Use
    piano_roll_write_notes for pitched notes and lengths. Does not save."""
    return await _performance_write(
        "set_step_sequence", pattern_number=pattern_number,
        channel_index=channel_index, expected_digest=expected_digest,
        updates=updates, session_fingerprint=session_fingerprint,
    )


@mcp.tool(
    name="channel_play_note",
    annotations=EPHEMERAL_MUTATING,
)
async def channel_play_note(
    channel_index: ChannelIndexArg,
    note: Annotated[int, Field(description="MIDI note number; 60 is middle C.", ge=0, le=127)],
    velocity: Annotated[int, Field(description="MIDI note-on velocity.", ge=1, le=127)],
    duration_ms: Annotated[
        int, Field(description="How long the note sounds before its note-off, in milliseconds.", ge=20, le=5000)
    ] = 250,
    midi_channel: Annotated[
        int, Field(description="FL MIDI channel override; -1 uses the default.", ge=-1, le=15)
    ] = -1,
    session_fingerprint: SessionFingerprintArg = None,
    expected_before: Annotated[
        ExpectedChannelTargetState | None,
        Field(default=None, description="Optional channel_fingerprint from channel_list; refuse if the channel changed."),
    ] = None,
) -> LiveNoteDispatch:
    """Play one short note on a channel so the user can hear the sound.

    Sends a bounded note-on/note-off pair; nothing is recorded or saved and
    the project is unchanged. Requires write mode. The receipt confirms the
    dispatch only: FL has no getter for what was heard. To add notes to the
    project, use piano_roll_write_notes."""
    return await _performance_write(
        "trigger_note", channel_index=channel_index, note=note, velocity=velocity,
        duration_ms=duration_ms, midi_channel=midi_channel,
        session_fingerprint=session_fingerprint, expected_before=expected_before,
    )


# ---------------------------------------------------------------------------
# pattern
# ---------------------------------------------------------------------------


PatternNumberArg = Annotated[
    int,
    Field(description="One-based pattern number.", ge=1, le=MAX_PATTERN_NUMBER),
]


@mcp.tool(
    name="pattern_list",
    annotations=READ_ONLY.model_copy(update={"title": "List patterns"}),
)
async def pattern_list() -> PatternList:
    """List the project's patterns with name, color, length, and which is current.

    Read-only. Also reports whether each pattern is FL's default empty pattern.
    Use pattern numbers with pattern_set, channel_get_steps, and the piano_roll
    tools; use pattern_create to make a new named pattern."""
    return await _performance_read("list_patterns")


@mcp.tool(
    name="pattern_set",
    annotations=MUTATING.model_copy(update={"title": "Change a pattern"}),
)
async def pattern_set(
    pattern_number: PatternNumberArg,
    name: Annotated[
        str | None, Field(default=None, max_length=64, description="Absolute pattern name.")
    ] = None,
    color: Annotated[
        int | None,
        Field(default=None, ge=0, le=0xFFFFFFFF, description="Absolute unsigned FL color word."),
    ] = None,
    length_beats: Annotated[
        int | None,
        Field(default=None, ge=1, le=MAX_PATTERN_LENGTH_BEATS, description="Absolute pattern length in beats."),
    ] = None,
    select: Annotated[
        bool,
        Field(description="Also make this FL's current pattern, after the other changes."),
    ] = False,
    expected_before: Annotated[
        ExpectedPatternFields | None,
        Field(default=None, description="Optional current values from pattern_list for the fields this call changes; refuse if any changed."),
    ] = None,
    session_fingerprint: SessionFingerprintArg = None,
    stop_on_unverified: StopOnUnverifiedArg = True,
) -> PatternEditResult:
    """Rename, recolor, resize, or select one existing pattern in one call.

    Set only the fields to change. Requires write mode. Writes run in the order
    name/color, length, then selection, each read back on a later tick with its
    own receipt; verified is true only when all verified. Non-atomic. Use
    pattern_create for a new empty pattern. Does not save the project."""
    return await _edit(
        set_pattern,
        pattern_number=pattern_number,
        name=name,
        color=color,
        length_beats=length_beats,
        select=select,
        expected_before=expected_before,
        session_fingerprint=session_fingerprint,
        stop_on_unverified=stop_on_unverified,
    )


@mcp.tool(
    name="pattern_create",
    annotations=MUTATING.model_copy(update={"title": "Create a named empty pattern"}),
)
async def pattern_create(
    name: Annotated[str, Field(min_length=1, max_length=64, description="Name for the new pattern.")],
    length_beats: Annotated[
        int, Field(ge=1, le=MAX_PATTERN_LENGTH_BEATS, description="Pattern length in beats.")
    ] = 16,
    color: Annotated[
        int | None, Field(default=None, ge=0, le=0xFFFFFFFF, description="Optional FL color word.")
    ] = None,
    start_pattern_number: Annotated[
        int, Field(ge=1, le=MAX_PATTERN_NUMBER, description="First pattern number to search for an empty one.")
    ] = 1,
) -> PatternPreparation:
    """Create a pattern: find the first empty one, select it, name it, and size it.

    Requires write mode. Searches from start_pattern_number for a pattern FL
    reports as empty, then selects it and sets its name, optional color, and
    length, verifying each step. outcome names the first step that did not
    verify. Use pattern_set to change an existing pattern. Does not save."""
    return await _mix(
        prepare_empty_pattern,
        name=name,
        length_beats=length_beats,
        color=color,
        start_pattern_number=start_pattern_number,
    )


# ---------------------------------------------------------------------------
# playlist
# ---------------------------------------------------------------------------


@mcp.tool(
    name="playlist_list_tracks",
    annotations=READ_ONLY.model_copy(update={"title": "List Playlist tracks"}),
)
async def playlist_list_tracks() -> PlaylistTrackList:
    """List every Playlist track with its name, color, and mute/solo/selection.

    Read-only. Playlist track indices are one-based. Use them with
    playlist_set_track. Clips on the Playlist are not readable through FL's
    scripting API."""
    return await _performance_read("list_playlist_tracks")


@mcp.tool(
    name="playlist_set_track",
    annotations=MUTATING.model_copy(update={"title": "Change a Playlist track"}),
)
async def playlist_set_track(
    track_index: Annotated[int, Field(description="One-based Playlist track index.", ge=1)],
    name: Annotated[
        str | None, Field(default=None, max_length=64, description="Absolute track name.")
    ] = None,
    color: Annotated[
        int | None,
        Field(default=None, ge=0, le=0xFFFFFFFF, description="Absolute unsigned FL color word."),
    ] = None,
    muted: Annotated[bool | None, Field(default=None, description="Absolute mute state.")] = None,
    soloed: Annotated[bool | None, Field(default=None, description="Absolute solo state.")] = None,
    selected: Annotated[
        bool | None,
        Field(default=None, description="Absolute selection state; FL's toggle is sent at most once."),
    ] = None,
    expected_before: Annotated[
        ExpectedPlaylistTrackFields | None,
        Field(default=None, description="Optional current values from playlist_list_tracks for the fields this call changes; refuse if any changed."),
    ] = None,
    session_fingerprint: SessionFingerprintArg = None,
    stop_on_unverified: StopOnUnverifiedArg = True,
) -> PlaylistTrackEditResult:
    """Rename, recolor, mute, solo, or select one Playlist track in one call.

    Set only the fields to change. Requires write mode. Name and color are
    written first, then mute/solo/selection, each read back on a later tick
    with its own receipt; verified is true only when both verified. Playlist
    clips cannot be created or moved through FL's scripting API. Does not
    save the project."""
    return await _edit(
        set_playlist_track,
        track_index=track_index,
        name=name,
        color=color,
        muted=muted,
        soloed=soloed,
        selected=selected,
        expected_before=expected_before,
        session_fingerprint=session_fingerprint,
        stop_on_unverified=stop_on_unverified,
    )


@mcp.tool(
    name="playlist_get_selection",
    annotations=READ_ONLY.model_copy(update={"title": "Read the Playlist time selection"}),
)
async def playlist_get_selection() -> SelectedRangeObservation:
    """Read the Playlist's current time selection and the project PPQ.

    Read-only. Returns the raw selection endpoints with validity and
    consistency evidence. PPQ is ticks per quarter note; the endpoints are not
    converted to bars and are not guaranteed render limits, so check the
    evidence before relying on them. Use project_get_summary for the playback
    position."""
    return await _run("selected_range")


@mcp.tool(
    name="playlist_add_markers",
    annotations=MUTATING.model_copy(update={"title": "Add arrangement section markers"}),
)
async def playlist_add_markers(
    markers: Annotated[
        list[SectionMarker],
        Field(min_length=1, max_length=32, description="Markers to add, each a name at a bar and optional beat offset."),
    ],
) -> ArrangementMarkerReceipt:
    """Add named section markers (Intro, Verse, Drop) to the Playlist timeline.

    Requires write mode. FL lets PostFader read marker names back but not
    their times, so the receipt verifies the new names and reports times as
    unverified. Does not save the project."""
    return await _mix(add_section_markers, markers)


# ---------------------------------------------------------------------------
# automation
# ---------------------------------------------------------------------------


@mcp.tool(
    name="automation_record_value",
    annotations=MUTATING.model_copy(update={"title": "Record one automation value"}),
)
async def automation_record_value(
    target_kind: Annotated[
        Literal["mixer", "channel"], Field(description="Whether target_index is a mixer track or a channel.")
    ],
    target_index: Annotated[int, Field(ge=0, description="Mixer track or global channel index.")],
    property: Annotated[
        Literal["volume", "pan", "stereo_separation"],
        Field(description="Control to record: channels support volume and pan; mixer tracks also stereo_separation."),
    ],
    value_normalized: Annotated[
        float, Field(ge=0.0, le=1.0, description="Value to record, normalized 0..1.")
    ],
    allow_master: Annotated[bool, Field(description="Permit mixer target 0 (Master).")] = False,
    expected_before: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=1.0, description="Optional current normalized value; refuse if it changed."),
    ] = None,
) -> AutomationRecordReceipt:
    """Record one automation value into FL while playback and recording run.

    For writing a control change into the arrangement as automation. Requires
    write mode, and FL must already be playing with recording armed (set both
    with transport_set). Sends one REC event for the control at the current
    song position and reports the control value before and after. Whether an
    automation point was created cannot be read back. To set a value without
    recording it, use mixer_set_track or channel_set."""
    return await _mix(
        record_automation_value,
        target_kind=target_kind,
        target_index=target_index,
        property=property,
        value_normalized=value_normalized,
        allow_master=allow_master,
        expected_before=expected_before,
    )


# ---------------------------------------------------------------------------
# plugin: loaded effects and instruments
# ---------------------------------------------------------------------------


@mcp.tool(
    name="plugin_list_loaded",
    annotations=READ_ONLY.model_copy(update={"title": "List loaded plug-ins"}),
)
async def plugin_list_loaded(
    only_used: Annotated[
        bool,
        Field(description="Apply the conservative used-mixer-track heuristic; generators stay included."),
    ] = False,
) -> TargetedLoadedPluginInventory:
    """List every loaded mixer effect and Channel Rack instrument as a target.

    Read-only. Each entry carries the target object that the other plugin_*
    tools take (mixer_effect with track and slot, or channel_generator with a
    channel index), the plug-in's reported name, and its parameter count. Use
    atlas_match_loaded to identify products, and plugin_list_parameters to see
    a plug-in's controls."""
    return await _performance_read("scan_loaded_plugins", only_used=only_used)


@mcp.tool(
    name="plugin_list_parameters",
    annotations=READ_ONLY.model_copy(update={"title": "List a plug-in's parameters"}),
)
async def plugin_list_parameters(
    target: PluginTargetArg,
    start: Annotated[
        int | None,
        Field(default=None, description="First parameter index to examine; defaults to 0.", ge=0),
    ] = None,
    end: Annotated[
        int | None,
        Field(default=None, description="Exclusive last index; defaults to FL's reported count.", ge=0),
    ] = None,
    max_indices: Annotated[
        int | None,
        Field(default=None, description="Stop after examining this many indices.", ge=1, le=8192),
    ] = None,
    max_results: Annotated[
        int | None,
        Field(default=None, description="Stop after collecting this many real controls.", ge=1),
    ] = None,
) -> TargetedPluginParameterScan:
    """List a loaded plug-in's real controls with their current values and text.

    Read-only. FL reports a padded parameter count for VST plug-ins, often
    thousands of empty slots; this walks the range inside FL and returns only
    real controls, each with its index, name, normalized value, and display
    text (which identifies unnamed controls). Check truncated before treating
    the list as complete. Use the indices, names, or display text with
    plugin_set_parameter."""
    return await _performance_read(
        "scan_plugin_parameters",
        target=target,
        start=start,
        end=end,
        max_indices=max_indices,
        max_results=max_results,
    )


@mcp.tool(
    name="plugin_set_parameter",
    annotations=MUTATING.model_copy(update={"title": "Set a plug-in parameter"}),
)
async def plugin_set_parameter(
    target: PluginTargetArg,
    parameter: Annotated[
        int | str,
        Field(
            description=(
                "Parameter index from plugin_list_parameters, or text matched against "
                "parameter names and display strings (many controls have no name). "
                "normalized_value needs an index."
            )
        ),
    ],
    normalized_value: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=1.0, description="Raw 0..1 value. Only when you know the control's mapping; prefer display_value or option."),
    ] = None,
    display_value: Annotated[
        float | None,
        Field(default=None, description="The number the plug-in should display: 20 for '20 ms', -18 for '-18.0 dB', 4000 with unit='Hz' for '4.0kHz'."),
    ] = None,
    unit: Annotated[
        str | None,
        Field(default=None, description="Optional unit for display_value: Hz, kHz, ms, seconds, dB, percent, or ratio; converts across prefixes."),
    ] = None,
    tolerance: Annotated[
        float | None,
        Field(default=None, ge=0.0, description="How close the displayed number must land; defaults to 2% of the target, at least 0.01."),
    ] = None,
    option: Annotated[
        str | None,
        Field(default=None, min_length=1, description="Exact option text for a control that shows words, such as 'Major' or 'Low Male'."),
    ] = None,
    sweep_steps: Annotated[
        int | None,
        Field(default=None, ge=2, le=256, description="Resolution of the option sweep; defaults to 64. Raise it only if an option is missed."),
    ] = None,
    expected_before: Annotated[
        ExpectedPluginParameterState | None,
        Field(default=None, description="Optional current normalized value and/or exact display text; refuse if either changed."),
    ] = None,
    session_fingerprint: SessionFingerprintArg = None,
) -> (
    VerifiedTargetedPluginParameterWrite
    | VerifiedTargetedPluginDisplayWrite
    | VerifiedTargetedPluginOptionWrite
):
    """Set one plug-in parameter by display value, option text, or raw 0..1 value.

    Supply exactly one value form. display_value searches the control until its
    own display reads the number asked for, so no unit curve is guessed; use it
    for anything with units (Hz, dB, ms, %). option sets a word-valued control
    (Key, Scale, Mode) by sweeping it, which moves the control while it looks;
    the result lists every option found, and an unknown option restores the
    original value before the error. normalized_value writes 0..1 directly.
    Requires write mode. Read controls with plugin_list_parameters first.
    Inspect verified and the readback; does not save the project."""
    forms = [normalized_value is not None, display_value is not None, option is not None]
    if sum(forms) != 1:
        raise ValueError("supply exactly one of normalized_value, display_value, or option")
    if display_value is None and (unit is not None or tolerance is not None):
        raise ValueError("unit and tolerance apply only to display_value")
    if option is None and sweep_steps is not None:
        raise ValueError("sweep_steps applies only to option")
    common = {
        "target": target,
        "session_fingerprint": session_fingerprint,
        "expected_before": expected_before,
    }
    if normalized_value is not None:
        if isinstance(parameter, str):
            raise ValueError(
                "normalized_value needs a parameter index; address a control by name "
                "or display text with display_value or option"
            )
        return await _performance_write(
            "set_plugin_parameter",
            parameter_index=parameter,
            normalized_value=normalized_value,
            **common,
        )
    if display_value is not None:
        return await _performance_write(
            "set_plugin_parameter_display",
            parameter=parameter,
            target_value=display_value,
            **({"target_unit": unit} if unit is not None else {}),
            tolerance=tolerance,
            **common,
        )
    return await _performance_write(
        "set_plugin_parameter_option",
        parameter=parameter,
        option=option,
        sweep_steps=64 if sweep_steps is None else sweep_steps,
        **common,
    )


@mcp.tool(
    name="plugin_list_presets",
    annotations=READ_ONLY.model_copy(update={"title": "List a plug-in's presets"}),
)
async def plugin_list_presets(
    target: PluginTargetArg,
    start: Annotated[int, Field(ge=0, description="First preset index in this page.")] = 0,
    limit: Annotated[
        int, Field(ge=1, le=256, description="Number of preset names in this page.")
    ] = 64,
    include_current: Annotated[
        bool, Field(description="Also report the current preset's name and index.")
    ] = True,
    include_empty_names: Annotated[
        bool, Field(description="Keep presets with blank names in the page.")
    ] = False,
) -> PluginPresetPage:
    """Read a page of a loaded plug-in's preset names, its preset count, and current preset.

    Read-only. Returns FL's authoritative preset_count, one page of names with
    their indices (follow next_start for more), and, unless include_current is
    false, the current preset with an index only when it is unique. Use a name
    or index from this list with plugin_select_preset; use sound_plan_palette
    to choose sounds across several roles."""
    return await _performance_read(
        "list_plugin_presets",
        target=target,
        start=start,
        limit=limit,
        include_current=include_current,
        include_empty_names=include_empty_names,
    )


@mcp.tool(
    name="plugin_select_preset",
    annotations=MUTATING.model_copy(update={"title": "Select a plug-in preset"}),
)
async def plugin_select_preset(
    target: PluginTargetArg,
    preset_name: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=256, description="Exact preset name from plugin_list_presets."),
    ] = None,
    preset_index: Annotated[
        int | None,
        Field(default=None, ge=0, le=999_999, description="Exact preset index from plugin_list_presets."),
    ] = None,
    expected_current: Annotated[
        ExpectedPluginPresetState | None,
        Field(default=None, description="Optional current preset name and/or index; refuse if it changed."),
    ] = None,
    session_fingerprint: SessionFingerprintArg = None,
    target_fingerprint: Annotated[
        str | None,
        Field(default=None, pattern=r"^[0-9a-f]{64}$", description="Optional target identity from plugin_list_presets; refuse if a different plug-in is now loaded there."),
    ] = None,
    max_navigation_steps: Annotated[
        int,
        Field(default=64, ge=0, le=256, description="Bound on next/previous preset steps."),
    ] = 64,
    settle_tick_limit: Annotated[
        int,
        Field(default=1, ge=1, le=8, description="Later FL ticks allowed for the plug-in to settle."),
    ] = 1,
) -> VerifiedPluginPresetSelection:
    """Load a known preset on one mixer effect or Channel Rack instrument.

    Supply an exact name and/or index from plugin_list_presets. Requires write
    mode. FL steps through presets to reach the target, which changes the sound
    through intermediate presets, bounded by max_navigation_steps; the landed
    preset's identity is read back. Inspect verified and warnings. Use
    sound_apply_palette for planned multi-role choices. This selects a preset;
    it does not set a parameter or load a new plug-in (plugin_load)."""
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
    name="plugin_get_pad_map",
    annotations=READ_ONLY.model_copy(update={"title": "Read a plug-in's pad map"}),
)
async def plugin_get_pad_map(target: PluginTargetArg) -> PluginPadMap:
    """Read an instrument's drum pads: MIDI note, color, and empty/muted state.

    Read-only. Read it before writing drums to a non-General-MIDI kit so each
    hit lands on the right pad; sound selection and the inspect_drum_map run
    operation turn these pads into the drum_map that compose_drums takes.
    complete is false when FL did not report every pad."""
    return await _performance_read("inspect_plugin_pad_map", target=target)


@mcp.tool(
    name="plugin_list_available",
    annotations=WORKFLOW_STATE.model_copy(update={"title": "List FL's Add-menu plug-ins"}),
)
async def plugin_list_available() -> PluginMenuInventory:
    """List the instruments and effects in FL's native Add menu (macOS only).

    Opens and closes the menu, which briefly changes focus; changes no project
    state. Reports exact loadable names and whether each is an instrument or
    an effect, for plugin_load. Menu presence is not proof of licensing or an
    exhaustive install scan."""
    return await _mix(list_available_plugins)


@mcp.tool(
    name="plugin_load",
    annotations=MUTATING.model_copy(update={"title": "Load an instrument or mixer effect"}),
)
async def plugin_load(
    request: Annotated[
        PluginLoadRequest,
        Field(description="Exact Add-menu name, kind (instrument or effect), and for effects the mixer track_index."),
    ],
) -> PluginLoadResult:
    """Add one instrument (new channel) or mixer effect from FL's Add menu (macOS).

    Use an exact name from plugin_list_available. Requires write mode; an
    effect load first selects its mixer track. Returns the new channel or
    effect slot as a target for plugin_select_preset or plugin_set_parameter.
    If the outcome is unknown, inspect plugin_list_loaded before trying again.
    Windows insertion is not implemented. Does not save the project."""
    return await _mix(load_plugin, request)


# ---------------------------------------------------------------------------
# atlas: bundled offline plug-in knowledge
# ---------------------------------------------------------------------------


@mcp.tool(
    name="atlas_search",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Search Plugin Atlas"}),
)
async def atlas_search(
    request: Annotated[
        AtlasSearchRequest,
        Field(description="Text query and optional filters; omit filters to search every product."),
    ],
) -> AtlasSearchResponse:
    """Search the bundled offline Plugin Atlas for products by text and filters.

    query searches product knowledge; vendor_id, origin, kind, technique_id,
    and stock_only narrow it, and limit caps the hits. No FL connection is
    needed, and a hit does not mean the product is installed or owned. Use
    atlas_get_product for one product's details and control adapters,
    atlas_recommend for a production goal, and atlas_match_loaded to identify
    plug-ins in the open project."""
    return await _mix(search_atlas, request)


@mcp.tool(
    name="atlas_get_product",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Get a Plugin Atlas product"}),
)
async def atlas_get_product(
    request: Annotated[
        AtlasGetProductRequest,
        Field(description="Exact product_id from atlas_search, atlas_recommend, or atlas_match_loaded."),
    ],
) -> AtlasProductResponse:
    """Read one Plugin Atlas product: vendor, controls, adapters, and alternatives.

    Offline and read-only. Returns the product's knowledge, its control
    adapters (which parameters mean what), evidence, and stock alternatives. An
    adapter record does not prove that a loaded copy is writable; join catalog
    knowledge to live targets with atlas_match_loaded."""
    return await _mix(get_atlas_product, request)


@mcp.tool(
    name="atlas_recommend",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Recommend plug-ins from Plugin Atlas"}),
)
async def atlas_recommend(
    request: Annotated[
        AtlasRecommendRequest,
        Field(description="Goal criteria (query, problems, techniques, sources, kind), or product_id with stock_alternatives=true."),
    ],
) -> AtlasRecommendationResponse:
    """Rank Plugin Atlas products for a production problem or technique.

    Describe the task with query, problems, techniques, sources, and kind;
    prefer_stock favors FL's stock plug-ins and limit bounds results. With
    product_id and stock_alternatives=true it lists stock alternatives to a
    known product. Offline static knowledge: not proof of availability or
    ownership. Use sound_plan_palette to assign sounds from what is loaded."""
    return await _mix(recommend_atlas, request)


@mcp.tool(
    name="atlas_match_loaded",
    annotations=READ_ONLY.model_copy(update={"title": "Identify loaded plug-ins with Plugin Atlas"}),
)
async def atlas_match_loaded(
    request: Annotated[
        AtlasInspectLoadedRequest,
        Field(description="Inventory scope and match limits; an empty request uses conservative defaults."),
    ],
) -> AtlasInspectLoadedResponse:
    """Identify the open project's loaded plug-ins against Plugin Atlas.

    Reads the live inventory and returns, for each loaded effect and
    instrument, its target, candidate Atlas products, and whether a control
    adapter makes its parameters known. Read-only. only_used limits mixer
    tracks; match_limit caps candidates; include_weak adds uncertain matches.
    A match is not proof of ownership or writable controls; confirm controls
    with plugin_list_parameters."""
    return await _mix(inspect_loaded_atlas, request)


# ---------------------------------------------------------------------------
# processing: goal-based effect settings
# ---------------------------------------------------------------------------


@mcp.tool(
    name="processing_plan",
    annotations=READ_ONLY.model_copy(update={"title": "Plan effect processing"}),
)
async def processing_plan(
    request: Annotated[
        ProcessingRequest,
        Field(
            description=(
                "Processing goals per role or target, such as reduce_mud, "
                "tame_harshness, add_air, add_punch, limit_peaks, or add_depth, "
                "resolved only against loaded, Atlas-matched, adapter-backed effects."
            )
        ),
    ],
) -> ProcessingPlan:
    """Turn processing goals into concrete settings for effects that are loaded.

    Read-only: inspects loaded effects once and returns a plan of semantic
    actions, each naming the effect, control, and value, plus what could not be
    resolved and why. Goals and strength produce first-pass settings; explicit
    controls override them. Review the plan, then apply it unchanged with
    processing_apply, or use the plan_processing operation inside run_execute.
    Re-bounce and listen before refining."""
    return await _mix(plan_live_processing, request)


@mcp.tool(
    name="processing_apply",
    annotations=MUTATING.model_copy(update={"title": "Apply an effect processing plan"}),
)
async def processing_apply(
    plan: Annotated[
        ProcessingPlan,
        # Advertised as an opaque echo of an earlier result; validated in full.
        WithJsonSchema({"type": "object"}),
        Field(description="The plan object returned by processing_plan, passed back unchanged."),
    ],
    session_fingerprint: RequiredSessionFingerprintArg,
    authorized_to_modify: Annotated[
        bool,
        Field(description="True only when the user explicitly asked for these processing changes."),
    ],
) -> ProductionRunResult:
    """Apply a reviewed processing_plan to the loaded effects in one run.

    Runs the plan as a single-operation Production Run: it checks readiness,
    enables write mode for the run, writes each control with its verified
    setter, and releases write mode. Requires the session_fingerprint from a
    recent live read and authorized_to_modify=true. Stops on an unknown or
    unverified outcome; earlier settings remain. Returns the run with
    per-action receipts; continue or inspect it with run_continue and run_get."""
    if (
        plan.session_fingerprint is not None
        and plan.session_fingerprint != session_fingerprint
    ):
        raise ValueError(
            "session_fingerprint does not match the processing plan's captured session"
        )
    if any(
        action.session_fingerprint is not None
        and action.session_fingerprint != session_fingerprint
        for action in plan.actions
    ):
        raise ValueError(
            "session_fingerprint does not match a semantic action's captured session"
        )
    prepared = plan.model_copy(
        update={
            "session_fingerprint": session_fingerprint,
            "actions": tuple(
                (
                    action
                    if action.session_fingerprint is not None
                    else action.model_copy(
                        update={"session_fingerprint": session_fingerprint}
                    )
                )
                for action in plan.actions
            ),
        }
    )
    request = ProductionRunRequest(
        brief="Apply the selected loaded-effect processing plan.",
        scope=ProductionScope(
            kind="whole_project",
            description="Processing targets in this plan.",
        ),
        allowed_changes=("plugin_parameters",),
        completion_target=plan.completion_target.replace("_", " "),
        interaction_policy="execute_once",
        max_operations=1,
        authorized_to_modify=authorized_to_modify,
    )
    run_plan = ProductionRunPlan(
        plan_id=f"processing-{plan.plan_id}"[:64],
        operations=(
            ApplyProcessingPlanOperation(
                operation_id="apply_processing",
                plan=prepared,
            ),
        ),
    )
    return await _mix(PRODUCTION_RUNS.execute, request, run_plan)


# ---------------------------------------------------------------------------
# sound: palette planning from loaded instruments, and local history
# ---------------------------------------------------------------------------


@mcp.tool(
    name="sound_get_inventory",
    annotations=READ_ONLY.model_copy(update={"title": "Inventory available sounds"}),
)
async def sound_get_inventory(
    request: Annotated[
        SoundSelectionRequest | None,
        Field(default=None, description="Optional sound request; it decides which loaded targets and effects are relevant."),
    ] = None,
    only_used: Annotated[
        bool,
        Field(description="Limit mixer observations to used tracks; instruments stay included."),
    ] = False,
    include_effects: Annotated[
        bool | None,
        Field(default=None, description="Include loaded effects; defaults from the request."),
    ] = None,
    preset_start: Annotated[int, Field(ge=0, description="First preset index read per target.")] = 0,
    preset_limit: Annotated[
        int,
        Field(ge=1, le=256, description="Maximum preset names read per loaded target."),
    ] = 64,
    include_current: Annotated[bool, Field(description="Read each target's current preset.")] = True,
    include_empty_names: Annotated[bool, Field(description="Keep presets with blank names.")] = False,
    include_pad_maps: Annotated[bool, Field(description="Read drum pad maps of loaded instruments.")] = True,
    include_atlas: Annotated[bool, Field(description="Add offline Plugin Atlas knowledge to each loaded target.")] = True,
) -> SoundInventory:
    """Read the pool of loaded instruments and their presets that palettes choose from.

    Read-only. Returns each loaded instrument (and effects when requested) with
    its preset names, current preset, drum pad map, and Atlas knowledge.
    sound_plan_palette reads this inventory itself, so call it only to show
    the user what is available. Atlas-only products are recommendations, not
    loaded sounds."""
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
    name="sound_plan_palette",
    annotations=READ_ONLY.model_copy(update={"title": "Plan a sound palette"}),
)
async def sound_plan_palette(
    request: Annotated[
        SoundSelectionRequest,
        Field(description="The brief, roles to fill, creative direction, preferences, exclusions, and history policy."),
    ],
    base_palette_id: Annotated[
        str | None,
        Field(
            default=None,
            min_length=1,
            max_length=128,
            description="Plan a section variation of this existing palette instead of a new palette.",
        ),
    ] = None,
    section: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=128, description="Section the variation is for, such as chorus. Variation only."),
    ] = None,
    replace_roles: Annotated[
        tuple[str, ...],
        Field(default=(), max_length=128, description="Roles a variation may replace; other anchors are kept. Variation only."),
    ] = (),
) -> SoundPalettePlan | SoundPaletteVariationPlan:
    """Choose an instrument and preset for each musical role from loaded sounds.

    Read-only: reads the live inventory and returns deterministic assignments
    with score reasons and a palette_id, without changing FL or history. With
    base_palette_id it plans a variation for a later section instead, keeping
    the base palette's anchors except replace_roles. Explicit roles and
    preferences override genre defaults. Review the plan, then apply it with
    sound_apply_palette before writing notes, so parts fit the chosen sounds."""
    if base_palette_id is None:
        if section is not None or replace_roles:
            raise ValueError("section and replace_roles apply only with base_palette_id")
        return await _mix(plan_sound_selection, request)
    return await _mix(
        create_sound_selection_variation,
        base_palette_id,
        request,
        section,
        replace_roles,
    )


@mcp.tool(
    name="sound_get_palette",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Get a sound palette"}),
)
async def sound_get_palette(
    palette_id: Annotated[
        str,
        Field(min_length=1, max_length=128, description="palette_id from sound_plan_palette."),
    ],
) -> SoundPaletteLookup:
    """Look up a previously planned sound palette by its palette_id.

    Read-only. Palettes live in this MCP process; an expired or unknown ID is
    reported in the result rather than raised as an error, so plan again if it
    is gone."""
    return await _mix(get_sound_selection, palette_id)


@mcp.tool(
    name="sound_apply_palette",
    annotations=MUTATING.model_copy(update={"title": "Apply a sound palette"}),
)
async def sound_apply_palette(
    palette: Annotated[
        SoundPalettePlan | SoundPaletteVariationPlan | str,
        # Advertised as an opaque echo of an earlier result; validated in full.
        WithJsonSchema(
            {
                "anyOf": [
                    {"type": "string", "minLength": 1},
                    {"type": "object"},
                ]
            }
        ),
        Field(
            description=(
                "The palette_id from sound_plan_palette, or the full palette or variation "
                "object it returned, passed back unchanged. A variation_id is not accepted."
            )
        ),
    ],
    session_fingerprint: RequiredSessionFingerprintArg,
    authorized_to_modify: Annotated[
        bool,
        Field(description="True only when the user explicitly asked for these sound changes."),
    ],
    role_ids: Annotated[
        tuple[str, ...],
        Field(default=(), max_length=128, description="Apply only these roles; empty applies every role."),
    ] = (),
    max_navigation_steps: Annotated[
        int, Field(default=64, ge=0, le=256, description="Bound on preset navigation steps per role.")
    ] = 64,
    settle_tick_limit: Annotated[
        int, Field(default=1, ge=1, le=8, description="Later FL ticks allowed for each plug-in to settle.")
    ] = 1,
    persist_history: Annotated[
        bool | None,
        Field(default=None, description="Override the palette's local-history policy for this application."),
    ] = None,
) -> SoundSelectionApplyResult:
    """Load the presets a reviewed palette assigned, role by role, in FL.

    Pass the palette (or its palette_id) unchanged, the session_fingerprint
    from a live read, and authorized_to_modify=true. Enables write mode for
    this application, selects each assignment's preset in a fixed order with
    later-tick identity readback, and stops on an unknown or unverified
    outcome; earlier roles remain applied. Does not install or load plug-ins
    (plugin_load). Inspect the receipts before any further attempt. Use
    plugin_select_preset for one known preset without a palette."""
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
    name="sound_record_feedback",
    annotations=WORKFLOW_STATE.model_copy(
        update={"title": "Record sound feedback", "open_world_hint": False}
    ),
)
async def sound_record_feedback(
    request: Annotated[
        SoundFeedbackRequest,
        Field(description="The user's explicit accepted, rejected, or neutral verdict on a palette, role, or assignment."),
    ],
) -> SoundFeedbackResult:
    """Record the user's explicit verdict on a palette so future picks follow it.

    Scope the verdict with palette_id and optionally role_id or assignment_id;
    descriptors name qualities the user wants more or less of. May write local
    history according to the request's persistence settings; never changes the
    FL project. Never infer acceptance from silence. Feedback does not generate
    a replacement: use sound_plan_palette with base_palette_id for that."""
    return await _mix(record_sound_selection_feedback, request)


@mcp.tool(
    name="sound_get_history",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Inspect sound history"}),
)
async def sound_get_history() -> SoundHistoryStatus:
    """Report the local sound-selection history: location, health, and record counts.

    Read-only. History stores accepted and rejected choices on this computer
    to steer later palettes. Use sound_reset_history only when the user asks
    to clear it."""
    return await _mix(get_sound_selection_history_status)


@mcp.tool(
    name="sound_reset_history",
    annotations=WORKFLOW_STATE.model_copy(
        update={
            "title": "Reset sound history",
            "destructive_hint": True,
            "idempotent_hint": True,
            "open_world_hint": False,
        }
    ),
)
async def sound_reset_history(
    confirm: Annotated[
        bool,
        Field(description="Must be true, after the user explicitly asked to delete local sound history."),
    ],
) -> SoundHistoryResetResult:
    """Delete the local sound-selection history after the user asks to.

    Removes the bounded history records that steer future palettes. Cannot be
    undone. The FL project and its presets are unchanged."""
    return await _mix(reset_sound_selection_history, confirm)


# ---------------------------------------------------------------------------
# audio: measurements of rendered files
# ---------------------------------------------------------------------------


@mcp.tool(
    name="audio_analyze_file",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Measure an audio file"}),
)
async def audio_analyze_file(
    path: Annotated[
        str,
        Field(description="Absolute path to an existing audio file exported from FL Studio."),
    ],
    include_pitch: Annotated[
        bool,
        Field(
            description="Also run the monophonic pitch tracker; useful for a lead vocal stem, unreliable for a full mix."
        ),
    ] = False,
    max_seconds: MaxSecondsArg = None,
) -> AudioFileAnalysis:
    """Measure one rendered file's loudness, peaks, spectrum, dynamics, and stereo image.

    Reads the file only; FL's live output is not available. Returns integrated
    and short-term loudness (LUFS), loudness range, sample and true peak,
    spectral band balance, dynamics, stereo correlation and mono compatibility,
    and optional pitch. Use audio_diagnose_mix for a pass/fail mix diagnosis,
    audio_compare_files against a reference, and audio_list_recent_bounces to
    find the user's latest export."""
    return await _measure(
        analyze_audio_file, path, include_pitch=include_pitch, max_seconds=max_seconds
    )


@mcp.tool(
    name="audio_compare_files",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Compare a mix with a reference"}),
)
async def audio_compare_files(
    reference_path: Annotated[
        str,
        Field(description="Absolute path to the reference render."),
    ],
    candidate_path: Annotated[
        str,
        Field(description="Absolute path to the candidate render being judged."),
    ],
    max_seconds: MaxSecondsArg = None,
) -> ReferenceRecommendationReport:
    """Compare a candidate render with a reference and suggest tonal adjustments.

    Reads both files; changes nothing. Aligns them, matches loudness, and
    measures per-band differences over their common overlap. When alignment
    and readiness checks pass, it adds bounded review ranges per band (reduce
    or increase the candidate); otherwise actionable is false. Suggestions are
    starting points to review, not targets to apply blindly."""
    return await _mix(
        reference_recommendations,
        reference_path,
        candidate_path,
        max_seconds=max_seconds,
    )


@mcp.tool(
    name="audio_analyze_masking",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Measure vocal masking"}),
)
async def audio_analyze_masking(
    vocal_path: Annotated[
        str,
        Field(description="Absolute path to the vocal stem."),
    ],
    instrumental_path: Annotated[
        str,
        Field(
            description="Absolute path to the instrumental stem of the same section, rendered sample-synchronously."
        ),
    ],
    max_seconds: MaxSecondsArg = None,
) -> MaskingRecommendationReport:
    """Measure where an instrumental masks a vocal and suggest remedies.

    Reads two sample-synchronous stems; changes nothing. Returns per-band
    spectral overlap and vocal-minus-instrument margins and, when the evidence
    is actionable, suggested instrument reductions per band (dynamic EQ or
    automation). Use audio_diagnose_mix to judge the full mix."""
    return await _mix(
        masking_recommendations,
        vocal_path,
        instrumental_path,
        max_seconds=max_seconds,
    )


@mcp.tool(
    name="audio_diagnose_mix",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Diagnose a mix"}),
)
async def audio_diagnose_mix(
    candidate_path: Annotated[str, Field(description="Absolute path to the mix to diagnose.")],
    target: Annotated[
        MixTarget,
        Field(description="Delivery target whose thresholds apply: dynamic, balanced, streaming, or club."),
    ] = "balanced",
    reference_path: Annotated[
        str | None, Field(default=None, description="Optional reference render to compare against.")
    ] = None,
    vocal_path: Annotated[
        str | None, Field(default=None, description="Optional sample-synchronous vocal stem for a masking check.")
    ] = None,
    instrumental_path: Annotated[
        str | None, Field(default=None, description="Optional instrumental stem matching vocal_path.")
    ] = None,
    max_seconds: MaxSecondsArg = None,
) -> MixDoctorReport:
    """Diagnose a rendered mix against technical thresholds for a delivery target.

    Reads the files; changes nothing. Measures the mix, optionally compares it
    with a reference and checks vocal masking, then lists issues with severity,
    the measurement, the threshold it broke, and a recommendation, plus whether
    the mix is technically export-ready. Passing is technical, not artistic
    approval. After changing the project, have the user export again and
    re-run it."""
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
    name="audio_list_recent_bounces",
    annotations=LOCAL_READ_ONLY_VOLATILE,
)
async def audio_list_recent_bounces(
    limit: Annotated[
        int,
        Field(description="Maximum number of files to return, newest first.", ge=1, le=200),
    ] = 20,
) -> RecentAudioListing:
    """List the newest audio files in FL Studio's Rendered, Audio, and Projects folders.

    Read-only. Use it to find the user's latest export before measuring it with
    the other audio_* tools; confirm with the user which file is the right one."""
    return await _measure(find_recent_audio_files, limit)


@mcp.tool(
    name="audio_estimate_tempo_and_key",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Estimate tempo and key"}),
)
async def audio_estimate_tempo_and_key(
    path: Annotated[str, Field(description="Absolute path to an audio file.")],
    max_seconds: Annotated[
        float | None,
        Field(default=300.0, ge=1.0, le=600.0, description="Seconds of audio to analyse."),
    ] = 300.0,
) -> AudioMusicAnalysis:
    """Estimate a file's tempo and major/minor key, with ranked alternatives.

    Reads the file only. Returns the best tempo and key, each with a confidence,
    plus ranked tempo candidates and an ambiguity note, so half/double-tempo
    readings stay visible. Use it to match a sample or reference before
    composing."""
    return await _measure(analyze_tempo_and_key, path, max_seconds=max_seconds)


@mcp.tool(
    name="audio_transcribe_melody",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Transcribe a melody"}),
)
async def audio_transcribe_melody(
    path: Annotated[str, Field(description="Absolute path to one isolated, monophonic pitched source.")],
    tempo_bpm: Annotated[
        float | None,
        Field(default=None, ge=10.0, le=522.0, description="Tempo for beat positions; estimated when omitted, 120 if estimation fails."),
    ] = None,
    fmin_hz: Annotated[float, Field(ge=30.0, le=3999.0, description="Lowest pitch to track, in Hz.")] = 55.0,
    fmax_hz: Annotated[float, Field(ge=31.0, le=4000.0, description="Highest pitch to track, in Hz.")] = 1760.0,
    minimum_note_seconds: Annotated[
        float, Field(ge=0.03, le=2.0, description="Shorter detected notes are dropped.")
    ] = 0.08,
    quantize_grid_beats: Annotated[
        float | None,
        Field(default=0.25, ge=0.03125, le=4.0, description="Snap grid in beats; null keeps raw timing."),
    ] = 0.25,
    max_seconds: Annotated[
        float | None,
        Field(default=180.0, ge=1.0, le=300.0, description="Seconds of audio to transcribe."),
    ] = 180.0,
) -> MelodyTranscription:
    """Transcribe a monophonic recording (a sung or played line) into notes.

    Reads the file only. Returns a note sequence in beats, ready to review and
    then write with piano_roll_write_notes or export with compose_export_midi.
    Chords and full mixes transcribe poorly; give it one isolated line."""
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


# ---------------------------------------------------------------------------
# compose: deterministic offline note generation and MIDI export
# ---------------------------------------------------------------------------


RootArg = Annotated[str, Field(description="Tonic note name, for example C, F#, or Bb.")]
CollectionArg = Annotated[
    str,
    Field(description="Bundled scale, mode, or raga name such as major, dorian, or harmonic_minor; custom uses custom_intervals."),
]
CustomIntervalsArg = Annotated[
    list[int] | None,
    Field(default=None, max_length=12, description="Semitone offsets from the root when collection is custom."),
]
SeedArg = Annotated[int, Field(description="Variation seed; the same inputs and seed give the same notes.")]
TempoArg = Annotated[
    float, Field(ge=10.0, le=522.0, description="Tempo recorded in the sequence, in BPM.")
]


@mcp.tool(
    name="compose_chord_progression",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Compose a chord progression"}),
)
async def compose_chord_progression(
    progression: Annotated[
        list[str],
        Field(min_length=1, max_length=64, description="Roman-numeral chords such as I, vi, IV, V, or V7."),
    ],
    root: RootArg = "C",
    collection: CollectionArg = "major",
    custom_intervals: CustomIntervalsArg = None,
    beats_per_chord: Annotated[
        float, Field(ge=0.125, le=32.0, description="Length of each chord in beats.")
    ] = 4.0,
    octave: Annotated[int, Field(ge=0, le=8, description="Octave of the voicing.")] = 4,
    voicing: Annotated[
        Literal["close", "open", "drop2"], Field(description="Voicing strategy.")
    ] = "close",
    velocity: Annotated[float, Field(ge=0.0, le=1.0, description="Note velocity 0..1.")] = 0.78,
    tempo_bpm: TempoArg = 120.0,
) -> NoteSequence:
    """Generate voice-led chords from Roman numerals in a key, without touching FL.

    Deterministic: the same inputs give the same notes. Returns a note sequence
    in beats with a digest. Write it with piano_roll_write_notes, export it with
    compose_export_midi, or pass the progression to compose_bassline."""
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
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Compose a melody"}),
)
async def compose_melody(
    root: RootArg = "C",
    collection: CollectionArg = "major",
    custom_intervals: CustomIntervalsArg = None,
    bars: Annotated[int, Field(ge=1, le=64, description="Melody length in bars.")] = 4,
    beats_per_bar: Annotated[int, Field(ge=1, le=16, description="Beats per bar.")] = 4,
    density: Annotated[
        float, Field(ge=0.05, le=1.0, description="How busy the line is, from sparse to dense.")
    ] = 0.65,
    register_low: Annotated[int, Field(ge=0, le=130, description="Lowest MIDI note allowed.")] = 60,
    register_high: Annotated[int, Field(ge=1, le=131, description="Highest MIDI note allowed.")] = 84,
    contour: Annotated[
        Literal["balanced", "rising", "falling", "arch", "wave"],
        Field(description="Overall melodic shape."),
    ] = "balanced",
    seed: SeedArg = 0,
    tempo_bpm: TempoArg = 120.0,
) -> NoteSequence:
    """Generate a scale-aware melody within a register, without touching FL.

    Deterministic for a given seed; change the seed for alternatives. Returns a
    note sequence in beats. Fit the register to the chosen sound
    (sound_plan_palette) before writing it with piano_roll_write_notes."""
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
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Compose a bassline"}),
)
async def compose_bassline(
    progression: Annotated[
        list[str],
        Field(min_length=1, max_length=64, description="Roman-numeral chords the bass follows."),
    ],
    root: RootArg = "C",
    collection: CollectionArg = "major",
    custom_intervals: CustomIntervalsArg = None,
    beats_per_chord: Annotated[
        float, Field(ge=0.5, le=32.0, description="Length of each chord in beats.")
    ] = 4.0,
    octave: Annotated[int, Field(ge=0, le=7, description="Bass octave.")] = 2,
    style: Annotated[
        Literal["roots", "eighths", "octaves", "walking"],
        Field(description="Rhythmic and melodic pattern."),
    ] = "roots",
    seed: SeedArg = 0,
    tempo_bpm: TempoArg = 120.0,
) -> NoteSequence:
    """Generate a bass part that follows a Roman-numeral progression, without touching FL.

    Deterministic for a given seed. Use the same progression and key as
    compose_chord_progression so the parts agree. Returns a note sequence in
    beats for piano_roll_write_notes or compose_export_midi."""
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
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Compose a drum pattern"}),
)
async def compose_drums(
    style: Annotated[
        Literal["house", "hiphop", "trap", "pop", "dnb"],
        Field(description="Groove style."),
    ] = "house",
    bars: Annotated[int, Field(ge=1, le=64, description="Pattern length in bars.")] = 4,
    beats_per_bar: Annotated[int, Field(ge=1, le=16, description="Beats per bar.")] = 4,
    seed: SeedArg = 0,
    swing: Annotated[
        float, Field(ge=0.0, le=0.49, description="Delay of offbeat eighths, in beats.")
    ] = 0.0,
    tempo_bpm: TempoArg = 120.0,
    drum_map: Annotated[
        DrumPadMap | None,
        Field(default=None, description="Drum map from sound selection for the loaded kit; omit to use General MIDI notes."),
    ] = None,
) -> NoteSequence:
    """Generate kick, snare, and hat patterns in a style, without touching FL.

    Deterministic for a given seed. Without drum_map it uses General MIDI
    drum notes, which many FL kits do not follow; read the kit's pads with
    plugin_get_pad_map and plan sounds first so hits land on the right pads.
    Returns a note sequence in beats for piano_roll_write_notes."""
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
    name="compose_export_midi",
    annotations=FILE_MUTATING.model_copy(update={"title": "Export a MIDI file"}),
)
async def compose_export_midi(
    path: Annotated[
        str, Field(description="Absolute .mid or .midi output path whose folder already exists.")
    ],
    tracks: Annotated[
        list[MidiTrackSpec],
        Field(min_length=1, max_length=32, description="One entry per MIDI track: name, MIDI channel, and notes in beats."),
    ],
    tempo_bpm: TempoArg = 120.0,
    ppq: Annotated[int, Field(ge=24, le=9600, description="Ticks per quarter note.")] = 480,
    numerator: Annotated[int, Field(ge=1, le=32, description="Time-signature numerator.")] = 4,
    denominator: Annotated[
        Literal[1, 2, 4, 8, 16, 32], Field(description="Time-signature denominator.")
    ] = 4,
    overwrite: Annotated[
        bool, Field(description="Allow atomic replacement of an existing file.")
    ] = False,
) -> MidiExportReceipt:
    """Write note sequences to a standard Type-1 MIDI file and verify it.

    Writes a local file only; the FL project is unchanged. Re-opens and parses
    the file to verify its digest, tracks, and events. Refuses to replace an
    existing file unless overwrite=true. The user can drag the file into FL;
    use piano_roll_write_notes to put notes in the open project instead."""
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


# ---------------------------------------------------------------------------
# piano_roll: notes through FL's Piano Roll scripting
# ---------------------------------------------------------------------------


PianoRollPatternArg = Annotated[
    int,
    Field(ge=1, le=999, description="Pattern whose score to open; selected before the script runs."),
]


@mcp.tool(
    name="piano_roll_setup",
    annotations=WORKFLOW_STATE.model_copy(update={"title": "Set up Piano Roll scripting"}),
)
async def piano_roll_setup(
    action: Annotated[
        Literal["status", "prepare", "confirm"],
        Field(description="status reads readiness; prepare writes the script; confirm records that the user ran it."),
    ] = "status",
    confirm_user_ran_script: Annotated[
        bool,
        Field(description="Required for action=confirm, after the user ran Postfader Apply once in FL."),
    ] = False,
) -> PianoRollBridgeStatus:
    """Prepare the one-time Piano Roll scripting setup that note tools need.

    status reports readiness without writing files. prepare writes the
    Postfader Apply script into FL's Piano Roll scripts folder; the user must
    then run it once from FL's Piano Roll menu. confirm with
    confirm_user_ran_script=true arms this MCP process. Writes no notes. Reuse
    the setup for the rest of the session."""
    return await _mix(
        PIANO_ROLL.bridge_action,
        action,
        confirm_user_ran_script=confirm_user_ran_script,
    )


@mcp.tool(
    name="piano_roll_read_notes",
    annotations=WORKFLOW_STATE.model_copy(update={"title": "Read Piano Roll notes"}),
)
async def piano_roll_read_notes(
    channel_index: ChannelIndexArg,
    pattern_number: PianoRollPatternArg,
    offset: Annotated[
        int, Field(ge=0, le=1_000_000, description="Raw note index to start this page at.")
    ] = 0,
    limit: Annotated[int, Field(ge=1, le=2048, description="Raw note indices per page.")] = 512,
    selected_only: Annotated[
        bool, Field(description="Return only notes selected in FL within this page.")
    ] = False,
    session_fingerprint: SessionFingerprintArg = None,
) -> PianoRollNoteSnapshot:
    """Read the notes of one channel in one pattern: pitch, timing, and expression.

    Opens that score in FL's Piano Roll (which changes focus) but never changes
    notes or enables writes. Needs the piano_roll_setup step done once. Read
    before composing around existing material or transforming it. Follow
    next_offset to page; a selected_only page can be empty and still have a
    next_offset."""
    return await _mix(
        read_piano_roll_notes, channel_index=channel_index, pattern_number=pattern_number,
        offset=offset, limit=limit, selected_only=selected_only,
        session_fingerprint=session_fingerprint,
    )


@mcp.tool(
    name="piano_roll_write_notes",
    annotations=MUTATING.model_copy(update={"title": "Write Piano Roll notes"}),
)
async def piano_roll_write_notes(
    notes: Annotated[
        list[CreativeNote],
        Field(min_length=1, max_length=2048, description="Notes with pitch, start and duration in quarter-note beats, and velocity."),
    ],
    channel_index: ChannelIndexArg,
    pattern_number: PianoRollPatternArg,
    mode: Annotated[
        Literal["append", "replace"],
        Field(description="append keeps existing notes; replace clears the score first."),
    ] = "append",
    auto_trigger: Annotated[
        bool,
        Field(description="Select the target and run the script automatically; false leaves both to the user."),
    ] = True,
) -> PianoRollDispatch:
    """Write notes into one channel's Piano Roll score in a pattern.

    Requires write mode and the one-time piano_roll_setup. Writes a local
    script, selects the channel and pattern, and triggers FL's run-last-script
    shortcut, then checks the selected target, the script's application, and
    persistence. A dispatch alone is not proof the notes landed: inspect the
    returned evidence, and treat a missing receipt as an unknown outcome, not a
    reason to retry. Use piano_roll_transform_notes to edit existing notes.
    Does not save the project."""
    return await _mix(
        write_piano_roll_notes,
        notes,
        channel_index=channel_index,
        pattern_number=pattern_number,
        mode=mode,
        auto_trigger=auto_trigger,
    )


@mcp.tool(
    name="piano_roll_transform_notes",
    annotations=MUTATING.model_copy(update={"title": "Transform Piano Roll notes"}),
)
async def piano_roll_transform_notes(
    request: Annotated[
        PianoRollTransform,
        Field(description="Operation (quantize, transpose, humanize, duplicate, delete, or clear), scope (selected or all), and its settings."),
    ],
    channel_index: ChannelIndexArg,
    pattern_number: PianoRollPatternArg,
    auto_trigger: Annotated[
        bool,
        Field(description="Select the target and run the script automatically; false leaves both to the user."),
    ] = True,
) -> PianoRollDispatch:
    """Quantize, transpose, humanize, duplicate, delete, or clear existing notes.

    Applies to the selected notes or the whole score of one channel in one
    pattern. Requires write mode and the one-time piano_roll_setup; read the
    notes first with piano_roll_read_notes. delete and clear remove notes in
    scope. Works like piano_roll_write_notes: a dispatch is not proof, so
    inspect the evidence. Use piano_roll_write_notes to add new notes."""
    return await _mix(
        transform_piano_roll,
        request,
        channel_index=channel_index,
        pattern_number=pattern_number,
        auto_trigger=auto_trigger,
    )


# ---------------------------------------------------------------------------
# run: task-scoped Production Runs
# ---------------------------------------------------------------------------


RunRequestArg = Annotated[
    ProductionRunRequest,
    Field(
        description=(
            "The task: brief, scope, preservation rules, allowed change categories, "
            "completion target, and authorized_to_modify=true when the user asked "
            "for project changes."
        )
    ),
]

RunPlanArg = Annotated[
    ProductionRunPlan,
    Field(
        description=(
            "{plan_id, operations}: ordered operations, each with a unique "
            "operation_id; get each operation's fields from run_describe_operations."
        )
    ),
]


@mcp.tool(
    name="run_describe_operations",
    annotations=LOCAL_READ_ONLY.model_copy(
        update={"title": "Describe Production Run operations"}
    ),
)
async def run_describe_operations(
    operations: Annotated[
        tuple[ProductionOperationName, ...],
        Field(
            default=(),
            max_length=MAX_DESCRIBED_OPERATIONS,
            description=(
                "Operations whose exact JSON Schema you need before building a plan. "
                "Omit to list every operation with its summary and required fields."
            ),
        ),
    ] = (),
) -> OperationCatalog:
    """List Production Run operations and return exact schemas for the ones named.

    Read-only and offline. Plan schemas in run_execute, run_validate, and
    run_continue name operations without listing their fields, so call this
    first with the operations you will use (up to eight per call). Operations
    cover composing, writing notes, patterns, markers, automation, direct
    edits, sound palettes, presets, drum kits, effect processing, and review
    steps; save, render, plug-in insertion, and Playlist clips are refused."""
    return describe_operations(operations)


@mcp.tool(
    name="run_validate",
    annotations=READ_ONLY.model_copy(update={"title": "Validate a Production Run plan"}),
)
async def run_validate(
    request: RunRequestArg,
    plan: RunPlanArg,
    include_readiness: Annotated[
        bool,
        Field(description="Also return the setup readiness scorecard: blockers, limitations, and manual actions."),
    ] = False,
) -> ProductionRunCheck:
    """Dry-run a Production Run plan against the live project without changing it.

    Checks the plan's structure, operation order, capabilities, targets, and
    scope rules against the current FL session and returns whether it is
    executable, its blockers, and a plan digest. include_readiness adds the
    setup scorecard (Piano Roll setup, missing plug-ins, manual steps). Use it
    only when the user wants a plan reviewed or a diagnosis: run_execute
    performs the same checks itself, so do not validate before every run."""
    return await _mix(
        check_production_run, request, plan, include_readiness=include_readiness
    )


@mcp.tool(
    name="run_execute",
    annotations=MUTATING.model_copy(update={"title": "Execute a Production Run"}),
)
async def run_execute(request: RunRequestArg, plan: RunPlanArg) -> ProductionRunResult:
    """Execute a multi-step production plan in FL Studio as one task-scoped run.

    The main path for multi-stage work such as "write a chorus" or "build a
    beat": it validates the whole plan, checks readiness, enables write mode
    once for the run, executes operations in order with per-operation
    receipts, and releases write mode when finished. It stops at a blocker or
    an unknown outcome and reports the blocked operation and a next step;
    resume with run_continue. Mutating plans need authorized_to_modify=true.
    The run is saved locally and survives MCP restarts. Never saves the
    project."""
    return await _mix(PRODUCTION_RUNS.execute, request, plan)


@mcp.tool(
    name="run_continue",
    annotations=MUTATING.model_copy(update={"title": "Continue a Production Run"}),
)
async def run_continue(
    run_id: RunIdArg,
    delta: Annotated[
        ProductionRunDelta,
        Field(
            description=(
                "mode=resume with no operations continues the saved plan; append adds "
                "operations; replace_remaining replaces only the unexecuted rest. An "
                "optional updated request may narrow scope or change task policy."
            )
        ),
    ],
) -> ProductionRunResult:
    """Resume a stopped or blocked run, or change its unexecuted remainder.

    Use it after the user's follow-up or after fixing a blocker. Completed
    operations and their receipts are kept and never re-run; an operation with
    an unknown outcome is never replayed. Write mode is enabled for the run
    and released afterwards, like run_execute. Find saved runs with run_list."""
    return await _mix(PRODUCTION_RUNS.continue_run, run_id, delta)


@mcp.tool(
    name="run_stop",
    annotations=WORKFLOW_STATE.model_copy(update={"title": "Stop a Production Run"}),
)
async def run_stop(run_id: RunIdArg) -> ProductionRunResult:
    """Stop a run so no further operations execute.

    Completed changes stay in the project; nothing is undone (use
    project_step_history to undo). Returns the run's final state and
    receipts."""
    return await _mix(PRODUCTION_RUNS.stop, run_id)


@mcp.tool(
    name="run_get",
    annotations=READ_ONLY.model_copy(update={"title": "Get a Production Run"}),
)
async def run_get(run_id: RunIdArg) -> ProductionRunLookup:
    """Read a run's status, generated outputs, and per-operation receipts.

    Read-only; works for current runs and runs saved before an MCP restart.
    Use it to inspect what a run did, which operation blocked, and the outputs
    (note sequences, palettes, plans) that later operations or reviews can
    reference."""
    return await _mix(PRODUCTION_RUNS.get, run_id)


@mcp.tool(
    name="run_list",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "List Production Runs"}),
)
async def run_list(
    limit: Annotated[int, Field(ge=1, le=64, description="Maximum number of recent runs to list.")] = 64,
) -> tuple[ProductionRunSummary, ...]:
    """List recent Production Runs saved on this computer, newest first.

    Read-only; executes nothing. Use it after an MCP restart to find a run_id
    for run_get or run_continue."""
    return await _mix(list_production_runs, limit=limit)


# ---------------------------------------------------------------------------
# review: Creation Review, revision, and delivery
# ---------------------------------------------------------------------------


@mcp.tool(
    name="review_start",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Start a Creation Review"}),
)
async def review_start(
    request: Annotated[
        ReviewSessionRequest,
        Field(description="source_run_id of a completed Production Run, the brief, focus and preservation rules, revision-pass limit, and persistence policy."),
    ],
) -> ReviewSession:
    """Start a Review Session for the result of one completed Production Run.

    The session keeps the run's outputs, palette, processing receipts, and
    section map as evidence for evaluating bounces of that work. Changes no
    project state. Next, have the user export a full mix and attach it with
    review_attach_assets. Sessions are process-local unless persist_session
    is set."""
    return await _mix(start_creation_review, request)


@mcp.tool(
    name="review_attach_assets",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Attach review audio"}),
)
async def review_attach_assets(
    request: Annotated[
        ReviewAttachAssetsRequest,
        Field(description="Review Session and explicit full-mix, reference, stem, or section file paths."),
    ],
) -> ReviewSession:
    """Validate exported audio files and attach them to a Review Session.

    Paths must be explicit caller-selected files (audio_list_recent_bounces can
    find them). Checks format, size, stability, hashes, and alignment evidence;
    rejects directories, changing files, and duplicates. Changes no project
    state. Evaluate the attached full mix with review_evaluate."""
    return await _mix(attach_creation_review_assets, request)


@mcp.tool(
    name="review_evaluate",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Evaluate a review bounce"}),
)
async def review_evaluate(
    request: Annotated[
        ReviewEvaluateRequest,
        Field(description="Review Session, the attached asset set to measure, and optional authoritative section ranges."),
    ],
) -> CreationEvaluationReport:
    """Measure an attached bounce as a whole and section by section, with findings.

    Reads the files only; makes no FL changes. Returns findings tied to
    measurements and sections that review_plan_revision can address.
    Measurements support decisions; they never establish the producer's
    approval, which only review_record_feedback records."""
    return await _mix(evaluate_creation_review, request)


@mcp.tool(
    name="review_get",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Get a Creation Review"}),
)
async def review_get(
    review_session_id: ReviewSessionIdArg,
    view: Annotated[
        Literal["session", "export_request", "delivery_manifest"],
        Field(
            description=(
                "session: retained state, evidence, status, and the next action. "
                "export_request: the exact next full-mix export (and only necessary "
                "stems) to ask the user for. delivery_manifest: the current delivery "
                "view, without writing files."
            )
        ),
    ] = "session",
) -> ReviewSessionLookup | ExportHandoff | DeliveryManifest:
    """Read a Review Session, its next export request, or its delivery manifest.

    Read-only. Use view=session to see where the review stands and what to do
    next; view=export_request when the user must export a new bounce, to tell
    them exactly what to render; view=delivery_manifest to summarize what is
    done and what export or import work remains. Write the manifest to files
    with review_export_delivery."""
    if view == "export_request":
        return await _mix(build_review_export_handoff, review_session_id)
    if view == "delivery_manifest":
        return await _mix(build_review_delivery_manifest, review_session_id)
    return await _mix(get_creation_review, review_session_id)


@mcp.tool(
    name="review_compare",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Compare before and after bounces"}),
)
async def review_compare(
    request: Annotated[
        ReviewCompareRequest,
        Field(description="Review Session, the distinct aligned before and after assets, and the revision objective."),
    ],
) -> RevisionComparison:
    """Compare the bounces before and after a revision against its objective.

    Reads the files only. Reports what changed toward or away from the
    revision objective, using matching export settings. A better measurement
    is not producer approval; ask the user and record it with
    review_record_feedback."""
    return await _mix(compare_creation_revision, request)


@mcp.tool(
    name="review_plan_revision",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Plan a revision"}),
)
async def review_plan_revision(
    request: Annotated[
        ReviewPlanRevisionRequest,
        Field(description="Review Session, the revision request with its findings and locks, and a closed operation list."),
    ],
) -> RevisionPlan:
    """Compile one bounded revision plan from evaluation findings and feedback.

    Read-only: validates that each operation traces to a finding or feedback,
    respects the producer's locks on accepted sound, notes, rhythm, register,
    processing, level, placement, and roles, and stays within the pass limit.
    Records the plan in the session for review_apply_revision."""
    return await _mix(plan_creation_revision, request)


@mcp.tool(
    name="review_apply_revision",
    annotations=MUTATING.model_copy(update={"title": "Apply a revision"}),
)
async def review_apply_revision(
    request: Annotated[
        ReviewApplyRevisionRequest,
        Field(description="Review Session, the recorded revision_plan_id, and task-scoped authorization."),
    ],
) -> RevisionPass:
    """Apply one recorded revision plan to the project through a Production Run.

    Needs authorized_to_modify=true from a request to revise in this task. Runs
    one readiness preflight, enables write mode for the run, applies the
    plan's operations with receipts, and releases write mode. Stops on a
    blocker or unknown outcome. Then ask the user for a new export
    (review_get view=export_request) and compare it with review_compare."""
    return await _mix(apply_creation_revision, request)


@mcp.tool(
    name="review_record_feedback",
    annotations=WORKFLOW_STATE.model_copy(
        update={"title": "Record review feedback", "open_world_hint": False}
    ),
)
async def review_record_feedback(
    feedback: Annotated[
        CreationFeedback,
        Field(description="The producer's explicit structured feedback and any locks on accepted elements."),
    ],
) -> ReviewSession:
    """Record the producer's explicit feedback and locks in a Review Session.

    Feedback outranks measurements. Locks protect accepted elements (sound,
    notes, rhythm, register, processing, level, placement, role) from later
    revisions. Changes no project state. Silence and good measurements never
    count as approval. Use sound_record_feedback for feedback on a sound
    palette."""
    return await _mix(record_creation_review_feedback, feedback)


@mcp.tool(
    name="review_stop",
    annotations=WORKFLOW_STATE.model_copy(
        update={"title": "Stop a Creation Review", "open_world_hint": False}
    ),
)
async def review_stop(review_session_id: ReviewSessionIdArg) -> ReviewSession:
    """Stop a Review Session so no further revision work happens.

    Completed project changes stay; nothing is undone. The session's record
    remains readable with review_get; use review_delete to remove it."""
    return await _mix(stop_creation_review, review_session_id)


@mcp.tool(
    name="review_delete",
    annotations=WORKFLOW_STATE.model_copy(
        update={
            "title": "Delete a Creation Review",
            "destructive_hint": True,
            "open_world_hint": False,
        }
    ),
)
async def review_delete(
    review_session_id: ReviewSessionIdArg,
    confirm: Annotated[
        bool,
        Field(description="Must be true, after the user explicitly asked to delete this review's record."),
    ],
) -> ReviewDeleteResult:
    """Delete one Review Session's stored record after the user asks to.

    Removes the session's metadata from the local store. Audio files and the FL
    project are untouched. Cannot be undone."""
    return await _mix(delete_creation_review, review_session_id, confirm=confirm)


@mcp.tool(
    name="review_export_delivery",
    annotations=FILE_MUTATING.model_copy(update={"title": "Write delivery files"}),
)
async def review_export_delivery(
    request: Annotated[
        ReviewDeliveryExportRequest,
        Field(description="Review Session, formats (json and/or markdown), and an optional output folder."),
    ],
) -> ReviewDeliveryExportResult:
    """Write a Review Session's delivery manifest to new JSON or Markdown files.

    Creates new local files only and never overwrites an existing one; the FL
    project is not saved or changed. Preview the content first with review_get
    view=delivery_manifest."""
    return await _mix(export_review_delivery_manifest, request)


# ---------------------------------------------------------------------------
# render: saved-project WAV renders
# ---------------------------------------------------------------------------


JobIdArg = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{32}$", description="Render job ID from render_start_job in this MCP process."),
]


@mcp.tool(
    name="render_start_job",
    annotations=WORKFLOW_STATE.model_copy(update={"title": "Render a saved project"}),
)
async def render_start_job(
    request: Annotated[
        SavedProjectRenderRequest,
        Field(description="Absolute path of a saved .flp, a parent folder for the new job directory, and an optional timeout."),
    ],
) -> SavedProjectRenderJob:
    """Render a saved .flp to a WAV with FL's command-line exporter, as a background job.

    Starts a separate FL process; the open project and its unsaved changes are
    not included, because only saved state renders. Returns a job_id at once;
    poll render_get_job for the output. Jobs live in this MCP process. Use the
    finished WAV with the audio_* or review_* tools."""
    return await _mix(get_saved_project_render_jobs().start, request)


@mcp.tool(
    name="render_get_job",
    annotations=LOCAL_READ_ONLY.model_copy(update={"title": "Get a render job"}),
)
async def render_get_job(job_id: JobIdArg) -> SavedProjectRenderJob:
    """Read a render job's status and, once ready, its decoded WAV details.

    Read-only. output_ready means the WAV decoded fully and is usable;
    completed also means the FL process has exited."""
    return await _mix(get_saved_project_render_jobs().status, job_id)


@mcp.tool(
    name="render_cancel_job",
    annotations=WORKFLOW_STATE.model_copy(update={"title": "Cancel a render job"}),
)
async def render_cancel_job(job_id: JobIdArg) -> SavedProjectRenderJob:
    """Cancel a render job: stop monitoring it and the process it started.

    On macOS the separate FL renderer may stay open after cancellation; check
    render_get_job for the final status."""
    return await _mix(get_saved_project_render_jobs().cancel, job_id)


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

Writes start off. Ask the connected AI to make your changes; a Production Run
enables writes once for that task. Individual setters can use the session
write-mode tool. FL Studio does not need to restart.

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
        try:
            mcp.run(transport="stdio")
        finally:
            shutdown_saved_project_render_jobs()
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
