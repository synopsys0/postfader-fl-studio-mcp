# PostFader FAQ

Short answers about PostFader, the FL Studio MCP server. For step-by-step
installation, see the [setup guide](setup.md).

- [Getting started](#getting-started)
- [Using PostFader](#using-postfader)
- [Safety and privacy](#safety-and-privacy)
- [Troubleshooting](#troubleshooting)
- [Compatibility](#compatibility)

## Getting started

### What is an FL Studio MCP server?

MCP (Model Context Protocol) is the open standard AI assistants use to call
tools. An FL Studio MCP server gives the assistant tools for FL Studio, so it
can read and change your project instead of only describing what to click.
PostFader is one, and it runs entirely on your computer.

### What do I need?

- FL Studio 2026, version 26.1.3 build 5336 or newer.
- Windows (tested on Windows 11) or macOS.
- Python 3.10 to 3.14.
- One virtual MIDI port: the built-in IAC Driver on macOS, or an app such as
  loopMIDI on Windows.
- An AI client that can run local MCP servers, such as Claude Desktop, Claude
  Code, Codex, or Cursor.

### Which download should I pick?

| You use | Download |
| --- | --- |
| Claude, Cursor, or another client on Windows | `PostFader-vX.Y.Z-Windows.zip` |
| Claude, Cursor, or another client on macOS | `PostFader-vX.Y.Z-macOS.zip` |
| Codex on Windows or macOS | The matching `-Codex-` ZIP, which also registers PostFader with Codex |
| Claude Desktop, after running one of the above | The `.mcpb` extension, as an alternative to pasting configuration |
| Your own Python environment | `pip install postfader-fl-studio-mcp`, then `postfader setup` |

All of them need the FL Studio MIDI settings step described in the
[setup guide](setup.md#3-configure-fl-studio-and-the-virtual-endpoint).

### Do I need loopMIDI?

On Windows you need one virtual MIDI port, and loopMIDI is a common free
choice. Create one port, keep the app running, and select that port in
PostFader's setup and in FL Studio. On macOS, enable the IAC Driver in Audio
MIDI Setup instead; nothing extra is needed. PostFader doesn't install MIDI
software for you.

### Is it free?

Yes. PostFader is open source under the Apache License 2.0, with no account,
subscription, or hosted service.

## Using PostFader

### Do I have to learn the tool names?

No. Ask for what you want in plain language and your AI picks the tools. The
[tool reference](tools.md) is there if you want to see what's possible.

### How do I let it make changes?

Ask for the change. The AI turns on write mode for your session, then makes
the edit. To stop edits, ask it to turn write mode off, or reload the project.
A multi-step job (a [Production Run](production-runs.md)) turns write mode on
once for that job when you asked for changes.

### What does "verified" mean?

After a supported edit, PostFader waits for FL Studio's next update and reads
the control again. `verified: true` means FL Studio reports the value you asked
for. It doesn't mean the change sounds good, and it doesn't create a save
point.

### Can it hear my song?

It analyzes audio files you export: loudness, true peak, tonal balance, stereo
width, masking between a vocal and the instrumental, and differences from a
reference. FL Studio doesn't give scripts access to its live output, so
PostFader can't listen in real time.

### Can it load plug-ins?

On macOS, yes: it adds instruments and effects from FL Studio's Add menu. That
needs Accessibility access for the app that runs PostFader, English menus, and
write mode. On Windows, load plug-ins yourself; PostFader can then read and set
their parameters and presets.

### Can it render or save my song?

It never saves your project. It can render a project you have already saved
(.flp) to a WAV file in a separate FL Studio process; unsaved changes aren't
included.

### Can it create Playlist clips?

No. FL Studio's scripting API doesn't allow creating, moving, or deleting
Playlist clips. PostFader can name and color Playlist tracks, prepare
patterns, add section markers, and tell you which placements to make by hand.
See [FL Studio limits](fl-constraints.md).

### Does it work on Piano Roll notes?

Yes. It can read notes, write new ones, and quantize, transpose, humanize,
duplicate, delete, or clear them. FL Studio runs Piano Roll scripts separately,
so once per PostFader session you run **Scripts → Postfader → Postfader
Apply** in a Piano Roll window when the AI asks.

## Safety and privacy

### Will it mess up my project?

PostFader starts read-only, refuses edits until write mode is on, reads back
every supported change, and never saves. If you don't like an edit, use FL
Studio's undo, or ask the AI to undo it. Try your first edits on a copy of a
project.

### Is there automatic rollback?

No. A batch of edits runs in order, and if one fails, the earlier ones stay
applied and are reported. An edit whose outcome is unknown is never retried
automatically. Use undo, which PostFader can also step through for you.

### Does PostFader upload my music or project?

No. PostFader has no server, account, or telemetry. Your AI client is separate
software: it sends your messages and tool results to its model provider under
that client's settings.

### Can anything else control FL Studio through PostFader?

The virtual MIDI port has no authentication, so use PostFader on a computer you
trust and don't share. See [SECURITY.md](../SECURITY.md) for the full model.

## Troubleshooting

Start with the doctor. It checks every link in the chain and names the first
thing to fix:

```text
postfader-doctor --midi-port "Your Exact Port Name"
```

### Universal Bridge isn't in FL Studio's controller list

The bridge script isn't installed in this FL Studio user-data folder. Quit FL
Studio, run `postfader setup` (or the installer) again, and start FL Studio.

### Script output doesn't end with `ready: MIDI SysEx`

FL Studio needs your port as both input and output with the same port number.
Fix it in *Options → MIDI settings*, then reload Universal Bridge from
*View → Script output*.

### The AI says FL Studio isn't connected

Check that FL Studio is running with the script loaded, that the port name in
your client configuration matches exactly, and that only one MCP client is
using PostFader at a time. Two running copies can't share one MIDI port.

### Edits are refused

Write mode is off, or the project was reloaded since it was turned on. Ask the
AI to turn write mode on and try again. Changes to the Master track also need
the request to name the Master track explicitly.

### It stopped working after an upgrade

Upgrade in this order: close your AI client and FL Studio, upgrade PostFader,
install the bridge from the same version (`postfader setup`), start FL Studio
and reload Universal Bridge, then reconnect the client. Mismatched versions
refuse to connect rather than guess.

### `pip install` fails building `python-rtmidi`

Python 3.13, 3.14, and Windows on ARM can need a C++ compiler for this
dependency. Install the build tools, or use Python 3.10 to 3.12.

### Plug-in loading fails on macOS

Give the app that runs PostFader (your terminal or AI client) Accessibility
access in System Settings, use FL Studio's English menus, and turn write mode
on. Then ask the AI to list the available plug-ins first.

## Compatibility

### Which FL Studio versions and editions work?

FL Studio 2026, version 26.1.3 build 5336 or newer. Older builds are refused
because they lack MIDI-scripting fixes PostFader relies on. Live testing used
Producer Edition on macOS and Windows 11.

### Which AI clients work?

Any client that can start a local stdio MCP server on the same computer:
Claude Desktop, Claude Code, Codex, Cursor, OpenCode, and others. Chat apps
that only connect to remote MCP servers over the internet can't reach a local
FL Studio.

### Does it work on Linux?

No. PostFader supports FL Studio on Windows and macOS.

### How is PostFader different from Gopher in FL Studio?

Gopher is the assistant built into FL Studio 2026 and uses Image-Line's AI.
PostFader connects the AI client you choose, adds analysis of your exported
mixes, and reads back each change it makes. They don't conflict. See the
[comparison](comparison.md).

### How is it different from other FL Studio MCP servers?

See the [comparison](comparison.md), which lists features, platforms, and
safety side by side.
