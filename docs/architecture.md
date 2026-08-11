# Architecture

Postfader is a local stdio MCP server connected to an FL Studio MIDI
controller script. The public MCP surface contains 24 tools: ten inspection
tools, ten opt-in verified-write tools, and four audio-file tools.

```text
MCP-compatible client
        │ stdio + strict MCP argument/result schemas
        ▼
fl_studio_mcp/mcp_server.py
        ├── readonly_inspector.py ─┐
        ├── verified_writer.py ────┼── bridge_client.py
        │                          │        │
        │                          │        │ local MIDI SysEx over CoreMIDI/IAC
        │                          │        ▼
        │                          └── device_UniversalBridge.py
        │                                   │ FL Studio MIDI scripting API
        │                                   ▼
        │                              FL Studio 2026
        │
        └── advisory.py ── audio.py ── caller-selected audio files
```

The MCP process and bridge are local. The MCP client is a separate trust
boundary: depending on that client's design, tool arguments and results may be
sent to a remote model provider.

## Components

### MCP server

`fl_studio_mcp/mcp_server.py` defines all 24 tools and their annotations. It
uses strict generated argument models that reject unknown fields, so a
misspelled argument fails instead of being silently ignored. Blocking bridge
and audio work runs off the MCP event loop.

The server does not expose a generic bridge-command tool. Every operation is a
named MCP tool with a bounded schema.

### Read-only inspection

`fl_studio_mcp/readonly_inspector.py` converts bridge replies into typed
contracts, enforces a fixed read-command allowlist, and applies the FL Studio,
MIDI API, and bridge-protocol compatibility gates.

Plug-in parameters with no connector profile are returned as
`profile_status="unprofiled_read_only"` and `safe_to_modify=false`. A caller
can observe the normalized value, name, and optional display string, but the
inspector does not infer what the control means.

### Verified writes

`fl_studio_mcp/verified_writer.py` has a separate allowlist containing only the
ten supported write commands. It validates normalized ranges, rejects an
implicit Master target, and passes through the bridge's readback verdict
without inventing a default. A missing `verified` field is a protocol error.

Write availability comes only from the live bridge handshake. If FL Studio was
not launched with `FL_BRIDGE_ENABLE_WRITES=1`, the writer raises an error that
names the missing opt-in rather than attempting a command.

### Contracts

`fl_studio_mcp/contracts.py` contains immutable Pydantic response models.
Unknown fields and non-finite numbers are rejected so a malformed bridge reply
cannot become a plausible-looking result.

### Transport

`fl_studio_mcp/bridge_client.py` supports TCP and file-mailbox transports for
testing, then local MIDI SysEx for the production FL Studio connection. On the
validated macOS host, FL Studio's embedded interpreter can use neither sockets
nor files, so CoreMIDI/IAC is the operational transport.

The client takes an exclusive local process lock for the resolved IAC port and
reports the owning PID when a second client tries to connect. This prevents
accidental duplicate ownership; it does not authenticate messages on the
shared MIDI bus.

After an ambiguous transport failure, a command is replayed at most once and
only if it is independently classified as read-only. A write with a lost
response has an unknown outcome and is never retried automatically.

### Audio measurement

`fl_studio_mcp/advisory.py` is the typed boundary around the DSP in
`fl_studio_mcp/audio.py`. The FL Studio scripting API provides no audio
buffers, so these modules measure exported or recorded files.

Direct measurement tools accept an absolute audio-file path supplied by the
MCP client, subject to format, regular-file, size, duration, and path-shape
checks. Recent-bounce discovery is different: it searches only a bounded,
fixed set of normal FL Studio output and project folders. Results include
canonical paths and file hashes but no audio samples.

### FL Studio bridge

`bridge/device_UniversalBridge.py` runs on FL Studio's main scripting thread.
It is ASCII-only, catches errors at callback boundaries, limits work per idle
tick, and implements long scans as generators resumed over later ticks.

The installer copies this file into FL Studio's hardware-script folder and
stamps its source hash into the deployed copy. The handshake reports that hash
so a stale installation can be detected.

## Bridge command surfaces

Normal operation has two disjoint command sets:

| Surface | Gate | Commands |
| --- | --- | --- |
| Read-only | Always | `ping`, `project.info`, `arrangement.selection`, `mixer.list`, `mixer.track`, `plugin.params`, `plugin.scan_params`, `channels.list` |
| Verified writes | `FL_BRIDGE_ENABLE_WRITES=1` in the FL Studio process | `mixer.set_volume`, `mixer.set_pan`, `mixer.set_mute`, `mixer.set_eq`, `mixer.set_name`, `mixer.set_send`, `mixer.set_send_level`, `plugin.set_param`, `plugin.set_param_display`, `plugin.set_param_option` |

The gate is applied before handler dispatch. Disabled writes do not appear in
the bridge's `available` list.

## Verified-write sequence

```text
validate → write → yield one FL idle tick → read back → report verdict
```

The idle-tick yield is required. FL Studio can return the previous value when
a control is read in the same callback that wrote it. Mixer controls are
verified by numeric readback within a narrow tolerance. Plug-in controls use
their display string and numeric readback because the two FL accessors can lag
differently.

`verified: false` is a normal result: the setter returned without raising, but
the later observation did not prove the requested outcome. The bridge does not
retry or roll the write back.

Each write requests one FL Studio undo point, then observes the undo-history
count and position. The response reports `undo_point_created` as true, false,
or null. The bridge never calls `saveProject`.

## Idle-tick budget

FL Studio calls `OnIdle` roughly every 20 milliseconds. A peak-enabled full
mixer scan can require thousands of API calls, so mixer and parameter scans are
chunked and resumed across ticks. No background thread calls the FL Studio API.

`tests/test_tick_budget.py` checks the bridge against a deterministic fake API
and holds the worst simulated tick under a fixed ceiling.

## Security properties and boundaries

- Read-only bridge mode is the default.
- MCP input and bridge output use strict schemas and bounded values.
- Mutating commands are narrow, separately gated, and never replayed after an
  ambiguous response.
- Master writes require explicit targeting.
- Readback is observation, not rollback or an audible-quality guarantee.
- The IAC transport is local but unauthenticated.
- Direct audio measurement can read caller-selected absolute audio paths.
- The connected MCP client and its model provider are outside this
  repository's trust boundary.

See [SECURITY.md](../SECURITY.md) for deployment guidance and vulnerability
reporting.
