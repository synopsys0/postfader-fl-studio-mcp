"""The MCP server for FL Studio 2026.  This is the default agent entry point.

Five surfaces, and nothing else is reachable from here:

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
from typing import Annotated

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
    VerifiedMixerEqWrite,
    VerifiedMixerMuteWrite,
    VerifiedMixerNameWrite,
    VerifiedMixerPanWrite,
    VerifiedMixerSendLevelWrite,
    VerifiedMixerSendWrite,
    VerifiedMixerVolumeWrite,
    VerifiedPluginDisplayWrite,
    VerifiedPluginOptionWrite,
    VerifiedPluginParameterWrite,
    WriteModeChange,
)
from .readonly_inspector import ReadOnlyInspector
from .performance import TrackBController, TrackBInspector
from .track_b_contracts import (
    ChannelList,
    ExpectedChannelIdentityState,
    ExpectedChannelMixState,
    ExpectedChannelRouteState,
    ExpectedChannelTargetState,
    ExpectedLoopModeState,
    ExpectedPlayingState,
    ExpectedSongPositionState,
    ExpectedStopState,
    ExpectedTempoState,
    LiveNoteDispatch,
    MAX_VERIFIED_STEP_COUNT,
    PluginTarget,
    StepCellUpdate,
    StepSequenceObservation,
    TargetedLoadedPluginInventory,
    TargetedPluginParameterPage,
    TargetedPluginParameterScan,
    VerifiedChannelIdentityWrite,
    VerifiedChannelMixWrite,
    VerifiedChannelRouteWrite,
    VerifiedLoopModeWrite,
    VerifiedPlayingWrite,
    VerifiedSongPositionWrite,
    VerifiedStepSequenceWrite,
    VerifiedStopWrite,
    VerifiedTargetedPluginDisplayWrite,
    VerifiedTargetedPluginOptionWrite,
    VerifiedTargetedPluginParameterWrite,
    VerifiedTempoWrite,
)
from .verified_writer import VerifiedWriter, WriteModeManager


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


INSTRUCTIONS = """\
FL Studio 2026 project inspector with a narrow verified control surface.

This server can observe the running project, mixer tracks, routing, loaded
effects and generators, transport state, global Channel Rack state, the current
pattern's step grid, and exposed plug-in parameters. It can apply absolute,
readback-verified transport, mixer, channel, routing, step, and parameter
targets. fl_set_plugin_param takes a normalised 0..1.
fl_set_plugin_param_display takes the number the plug-in itself shows, so
"Attack to 20 ms" needs no knowledge of the curve.
fl_set_plugin_param_option takes the text of an enumerated control such as Key
or Scale, and also reports every option it found. Prefer the latter two: real
third-party controls frequently have no name at all and are identified only
by what they display. Plug-in insertion, removal, and reordering are unavailable
through the public MIDI scripting backend. It cannot render audio, save
projects, write automation, undo, set playback speed without a verifiable
getter, or invoke arbitrary FL API functions.

Two FL limits shape this surface. A send's level cannot be set before the send
exists, so create the route with fl_set_mixer_send first. And FL ignores both
of its per-effect-slot controls when a script drives them -- the slot mute
cannot be undone and the wet/dry mix never moves -- so there is no tool for
bypassing or blending an individual plug-in. Change the plug-in's own
parameters instead, or the track's send levels.

The state-setting tools apply the change and report what happened; they never ask
first. Every one of them reads FL back on a later idle tick and returns
`verified`. Treat `verified: false` as the headline of that result, not a
footnote: FL genuinely accepts writes it then ignores, so an unverified write
means the control may not have moved. Mixer and plug-in parameter handlers may
repeat an FL-facing setter inside one dispatched command because FL drops a
lone call; transport, direct Channel Rack state, routing, and step setters are
issued once. No mutating command is dispatched again after an ambiguous
transport outcome, an unverified result is not retried for the caller, and
nothing that landed is rolled back. Re-read the target before deciding what to
do next.

Every mutation reports `undo_point_created`. Commands that ask FL for an undo
point report whether one was observed by watching its undo history; transient
transport actions and live-note dispatch truthfully return null. Treat false or
null as "this may not be undoable with Ctrl+Z". The project is never saved.
`fl_trigger_note` is a separate bounded note-on/note-off dispatch receipt. It
has no authoritative state readback, returns no `verified` field, and must not
be presented as a verified mutation or proof that sound was produced.

Writes are dispatchable only while the bridge reports verified write mode. It
starts read-only by default. When the user explicitly asks to enable writes,
call fl_set_write_mode with enabled=true and confirm_user_present=true. That
changes only the current loaded bridge session, never saves the project, and is
proved with a second handshake; call it with enabled=false to lock writes again.
A write also refuses when the running bridge source hash does not match this
package. Optional session and expected-before guards let callers reject a
decision made against stale state.

Call fl_get_capabilities before relying on a feature. Treat every unprofiled
plug-in parameter as unsafe to modify, even though its normalized value can be
read. Display text is optional because Image-Line documents it as supported by
only some plug-ins.

fl_get_selected_range preserves repeated raw endpoints and project PPQ only.
Those values do not establish a time-signature denominator or a complete
marker map. Therefore state, presence, units, and normalized ticks remain
unknown/null for every ordinary live result. Render endpoint inclusivity
remains unknown and every response is structurally marked unsafe for rendering.

Step reads can observe up to 512 cells. Verified step writes refuse grids over
256 cells and require step_count + update_count + 8 <= 320 so the final digest
recheck and batch remain atomic below the idle-tick call ceiling. A 256-cell
grid therefore permits at most 56 updates per call. Split larger edits and call
fl_get_step_sequence again for a fresh digest between successful batches.

The server is pinned to FL Studio 2026 version 26.1.3 build 5336 or newer. It
will refuse project inspection if the shared bridge is attached to FL Studio
2025.

The audio_* tools answer the questions the FL API cannot: they measure a
rendered file on disk. Bounce a stem, mix, or instrumental from FL, then call
audio_analyze_file, audio_compare_files, or audio_analyze_masking on absolute
paths, or audio_find_recent_bounces to see what FL wrote most recently. These
tools return measurements, provenance, the analyzer's own confidence, and its
limitations. They never rank a mix, choose a candidate, or emit a processing
instruction; interpreting the numbers against the observed mixer state is the
agent's job.
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
    """Capture a compact project/mixer/effect report for Phase 1 validation."""
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
    name="fl_list_channels",
    annotations=READ_ONLY.model_copy(update={"title": "List Channel Rack channels"}),
)
async def fl_list_channels() -> ChannelList:
    """List globally addressed channels, mix state, routing, and generator identity."""
    return await _performance_read("list_channels")


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


USAGE = """\
Postfader - unofficial local MCP server for FL Studio 2026

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
virtual endpoint in FL Studio. Postfader never installs a virtual MIDI driver.

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
