<div align="center">

# PostFader

### The verified AI copilot for FL Studio

Read the project you already have open, diagnose exported mixes, apply guarded
changes after an explicit request, and see whether FL Studio actually accepted
them.

[![CI](https://github.com/synopsys0/postfader-fl-studio-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/synopsys0/postfader-fl-studio-mcp/actions/workflows/ci.yml) [![Version](https://img.shields.io/badge/version-0.20.0-blue)](#supported-versions) [![FL Studio](https://img.shields.io/badge/FL%20Studio-2026-orange)](#supported-versions) [![Python](https://img.shields.io/badge/python-3.10--3.14-blue)](https://www.python.org/downloads/) [![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows-lightgrey)](#supported-versions) [![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

[Download](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/latest) · [Features](#what-you-can-do) · [Quick start](#quick-start) · [AI clients](#works-with-your-ai-client) · [Safety](#safe-by-default) · [Documentation](#documentation)

</div>

> [!NOTE]
> PostFader is an unofficial community project. It is not made by or affiliated
> with Image-Line.

PostFader starts read-only, never saves your project automatically, and reports
the evidence behind every supported action. Direct setters are read back on a
later FL Studio update; workflows with weaker evidence say so instead of
claiming success they cannot prove.

> “Find the conflict between my lead vocal and synth, propose the safest fix,
> and do not touch the Master.”

PostFader can read the real mixer and loaded plug-ins, measure an exported
bounce, present a reviewable plan, apply that reviewed plan once, and show the
resulting readback. It also composes deterministic MIDI parts and supports
an explicitly limited Piano Roll scripting workflow.

It includes **90 supported tools and 8 live resources**, with no catalog entries
whose only behavior is “unsupported.”

## What you can do

| 🔎 Understand your project | 🎚️ Control the session |
| --- | --- |
| Read project, transport, mixer, channels, plug-ins, patterns, Playlist tracks, history, presets, and eight always-addressable MCP resources. | Apply 39 direct guarded setters across transport, mixer, Channel Rack, patterns, Playlist tracks, plug-ins, history, and the step sequencer. |
| **🩺 Finish the mix** | **🎹 Compose and arrange** |
| Run Mix Doctor, real-bounce reference and masking analysis, persistent peak watches, gain staging, processing intents, plug-in profiles, and reviewed mix plans. | Generate chords, melodies, bass, and drums; export verified Type-1 MIDI; estimate tempo/key; transcribe monophonic audio; prepare patterns; add markers; and record automation values. |
| **⚡ Work in fewer calls** | **🎛️ Edit the Piano Roll** |
| Use one-session write mode, closed-union verified batches, compact aggregate receipts, and plan/apply separation. | Append, replace, quantize, transpose, humanize, duplicate, delete, or clear notes through FL's separate Piano Roll script runtime. |

PostFader can also inspect and control parameters exposed by native FL effects,
VST/VST3/AU effects, and Channel Rack generators. Plug-in support is discovered
from FL Studio at runtime rather than limited to a fixed list.

## Tools

PostFader exposes 90 tools in these groups:

- project status, capabilities, transport, and live resources;
- mixer, routing, Channel Rack, patterns, Playlist, history, and step sequencing;
- loaded plug-in inspection and guarded parameter control;
- audio analysis, reference comparison, masking, and Mix Doctor;
- reviewed plans, verified batches, gain staging, and peak watches; and
- composition, MIDI export, arrangement, automation, and Piano Roll workflows.

See the [complete tool reference](docs/tool-contracts.md) for exact arguments,
results, refusals, and evidence boundaries.

### Example requests

~~~text
“Show me every used mixer track and where it routes.”

“Which Channel Rack instruments are not assigned to a mixer insert?”

“Rename insert 4 to Lead Vocal, set its pan to 10% left, and confirm both changes.”

“Set the tempo to 128 BPM while the project is stopped.”

“Enable write mode for this session, then rename insert 4 to Lead Vocal.”

“Run Mix Doctor on this bounce, compare it to the reference, and create a
reviewable plan for the highest-confidence fixes.”

“Watch peaks through one full playback, then build a gain-staging plan with a
-12 dBFS target.”

“Write an eight-bar D Dorian melody and bassline, export them as Type-1 MIDI,
then prepare an empty pattern for the result.”
~~~

## Why use PostFader?

- **Stay in the creative flow.** Ask for project information or routine changes
  without hunting through several FL Studio windows.
- **Know whether a change landed.** PostFader reads supported controls back
  after changing them and reports the before state, after state, and result.
- **Start safely.** PostFader connects in read-only mode. When you are ready,
  ask your AI client to enable writes for the current session—no FL Studio
  restart required.
- **Use the plug-ins you already own.** PostFader discovers the parameter
  surface FL Studio exposes instead of requiring a custom profile for every
  plug-in.
- **Move from evidence to action.** Mix recommendations remain separate from
  reviewed, one-shot plan application, with a receipt for every operation.
- **Compose deterministically.** Creative generators accept a seed and return
  a content digest, so the same request can be reproduced or exported.
- **Keep it local.** PostFader has no hosted service, account, telemetry, or
  project upload step.

## What “verified” means

For a supported change, PostFader:

1. checks the target, value, current session, and any supplied before-state;
2. asks FL Studio to make the change;
3. waits for a later FL Studio update and reads the control again; and
4. reports whether the requested state was actually observed.

This catches a common automation failure: FL Studio or a plug-in accepting a
command but ignoring the value.

> [!IMPORTANT]
> A verified result means the control was observed at the requested setting. It
> does not mean the musical choice sounds good. Writes affect the open project
> immediately and are not automatically rolled back. PostFader never saves the
> project for you.

## Works with your AI client

PostFader is model- and vendor-independent. It uses the standard local `stdio`
MCP transport, so it works with any MCP-compatible desktop or coding client
that can launch a local process.

| Client | How PostFader connects |
| --- | --- |
| [Claude Desktop and Claude Code](https://docs.anthropic.com/en/docs/mcp) | Standard `mcpServers` configuration; the `.mcpb` release download is an optional Claude Desktop convenience. |
| [ChatGPT desktop — Codex host, Codex CLI, and Codex IDE extension](https://developers.openai.com/codex/mcp/) | Shared local MCP configuration across the supported Codex hosts. |
| [Cursor](https://docs.cursor.com/context/model-context-protocol) | Local `stdio` server through Cursor's `mcp.json` configuration. |
| [OpenCode](https://opencode.ai/docs/mcp-servers/) | Local MCP server through OpenCode's `opencode.json` or `opencode.jsonc` configuration. |
| [T3 Code](https://github.com/pingdotgg/t3code) | Through a configured MCP-capable Codex, Claude, or Cursor agent. |
| Other MCP clients | Use the same executable, arguments, and environment values generated by PostFader. |

This compatibility applies to local clients that can start PostFader on the
same computer as FL Studio. A browser-only chat surface needs its own local MCP
or plug-in runtime before it can connect.

## How it works

~~~mermaid
flowchart LR
    A["Your AI client<br/>(MCP)"] --> B["PostFader<br/>runs locally"]
    B -->|"Virtual MIDI"| C["Universal Bridge<br/>inside FL Studio"]
    C --> D["Your open project"]
    B -->|"Files you choose"| E["Exported audio<br/>measurements"]
    B --> F["Mix workflows<br/>and creative engine"]
    F -->|"Type-1 MIDI"| G["Verified MIDI files"]
    F -->|"Optional generated script"| H["FL Piano Roll"]
~~~

MCP is the connection that lets an AI client call PostFader's named tools.
Live FL Studio communication travels over one local virtual MIDI endpoint.
Audio analysis reads exported files from disk because FL Studio's scripting API
does not provide live audio buffers.

## Supported versions

| Component | Support |
| --- | --- |
| PostFader | 0.20.0 |
| FL Studio | FL Studio 2026, version 26.1.3 build 5336 or newer |
| FL MIDI scripting API | Version 44 or newer |
| Python | 3.10 through 3.14 |
| macOS | Supported; v0.20 was live-qualified on macOS 27.0 arm64 with FL Studio Producer Edition 26.1.3 build 5336 and the built-in IAC bus. |
| Windows | Supported; v0.20 was live-qualified on Windows 11 x64 with FL Studio Producer Edition 26.1.4 build 5589. |
| AI clients | Any local `stdio` MCP-compatible client; see [Works with your AI client](#works-with-your-ai-client) |

Python 3.13/3.14 and Windows ARM64 may need a native compiler for
<code>python-rtmidi</code>. Current Windows CI runs on x64.

The maintainer-supervised v0.20 live matrix at
[`3f63d43`](https://github.com/synopsys0/postfader-fl-studio-mcp/commit/3f63d43a6a597b13141e2491a47733738bf26313)
passed on both tested platforms: all 34 authoritative reads, all 8 resources,
all 39 direct writes, and restoration checks for every operation. The macOS
qualification also covered mixing, real-audio, composition, arrangement, live
note, and manual Piano Roll apply/transpose/undo workflows. These are
qualification results for that revision and the systems above, not a claim
that every FL Studio build, plug-in, or workstation behaves identically.

## Quick start

### 1. Prepare a virtual MIDI endpoint

- **macOS:** enable an IAC bus in **Audio MIDI Setup**.
- **Windows:** create one bidirectional endpoint with the virtual MIDI software
  of your choice.

PostFader does not install or configure virtual MIDI software. You will use the
same endpoint for FL Studio's MIDI input and output.

Launch FL Studio once before installing PostFader so it creates its user-data
folders, then quit FL Studio.

### 2. Install PostFader

For the easiest install, download the latest release and choose
`PostFader-v0.20.0-macOS.zip` or `PostFader-v0.20.0-Windows.zip`. Extract the
whole ZIP, open its **START HERE** guide, and run the top-level installer.

To install from source instead:

**macOS**

~~~bash
git clone https://github.com/synopsys0/postfader-fl-studio-mcp.git
cd postfader-fl-studio-mcp
./scripts/install.sh
~~~

**Windows PowerShell**

~~~powershell
git clone https://github.com/synopsys0/postfader-fl-studio-mcp.git
Set-Location postfader-fl-studio-mcp
.\scripts\install.ps1 -DryRun
.\scripts\install.ps1
~~~

The installers create a local <code>.venv</code>, install PostFader, and copy
<code>Universal Bridge</code> into FL Studio's controller-script folder. They
do not change your AI client's configuration.

You can also install the published Python package:

~~~text
pip install postfader-fl-studio-mcp
postfader-install-bridge
~~~

### 3. Connect FL Studio

Open **Options → MIDI settings** in FL Studio:

1. Enable your virtual endpoint under **Input**.
2. Set its controller type to **Universal Bridge**.
3. Give the input an FL Studio Port number.
4. Enable the same endpoint under **Output** and give it the same Port number.
5. Open **View → Script output** and reload the script.

The script should report <code>ready: MIDI SysEx</code>.

### 4. Check the connection

**macOS**

~~~bash
./.venv/bin/postfader-doctor --midi-port "IAC Driver Bus 1" --json
~~~

**Windows PowerShell**

~~~powershell
.\.venv\Scripts\postfader-doctor.exe --midi-port "Exact Virtual MIDI Endpoint Name" --json
~~~

A healthy connection reports:

- <code>overall: "pass"</code>;
- a live FL Studio connection;
- a controller script that matches the installed PostFader version;
- <code>bridge_mode: "read_only"</code>; and
- <code>verified_writes_enabled: false</code>; and
- <code>runtime_write_mode_control: true</code>.

### 5. Add PostFader to your AI client

Use the included configuration generator so interpreter paths, repository
paths, and endpoint names are explicit:

~~~bash
./.venv/bin/python scripts/generate_mcp_config.py --help
~~~

~~~powershell
.\.venv\Scripts\python.exe scripts\generate_mcp_config.py --help
~~~

It can produce a Codex command, Codex TOML, or standard `mcpServers` JSON used
by Claude-compatible clients, Cursor, and many other MCP clients. See the
[client configuration guide](docs/setup.md#4-generate-client-configuration)
for complete macOS and Windows examples.

Claude Desktop users can open the release <code>.mcpb</code>, but the extension
does not install the FL Studio controller script or create the virtual MIDI
endpoint. Complete steps 1–4 first.

## Safe by default

### Read-only mode

A normal FL Studio launch keeps PostFader read-only: project inspection works,
but project-changing tools do not. Audio-file analysis also works without a
live FL Studio connection. This is the recommended mode for exploring a real
session.

### Write mode

When you want PostFader to make changes, ask your connected AI client:

~~~text
“Enable write mode for this session.”
~~~

Your client calls <code>fl_set_write_mode</code> and must carry an explicit
user-present confirmation. PostFader then checks the running controller script,
changes only that live session, and performs a second handshake before reporting
that writes are available. FL Studio does not need to restart.

Ask the client to “disable write mode” when you are done. The setting is never
stored in your project or AI-client configuration, and an ordinary controller
script reload or new FL Studio process starts read-only again. Use a blank or
disposable project the first time you try writes.

Additional safeguards include:

- enabling writes requires an explicit user request and is exposed to MCP
  clients as a destructive capability change;
- writes to the Master track require explicit permission;
- if the installed controller script and PostFader version do not match,
  writes are blocked;
- writes are never automatically repeated after a lost or ambiguous response;
- supplied session and before-state checks can reject stale decisions;
- only one local PostFader connection can use the selected virtual MIDI bus at
  a time; and
- the controller script never calls FL Studio's save-project function.

See [Tool contracts](docs/tool-contracts.md#write-tools) for the exact behavior
of every write.

### Batches, plans, and creative writes

`fl_apply_verified_batch` accepts a bounded closed union of supported direct
operations, performs one live preflight, and returns ordered per-item receipts.
It is not an ACID transaction: an unverified item is reported and later items
can be skipped, but earlier changes are not rolled back.

Mix workflows keep analysis and mutation separate. `mix_create_plan` stores a
session-bound plan in the MCP process; `mix_apply_plan` can apply that plan
once through the same verified batch kernel. Peak watches and plans disappear
when the MCP process exits.

Piano Roll editing uses FL Studio's separate `.pyscript` runtime. Call
`piano_roll_bridge(action="prepare")`, run **Postfader Apply** once from the
Piano Roll Scripts menu, then confirm that manual step with
`piano_roll_bridge(action="confirm", confirm_user_ran_script=true)`. Automatic
calls verify the target channel/pattern and report whether the platform hotkey
was dispatched; FL exposes no controller-side note readback, so they never
claim the notes were applied. `auto_trigger=false` prepares the script for a
manual run instead.

## Plug-in support

PostFader can work with any mixer effect or Channel Rack generator whose
parameters FL Studio exposes to controller scripts. That includes native
Image-Line plug-ins and many VST, VST3, and AU plug-ins.

Compatibility is intentionally honest:

- an unfamiliar plug-in can be inspected without first adding it to PostFader;
- a parameter that FL does not expose cannot be controlled;
- very large parameter maps are scanned with explicit limits;
- a write that FL accepts but ignores is reported as unverified; and
- named options must use the exact label FL Studio reports, ignoring case.

See [Plug-in support](docs/plugin-support.md) for parameter discovery, option
searches, scan limits, troubleshooting, and the community evidence format.

## Important limitations

PostFader cannot currently:

- add, remove, or reorder plug-ins;
- reliably control an effect slot's bypass or wet/dry mix;
- hear FL Studio's live output;
- render, export, or save a project;
- create, move, or delete Playlist clips through the public scripting API;
- prove Piano Roll note application after a focus-sensitive script shortcut;
- read section-marker times or recorded automation points back from FL;
- turn a technical mix diagnosis into objective artistic truth; or
- turn raw Playlist selection endpoints into a safe automatic render range.

Audio and mix tools analyze files you explicitly select or recent bounces found
in bounded FL Studio folders. They return measurements, threshold-driven
diagnoses, and bounded recommendations—not audio samples or a claim that one
creative choice is universally correct.

The local virtual MIDI bus is shared and unauthenticated. Use PostFader on a
trusted, single-user workstation.

## Privacy

PostFader itself runs locally and has no telemetry or cloud service. It does
not store your projects, recordings, stems, presets, exports, or live-session
evidence in this repository.

Your AI client is a separate application and may send tool arguments and
results to its model provider. Audio results can include file paths, hashes,
and measurements, but never audio samples. Review your AI client's privacy
policy and PostFader's [security policy](SECURITY.md) before using sensitive
projects.

## Documentation

| Guide | What it covers |
| --- | --- |
| [Setup and troubleshooting](docs/setup.md) | Installation, client configuration, upgrades, diagnostics, write mode, and common errors |
| [Tool reference](docs/tool-contracts.md) | All 90 tools, 8 resources, accepted values, results, refusals, and evidence boundaries |
| [Plug-in support](docs/plugin-support.md) | Effects, generators, parameter scans, option controls, and compatibility evidence |
| [FL Studio constraints](docs/fl-constraints.md) | What FL Studio's scripting API allows and where PostFader deliberately stops |
| [Architecture](docs/architecture.md) | Components, transport, bridge behavior, and trust boundaries |
| [Security](SECURITY.md) | Threat model, privacy boundaries, and vulnerability reporting |
| [Contributing](CONTRIBUTING.md) | Development workflow and contribution guidelines |

## Development

Run the safe, hardware-free test suite from the source checkout:

~~~bash
./.venv/bin/python scripts/run_safe_tests.py
~~~

~~~powershell
.\.venv\Scripts\python.exe scripts\run_safe_tests.py
~~~

The safe suite prevents real MIDI access even when ambient environment
variables request it. Any live hardware test must use a blank, unsaved project,
and its logs, screenshots, and run notes must stay outside the public repository.

## License

PostFader is available under the [Apache License 2.0](LICENSE). See
[NOTICE](NOTICE) for attribution details.

<!-- mcp-name: io.github.synopsys0/postfader-fl-studio-mcp -->
