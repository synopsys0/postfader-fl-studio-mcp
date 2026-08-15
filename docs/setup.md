# Setup and usage

Postfader 0.13.0 is a cross-platform release candidate. Its changed MIDI
framing/correlation path has fresh supervised evidence on macOS 27.0 arm64
with FL Studio 2026 Producer Edition 26.1.3 build 5336 and MIDI scripting API
44. Windows implementation and hermetic coverage are complete, but the
supervised FL Studio/virtual-MIDI evidence in
[windows-acceptance.md](windows-acceptance.md) is still required before calling
that path validated.

Postfader does not install or configure virtual MIDI software. Configure one
bidirectional virtual endpoint yourself, then give Postfader its exact name.

## 1. Find the FL Studio user-data directory

The directory contains `Settings/Hardware` and is not necessarily beside the
FL Studio executable. On Windows, Postfader asks the Known Documents API first,
which handles OneDrive or enterprise redirection. On macOS the conventional
location is under `~/Documents/Image-Line/FL Studio`.

All explicit paths and `FL_STUDIO_USER_DATA_DIR` values must be absolute. This
prevents the installer and MCP client from resolving one configuration against
different working directories. A configuration path may be generated before
it exists; installation still requires the expected parent layout.

## 2. Preview and install

Quit FL Studio before deploying or replacing the bridge.

Windows PowerShell:

```powershell
.\scripts\install.ps1 -DryRun
.\scripts\install.ps1
```

Override discovery when needed:

```powershell
.\scripts\install.ps1 -UserDataDir 'D:\FL Data\Image-Line\FL Studio'
```

The script uses `.venv\Scripts\python.exe`, creates the venv if needed,
installs the checkout editable, and invokes the same packaged bridge installer
as the console command. It does not edit MCP client configuration or persist
environment variables.

macOS:

```bash
./scripts/install.sh
```

Override discovery with an absolute environment value:

```bash
FL_STUDIO_USER_DATA_DIR='/absolute/path/to/FL Studio' ./scripts/install.sh
```

From an installed Python distribution, deploy the controller script with:

```text
postfader-install-bridge
```

Use `postfader-install-bridge --help` when an explicit user-data path is
required. FL Studio cannot list `Universal Bridge` until deployment succeeds.

### Claude Desktop extension and upgrades

The release `.mcpb` registers the local MCP server in Claude Desktop. It does
not deploy `device_UniversalBridge.py`, create a virtual endpoint, or edit FL
Studio MIDI settings. Install the matching Python distribution or use the
matching checkout, run `postfader-install-bridge`, and configure the endpoint
before connecting the extension. The bundle never enables writes.

Upgrades have one mandatory order on both hosts: close MCP clients and FL
Studio; upgrade the server/package or extension; deploy the bridge from that
same version; start FL Studio and reload `Universal Bridge`; only then
reconnect the MCP client. Mixed versions fail closed. Command protocol 2
describes the bridge command API, while MIDI wire protocol 2 separately
describes the bounded SysEx framing and must match before the first request.

## 3. Configure FL Studio and the virtual endpoint

Create or enable one virtual MIDI endpoint using host software you trust. On
macOS, an IAC Driver bus is the established validation path. Windows has no
provider-specific default.

In **Options → MIDI settings**:

1. Select the endpoint under Input, enable it, choose `Universal Bridge`, and
   assign a Port number.
2. Select the matching Output, enable it, and assign the same Port number.
3. Open **View → Script output** and reload the script. After an upgrade,
   complete this reload before reconnecting any MCP client.

The expected final line is `ready: MIDI SysEx`. The bridge refuses to become
ready if output is missing because responses travel over that endpoint.

Postfader endpoint matching is deterministic. A case-insensitive exact match
wins. If none exists, one unique case-insensitive substring may be used so the
macOS `IAC Driver` default remains compatible. Zero or multiple matches fail
with bounded candidates before any ownership lock or endpoint open.

## 4. Generate client configuration

Configuration generation is pure and create-only. For an ordinary live,
read-only Windows connection, supply absolute paths and the exact endpoint;
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

Available formats:

- `codex-toml` emits `[mcp_servers.fl-studio]` configuration;
- `codex-command` emits a PowerShell-safe `codex mcp add` command;
- `claude-json` emits the `mcpServers` JSON shape.

Automatic mode emits only `FL_STUDIO_USER_DATA_DIR`. It is an offline,
fail-closed configuration with no native MIDI transport, not the ordinary live
FL setup. Live mode requires:

```text
--transport midi --midi-port "Exact Endpoint Name"
```

This adds `FL_BRIDGE_ENABLE_MIDI=1` and `FL_BRIDGE_MIDI_PORT`. It never adds
`FL_BRIDGE_ENABLE_WRITES`. Use `--output` to create a new file; an existing
destination is refused. `.mcp.json.example` is a placeholder template and is
not automatically installed. It is explicitly configured for live MIDI and
therefore includes both MIDI environment variables; replace its endpoint and
absolute-path placeholders before use.

Codex CLI uses the supported `codex mcp add NAME --env KEY=VALUE -- COMMAND`
shape; inspect registered servers with `codex mcp list`. Codex CLI and the
Codex desktop/IDE surfaces share the user MCP configuration. Client-specific
approval and trust prompts still apply.

Only one MCP process may own a selected endpoint pair. Disable duplicate
project/user registrations before testing.

## 5. Run the doctor

```powershell
$Port = 'Exact Virtual MIDI Endpoint Name'
.\.venv\Scripts\postfader-doctor.exe --midi-port $Port --json
```

or:

```bash
./.venv/bin/postfader-doctor --json
```

The JSON and human views are derived from the same evidence. They include:

- platform, architecture, Python, and package version;
- FL executable candidates and selected path;
- user-data path plus resolution source;
- hardware/script locations and repository/deployed bridge hashes;
- configured endpoint query, enumerated candidates, and selected input/output;
- live connection attempt/transport, FL/build/API/protocol/hash/session data;
- bridge mode, read-only state, and verified-write availability.

Important failure classes remain distinct: FL missing, hardware directory
missing, bridge missing/stale, endpoint not configured, zero endpoints,
configured endpoint missing/ambiguous, output missing, native probe failure,
live transport unavailable, incompatible handshake, and stale running bridge.

On Windows, a missing MIDI query fails before native enumeration. Set
`--midi-port "Exact Endpoint Name"` on the doctor, or export
`FL_BRIDGE_MIDI_PORT`. On macOS, the no-argument doctor enables MIDI and keeps
the compatible `IAC Driver` default. A live handshake validates command
protocol 2 separately from MIDI wire protocol 2 before normal requests.

Installed and checkout live utilities use the same endpoint option on Windows:

```powershell
.\.venv\Scripts\postfader-plugin-report.exe --midi-port $Port --track 1 --slot 0
& .\.venv\Scripts\python.exe .\scripts\inspect_readonly.py --midi-port $Port
& .\.venv\Scripts\python.exe .\scripts\validate_selection_readonly.py --midi-port $Port
```

Set `FL_BRIDGE_SANDBOXED=1` whenever native MIDI access is prohibited; this skips
enumeration and live handshake and reports the skip rather than claiming
success.

## 6. Read-only and write-mode operation

The normal FL launch is read-only. On Windows:

```powershell
.\scripts\launch_fl_studio.ps1 -DryRun
.\scripts\launch_fl_studio.ps1
```

The launcher discovers or accepts an absolute FL executable, refuses a real
launch if any FL process is already running, and restores its own process
environment after child creation. For supervised disposable-project tests:

```powershell
.\scripts\launch_fl_studio.ps1 -EnableWrites
```

On macOS, quit FL Studio and use:

```bash
FL_BRIDGE_ENABLE_WRITES=1 open -a 'FL Studio 2026'
```

The bridge reads the flag only when FL loads the script. Never place
`FL_BRIDGE_ENABLE_WRITES` in MCP configuration.

## 7. Supervised live acceptance

Plan and evidence modes are separated. These entry points contact FL/MIDI in
non-plan mode. Invoke them with the checkout interpreter.

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

### macOS v0.13 transport evidence

The v0.13 MIDI wire protocol completed this fresh regression on macOS 27.0
arm64 with FL Studio 2026 Producer Edition 26.1.3 build 5336 and MIDI scripting
API 44. The procedure below is the reproducible release check. It uses the
exact IAC bus and private outputs, runs reads with FL in normal read-only mode,
then relaunches only a reviewed disposable project with writes enabled:

```bash
PORT='IAC Driver Bus 1'
mkdir -p .private
./.venv/bin/postfader-doctor --midi-port "$PORT" --json
./.venv/bin/python scripts/live_read_acceptance.py --midi-port "$PORT" --mixer-track 1 --plugin-track 1 --plugin-slot 0 --pattern 1 --channel 0 --output .private/macos-read.json
cp docs/windows-write-scenario-v1.json .private/macos-write-scenario-reviewed.json
# Review every target/value and set the required reviewed fixture marker.
./.venv/bin/python scripts/live_write_acceptance.py --plan --midi-port "$PORT" --scenario .private/macos-write-scenario-reviewed.json
./.venv/bin/python scripts/live_write_acceptance.py --midi-port "$PORT" --scenario .private/macos-write-scenario-reviewed.json --confirm-user-present --confirm-disposable-project --confirm-safe-to-edit --output .private/macos-write.json
./.venv/bin/python scripts/live_note_acceptance.py --plan --midi-port "$PORT" --channel 0
./.venv/bin/python scripts/live_note_acceptance.py --midi-port "$PORT" --channel 0 --note 60 --velocity 80 --duration-ms 250 --confirm-user-present --confirm-disposable-project --confirm-live-note-dispatch --output .private/macos-note.json
```

The release gate requires command protocol 2 and MIDI wire protocol 2 in the
doctor, every read passing, every persistent write independently restored, a
live-note dispatch receipt, and a final restart proving read-only mode. Each
output path is create-only, so use a new name for every rerun.

The read harness derives the read surface from actual MCP annotations and
exercises every read tool with atomic per-tool checkpoints, isolated workers,
and validated per-tool and overall deadlines. A timed-out worker is terminated
and reaped before all later reads are skipped. The write scenario must contain exactly one
operation for each authoritative persistent write tool. Each operation names:

```json
{
  "tool": "fl_set_mixer_volume",
  "before": {"tool": "fl_inspect_mixer_track", "arguments": {"track_index": 1}},
  "mutation_arguments": {"track_index": 1, "volume_normalized": 0.73},
  "restore": [{
    "tool": "fl_set_mixer_volume",
    "arguments": {
      "track_index": 1,
      "volume_normalized": {"$before": "track.volume_normalized"}
    }
  }],
  "verify_paths": ["track.volume_normalized"]
}
```

The reviewed scenario is validated against the public surface before any
preflight. Real execution requires `scenario_version: 1`, the exact
`fixture_status: "REVIEWED_FOR_THIS_DISPOSABLE_PROJECT"` marker,
`safe_to_edit: true`, and the user-present, disposable-project, and
safe-to-edit CLI confirmations. Plan mode can still validate the public
`TEMPLATE_REQUIRES_REVIEW` fixture without making it live-eligible. Live mode
refuses playing/recording, unverified provenance, read-only mode, or a missing
session fingerprint. Master-targeting operations require per-tool
acknowledgements. A mutation is attempted once, never retried after an
exception, then restored and reread. A failed restore stops the run loudly.
Nothing saves the project.

List state can use a `$select` reference containing `path`, `where`, and
`value`. It resolves exactly one member by logical identity and refuses absent
or ambiguous matches before the first mutation. The Windows fixture uses this
for mixer destinations, plug-in parameter indices, and EQ band indices; do not
replace those identities with list positions. When adapting plug-in controls,
update mutation indices, before selectors, restore indices, and verification
selectors together. The playing operation restores both stopped playback and
the captured song position, in that order, and independently verifies both.

Each live evidence update is a same-directory atomic snapshot containing an
append-only logical checkpoint journal. Completed preflight, before-state,
mutation, restoration, and independent-reread phases are flushed durably, so
an unexpected exception leaves the last completed state visible instead of a
truncated file. Every snapshot continues to report `project_saved: false`.

Live note has a separate confirmation and evidence path because it is
ephemeral rather than restorable persistent state. Complete the full morning
procedure in [windows-acceptance.md](windows-acceptance.md).

## 8. Troubleshooting

**Universal Bridge is not listed.** Quit FL, rerun the native installer with
the correct absolute user-data path, reopen FL, and reload scripts.

**The script is stale.** Reinstall, then reload the FL script. The doctor
compares repository, deployed, and running bridge digests.

**No endpoint is configured on Windows.** Set an exact
`FL_BRIDGE_MIDI_PORT` in generated client configuration. Postfader will not
guess a provider.

**Endpoint selection is ambiguous.** Use the full exact endpoint name printed
by the doctor. Postfader refuses ambiguity before lock/open.

**The endpoint is already owned.** Close the other MCP server and retry. The
reported PID is local ownership evidence, not authentication.

**Writes are refused.** Check the live handshake. It must report
`bridge_mode=write_test` and `verified_writes_enabled=true`; otherwise quit and
relaunch FL in write mode.

**A write is unverified.** Do not retry automatically. Inspect before/after,
verification detail, warnings, and the project itself.

## Environment variables

| Variable | Meaning |
|---|---|
| `FL_STUDIO_USER_DATA_DIR` | Absolute FL user-data directory. Relative values are rejected. |
| `FL_BRIDGE_ENABLE_MIDI` | `1` allows construction of native MIDI transport. |
| `FL_BRIDGE_MIDI_PORT` | Exact endpoint query; required on Windows for native MIDI. |
| `FL_BRIDGE_ENABLE_WRITES` | FL process only: exposes persistent/live mutations at script load. |
| `FL_BRIDGE_SANDBOXED` | `1` forbids native MIDI enumeration/open and live handshake. |
| `FL_BRIDGE_TIMEOUT` | Bridge response timeout in seconds. |
| `FL_BRIDGE_HOST`, `FL_BRIDGE_PORT` | Test-only loopback TCP transport. |
| `FL_BRIDGE_MAILBOX` | Test-only file-mailbox transport directory. |

## 9. Hermetic tests

```powershell
.\.venv\Scripts\python.exe scripts\run_safe_tests.py
```

Every child is forced to `FL_BRIDGE_ENABLE_MIDI=0` and
`FL_BRIDGE_SANDBOXED=1`, with a bounded timeout. The explicit allowlist omits
`tests/test_midi_transport.py`, which is the only real shared-MIDI test.
