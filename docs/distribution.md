# Canonical distribution metadata

This page is the reusable source of truth for directory listings, package
indexes, community catalogs, and maintained software lists. Copy the metadata
without strengthening its claims. A listing is not evidence that a directory
has accepted PostFader; record actual submissions in the ledger at the bottom.

## Product name

PostFader

## One-line description

A local, verified-control MCP copilot for FL Studio mixing, plug-ins,
composition, arrangement, and audio analysis.

## Short description

PostFader is a local MCP server for FL Studio. It starts read-only, discovers
the open project through a Universal Bridge, and supports guarded controls with
later-update readback for supported setters. It provides 90 tools, 8 live
resources, guided setup, Windows and macOS packages, deterministic composition,
and bounded audio-file analysis. PostFader has no hosted service, telemetry, or
automatic project save.

## Long description

PostFader is a local Model Context Protocol (MCP) copilot for producers using
FL Studio. An AI client launches the server over local `stdio`; the server
communicates with the open project through a user-selected virtual MIDI
endpoint and the PostFader Universal Bridge. The v0.20 surface contains 90
tools and 8 live resources for project inspection, mixer and plug-in control,
Channel Rack and pattern work, Playlist and transport workflows, composition,
Piano Roll preparation, Mix Doctor, reference and masking analysis, and
bounded audio-file measurements.

PostFader starts read-only. Session-only write authorization is explicit,
Master changes require separate protection, and supported direct setters wait
for a later FL Studio update before reporting readback evidence. Partial,
unverified, and focus-sensitive workflows say so. The server never saves a
project automatically, has no hosted PostFader service, and does not collect
telemetry. Windows and macOS packages, dedicated Codex packages, a Claude
Desktop MCPB, and Python distributions are published for different setup
needs. FL Studio, virtual MIDI software, client accounts, and user plug-ins
remain outside the package boundary.

## Safe claims to reuse

- PostFader is a local MCP server for FL Studio.
- Windows and macOS are supported, with the exact qualified environments
  documented in the repository.
- Startup is read-only.
- Writes require explicit session-only authorization and retain the existing
  Master protection.
- Supported direct setters use later-update readback; weaker evidence is
  labeled partial or unverified.
- The current public surface has 90 tools and 8 live resources.
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
