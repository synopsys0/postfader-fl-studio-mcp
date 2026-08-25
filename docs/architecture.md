# Architecture

PostFader is a local stdio MCP server connected to an FL Studio MIDI
controller script. The v0.20 public surface contains 90 tools and 8 resources.
It is organized as a verified control kernel, a production-workflow layer, and
an optional creative layer rather than one undifferentiated raw API catalog.

```text
MCP-compatible client
        │ stdio + strict MCP argument/result schemas
        ▼
fl_studio_mcp/mcp_server.py
        ├── readonly_inspector.py ─┐
        ├── verified_writer.py ────┤
        ├── performance.py ────────┼── bridge_client.py
        ├── workflows.py ──────────┤
        ├── mixing.py ─────────────┤
        ├── creative.py ───────────┤
        │                          │        │
        │                          │        │ local SysEx over a configured virtual MIDI endpoint
        │                          │        ▼
        │                          └── device_UniversalBridge.py
        │                                   │ FL Studio MIDI scripting API
        │                                   ▼
        │                              FL Studio 2026
        │
        ├── advisory.py ── audio.py ───────── caller-selected audio files
        └── music_analysis.py ───────────────── tempo/key/transcription
```

The MCP process and bridge are local. The MCP client is a separate trust
boundary: depending on that client's design, tool arguments and results may be
sent to a remote model provider.

## Components

### MCP server

`fl_studio_mcp/mcp_server.py` defines all 90 tools, 8 resources, and their
annotations. It
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

`fl_studio_mcp/verified_writer.py` has a separate allowlist for mixer and
plug-in writes. `fl_studio_mcp/performance.py` adds
separate read and non-replayable mutation gateways for transport, global
Channel Rack targets, patterns, Playlist tracks, project history,
current-pattern step cells, live-note audition, and target-aware generator
parameters. Both paths validate ranges and pass through
the bridge's proof fields without inventing defaults. A missing or
contradictory verification field is a protocol error.

Write availability comes only from the live bridge handshake. If FL Studio was
not yet enabled for the current bridge session, the writer names the
user-confirmed `fl_set_write_mode` control rather than attempting a project
command.

`WriteModeManager` owns that capability transition through a separate gateway
whose only command is `session.set_write_mode`. Enabling requires literal
user-present confirmation plus matching provenance and session identity. The
bridge changes only its in-memory gate, and the manager performs a new
handshake before returning a typed success. Disabling uses the same absolute
command and needs no positive confirmation. The mode tool is marked
destructive so MCP clients can put approval UI in front of the access change,
but idempotent because repeating an absolute session state has no additional
effect.

Before dispatch, every mutation also requires the running bridge's stamped
source SHA-256 to match the bridge packaged with the server. Reads remain
available when provenance is missing or stale, but carry an explicit warning.
An optional 32-character bridge-lifetime session fingerprint and typed
`expected_before` state can close stale-decision races; the bridge rechecks
both after resolving the target and immediately before it requests undo or
mutates FL. The fingerprint is a concurrency token, not authentication or a
durable project identity.

### Workflow engine

`fl_studio_mcp/workflows.py` defines the closed batch-operation union and
executes it with one pinned bridge preflight. A batch is ordered and
non-atomic: each item retains its own typed verification receipt, and earlier
successes are never hidden or rolled back when a later item is unverified.

`fl_studio_mcp/mixing.py` adds process-local peak watches and mix plans plus
Mix Doctor, gain-staging, actual-bounce reference/masking recommendations,
processing intents, plug-in profiles, and finish assessment. Analysis creates
recommendations or plans; only the explicit apply surface mutates FL. Registry
IDs are intentionally process-lifetime objects.

### Creative pack

`fl_studio_mcp/creative.py` owns deterministic note generators, Type-1 MIDI
serialization/readback, pattern preparation, section markers, automation
recording helpers, and the Piano Roll script handshake. The generated Piano
Roll script runs in FL's separate `.pyscript` environment; the normal bridge
verifies the channel/pattern target, while the host reports hotkey dispatch
without fabricating note readback.

`fl_studio_mcp/music_analysis.py` estimates periodic tempo, ranks global
major/minor key candidates, and performs bounded monophonic transcription from
decoded audio. These are offline analyses and never contact FL.

### Contracts

`fl_studio_mcp/contracts.py` and `fl_studio_mcp/track_b_contracts.py` contain
immutable Pydantic response and precondition models. Unknown fields and
non-finite numbers are rejected so a malformed bridge reply cannot become a
plausible-looking result.

### Transport

`fl_studio_mcp/bridge_client.py` supports TCP and file-mailbox transports for
testing, then local MIDI SysEx for the production FL Studio connection. On the
validated macOS host, FL Studio's embedded interpreter can use neither sockets
nor files, so CoreMIDI/IAC is the retained operational path. Windows uses the
same SysEx protocol over a user-configured virtual endpoint. The v0.20 surface
at revision `3f63d43` was live-qualified on both the documented macOS arm64/IAC
host and Windows 11 x64 host; those results qualify that revision and the tested
systems rather than every possible host, virtual MIDI provider, FL Studio
build, or plug-in.

The client takes an exclusive per-user process lock for the resolved endpoint
pair and reports the owning PID when a second client tries to connect. POSIX
uses advisory locking in `/tmp`; Windows uses a byte-range lock under
`LOCALAPPDATA`. This prevents accidental duplicate ownership; it does not
authenticate messages on the shared MIDI bus.

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

`fl_studio_mcp/_bridge/device_UniversalBridge.py` runs on FL Studio's main scripting thread.
It is ASCII-only, catches errors at callback boundaries, limits work per idle
tick, and implements long scans as generators resumed over later ticks.

The installer copies this file into FL Studio's hardware-script folder and
stamps its source hash into the deployed copy. The handshake reports that hash
so a stale installation can be detected.

## Bridge command surfaces

Normal operation has four bounded bridge-command surfaces. Host-only
composition, audio, MIDI-file, registry, and plan operations do not enter this
table:

| Surface | Gate | Commands |
| --- | --- | --- |
| Read-only | Always | 15 commands covering handshake/project/selection, mixer and peaks, channels, plug-ins/presets, patterns, Playlist tracks, history, and sequencer reads |
| Session capability control | Always; enabling requires confirmation and the current session fingerprint | `session.set_write_mode` |
| Direct state changes | Current bridge session reports write mode | 39 MCP setters backed by 39 direct bridge commands across transport/project, mixer, channels, plug-ins, patterns, Playlist tracks, and sequencer state |
| Getter-limited or dispatch-only creative changes | Same session write gate | `channel.trigger_note`, `creative.prepare_piano_roll`, `arrangement.add_markers`, and `automation.record_value` |

The gate is applied before handler dispatch. Disabled writes do not appear in
the bridge's `available` list.

## Verified-write sequence

```text
handshake/provenance → resolve target → check session/expected state
                     → request undo → write → yield an FL idle tick
                     → read back → report per-field and aggregate verdicts
```

The idle-tick yield is required. FL Studio can return the previous value when
a control is read in the same callback that wrote it. Mixer, transport,
channel, and sequencer controls are verified by absolute readback within their
declared tolerance. Multi-field operations expose a proof flag for each
requested field, and aggregate `verified` is their logical AND. Plug-in
controls use their display string and numeric readback because the two FL
accessors can lag differently.

`verified: false` is a normal result: the setter returned without raising, but
the later observation did not prove the requested outcome. The bridge does not
replay the mutating command or roll the write back. Some mixer and plug-in
handlers deliberately repeat their FL-facing setter inside that single command
because FL can drop a lone call; the response still reports only readback proof.

Persistent mixer, Channel Rack, pattern, Playlist, sequencer, tempo, and plug-in
mutations request one FL Studio undo point, then observe the undo-history count
and position.
Every mutation reports `undo_point_created` as true, false, or null; transient
transport actions and live-note audition report null. Live-note audition only
proves that bounded note-on and note-off dispatch completed; it does not claim
state readback. The bridge never calls `saveProject`.

The step-grid API is explicitly current-pattern-only. A read or write names the
pattern number and a global channel index; the bridge refuses if that pattern
is not still current. A write additionally requires the canonical SHA-256
digest returned by the read and checks it immediately before changing any
cell. It never switches patterns implicitly.

Plug-in commands use a discriminated target. A `mixer_effect` keeps the
track/slot 0–9 contract and explicit Master authorization. A
`channel_generator` uses a global Channel Rack index and FL's separate
`slotIndex=-1` addressing form. The legacy track/slot MCP arguments remain
available for compatibility, but callers may not mix the two forms.

## Idle-tick budget

FL Studio calls `OnIdle` roughly every 20 milliseconds. A peak-enabled full
mixer scan can require thousands of API calls, so mixer and parameter scans are
chunked and resumed across ticks. No background thread calls the FL Studio API.

`tests/test_tick_budget.py` checks the bridge against a deterministic fake API
and holds the worst simulated tick under a fixed ceiling.

## Security properties and boundaries

- Read-only bridge mode is the default.
- Enabling writes is session-only, requires an explicit user-present
  confirmation, and is verified by a second handshake.
- MCP input and bridge output use strict schemas and bounded values.
- Mutating commands are narrow, separately gated, and never replayed after an
  ambiguous response.
- Stale or unrecognized bridge provenance fails closed for mutation while
  preserving warning-bearing reads.
- Optional session and expected-state preconditions are enforced inside the
  bridge immediately before mutation.
- Master writes require explicit targeting.
- Readback is observation, not rollback or an audible-quality guarantee.
- The IAC transport is local but unauthenticated.
- Direct audio measurement can read caller-selected absolute audio paths.
- The connected MCP client and its model provider are outside this
  repository's trust boundary.

See [SECURITY.md](../SECURITY.md) for deployment guidance and vulnerability
reporting.
