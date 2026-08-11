"""The MCP server for FL Studio 2026.  This is the default agent entry point.

Three surfaces, and nothing else is reachable from here:

* **Reads** over the live project, through the fail-closed inspector allowlist.
* **Measurements** of rendered audio files, because the FL API exposes no
  audio. Those tools read files the caller names, plus a bounded lookup over
  FL Studio's usual output and project folders; the discovery roots are fixed
  in code and cannot be chosen by an agent.
* **Ten verified writes** -- a mixer track's volume, pan, mute state, name, one
  band of its built-in EQ, one send, that send's level, and one plug-in
  parameter addressed by index, by displayed units, or by option text.  Each
  changes exactly one thing, reads FL back on a *later* idle tick, and reports
  ``verified`` from that readback alone.

There are still no transport controls, no undo command, no render, no
project save, no caller-directed filesystem search, and no reflective FL API
escape hatch.

The write tools apply and report; there is no confirmation round-trip and no
rollback ceremony.  FL's own undo is the safety net, and whether a point
actually appeared is reported as ``undo_point_created`` rather than assumed.  They are dispatchable only when FL Studio itself was launched with
``FL_BRIDGE_ENABLE_WRITES=1``; when it was not, they refuse locally with
:class:`~fl_studio_mcp.verified_writer.VerifiedWritesUnavailable`, which names
the flag, rather than surfacing a raw bridge dispatch rejection.
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
)
from .readonly_inspector import ReadOnlyInspector
from .verified_writer import VerifiedWriter


# The upstream MCP SDK's generated function-argument models ignore unknown
# properties by default. Tighten the shared base before any tools are
# registered so misspelled or unexpected agent input fails closed. Tests pin
# this behavior because it is part of this server's safety boundary.
ArgModelBase.model_config = ConfigDict(
    arbitrary_types_allowed=True,
    extra="forbid",
)
ArgModelBase.model_rebuild(force=True)


INSTRUCTIONS = """\
FL Studio 2026 project inspector with a narrow verified write surface.

This server can observe the running project, mixer tracks, routing, loaded
effects, transport state, and exposed plug-in parameters. It can change eight
things on one mixer track -- volume, pan, mute state, name, one band of the
built-in EQ, whether it sends to another track, how much it sends there, and
one parameter of one loaded plug-in -- and a plug-in parameter can be addressed
three ways. fl_set_plugin_param takes a normalised 0..1.
fl_set_plugin_param_display takes the number the plug-in itself shows, so
"Attack to 20 ms" needs no knowledge of the curve.
fl_set_plugin_param_option takes the text of an enumerated control such as Key
or Scale, and also reports every option it found. Prefer the latter two: real
third-party controls frequently have no name at all and are identified only
by what they display. It cannot add, remove or reorder plug-ins
-- FL's scripting API has no function for it -- and it cannot control playback,
render audio, save projects, write automation, undo, or invoke arbitrary FL API
functions.

Two FL limits shape this surface. A send's level cannot be set before the send
exists, so create the route with fl_set_mixer_send first. And FL ignores both
of its per-effect-slot controls when a script drives them -- the slot mute
cannot be undone and the wet/dry mix never moves -- so there is no tool for
bypassing or blending an individual plug-in. Change the plug-in's own
parameters instead, or the track's send levels.

The fl_set_* tools apply the change and report what happened; they never ask
first. Every one of them reads FL back on a later idle tick and returns
`verified`. Treat `verified: false` as the headline of that result, not a
footnote: FL genuinely accepts writes it then ignores, so an unverified write
means the control may not have moved. The setter is repeated inside a single
write because FL drops a lone one, but an unproven write is not replayed
afterwards and nothing is rolled back. Re-read the track before deciding what
to do next. Each write
asks FL for one undo point and reports `undo_point_created`, observed by
watching FL's undo history rather than assumed -- treat false or null as "this
may not be undoable with Ctrl+Z". The project is never saved.

Writes are dispatchable only when FL Studio itself was launched with
FL_BRIDGE_ENABLE_WRITES=1. Without it the write tools fail with an error naming
that flag, and reading still works normally.

Call fl_get_capabilities before relying on a feature. Treat every unprofiled
plug-in parameter as unsafe to modify, even though its normalized value can be
read. Display text is optional because Image-Line documents it as supported by
only some plug-ins.

fl_get_selected_range preserves repeated raw endpoints and project PPQ only.
Those values do not establish a time-signature denominator or a complete
marker map. Therefore state, presence, units, and normalized ticks remain
unknown/null for every ordinary live result. Render endpoint inclusivity
remains unknown and every response is structurally marked unsafe for rendering.

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
#   asks FL for an undo point and reports whether one was observed, but an undo
#   point is not guaranteed and does not make the mutation non-destructive.
# * idempotentHint=True, because every one of them states an absolute value --
#   including mute, which is a stated state and never a toggle -- so applying
#   the same call twice leaves FL in the same place as applying it once.
# * openWorldHint=True, because the outcome depends on a live FL Studio.
MUTATING = ToolAnnotations(
    title="Change FL Studio state",
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
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
    successful-looking result: VerifiedWritesUnavailable when FL was not
    launched with the write flag, IncompatibleFLStudio when the handshake is
    wrong, ValueError for a value this layer rejected before the bridge saw it.
    A write that FL accepted and ignored is not an error -- it returns normally
    with verified=false.
    """

    def invoke():
        writer = VerifiedWriter()
        return getattr(writer, method_name)(**arguments)

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
) -> LoadedPluginInventory:
    """Return every effect currently loaded on the observed mixer tracks."""
    return await _run("scan_loaded_plugins", only_used=only_used)


@mcp.tool(
    name="plugins_inspect_parameter_map",
    annotations=READ_ONLY.model_copy(update={"title": "Inspect plug-in parameters"}),
)
async def plugins_inspect_parameter_map(
    track_index: Annotated[
        int,
        Field(description="Zero-based mixer track index.", ge=0),
    ],
    slot_index: Annotated[
        int,
        Field(description="Zero-based effect slot index (0 through 9).", ge=0, le=9),
    ],
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
) -> PluginParameterPage:
    """Read a bounded page of exposed parameters; never marks unknown controls safe."""
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
        int,
        Field(description="Zero-based mixer track index.", ge=0),
    ],
    slot_index: Annotated[
        int,
        Field(description="Zero-based effect slot index (0 through 9).", ge=0, le=9),
    ],
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
) -> PluginParameterScan:
    """De-pad a whole plug-in in one call; prefer this over paging a VST.

    FL reports a padded maximum rather than a parameter count for VST plug-ins
    -- often thousands of slots for a VST3 -- with the real controls sparse
    inside it.
    Paging that with `plugins_inspect_parameter_map` is about a thousand round
    trips. This walks the range inside FL and returns only what is real, with
    each control's display string, which is what actually identifies it.

    Check `truncated` before treating the result as the whole plug-in.
    """
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
# verified writes
#
# Each tool applies the change and reports; there is no confirmation
# round-trip. The returned model always carries `verified`, which the bridge
# decided by reading FL back on a later idle tick, plus a
# `verification_summary` and -- when unverified -- a leading warning. None of
# these tools raises because a write went unverified: that is a real outcome
# about the user's project and it is reported, not hidden behind an exception.
# ---------------------------------------------------------------------------


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
) -> VerifiedMixerVolumeWrite:
    """Set one mixer fader and report the readback FL gave on a later tick."""
    return await _write(
        "set_mixer_volume",
        track_index=track_index,
        volume_normalized=volume_normalized,
        allow_master=allow_master,
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
) -> VerifiedMixerPanWrite:
    """Set one mixer pan and report the readback FL gave on a later tick."""
    return await _write(
        "set_mixer_pan",
        track_index=track_index,
        pan=pan,
        allow_master=allow_master,
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
) -> VerifiedMixerMuteWrite:
    """Set one track's mute state and report the readback FL gave on a later tick."""
    return await _write(
        "set_mixer_mute",
        track_index=track_index,
        muted=muted,
        allow_master=allow_master,
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
) -> VerifiedMixerEqWrite:
    """Set gain and/or frequency on one built-in EQ band; at least one is required."""
    return await _write(
        "set_mixer_eq",
        track_index=track_index,
        band_index=band_index,
        gain_normalized=gain_normalized,
        frequency_normalized=frequency_normalized,
        allow_master=allow_master,
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
    )


@mcp.tool(
    name="fl_set_plugin_param_display",
    annotations=MUTATING.model_copy(
        update={"title": "Set a plug-in parameter in its own units"}
    ),
)
async def fl_set_plugin_param_display(
    track_index: Annotated[
        int,
        Field(description="Zero-based mixer index. Index 0 is Master.", ge=0),
    ],
    slot_index: Annotated[
        int,
        Field(description="Zero-based effect slot index (0 through 9).", ge=0, le=9),
    ],
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
) -> VerifiedPluginDisplayWrite:
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
    return await _write(
        "set_plugin_parameter_display",
        track_index=track_index,
        slot_index=slot_index,
        parameter=parameter,
        target_value=target_value,
        tolerance=tolerance,
        allow_master=allow_master,
    )


@mcp.tool(
    name="fl_set_plugin_param_option",
    annotations=MUTATING.model_copy(
        update={"title": "Set an enumerated plug-in parameter"}
    ),
)
async def fl_set_plugin_param_option(
    track_index: Annotated[
        int,
        Field(description="Zero-based mixer index. Index 0 is Master.", ge=0),
    ],
    slot_index: Annotated[
        int,
        Field(description="Zero-based effect slot index (0 through 9).", ge=0, le=9),
    ],
    parameter: Annotated[
        int | str,
        Field(description="Parameter index, or text matched against names and displays."),
    ],
    option: Annotated[
        str,
        Field(description="The option text to land on, e.g. 'A', 'Major', 'Low Male'."),
    ],
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
) -> VerifiedPluginOptionWrite:
    """Set a parameter that shows words rather than numbers: Key, Scale, Input Type.

    Use this where `fl_set_plugin_param_display` refuses. That tool searches on
    a number, and an enumeration has none.

    **This moves the control while it looks.** FL cannot report a control's
    options, so the only way to find them is to walk the parameter across its
    range and read what it displays. If the requested option does not exist,
    the original value is restored before the error, and the error lists every
    option that was found.

    The result carries `options` -- the whole enumeration, in order -- so one
    call is also how you discover what a control accepts.
    """
    return await _write(
        "set_plugin_parameter_option",
        track_index=track_index,
        slot_index=slot_index,
        parameter=parameter,
        option=option,
        sweep_steps=sweep_steps,
        allow_master=allow_master,
    )


@mcp.tool(
    name="fl_set_plugin_param",
    annotations=MUTATING.model_copy(update={"title": "Set one plug-in parameter"}),
)
async def fl_set_plugin_param(
    track_index: Annotated[
        int,
        Field(
            description="Zero-based mixer index. Index 0 is Master and is refused unless allow_master is true.",
            ge=0,
        ),
    ],
    slot_index: Annotated[
        int,
        Field(description="Zero-based effect slot index (0 through 9).", ge=0, le=9),
    ],
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
    allow_master: Annotated[
        bool,
        Field(description="Deliberately target the master bus at index 0."),
    ] = False,
) -> VerifiedPluginParameterWrite:
    """Set one plug-in parameter; verified from FL's display string changing."""
    return await _write(
        "set_plugin_parameter",
        track_index=track_index,
        slot_index=slot_index,
        parameter_index=parameter_index,
        normalized_value=normalized_value,
        allow_master=allow_master,
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
Postfader - unofficial local MCP server for FL Studio 2026 (macOS)

Usage:
  fl-studio-mcp              Serve the Model Context Protocol over stdio.
  fl-studio-mcp --help       Show this message.
  fl-studio-mcp --version    Print the version.

This command speaks MCP on stdin/stdout and is meant to be launched by an MCP
client, not run interactively -- on its own it will appear to hang, because it
is waiting for a client to say something. Register it with your client instead,
using the absolute path to this interpreter. For Claude Code, registering it at
user scope makes it available in every project rather than only in the
connector's own checkout:

  claude mcp add fl-studio --scope user --env FL_BRIDGE_ENABLE_MIDI=1 --
      /absolute/path/to/postfader-fl-studio-mcp/.venv/bin/python
      -m fl_studio_mcp.mcp_server

(one line; wrapped here to fit. --scope user is the part that matters: a
project-scoped entry only loads inside its own directory.)

Writes are off unless FL Studio itself was launched with
FL_BRIDGE_ENABLE_WRITES=1.

Setup checks (scripts/doctor.py, scripts/inspect_readonly.py) live in the
source repository, not in the installed package, because they configure and
probe a local FL Studio install rather than serve MCP. Clone the repository to
use them: https://github.com/synopsys0/postfader-fl-studio-mcp
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
