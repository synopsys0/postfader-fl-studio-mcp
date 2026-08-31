# Canonical distribution metadata

This page is the reusable source of truth for directory listings, package
indexes, community catalogs, and maintained software lists. Copy the metadata
without strengthening its claims. A listing is not evidence that a directory
has accepted PostFader; record actual submissions in the ledger at the bottom.

## Product name

PostFader

## One-line description

The AI copilot for inspecting, mixing, controlling, composing, and arranging
inside the FL Studio project already open.

## Short description

PostFader connects Claude, Codex, Cursor, and other local MCP clients to the
project open in FL Studio. Inspect mixer routing and loaded plug-ins, diagnose
exported mixes, choose coherent sound palettes, control the session, generate
MIDI parts, and prepare patterns and arrangement markers through 111 tools and
8 live resources. Guided packages support Windows and macOS. PostFader starts
read-only and never saves automatically.

## Long description

PostFader is the local Model Context Protocol (MCP) copilot for producers using
FL Studio. Connect Claude, Codex, Cursor, or another local MCP host to the
project already open. The AI can inspect mixer inserts and routing, Channel
Rack generators, loaded effects, patterns, Playlist tracks, undo/redo history
state, transport, steps, and plug-in parameters exposed by FL Studio. It can
control the session and supported loaded plug-ins without assuming every
producer uses the same chain.

PostFader also connects production decisions across tools. Run Mix Doctor on an
exported bounce, compare loudness and tonal balance with a reference, examine
synchronized vocal and instrumental renders for likely masking, watch mixer
peaks during playback, and use your AI to turn selected findings into
reviewable mix plans. Generate chords, melody, bass, and drums; export
multi-track Type-1 MIDI; estimate tempo and key; transcribe monophonic audio;
and prepare patterns, section markers, or Piano Roll transforms. Sound Selection
can choose a coherent role-based palette from loaded generators, verify exact
preset navigation, preserve anchors across sections, and map reported drum
pads.

The v0.20 surface contains 111 tools and 8 live resources. Guided Windows and
macOS packages, dedicated Codex packages, a Claude Desktop MCPB, and Python
distributions cover different setup needs. PostFader starts read-only, requires
session-only authorization for writes, never saves automatically, and reads
supported direct changes back from FL Studio. It has no hosted PostFader
service, account, or telemetry.

## Safe claims to reuse

- PostFader is a local MCP server for FL Studio.
- Windows and macOS are supported, with the exact qualified environments
  documented in the repository.
- Startup is read-only.
- Writes require explicit session-only authorization and retain the existing
  Master protection.
- Supported direct setters use later-update readback; weaker evidence is
  labeled partial or unverified.
- The current public surface has 111 tools and 8 live resources.
- Sound Selection plans and applies exact loaded presets, coherent role-based
  palettes, section variations, and reported drum maps; it does not insert
  plug-ins or claim that a preset was heard.
- Guided setup connects the local package, Universal Bridge, virtual MIDI
  selection, client configuration, and doctor checks; it does not install a
  virtual MIDI provider.
- PostFader has no hosted PostFader service, no PostFader telemetry, and no
  automatic project save.
- Standard ZIPs, Codex ZIPs, the Claude Desktop MCPB, and Python distributions
  are separate package choices with documented boundaries.

## Claims that must not be made

- PostFader guarantees rollback or a particular undo point.
- Every action or every plug-in parameter is verified.
- Every plug-in is fully supported.
- PostFader can hear FL Studio's live audio output.
- PostFader can load every plug-in or insert, remove, and reorder plug-ins
  through the current backend.
- PostFader cannot damage or dirty a project.
- PostFader is made by, endorsed by, or affiliated with Image-Line.
- PostFader saves, backs up, or automatically restores a project.

## Categories and tags

Use only tags that fit the listing's vocabulary. The canonical set is:

`audio`, `music-production`, `mixing`, `composition`, `arrangement`, `FL Studio`,
`MCP`, `Model Context Protocol`, `AI tools`, `local-first`, `MIDI`,
`audio-analysis`, `plug-in-compatibility`, `Windows`, `macOS`.

Do not add `remote MCP`, `hosted service`, `telemetry`, `rollback`, or
`Image-Line` as a category or tag.

## Canonical links

| Purpose | Link |
| --- | --- |
| Repository | [github.com/synopsys0/postfader-fl-studio-mcp](https://github.com/synopsys0/postfader-fl-studio-mcp) |
| Latest release | [GitHub releases/latest](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/latest) |
| Setup guide | [docs/setup.md](setup.md) |
| Tool contracts | [docs/tool-contracts.md](tool-contracts.md) |
| Sound Selection | [docs/sound-selection.md](sound-selection.md) |
| Security policy | [SECURITY.md](../SECURITY.md) |
| Plug-in matrix | [docs/plugin-matrix.md](plugin-matrix.md) |
| Issue tracker | [GitHub Issues](https://github.com/synopsys0/postfader-fl-studio-mcp/issues) |
| Python package | [PyPI: postfader-fl-studio-mcp](https://pypi.org/project/postfader-fl-studio-mcp/) |

Use the repository's release page for checksums and platform assets. Do not
invent a directory listing URL, an acceptance status, or an independent
qualification result.

## Package-selection explanation

| Package | Intended user | Boundary to state clearly |
| --- | --- | --- |
| Standard Windows ZIP | Windows users of Claude Desktop, Claude Code, Cursor, OpenCode, T3 Code provider flows, Grok Build, or another local `stdio` host | Platform installer and guided setup; still requires FL Studio, the matching Universal Bridge, a virtual MIDI endpoint, and FL Studio MIDI configuration. |
| Standard macOS ZIP | macOS users of the same local MCP hosts | Same platform/setup boundary as the Windows ZIP; the package does not install or configure the IAC bus for the user. |
| Codex Windows ZIP | Codex CLI, IDE extension, or desktop Codex users on Windows | Adds Codex-specific guided registration after the same local platform setup. It is not a remote service and does not replace FL Studio, bridge, MIDI, or Python prerequisites. |
| Codex macOS ZIP | Codex users on macOS | Same Codex registration boundary; the user still completes the local FL Studio and virtual MIDI setup. |
| Claude Desktop MCPB | Users who want Claude Desktop's extension packaging after base setup | The MCPB does not replace the platform ZIP/Python installation, Universal Bridge, virtual MIDI endpoint, FL Studio MIDI settings, or doctor. It is not a general MCP host package. |
| Python wheel/source archive | Advanced users, maintainers, and environments that manage Python dependencies themselves | The user runs the documented setup flow and supplies FL Studio, a virtual MIDI provider, and a local MCP host. The source archive is not a prequalified platform installer. |

Keep package names and compatibility identifiers exactly as published. Public
prose uses **PostFader**; Python package, command, registry, and compatibility
names remain lowercase where their interfaces require it.

## Directory-maintenance ledger

Record only real submissions and verify each listing against the canonical
metadata. `—` means no submission has been made; it is not an acceptance.

| Directory name | Submission URL | Listing URL | Claimed / unclaimed | Version shown | Tool count shown | Platform description | Last verified date | Correction needed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| — | — | — | Unclaimed | — | — | — | — | — |

Before updating a listing, check the product name, one-line description, safe
claims, package boundary, repository/release links, version, tool count, and
platform text. Never claim that a directory has accepted PostFader until its
listing is visible and verified by a maintainer.
