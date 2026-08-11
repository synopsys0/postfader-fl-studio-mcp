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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "fakefl"))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
sys.path.insert(0, ROOT)

import _state  # noqa: E402
import device_UniversalBridge as bridge  # noqa: E402

# No deterministic process may contact the production bridge port.  The
# kernel-selected listener is passed explicitly to the child MCP process.
bridge.PORT = 0

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


PASS = 0
FAIL = 0
STOP = threading.Event()
MAILBOX = tempfile.mkdtemp(prefix="flmcp-readonly-e2e-")
BRIDGE_PORT = None

WRITE_TOOLS = {
    "fl_set_mixer_volume",
    "fl_set_mixer_pan",
    "fl_set_mixer_mute",
    "fl_set_track_eq",
    "fl_set_mixer_name",
    "fl_set_mixer_send",
    "fl_set_mixer_send_level",
    "fl_set_plugin_param",
    "fl_set_plugin_param_display",
    "fl_set_plugin_param_option",
}
EXPECTED_TOOLS = WRITE_TOOLS | {
    "fl_get_capabilities",
    "fl_get_project_summary",
    "fl_get_transport_state",
    "fl_get_selected_range",
    "fl_list_mixer_tracks",
    "fl_inspect_mixer_track",
    "plugins_scan_loaded_plugins",
    "plugins_inspect_parameter_map",
    "plugins_scan_parameters",
    "copilot_capture_readonly_inspection",
    # File measurement, not FL control.
    "audio_analyze_file",
    "audio_compare_files",
    "audio_analyze_masking",
    "audio_find_recent_bounces",
}
# One minimal, in-range call per write tool. This fake bridge is pumped
# in-process without FL_BRIDGE_ENABLE_WRITES, so each of these must be refused
# by name rather than reaching FL.
WRITE_CALLS = {
    "fl_set_mixer_volume": {"track_index": 3, "volume_normalized": 0.65},
    "fl_set_mixer_pan": {"track_index": 3, "pan": -0.4},
    "fl_set_mixer_mute": {"track_index": 3, "muted": True},
    "fl_set_track_eq": {"track_index": 3, "band_index": 1, "gain_normalized": 0.7},
    "fl_set_mixer_name": {"track_index": 3, "name": "Lead Verb"},
    "fl_set_mixer_send": {
        "track_index": 3,
        "destination_track_index": 5,
        "enabled": True,
    },
    "fl_set_mixer_send_level": {
        "track_index": 3,
        "destination_track_index": 0,
        "level_normalized": 0.5,
    },
    "fl_set_plugin_param": {
        "track_index": 3,
        "slot_index": 1,
        "parameter_index": 0,
        "normalized_value": 0.3,
    },
    "fl_set_plugin_param_display": {
        "track_index": 3,
        "slot_index": 1,
        "parameter": "Threshold",
        "target_value": 40.0,
    },
    "fl_set_plugin_param_option": {
        "track_index": 9,
        "slot_index": 0,
        "parameter": "Key",
        "option": "A",
    },
}


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s  %s" % (label, detail))


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
            "channels": [vars(channel) for channel in _state.CHANNELS],
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
            initialized = await session.initialize()
            check(
                "server initialises",
                initialized.server_info.name == "postfader-fl-studio-mcp",
                initialized.server_info,
            )
            tools = (await session.list_tools()).tools
            names = {tool.name for tool in tools}
            # The exact published surface. This used to be a blanket ban on any
            # tool name containing "set_", which encoded a read-only-only
            # product; the ten verified writes are part of the surface now, so
            # the guard is an exact set. An unintended tool appearing still
            # fails here.
            check(
                "exactly the published tool set is exposed",
                names == EXPECTED_TOOLS,
                sorted(names ^ EXPECTED_TOOLS),
            )
            check(
                "no transport, undo, render or generic API tools exposed",
                not [
                    name
                    for name in names
                    if any(
                        token in name
                        for token in ("undo", "apply", "render", "api_call", "save")
                    )
                ],
                sorted(names),
            )
            check(
                "every read and audio tool annotated read-only",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint
                    and tool.annotations.destructive_hint is False
                    for tool in tools
                    if tool.name not in WRITE_TOOLS
                ),
            )
            check(
                "every write tool annotated mutating, destructive and idempotent",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint is False
                    and tool.annotations.destructive_hint is True
                    and tool.annotations.idempotent_hint is True
                    for tool in tools
                    if tool.name in WRITE_TOOLS
                ),
                sorted(WRITE_TOOLS),
            )

            project = payload(await session.call_tool("fl_get_project_summary", {}))
            check("FL 2026 version gate passed", project["connection"]["compatible"], project)
            check(
                "fixture project title returned",
                project["project_title"] == "Synthetic Test Project",
            )

            selection = payload(await session.call_tool("fl_get_selected_range", {}))
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
                "fl_get_selected_range", {"unexpected": True}
            )
            check(
                "selection tool rejects extra input",
                bool(getattr(invalid_selection, "is_error", False)),
                invalid_selection,
            )

            mixer = payload(await session.call_tool("fl_list_mixer_tracks", {}))
            check("mixer tracks returned", mixer["total_track_count"] == 126, mixer)
            check("loaded effects identified", any(t["plugins"] for t in mixer["tracks"]))

            params = payload(
                await session.call_tool(
                    "plugins_inspect_parameter_map",
                    {"track_index": 3, "slot_index": 1, "limit": 4},
                )
            )
            check("parameter page bounded", params["scanned_count"] == 4, params)
            check(
                "parameters explicitly unsafe",
                all(not item["safe_to_modify"] for item in params["parameters"]),
            )

            # The de-padded whole-plug-in walk. It goes through the same
            # gateway allowlist, so a missing entry fails here rather than
            # only against live FL.
            scan = payload(
                await session.call_tool(
                    "plugins_scan_parameters",
                    {"track_index": 3, "slot_index": 1},
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
                    "plugins_scan_parameters",
                    {"track_index": 3, "slot_index": 1, "max_indices": 2},
                )
            )
            check(
                "a bounded scan says it is partial",
                bounded["truncated"] and bounded["truncated_by"] == "max_indices",
                bounded,
            )

            report = payload(
                await session.call_tool(
                    "copilot_capture_readonly_inspection",
                    {"parameter_limit": 4, "max_plugins": 4},
                )
            )
            check("capture declares read-only mode", report["mode"] == "read_only", report)

            # This bridge is pumped in-process without FL_BRIDGE_ENABLE_WRITES,
            # so it reports bridge_mode="read_only". Every write tool must
            # refuse over the wire by naming the flag, rather than surfacing a
            # raw dispatch rejection or, worse, changing anything.
            for name, arguments in WRITE_CALLS.items():
                refusal = await session.call_tool(name, arguments)
                text = " ".join(
                    block.text
                    for block in refusal.content
                    if getattr(block, "type", None) == "text"
                )
                check(
                    "%s refused without the write flag" % name,
                    bool(getattr(refusal, "is_error", False))
                    and "FL_BRIDGE_ENABLE_WRITES=1" in text,
                    text,
                )
                rejected = await session.call_tool(name, dict(arguments, nudge_by=0.1))
                check(
                    "%s rejects an unknown argument" % name,
                    bool(getattr(rejected, "is_error", False)),
                    rejected,
                )

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
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
