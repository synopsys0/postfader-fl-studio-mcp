"""End-to-end MCP-over-stdio test for the default read-only server."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import time

from _checks import check, section, summary


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "fakefl"))
sys.path.insert(0, os.path.join(ROOT, "fl_studio_mcp", "_bridge"))
sys.path.insert(0, ROOT)

import _state  # noqa: E402
import device_UniversalBridge as bridge  # noqa: E402

from fl_studio_mcp.bridge_install import expected_bridge_deployment  # noqa: E402


# The installed bridge carries this stamp. The source-tree fixture has the
# installer placeholder, so inject the digest a real installation reports.
bridge.BRIDGE_SOURCE_SHA256 = expected_bridge_deployment()[1]

# No deterministic process may contact the production bridge port.  The
# kernel-selected listener is passed explicitly to the child MCP process.
bridge.PORT = 0

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


STOP = threading.Event()
MAILBOX = tempfile.mkdtemp(prefix="flmcp-readonly-e2e-")
BRIDGE_PORT = None

EXPECTED_RESOURCES = {
    "fl://capabilities",
    "fl://status",
    "fl://project",
    "fl://transport",
    "fl://mixer",
    "fl://channels",
    "fl://plugins",
    "fl://patterns",
}
# One minimal, in-range call per write tool. This fake bridge is pumped
# in-process without FL_BRIDGE_ENABLE_WRITES, so each of these must be refused
# by name rather than reaching FL.
WRITE_CALLS = {
    "project_apply_edits": {
        "operations": [
            {
                "operation_id": "volume-1",
                "operation": "mixer_volume",
                "track_index": 3,
                "volume_normalized": 0.65,
            }
        ]
    },
    "project_step_history": {"direction": "undo"},
    "mixer_set_track": {
        "track_index": 3,
        "name": "Lead Verb",
        "color": 0x0055AA,
        "volume_db": -6.0,
        "pan": -0.4,
        "stereo_separation": 0.35,
        "muted": True,
        "soloed": True,
        "armed": True,
        "eq": [{"band_index": 1, "gain_normalized": 0.7}],
        "sends": [
            {"destination_track_index": 5, "enabled": True, "level_normalized": 0.5}
        ],
        "select": True,
    },
    "plugin_set_parameter": {
        "target": {"kind": "mixer_effect", "track_index": 3, "slot_index": 1},
        "parameter": 0,
        "normalized_value": 0.3,
    },
    "transport_set": {
        "tempo_bpm": 128.0,
        "time_signature_numerator": 3,
        "loop_mode": "pattern",
        "metronome": True,
        "precount": True,
        "position_normalized": 0.25,
        "recording": True,
        "playing": True,
    },
    "channel_set": {
        "channel_index": 0,
        "name": "Demo",
        "mixer_destination": 3,
        "volume_normalized": 0.7,
        "soloed": True,
        "pitch_normalized": 0.25,
        "select": True,
    },
    "pattern_set": {"pattern_number": 1, "name": "Intro", "length_beats": 8, "select": True},
    "playlist_set_track": {"track_index": 1, "name": "Vocals", "muted": True},
    "channel_set_steps": {
        "pattern_number": 1,
        "channel_index": 0,
        "expected_digest": "0" * 64,
        "updates": [{"step_index": 0, "enabled": True}],
    },
    "channel_play_note": {"channel_index": 0, "note": 60, "velocity": 100},
}
PRESET_WRITE_CALLS = {
    "plugin_select_preset": {
        "target": {"kind": "mixer_effect", "track_index": 3, "slot_index": 1},
        "preset_name": "Preset 1",
    },
}


def pump_bridge():
    bridge.OnInit()
    while not STOP.is_set():
        bridge.OnIdle()
        time.sleep(0.005)
    bridge.OnDeInit()


def payload(result):
    if getattr(result, "structured_content", None):
        body = result.structured_content
        return body.get("result", body)
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    return None


def resource_payload(result):
    for content in result.contents:
        text = getattr(content, "text", None)
        if isinstance(text, str):
            return json.loads(text)
    return None


def fingerprint():
    return copy.deepcopy(
        {
            "tracks": [
                (
                    track.name,
                    track.volume,
                    track.pan,
                    track.stereo_sep,
                    track.muted,
                    track.solo,
                    track.armed,
                    track.selected,
                    track.routes,
                    track.eq,
                    {
                        slot: (plugin.name, plugin.param_names, plugin.values)
                        for slot, plugin in track.slots.items()
                    },
                )
                for track in _state.TRACKS
            ],
            "channels": [
                {
                    **{
                        key: value
                        for key, value in vars(channel).items()
                        if key != "generator_plugin"
                    },
                    "generator_plugin": {
                        "name": channel.generator_plugin.name,
                        "names": channel.generator_plugin.param_names,
                        "values": channel.generator_plugin.values,
                        "presets": channel.generator_plugin.presets,
                        "current_preset": channel.generator_plugin.current_preset,
                        "pads": channel.generator_plugin.pads,
                    },
                }
                for channel in _state.CHANNELS
            ],
            "undo": _state.UNDO,
            "playing": _state.PLAYING,
            "recording": _state.RECORDING,
            "position": _state.SONG_POS,
            "selection": (_state.SELECTION_START, _state.SELECTION_END),
            "loop_mode": _state.LOOP_MODE,
            "ppq": _state.REC_PPQ,
        }
    )


async def run():
    if BRIDGE_PORT is None:
        raise RuntimeError("ephemeral fake-bridge port was not captured")
    _state.REC_PPQ = 192
    _state.SELECTION_START = 576
    _state.SELECTION_END = 1344
    _state.LOOP_MODE = 1
    before = fingerprint()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "fl_studio_mcp.mcp_server"],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": ROOT,
            "FL_BRIDGE_PORT": str(BRIDGE_PORT),
            "FL_BRIDGE_MAILBOX": MAILBOX,
            "FL_BRIDGE_ENABLE_MIDI": "0",
        },
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            section("stdio handshake, tool listing and live resources")
            initialized = await session.initialize()
            check(
                "server initialises",
                initialized.server_info.name == "postfader-fl-studio-mcp",
                initialized.server_info,
            )
            tools = (await session.list_tools()).tools
            missing = (set(WRITE_CALLS) | set(PRESET_WRITE_CALLS)) - {
                tool.name for tool in tools
            }
            check(
                "every write tool exercised below is listed over stdio",
                not missing,
                sorted(missing),
            )
            resources = (await session.list_resources()).resources
            resource_uris = {str(resource.uri) for resource in resources}
            check(
                "exactly the live FL resources are exposed",
                resource_uris == EXPECTED_RESOURCES,
                sorted(resource_uris ^ EXPECTED_RESOURCES),
            )
            capabilities_resource = resource_payload(
                await session.read_resource("fl://capabilities")
            )
            check(
                "capabilities resource uses the live compatible bridge",
                capabilities_resource["connection"]["compatible"] is True,
                capabilities_resource,
            )
            session_fingerprint = capabilities_resource["connection"][
                "session_fingerprint"
            ]
            status_resource = resource_payload(
                await session.read_resource("fl://status")
            )
            check(
                "status resource combines project, transport, and write mode",
                status_resource["project_title"] == "Synthetic Test Project"
                and status_resource["transport"]["playing"] is False
                and status_resource["verified_writes_enabled"] is False,
                status_resource,
            )
            mixer_resource = resource_payload(
                await session.read_resource("fl://mixer")
            )
            check(
                "mixer resource returns the bounded authoritative inventory",
                mixer_resource["total_track_count"] == 126
                and len(mixer_resource["tracks"]) == 126,
                mixer_resource,
            )
            channels_resource = resource_payload(
                await session.read_resource("fl://channels")
            )
            check(
                "channel resource preserves global channel scope",
                channels_resource["total_channel_count"]
                == len(channels_resource["channels"])
                and all(
                    channel["index_scope"] == "global"
                    for channel in channels_resource["channels"]
                ),
                channels_resource,
            )
            plugins_resource = resource_payload(
                await session.read_resource("fl://plugins")
            )
            check(
                "plugin resource includes target-aware loaded plug-ins",
                bool(plugins_resource["plugins"])
                and all(
                    plugin["target"]["kind"]
                    in {"mixer_effect", "channel_generator"}
                    for plugin in plugins_resource["plugins"]
                ),
                plugins_resource,
            )
            patterns_resource = resource_payload(
                await session.read_resource("fl://patterns")
            )
            check(
                "pattern resource reports the live current pattern",
                patterns_resource["current_pattern_number"] == 1
                and patterns_resource["patterns"][0]["current"] is True,
                patterns_resource,
            )
            section("read tools over stdio")
            project = payload(await session.call_tool("project_get_summary", {}))
            check("FL 2026 version gate passed", project["connection"]["compatible"], project)
            check(
                "fixture project title returned",
                project["project_title"] == "Synthetic Test Project",
            )

            selection = payload(await session.call_tool("playlist_get_selection", {}))
            check(
                "selection preserves raw PPQ-192 observation",
                selection["raw_start_time"] == 576
                and selection["raw_end_time"] == 1344
                and selection["timebase_ppq"] == 192
                and selection["raw_time_unit"] == "unknown"
                and selection["selection_state"] == "unknown"
                and selection["selection_presence"] == "unknown"
                and selection["start_ticks"] is None
                and selection["end_ticks"] is None
                and selection["duration_ticks"] is None,
                selection,
            )
            check(
                "raw selection remains semantically unvalidated and render-unsafe",
                selection["interpretation_status"] == "unvalidated"
                and selection["semantic_scope"] is None
                and selection["render_endpoint_inclusivity"] == "unknown"
                and selection["safe_for_rendering"] is False,
                selection,
            )
            invalid_selection = await session.call_tool(
                "playlist_get_selection", {"unexpected": True}
            )
            check(
                "selection tool rejects extra input",
                bool(getattr(invalid_selection, "is_error", False)),
                invalid_selection,
            )

            mixer = payload(await session.call_tool("mixer_list_tracks", {}))
            check("mixer tracks returned", mixer["total_track_count"] == 126, mixer)
            check("loaded effects identified", any(t["plugins"] for t in mixer["tracks"]))

            # The de-padded whole-plug-in walk. It goes through the same
            # gateway allowlist, so a missing entry fails here rather than
            # only against live FL.
            scan = payload(
                await session.call_tool(
                    "plugin_list_parameters",
                    {"target": {"kind": "mixer_effect", "track_index": 3, "slot_index": 1}},
                )
            )
            check(
                "scan returns only real controls",
                scan["real_count"] == len(scan["parameters"]),
                scan,
            )
            check(
                "scan accounts for every index it examined",
                scan["real_count"] + scan["padding_skipped"] == scan["examined_count"],
                scan,
            )
            check(
                "scanned controls are still unsafe to modify",
                all(not item["safe_to_modify"] for item in scan["parameters"]),
                scan,
            )
            bounded = payload(
                await session.call_tool(
                    "plugin_list_parameters",
                    {
                        "target": {"kind": "mixer_effect", "track_index": 3, "slot_index": 1},
                        "max_indices": 2,
                    },
                )
            )
            check(
                "a bounded scan says it is partial",
                bounded["truncated"] and bounded["truncated_by"] == "max_indices",
                bounded,
            )

            section("writes are refused while the bridge is read-only")
            # This bridge starts read-only. Every project write must refuse
            # over the wire by naming the user-confirmed mode tool, rather than
            # surfacing a raw dispatch rejection or changing anything.
            for name, arguments in WRITE_CALLS.items():
                refusal = await session.call_tool(name, arguments)
                text = " ".join(
                    block.text
                    for block in refusal.content
                    if getattr(block, "type", None) == "text"
                )
                check(
                    "%s refused before write mode was enabled" % name,
                    bool(getattr(refusal, "is_error", False))
                    and "session_set_write_mode" in text
                    and "confirm_user_present=true" in text,
                    text,
                )

            # Exact preset selection is a verified project mutation too, but
            # its contract has preset identity guards rather than the generic
            # expected-before field used by the older write set.
            for name, arguments in PRESET_WRITE_CALLS.items():
                refusal = await session.call_tool(name, arguments)
                text = " ".join(
                    block.text
                    for block in refusal.content
                    if getattr(block, "type", None) == "text"
                )
                check(
                    "%s refused before write mode was enabled" % name,
                    bool(getattr(refusal, "is_error", False))
                    and "session_set_write_mode" in text
                    and "confirm_user_present=true" in text,
                    text,
                )

            # Palette application must fail closed on missing conversational
            # authorization before it can resolve a process-local plan or
            # reach any FL write boundary.
            unauthorized_palette = await session.call_tool(
                "sound_apply_palette",
                {
                    "palette": "missing",
                    "session_fingerprint": session_fingerprint,
                    "authorized_to_modify": False,
                },
            )
            unauthorized_text = " ".join(
                block.text
                for block in unauthorized_palette.content
                if getattr(block, "type", None) == "text"
            )
            check(
                "sound_apply_palette requires explicit authorization",
                bool(getattr(unauthorized_palette, "is_error", False))
                and "explicit authorization" in unauthorized_text,
                unauthorized_text,
            )

            section("runtime write mode over stdio")
            state_before_mode = fingerprint()
            unconfirmed = await session.call_tool(
                "session_set_write_mode",
                {"enabled": True},
            )
            check(
                "runtime enable refuses without explicit user confirmation",
                bool(getattr(unconfirmed, "is_error", False))
                and "confirm_user_present=true" in " ".join(
                    block.text
                    for block in unconfirmed.content
                    if getattr(block, "type", None) == "text"
                ),
                unconfirmed,
            )
            enabled = payload(
                await session.call_tool(
                    "session_set_write_mode",
                    {"enabled": True, "confirm_user_present": True},
                )
            )
            check(
                "MCP enables writes in the current bridge session",
                enabled["verified"] is True
                and enabled["before_enabled"] is False
                and enabled["after_enabled"] is True
                and enabled["bridge_mode"] == "write_test"
                and enabled["session_only"] is True,
                enabled,
            )
            disabled = payload(
                await session.call_tool(
                    "session_set_write_mode",
                    {"enabled": False},
                )
            )
            check(
                "MCP locks the same session again without positive confirmation",
                disabled["verified"] is True
                and disabled["before_enabled"] is True
                and disabled["after_enabled"] is False
                and disabled["bridge_mode"] == "read_only",
                disabled,
            )
            rejected_mode_argument = await session.call_tool(
                "session_set_write_mode",
                {"enabled": True, "confirm_user_present": True, "forever": True},
            )
            check(
                "mode tool rejects unknown input",
                bool(getattr(rejected_mode_argument, "is_error", False)),
                rejected_mode_argument,
            )
            check(
                "mode transitions did not touch project, undo, or save state",
                state_before_mode == fingerprint(),
                (state_before_mode, fingerprint()),
            )

    section("session left FL state untouched")
    check("end-to-end session did not mutate FL state", before == fingerprint())


def main():
    global BRIDGE_PORT
    _state.reset()
    bridge.MAILBOX = MAILBOX
    thread = threading.Thread(target=pump_bridge, daemon=True)
    thread.start()
    time.sleep(0.3)
    if bridge._transport is None or bridge._transport.server is None:
        raise RuntimeError("ephemeral fake bridge did not start")
    BRIDGE_PORT = bridge._transport.server.getsockname()[1]
    try:
        asyncio.run(run())
    finally:
        STOP.set()
        thread.join(timeout=2)
        shutil.rmtree(MAILBOX, ignore_errors=True)
    return summary()


if __name__ == "__main__":
    raise SystemExit(main())
