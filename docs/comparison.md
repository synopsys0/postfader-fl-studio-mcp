# FL Studio MCP servers compared

There are several ways to connect an AI assistant to FL Studio. This page puts
PostFader next to the most-used open-source FL Studio MCP servers and FL
Studio's built-in assistant, so you can pick the one that fits your work.

Facts about other projects come from their own READMEs as of **October 1,
2026**. Projects change quickly; check the links for their current state, and
[open an issue](https://github.com/synopsys0/postfader-fl-studio-mcp/issues)
if something here is out of date.

## At a glance

| | PostFader | [karl-andres/fl-studio-mcp](https://github.com/karl-andres/fl-studio-mcp) | [rosasynthesiz/flstudio-mcp](https://github.com/rosasynthesiz/flstudio-mcp) | [calvinw/fl-studio-mcp](https://github.com/calvinw/fl-studio-mcp) |
| --- | --- | --- | --- | --- |
| Tools | 85, plus 8 resources | 52 | 67, plus 6 resources | 4 |
| Windows | ✅ | ✅ | ✅ | Partial |
| macOS | ✅ | ✅ | ✅ | ✅ |
| FL Studio | 2026 (26.1.3+) | 20.7+ | 2025+ | Not stated |
| Connection | 1 virtual MIDI port | 1 virtual MIDI port | 2 virtual MIDI ports and a background daemon | Keyboard shortcut and JSON files |
| Read-only until you allow edits | ✅ | — | Shows changes before applying | — |
| Reads each change back from FL Studio | ✅ | Not documented | ✅ | Exports state after changes |
| Automatic rollback | — (undo tools instead) | — | ✅ | — |
| Mixer, channel, and transport control | ✅ | ✅ | ✅ | — |
| Loaded plug-in parameters | ✅ | ✅ | ✅ | — |
| Piano Roll notes | ✅ | ✅ | ✅ | ✅ |
| Mix diagnosis | ✅ | — | ✅ | — |
| Audio file analysis | Loudness, true peak, spectrum, stereo, masking, reference, tempo, key, melody transcription | — | Tempo, key, melody transcription, reference matching | — |
| Load plug-ins | macOS | — | — | — |
| Render a saved project to WAV | ✅ | — | — | — |
| Automated test suite in CI | ✅ Windows and macOS | — | Not documented | Not documented |
| Installers | Windows and macOS ZIPs, Codex ZIPs, Claude Desktop extension, PyPI | Install scripts | Install scripts, PyPI | Install scripts |
| License | Apache-2.0 | MIT | MIT | MIT |

✅ supported · — not available · "Not documented" means the project's README
doesn't say either way.

## Where each one fits

**PostFader** is built for working on real projects safely. It starts
read-only, reads back every supported change, never saves, and is tested on
Windows and macOS for every change. Beyond direct control it analyzes your
exported mixes, picks presets from your loaded instruments, runs multi-step
jobs that can resume after a restart, reviews a draft bounce and plans one
revision, renders saved projects, and loads plug-ins on macOS. The trade-offs:
it requires FL Studio 2026, and it has no automatic rollback; you use FL
Studio's undo instead.

**karl-andres/fl-studio-mcp** is the most popular and supports the widest
range of FL Studio versions (20.7 and later). It covers transport, mixer,
channels, plug-in parameters, and Piano Roll notes. Its README notes that it
has no test suite or CI yet, and it does not analyze audio.

**rosasynthesiz/flstudio-mcp** focuses on mixing. It routes project changes
through a snapshot, readback, and rollback layer, and includes mix diagnosis,
gain staging, and reference matching. It needs FL Studio 2025 or newer, two
virtual MIDI ports, and a background daemon.

**calvinw/fl-studio-mcp** does one thing: reading and writing Piano Roll notes,
triggered by a keyboard shortcut. It is primarily for macOS.

## What about Gopher?

Gopher is the AI assistant built into FL Studio 2026. It works inside FL
Studio with Image-Line's own AI, so there's nothing to install. You can't point
Claude, Codex, or Cursor at it, though. Use PostFader when you want the AI
client you already use, analysis of your exported mixes, a receipt for every
change, or work that spans many steps. The two don't conflict.

## Why PostFader's numbers look the way they do

- **Tool count isn't a goal.** PostFader has more tools because it covers more
  ground, such as exported-mix analysis, multi-step jobs, and draft review.
  Tools that did the same job are merged: one call can change several settings
  on a mixer track, and each change is still read back on its own. Large plan
  formats are described on demand, so the full set loads compactly into your
  AI client.
- **FL Studio 2026 only.** That build contains MIDI-scripting stability fixes
  PostFader relies on, so older builds are refused instead of half-working.
- **No rollback promise.** Writing old values back can't undo everything an
  edit can change, such as a preset switch or written notes. PostFader reports
  exactly what changed and leaves undoing to FL Studio's undo history.

See the [tool reference](tools.md) for every tool and the
[FL Studio limits](fl-constraints.md) that apply to every FL Studio MCP server.
