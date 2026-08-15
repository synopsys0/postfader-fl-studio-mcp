<div align="center">

# Postfader

**An AI copilot for FL Studio**

Inspect a running project, measure bounces, and make readback-verified mixer,
Channel Rack, sequencer, transport, and plug-in changes from an MCP client.

[![CI](https://github.com/synopsys0/postfader-fl-studio-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/synopsys0/postfader-fl-studio-mcp/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache_2.0-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10--3.14-blue)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows-lightgrey)](#status)

[Setup](docs/setup.md) · [Windows acceptance](docs/windows-acceptance.md) ·
[Tools](docs/tool-contracts.md) · [Architecture](docs/architecture.md) ·
[Security](SECURITY.md)

</div>

Postfader is a local Model Context Protocol server and FL Studio MIDI script.
It does not expose a generic FL API. Persistent writes use absolute targets,
are read back on a later FL idle tick, and report whether the requested state
was actually observed. Reads are always available; writes remain absent from
the FL-side allowlist unless FL Studio itself starts in write mode.

## Status

Version 0.13.0 is a cross-platform release candidate:

- macOS retains the v0.12 Apple-silicon evidence for FL Studio 2026 Producer
  Edition 26.1.3 build 5336 and MIDI scripting API 44, but v0.13 changes MIDI
  framing/correlation, so a fresh macOS live smoke is a release gate;
- Windows 11 x64 has native path discovery, process ownership, bootstrap,
  diagnostics, configuration generation, CI, packaging, and hermetic tests;
- the Windows FL Studio and virtual-MIDI path still requires the supervised
  acceptance run in [docs/windows-acceptance.md](docs/windows-acceptance.md)
  before Windows support is called validated.

Postfader does **not** install or configure a virtual MIDI driver. Select a
provider appropriate for your host, create one bidirectional virtual endpoint,
and configure its exact name in both FL Studio and Postfader. The macOS default
query `IAC Driver` remains for compatibility. Windows intentionally has no
provider-specific default.

No user FL projects, recordings, stems, samples, presets, exports, or
session-derived evidence are stored in this repository.

## What it provides

The public server exposes 36 tools:

- 12 FL inspection tools;
- 19 opt-in, readback-verified persistent state tools;
- one separately classified, bounded live-note audition tool; and
- four local audio-file analysis tools.

FL Studio's MIDI scripting API exposes no live audio buffers or render command,
so audio tools operate only on files explicitly selected by the caller.

## Safety model

> Write tools change the open project immediately. Readback is proof of the
> observed state, not an automatic rollback or a guarantee of audible quality.
> Use a disposable copy until the workflow is trusted.

- The default bridge mode is read-only.
- Write enablement belongs only to the FL Studio child process, never the MCP
  server configuration.
- Master is refused unless a tool explicitly receives `allow_master=true`.
- The running bridge source hash must match the installed package source.
- Mutations are never replayed after an ambiguous transport result.
- The bridge never calls `saveProject`.
- Only one MCP process may own a resolved virtual endpoint pair at a time.

## Requirements

- FL Studio 2026 version 26.1.3 build 5336 or newer
- MIDI scripting API 44 or newer
- Python 3.10 through 3.14
- macOS or Windows 11 x64
- an MCP-compatible client
- a user-configured bidirectional virtual MIDI endpoint for live FL transport

Python 3.13/3.14 and Windows ARM64 may require a native compiler for
`python-rtmidi`; the release CI currently proves x64-hosted Windows and macOS.

## Install from a source checkout

Launch FL Studio once, then quit it, so the user-data tree exists. Clone the
repository and use the native bootstrap for the host.

Windows PowerShell:

```powershell
git clone https://github.com/synopsys0/postfader-fl-studio-mcp.git
Set-Location postfader-fl-studio-mcp
.\scripts\install.ps1 -DryRun
.\scripts\install.ps1
```

macOS:

```bash
git clone https://github.com/synopsys0/postfader-fl-studio-mcp.git
cd postfader-fl-studio-mcp
./scripts/install.sh
```

Both paths create/reuse `.venv`, install the package, and deploy the FL MIDI
script. Neither overwrites MCP client configuration. If automatic discovery
is wrong, pass an absolute user-data directory:

```powershell
.\scripts\install.ps1 -UserDataDir 'D:\FL Data\Image-Line\FL Studio'
```

```bash
FL_STUDIO_USER_DATA_DIR='/absolute/path/to/FL Studio' ./scripts/install.sh
```

On Windows, default discovery uses the Windows Known Documents folder, so a
OneDrive-redirected Documents directory is supported. Explicit and environment
paths must be absolute; configured paths need not exist when configuration is
generated, but the installer requires the expected FL directory structure.

Installed-package users can instead run:

```text
pip install postfader-fl-studio-mcp
postfader-install-bridge
```

Use `postfader-install-bridge --help` first when an explicit user-data path is
required. The install command must complete before FL Studio can list
`Universal Bridge`.

### Claude Desktop extension (`.mcpb`)

Opening the release `.mcpb` in Claude Desktop registers and runs the MCP
server, but the bundle does **not** deploy FL Studio's controller script or
configure a virtual MIDI endpoint. Before connecting the extension, install
the matching Python package or use a matching source checkout, run
`postfader-install-bridge`, and complete the FL input/output setup below. The
extension never enables writes.

### Mandatory upgrade order

For every install method, close MCP clients and FL Studio first. Upgrade the
server/package or extension, deploy the bridge from that same version, then
start FL Studio and reload `Universal Bridge` before reconnecting the MCP
client. A mixed old-bridge/new-client session is deliberately refused. Command
protocol version 2 identifies the request/response API; MIDI wire protocol
version 2 separately identifies the bounded SysEx framing required on both
macOS and Windows.

## Configure the virtual endpoint and FL script

In FL Studio, open **Options → MIDI settings**:

1. Enable the virtual endpoint as an input and choose controller type
   `Universal Bridge`.
2. Assign a Port number.
3. Enable the matching output and assign the same Port number.
4. Open **View → Script output** and reload the script.

The script should report `ready: MIDI SysEx`. After an upgrade, do not connect
the MCP client until this reload has completed. The endpoint name supplied to
Postfader is matched case-insensitively: an exact match wins; otherwise one
unique substring is allowed for macOS compatibility. Ambiguous matches are
refused before ownership or endpoint open.

## Generate MCP client configuration

The generator emits absolute, host-correct paths and never overwrites a file.
For an ordinary live, read-only Windows connection, configure MIDI explicitly;
write tools remain independently disabled:

```powershell
$Port = 'Exact Virtual MIDI Endpoint Name'
.\.venv\Scripts\python.exe scripts\generate_mcp_config.py `
  --format codex-toml `
  --repository-root $PWD `
  --python "$PWD\.venv\Scripts\python.exe" `
  --user-data-dir 'C:\ABSOLUTE\Image-Line\FL Studio' `
  --transport midi `
  --midi-port $Port
```

macOS:

```bash
PORT='Exact IAC Bus Name'
./.venv/bin/python scripts/generate_mcp_config.py \
  --format codex-toml \
  --repository-root "$PWD" \
  --python "$PWD/.venv/bin/python" \
  --user-data-dir "$HOME/Documents/Image-Line/FL Studio" \
  --transport midi \
  --midi-port "$PORT"
```

Omitting `--transport midi` produces an offline/fail-closed configuration with
no native MIDI transport; it is not the normal live FL setup. Formats are
`codex-toml`, `codex-command`, and `claude-json`. Use `--output` only for a new
destination. The generated Codex command follows the supported
`codex mcp add NAME --env KEY=VALUE -- COMMAND ...` form; verify registration
with `codex mcp list`. The checked-in `.mcp.json.example` is an explicitly live
template: it contains conspicuous absolute-path and endpoint placeholders plus
both MIDI environment variables. Replace every placeholder before using it.

Generated environments never contain `FL_BRIDGE_ENABLE_WRITES`.

## Diagnose the installation

```powershell
$Port = 'Exact Virtual MIDI Endpoint Name'
.\.venv\Scripts\postfader-doctor.exe --midi-port $Port --json
```

```bash
./.venv/bin/postfader-doctor --json
```

The doctor reports platform, package/Python versions, FL candidates, resolved
user-data source, bridge source/deployment hashes, configured MIDI query and
selected endpoints, compatibility, session, bridge mode, and write state.
JSON is strict machine-readable output; the human view is rendered from the
same evidence. On Windows, missing endpoint configuration fails before native
MIDI enumeration; pass `--midi-port 'Exact Endpoint Name'` or set
`FL_BRIDGE_MIDI_PORT`. On macOS, the no-argument doctor enables MIDI and uses
the compatible `IAC Driver` query by default. The live connection validates
command protocol 2 and the separately versioned MIDI wire protocol before
ordinary requests.

The installed reporter and checkout live utilities accept the same explicit
Windows endpoint option:

```powershell
.\.venv\Scripts\postfader-plugin-report.exe --midi-port $Port --track 1 --slot 0
& .\.venv\Scripts\python.exe .\scripts\inspect_readonly.py --midi-port $Port
```

Use `FL_BRIDGE_SANDBOXED=1` for CI or any process that must not enumerate or
open MIDI. In that mode the doctor reports the skipped transport explicitly.

## Read-only and write-mode FL launches

Windows source checkouts include a safe launcher:

```powershell
.\scripts\launch_fl_studio.ps1 -DryRun
.\scripts\launch_fl_studio.ps1
```

Read-only is the default. The launcher refuses a real launch while FL Studio
is already running and restores its own environment after child creation. For
a supervised disposable-project session only:

```powershell
.\scripts\launch_fl_studio.ps1 -EnableWrites
```

On macOS, quit FL Studio and use
`FL_BRIDGE_ENABLE_WRITES=1 open -a 'FL Studio 2026'` for the same explicit
write-mode child. Never put this variable in MCP configuration.

## Supervised acceptance harnesses

The source checkout provides three evidence-producing entry points. Use the
checkout interpreter so the commands do not depend on ambient Python packages.

Windows PowerShell:

```powershell
& .\.venv\Scripts\python.exe .\scripts\live_read_acceptance.py --help
& .\.venv\Scripts\python.exe .\scripts\live_write_acceptance.py --help
& .\.venv\Scripts\python.exe .\scripts\live_note_acceptance.py --help
```

macOS:

```bash
./.venv/bin/python scripts/live_read_acceptance.py --help
./.venv/bin/python scripts/live_write_acceptance.py --help
./.venv/bin/python scripts/live_note_acceptance.py --help
```

The read harness derives and exercises every public read-only MCP tool,
including bounded large mixer and plug-in scans. Each live read has an
isolated worker and parent-owned deadline; ordinary completed failures remain
collectable, while a timeout is durably identified, reaped, and stops later
reads. The persistent-write harness requires a reviewed scenario covering
every authoritative write tool exactly once, captures before state, applies
one mutation attempt, restores, and independently rereads the target. It stops
loudly on a failed restore and never saves. Live-note audition is kept in its
own harness because it has no persistent-state restoration contract.

These scripts perform real FL/MIDI activity unless `--plan` is used. Windows
release owners must follow [docs/windows-acceptance.md](docs/windows-acceptance.md);
macOS release owners must also complete the concise v0.13 transport smoke in
[docs/setup.md](docs/setup.md#required-macos-v013-transport-smoke).

## Testing and release verification

The safe suite has an explicit allowlist and runs with MIDI disabled and
sandboxed in every child, even under hostile ambient transport variables:

```powershell
.\.venv\Scripts\python.exe scripts\run_safe_tests.py
```

`tests/test_midi_transport.py` is intentionally excluded because it touches a
real shared MIDI resource. CI covers Windows and macOS and separately validates
MCPB packaging. Release checks also inspect the wheel/sdist and install the
wheel into a clean temporary venv before executing every console `--help`.

## Important limitations

- Fresh macOS v0.13 live transport regression is pending after the MIDI wire
  framing/correlation change; retained v0.12 evidence is not a substitute.
- Windows live FL/MIDI validation is pending the supervised acceptance record.
- Plug-in insertion, removal, and reordering are not exposed.
- Playback speed lacks an authoritative public getter and is not exposed.
- FL scripted per-slot bypass/wet-dry behavior was unreliable on the validated
  host and is not exposed.
- Step reads observe at most 512 cells; verified writes have tighter atomic
  call-budget limits documented in [docs/tool-contracts.md](docs/tool-contracts.md).
- Playlist selection endpoints are raw observations, not a rendering contract.
- Readback proves observed control state, not musical correctness.
- The local virtual MIDI bus is shared and unauthenticated.

## Privacy and security

Postfader runs locally and implements no telemetry or hosted service. Your MCP
client may send tool arguments/results to its model provider. Audio results
contain measurements, canonical paths, and hashes, not audio samples. Recent
bounce discovery is bounded to normal FL user-data folders; direct audio tools
can read a caller-selected absolute file path subject to validation.

See [SECURITY.md](SECURITY.md) and [docs/setup.md](docs/setup.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

<!-- mcp-name: io.github.synopsys0/postfader-fl-studio-mcp -->
