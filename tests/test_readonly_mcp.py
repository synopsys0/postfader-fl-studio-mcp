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


PASS = 0
FAIL = 0
STOP = threading.Event()
MAILBOX = tempfile.mkdtemp(prefix="flmcp-readonly-e2e-")
BRIDGE_PORT = None

WRITE_TOOLS = {
    "fl_apply_verified_batch",
    "fl_set_mixer_volume",
    "fl_set_mixer_volume_db",
    "fl_set_mixer_pan",
    "fl_set_mixer_mute",
    "fl_set_mixer_solo",
    "fl_set_mixer_arm",
    "fl_set_mixer_color",
    "fl_set_mixer_stereo_separation",
    "fl_select_mixer_track",
    "fl_set_track_eq",
    "fl_set_mixer_name",
    "fl_set_mixer_send",
    "fl_set_mixer_send_level",
    "fl_set_plugin_param",
    "fl_set_plugin_param_display",
    "fl_set_plugin_param_option",
    "fl_set_playing",
    "fl_stop",
    "fl_set_song_position",
    "fl_set_loop_mode",
    "fl_set_tempo",
    "fl_set_recording",
    "fl_set_metronome",
    "fl_set_precount",
    "fl_set_time_signature_numerator",
    "fl_undo",
    "fl_redo",
    "fl_set_channel_mix",
    "fl_set_channel_solo",
    "fl_set_channel_pitch",
    "fl_select_channel",
    "fl_select_pattern",
    "fl_set_pattern_identity",
    "fl_set_pattern_length",
    "fl_set_playlist_track_identity",
    "fl_set_playlist_track_state",
    "fl_set_channel_identity",
    "fl_route_channel_to_mixer",
    "fl_set_step_sequence",
}
PRODUCTION_READ_TOOLS = {
    "postfader_validate_run",
    "postfader_get_run",
}
PRODUCTION_MUTATING_TOOLS = {
    "postfader_execute_run",
    "postfader_continue_run",
}
PRODUCTION_WORKFLOW_TOOLS = {"postfader_stop_run"}
EPHEMERAL_TOOLS = {"fl_trigger_note"}
MODE_TOOLS = {"fl_set_write_mode"}
MIX_READ_TOOLS = {
    "mix_doctor",
    "mix_reference_recommendations",
    "mix_masking_recommendations",
    "mix_get_peak_watch",
    "mix_list_plugin_profiles",
    "mix_inspect_plugin_compatibility",
    "mix_resolve_processing_intent",
    "mix_get_plan",
    "mix_finish_assessment",
}
PRESET_READ_TOOLS = {
    "plugins_list_presets",
    "plugins_get_current_preset",
    "plugins_inspect_pad_map",
}
SOUND_SELECTION_READ_TOOLS = {
    "sound_selection_inventory",
    "sound_selection_plan",
    "sound_selection_get",
    "sound_selection_create_variation",
    "sound_selection_history_status",
}
PRESET_MUTATING_TOOLS = {"fl_select_plugin_preset"}
SOUND_SELECTION_MUTATING_TOOLS = {"sound_selection_apply"}
SOUND_SELECTION_WORKFLOW_TOOLS = {
    "sound_selection_record_feedback",
    "sound_selection_history_reset",
}
WORKFLOW_STATE_TOOLS = {
    "mix_start_peak_watch",
    "mix_stop_peak_watch",
    "mix_create_gain_stage_plan",
    "mix_create_plan",
    "piano_roll_bridge",
}
PLAN_APPLY_TOOLS = {"mix_apply_plan"}
CREATIVE_READ_TOOLS = {
    "compose_chord_progression",
    "compose_melody",
    "compose_bassline",
    "compose_drums",
    "audio_estimate_tempo_and_key",
    "audio_transcribe_melody",
}
CREATIVE_FL_TOOLS = {
    "piano_roll_write_notes",
    "piano_roll_transform",
    "arrangement_prepare_pattern",
    "arrangement_add_section_markers",
    "automation_record_value",
}
FILE_MUTATING_TOOLS = {"midi_export_type1"}
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
    "plugins_atlas_search",
    "plugins_atlas_get_product",
    "plugins_atlas_recommend",
    "plugins_atlas_inspect_loaded",
    "copilot_capture_readonly_inspection",
    "fl_list_channels",
    "fl_get_step_sequence",
    "fl_list_patterns",
    "fl_find_empty_pattern",
    "fl_list_playlist_tracks",
    "fl_get_project_history",
    "fl_get_plugin_preset_count",
    *EPHEMERAL_TOOLS,
    *PRODUCTION_READ_TOOLS,
    *PRODUCTION_MUTATING_TOOLS,
    *PRODUCTION_WORKFLOW_TOOLS,
    *MODE_TOOLS,
    *MIX_READ_TOOLS,
    *WORKFLOW_STATE_TOOLS,
    *PLAN_APPLY_TOOLS,
    *CREATIVE_READ_TOOLS,
    *CREATIVE_FL_TOOLS,
    *FILE_MUTATING_TOOLS,
    *PRESET_READ_TOOLS,
    *SOUND_SELECTION_READ_TOOLS,
    *PRESET_MUTATING_TOOLS,
    *SOUND_SELECTION_MUTATING_TOOLS,
    *SOUND_SELECTION_WORKFLOW_TOOLS,
    # File measurement, not FL control.
    "audio_analyze_file",
    "audio_compare_files",
    "audio_analyze_masking",
    "audio_find_recent_bounces",
}
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
    "fl_apply_verified_batch": {
        "operations": [
            {
                "operation_id": "volume-1",
                "operation": "mixer_volume",
                "track_index": 3,
                "volume_normalized": 0.65,
            }
        ]
    },
    "fl_set_mixer_volume": {"track_index": 3, "volume_normalized": 0.65},
    "fl_set_mixer_volume_db": {"track_index": 3, "volume_db": -6.0},
    "fl_set_mixer_pan": {"track_index": 3, "pan": -0.4},
    "fl_set_mixer_mute": {"track_index": 3, "muted": True},
    "fl_set_mixer_solo": {"track_index": 3, "soloed": True},
    "fl_set_mixer_arm": {"track_index": 3, "armed": True},
    "fl_set_mixer_color": {"track_index": 3, "color": 0x0055AA},
    "fl_set_mixer_stereo_separation": {
        "track_index": 3,
        "stereo_separation": 0.35,
    },
    "fl_select_mixer_track": {"track_index": 3},
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
    "fl_set_playing": {"playing": True},
    "fl_stop": {},
    "fl_set_song_position": {"position_normalized": 0.25},
    "fl_set_loop_mode": {"loop_mode": "pattern"},
    "fl_set_tempo": {"tempo_bpm": 128.0},
    "fl_set_recording": {"recording": True},
    "fl_set_metronome": {"enabled": True},
    "fl_set_precount": {"enabled": True},
    "fl_set_time_signature_numerator": {"numerator": 3},
    "fl_undo": {},
    "fl_redo": {},
    "fl_set_channel_mix": {"channel_index": 0, "volume_normalized": 0.7},
    "fl_set_channel_solo": {"channel_index": 0, "soloed": True},
    "fl_set_channel_pitch": {"channel_index": 0, "pitch_normalized": 0.25},
    "fl_select_channel": {"channel_index": 1},
    "fl_select_pattern": {"pattern_number": 2},
    "fl_set_pattern_identity": {"pattern_number": 1, "name": "Intro"},
    "fl_set_pattern_length": {"pattern_number": 1, "length_beats": 8},
    "fl_set_playlist_track_identity": {"track_index": 1, "name": "Vocals"},
    "fl_set_playlist_track_state": {"track_index": 1, "muted": True},
    "fl_set_channel_identity": {"channel_index": 0, "name": "Demo"},
    "fl_route_channel_to_mixer": {"channel_index": 0, "mixer_destination": 3},
    "fl_set_step_sequence": {
        "pattern_number": 1,
        "channel_index": 0,
        "expected_digest": "0" * 64,
        "updates": [{"step_index": 0, "enabled": True}],
    },
    "fl_trigger_note": {"channel_index": 0, "note": 60, "velocity": 100},
}
PRESET_WRITE_CALLS = {
    "fl_select_plugin_preset": {
        "target": {"kind": "mixer_effect", "track_index": 3, "slot_index": 1},
        "preset_name": "Preset 1",
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
            resources = (await session.list_resources()).resources
            resource_uris = {str(resource.uri) for resource in resources}
            check(
                "exactly the eight live FL resources are exposed",
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
            check(
                "no render, project-save or generic API tools exposed",
                not [
                    name
                    for name in names
                    if any(
                        token in name
                        for token in ("render", "api_call", "save")
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
                    if tool.name
                    not in WRITE_TOOLS
                    | PRODUCTION_MUTATING_TOOLS
                    | PRODUCTION_WORKFLOW_TOOLS
                    | EPHEMERAL_TOOLS
                    | MODE_TOOLS
                    | WORKFLOW_STATE_TOOLS
                    | PLAN_APPLY_TOOLS
                    | CREATIVE_FL_TOOLS
                    | FILE_MUTATING_TOOLS
                    | PRESET_MUTATING_TOOLS
                    | SOUND_SELECTION_MUTATING_TOOLS
                    | SOUND_SELECTION_WORKFLOW_TOOLS
                ),
            )
            check(
                "Production Run read tools are annotated read-only",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint
                    and tool.annotations.destructive_hint is False
                    and tool.annotations.idempotent_hint is True
                    for tool in tools
                    if tool.name in PRODUCTION_READ_TOOLS
                ),
                sorted(PRODUCTION_READ_TOOLS),
            )
            check(
                "Production Run execute and continue tools are mutating",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint is False
                    and tool.annotations.destructive_hint is True
                    and tool.annotations.idempotent_hint is False
                    and tool.annotations.open_world_hint is True
                    for tool in tools
                    if tool.name in PRODUCTION_MUTATING_TOOLS
                ),
                sorted(PRODUCTION_MUTATING_TOOLS),
            )
            check(
                "Production Run stop is non-destructive workflow state",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint is False
                    and tool.annotations.destructive_hint is False
                    and tool.annotations.idempotent_hint is False
                    for tool in tools
                    if tool.name in PRODUCTION_WORKFLOW_TOOLS
                ),
                sorted(PRODUCTION_WORKFLOW_TOOLS),
            )
            check(
                "every write tool annotated mutating, destructive and non-idempotent",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint is False
                    and tool.annotations.destructive_hint is True
                    and tool.annotations.idempotent_hint is False
                    for tool in tools
                    if tool.name in WRITE_TOOLS
                ),
                sorted(WRITE_TOOLS),
            )
            check(
                "live note is annotated as non-idempotent dispatch, not a verified write",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint is False
                    and tool.annotations.destructive_hint is False
                    and tool.annotations.idempotent_hint is False
                    for tool in tools
                    if tool.name in EPHEMERAL_TOOLS
                ),
            )
            check(
                "write-mode control is destructive capability change and idempotent",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint is False
                    and tool.annotations.destructive_hint is True
                    and tool.annotations.idempotent_hint is True
                    for tool in tools
                    if tool.name in MODE_TOOLS
                ),
            )
            check(
                "mix workflow registries are non-destructive process-local state",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint is False
                    and tool.annotations.destructive_hint is False
                    and tool.annotations.idempotent_hint is False
                    for tool in tools
                    if tool.name in WORKFLOW_STATE_TOOLS
                ),
                sorted(WORKFLOW_STATE_TOOLS),
            )
            check(
                "mix plan application is an explicit non-idempotent FL mutation",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint is False
                    and tool.annotations.destructive_hint is True
                    and tool.annotations.idempotent_hint is False
                    for tool in tools
                    if tool.name in PLAN_APPLY_TOOLS
                ),
                sorted(PLAN_APPLY_TOOLS),
            )
            check(
                "creative FL tools are explicit non-idempotent live mutations",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint is False
                    and tool.annotations.destructive_hint is True
                    and tool.annotations.idempotent_hint is False
                    and tool.annotations.open_world_hint is True
                    for tool in tools
                    if tool.name in CREATIVE_FL_TOOLS
                ),
                sorted(CREATIVE_FL_TOOLS),
            )
            check(
                "MIDI export is a closed-world local file mutation",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint is False
                    and tool.annotations.destructive_hint is True
                    and tool.annotations.idempotent_hint is False
                    and tool.annotations.open_world_hint is False
                    for tool in tools
                    if tool.name in FILE_MUTATING_TOOLS
                ),
                sorted(FILE_MUTATING_TOOLS),
            )
            check(
                "preset and Sound Selection read tools are annotated read-only",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint
                    and tool.annotations.destructive_hint is False
                    and tool.annotations.idempotent_hint is True
                    for tool in tools
                    if tool.name in PRESET_READ_TOOLS | SOUND_SELECTION_READ_TOOLS
                ),
                sorted(PRESET_READ_TOOLS | SOUND_SELECTION_READ_TOOLS),
            )
            check(
                "preset and Sound Selection applications are mutating",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint is False
                    and tool.annotations.destructive_hint is True
                    and tool.annotations.idempotent_hint is False
                    and tool.annotations.open_world_hint is True
                    for tool in tools
                    if tool.name in PRESET_MUTATING_TOOLS | SOUND_SELECTION_MUTATING_TOOLS
                ),
                sorted(PRESET_MUTATING_TOOLS | SOUND_SELECTION_MUTATING_TOOLS),
            )
            check(
                "Sound Selection feedback is closed-world workflow state",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint is False
                    and tool.annotations.destructive_hint is False
                    and tool.annotations.idempotent_hint is False
                    and tool.annotations.open_world_hint is False
                    for tool in tools
                    if tool.name == "sound_selection_record_feedback"
                ),
                "sound_selection_record_feedback",
            )
            check(
                "Sound Selection history reset is an idempotent local deletion",
                all(
                    tool.annotations
                    and tool.annotations.read_only_hint is False
                    and tool.annotations.destructive_hint is True
                    and tool.annotations.idempotent_hint is True
                    and tool.annotations.open_world_hint is False
                    for tool in tools
                    if tool.name == "sound_selection_history_reset"
                ),
                "sound_selection_history_reset",
            )
            by_name = {tool.name: tool for tool in tools}
            check(
                "every write tool exposes optional session and before-state preconditions",
                all(
                    {"session_fingerprint"}
                    <= set(by_name[name].input_schema.get("properties", {}))
                    and "session_fingerprint"
                    not in set(by_name[name].input_schema.get("required", []))
                    and (
                        name in {"fl_set_step_sequence", "fl_apply_verified_batch"}
                        or (
                            "expected_before"
                            in set(by_name[name].input_schema.get("properties", {}))
                            and "expected_before"
                            not in set(by_name[name].input_schema.get("required", []))
                        )
                    )
                    for name in WRITE_TOOLS
                ),
                {
                    name: by_name[name].input_schema
                    for name in sorted(WRITE_TOOLS)
                    if name in by_name
                },
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
                    and "fl_set_write_mode" in text
                    and "confirm_user_present=true" in text,
                    text,
                )
                rejected = await session.call_tool(name, dict(arguments, nudge_by=0.1))
                check(
                    "%s rejects an unknown argument" % name,
                    bool(getattr(rejected, "is_error", False)),
                    rejected,
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
                    and "fl_set_write_mode" in text
                    and "confirm_user_present=true" in text,
                    text,
                )
                rejected = await session.call_tool(name, dict(arguments, nudge_by=0.1))
                check(
                    "%s rejects an unknown argument" % name,
                    bool(getattr(rejected, "is_error", False)),
                    rejected,
                )

            # Palette application must fail closed on missing conversational
            # authorization before it can resolve a process-local plan or
            # reach any FL write boundary.
            unauthorized_palette = await session.call_tool(
                "sound_selection_apply",
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
                "sound_selection_apply requires explicit authorization",
                bool(getattr(unauthorized_palette, "is_error", False))
                and "explicit authorization" in unauthorized_text,
                unauthorized_text,
            )

            state_before_mode = fingerprint()
            unconfirmed = await session.call_tool(
                "fl_set_write_mode",
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
                    "fl_set_write_mode",
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
                    "fl_set_write_mode",
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
                "fl_set_write_mode",
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
