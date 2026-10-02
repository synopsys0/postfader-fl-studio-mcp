#!/usr/bin/env python3
"""Write ``docs/tools.md`` from the MCP decorators in the runtime server.

Like ``sync_mcpb_manifest.py`` this parses the server instead of importing it,
so it is hermetic. Each tool's first docstring line is its description, and
the annotation constant it is registered with decides whether it can change
the open project. Run it after adding, removing, or renaming a tool; ``--check``
fails when the page is stale.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import sys
from pathlib import Path

from sync_mcpb_manifest import SERVER, discover_tools


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs" / "tools.md"

# What each annotation constant in mcp_server.py means for the open project.
EFFECTS = {
    "READ_ONLY": "No",
    "LOCAL_READ_ONLY": "No",
    "LOCAL_READ_ONLY_VOLATILE": "No",
    "WORKFLOW_STATE": "No (local state)",
    "FILE_MUTATING": "No (writes a file)",
    "EPHEMERAL_MUTATING": "No (plays a note)",
    "WRITE_MODE_CONTROL": "Turns writes on/off",
    "MUTATING": "**Yes**",
}

PROJECT_AND_SESSION = {
    "fl_get_capabilities",
    "fl_get_project_summary",
    "fl_get_transport_state",
    "fl_get_selected_range",
    "fl_get_project_history",
    "copilot_capture_readonly_inspection",
    "fl_set_write_mode",
    "fl_undo",
    "fl_redo",
}
TRANSPORT = {
    "fl_set_playing",
    "fl_stop",
    "fl_set_song_position",
    "fl_set_loop_mode",
    "fl_set_tempo",
    "fl_set_recording",
    "fl_set_metronome",
    "fl_set_precount",
    "fl_set_time_signature_numerator",
}
PRODUCTION_RUNS = {
    "postfader_creation_readiness",
    "postfader_describe_operations",
    "postfader_validate_run",
    "postfader_execute_run",
    "postfader_list_runs",
    "postfader_get_run",
    "postfader_continue_run",
    "postfader_stop_run",
    "processing_plan",
    "processing_apply_plan",
}

# Ordered: the first matching group wins.
GROUPS = (
    (
        "Project and session",
        "Read the open project and control write access and undo.",
        lambda name: name in PROJECT_AND_SESSION,
    ),
    (
        "Transport",
        "Playback, recording, position, tempo, and time signature.",
        lambda name: name in TRANSPORT,
    ),
    (
        "Mixer",
        "Levels, pan, routing, sends, names, colors, and the built-in EQ.",
        lambda name: name.startswith(("fl_list_mixer", "fl_inspect_mixer", "fl_set_mixer"))
        or name in {"fl_select_mixer_track", "fl_set_track_eq", "fl_apply_verified_batch"},
    ),
    (
        "Channels, patterns, and Playlist",
        "Channel Rack, step sequencer, patterns, and Playlist tracks.",
        lambda name: name.startswith(
            ("fl_list_channels", "fl_set_channel", "fl_select_channel", "fl_route_channel")
        )
        or "step_sequence" in name
        or "pattern" in name and name.startswith("fl_")
        or "playlist" in name
        or name == "fl_trigger_note",
    ),
    (
        "Plug-ins and presets",
        "Loaded plug-in parameters, presets, drum pads, and loading on macOS.",
        lambda name: name.startswith(("fl_set_plugin", "fl_select_plugin", "fl_get_plugin"))
        or (name.startswith("plugins_") and not name.startswith("plugins_atlas")),
    ),
    (
        "Plugin Atlas",
        "Offline knowledge about plug-ins, whether or not they are loaded.",
        lambda name: name.startswith("plugins_atlas"),
    ),
    (
        "Audio analysis",
        "Measure exported audio files. FL Studio's live output is not available.",
        lambda name: name.startswith("audio_"),
    ),
    (
        "Mixing workflows",
        "Mix Doctor, reference and masking checks, peak watches, and mix plans.",
        lambda name: name.startswith("mix_"),
    ),
    (
        "Composition and MIDI",
        "Generate chords, melodies, basslines, and drums; export MIDI files.",
        lambda name: name.startswith("compose_") or name == "midi_export_type1",
    ),
    (
        "Piano Roll and arrangement",
        "Read and write notes, transform them, add markers, record automation.",
        lambda name: name.startswith(("piano_roll_", "arrangement_", "automation_")),
    ),
    (
        "Sound Selection",
        "Choose and apply presets from the instruments already loaded.",
        lambda name: name.startswith("sound_selection_"),
    ),
    (
        "Production Runs",
        "Multi-step jobs that PostFader validates and executes in order.",
        lambda name: name in PRODUCTION_RUNS,
    ),
    (
        "Creation Review and delivery",
        "Review an exported draft, plan one revision, and prepare delivery.",
        lambda name: name.startswith(("postfader_review_", "postfader_delivery_")),
    ),
    (
        "Saved-project rendering",
        "Render a saved .flp to WAV in a separate FL Studio process.",
        lambda name: name.startswith("postfader_render_"),
    ),
)


def _annotation_constants(server: Path = SERVER) -> dict[str, str]:
    """Map each tool name to the annotation constant it is registered with."""

    tree = ast.parse(server.read_text(encoding="utf-8"), filename=str(server))
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
            ):
                continue
            keywords = {keyword.arg: keyword.value for keyword in decorator.keywords}
            name = keywords.get("name")
            annotation = keywords.get("annotations")
            # Either CONSTANT or CONSTANT.model_copy(update={...}).
            if isinstance(annotation, ast.Call) and isinstance(annotation.func, ast.Attribute):
                annotation = annotation.func.value
            if isinstance(name, ast.Constant) and isinstance(annotation, ast.Name):
                found[str(name.value)] = annotation.id
    return found


def _resources(server: Path = SERVER) -> list[tuple[str, str]]:
    tree = ast.parse(server.read_text(encoding="utf-8"), filename=str(server))
    resources = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "resource"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
            ):
                continue
            description = next(
                (
                    keyword.value.value
                    for keyword in decorator.keywords
                    if keyword.arg == "description"
                    and isinstance(keyword.value, ast.Constant)
                ),
                "",
            )
            resources.append((str(decorator.args[0].value), str(description)))
    return resources


def _anchor(title: str) -> str:
    kept = "".join(
        character for character in title.lower() if character.isalnum() or character in " -"
    )
    return kept.replace(" ", "-")


def render() -> str:
    tools = discover_tools()
    effects = _annotation_constants()
    grouped: dict[str, list[dict[str, str]]] = {title: [] for title, _, _ in GROUPS}
    for tool in tools:
        name = tool["name"]
        constant = effects.get(name)
        if constant not in EFFECTS:
            raise ValueError("tool %r has no recognised annotation constant" % name)
        title = next((title for title, _, matches in GROUPS if matches(name)), None)
        if title is None:
            raise ValueError("tool %r matches no group; add it to %s" % (name, __file__))
        grouped[title].append(tool)
    resources = _resources()
    changing = sum(1 for tool in tools if effects[tool["name"]] == "MUTATING")

    lines = [
        "# PostFader tool reference",
        "",
        "<!-- Generated by scripts/generate_tool_reference.py from"
        " fl_studio_mcp/mcp_server.py. Do not edit by hand. -->",
        "",
        "PostFader gives your AI client **%d tools** and **%d resources** for FL Studio."
        % (len(tools), len(resources)),
        "You never call them yourself: ask for what you want in plain language and",
        "the AI picks the tools. Use this page to see what is possible, or to check",
        "what your AI did.",
        "",
        "%d tools can change the open project. They are refused until write mode is"
        % changing,
        "on for the session; a Production Run turns it on itself when you asked for",
        "changes. PostFader never saves the project, and every other tool leaves it",
        "as it is. Details: [security policy](../SECURITY.md) ·",
        "[exact arguments and results](tool-contracts.md).",
        "",
        "| Group | Tools | What it covers |",
        "| --- | ---: | --- |",
    ]
    for title, summary, _ in GROUPS:
        lines.append(
            "| [%s](#%s) | %d | %s |" % (title, _anchor(title), len(grouped[title]), summary)
        )
    for title, summary, _ in GROUPS:
        lines += [
            "",
            "## %s" % title,
            "",
            summary,
            "",
            "| Tool | What it does | Changes the project |",
            "| --- | --- | --- |",
        ]
        for tool in grouped[title]:
            description = tool["description"].replace("|", "\\|")
            lines.append(
                "| `%s` | %s | %s |"
                % (tool["name"], description, EFFECTS[effects[tool["name"]]])
            )
    lines += [
        "",
        "## Resources",
        "",
        "Clients that support MCP resources can read these for context. They follow",
        "the same rules as the matching read tools.",
        "",
        "| Resource | Contents |",
        "| --- | --- |",
    ]
    for uri, description in resources:
        lines.append("| `%s` | %s |" % (uri, description))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if docs/tools.md is stale instead of rewriting it",
    )
    args = parser.parse_args(argv)
    expected = render()
    current = REFERENCE.read_text(encoding="utf-8") if REFERENCE.exists() else ""
    if current == expected:
        print("docs/tools.md is up to date")
        return 0
    if args.check:
        print("docs/tools.md is stale; regenerate with:", file=sys.stderr)
        print("  python scripts/generate_tool_reference.py", file=sys.stderr)
        sys.stderr.writelines(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile="docs/tools.md",
                tofile="generated",
            )
        )
        return 1
    REFERENCE.write_text(expected, encoding="utf-8")
    print("wrote docs/tools.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
