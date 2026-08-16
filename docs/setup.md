# Setup and usage

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

### Optional Piano Roll scripting bridge

Piano Roll notes live in FL Studio's separate `.pyscript` runtime, not its MIDI
controller API. The creative tools therefore use one generated **Postfader
Apply** script under:

```text
<FL Studio user-data>/Settings/Piano roll scripts/Postfader/
```

The directory follows the same Windows Known Documents and
`FL_STUDIO_USER_DATA_DIR` resolution as the controller-script installer. An
absolute `POSTFADER_PIANO_ROLL_SCRIPTS_DIR` can override only this generated
script location.

For each new MCP process, call `piano_roll_bridge(action="prepare")`, open any
Piano Roll, and run **Scripts → Postfader → Postfader Apply** once. Then call
`piano_roll_bridge(action="confirm", confirm_user_ran_script=true)`. Automatic
writes can then target the requested channel and pattern and dispatch FL's
run-last-script shortcut. They report focus and key dispatch, never fabricated
note readback; inspect the Piano Roll before issuing another mutation. Set
`auto_trigger=false` to generate the script for manual execution instead.

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

The normal FL launch is read-only. Start FL Studio normally, connect the AI
client, and ask it:

```text
Enable write mode for this session.
```

The client calls `fl_set_write_mode(enabled=true,
confirm_user_present=true)`. Enabling requires that explicit present-user
request, matching bridge provenance, runtime-control support, and the current
session fingerprint. A second handshake must then confirm all of:

- `bridge_mode="write_test"`;
- `verified_writes_enabled=true`; and
- `write_mode_origin="runtime_request"`.

No project value is changed or saved by the mode transition, and FL Studio
does not restart. Ask the client to disable write mode when finished. A normal
new FL process starts read-only, and a bridge reload also resets to the process
startup default.

The Windows launcher remains useful for finding FL Studio and starting it
normally:

```powershell
.\scripts\launch_fl_studio.ps1 -DryRun
.\scripts\launch_fl_studio.ps1
```

The launcher discovers or accepts an absolute FL executable, refuses a real
launch if any FL process is already running, and restores its own process
environment after child creation.

The legacy startup opt-in remains available for backward-compatible supervised
harnesses:

```powershell
.\scripts\launch_fl_studio.ps1 -EnableWrites
```

On macOS its equivalent is:

```bash
FL_BRIDGE_ENABLE_WRITES=1 open -a 'FL Studio 2026'
```

The bridge reads that legacy flag only when FL loads the script. It is no
longer required for ordinary AI-client use. Never place
`FL_BRIDGE_ENABLE_WRITES` in MCP configuration.

## 7. Troubleshooting

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
`runtime_write_mode_control=true` and matching bridge provenance. Ask the
connected client to enable write mode and approve its capability-change prompt.
Success reports `bridge_mode=write_test`, `verified_writes_enabled=true`, and
`write_mode_origin=runtime_request`.

**A write is unverified.** Do not retry automatically. Inspect before/after,
verification detail, warnings, and the project itself.

## Environment variables

| Variable | Meaning |
|---|---|
| `FL_STUDIO_USER_DATA_DIR` | Absolute FL user-data directory. Relative values are rejected. |
| `FL_BRIDGE_ENABLE_MIDI` | `1` allows construction of native MIDI transport. |
| `FL_BRIDGE_MIDI_PORT` | Exact endpoint query; required on Windows for native MIDI. |
| `FL_BRIDGE_ENABLE_WRITES` | Legacy FL-process startup opt-in. Ordinary clients use the session-only `fl_set_write_mode` tool instead. |
| `FL_BRIDGE_SANDBOXED` | `1` forbids native MIDI enumeration/open and live handshake. |
| `FL_BRIDGE_TIMEOUT` | Bridge response timeout in seconds. |
| `FL_BRIDGE_HOST`, `FL_BRIDGE_PORT` | Test-only loopback TCP transport. |
| `FL_BRIDGE_MAILBOX` | Test-only file-mailbox transport directory. |
| `POSTFADER_PIANO_ROLL_SCRIPTS_DIR` | Optional absolute override for the generated Piano Roll script directory. Otherwise it follows `FL_STUDIO_USER_DATA_DIR`. |

## 8. Hermetic tests

```powershell
.\.venv\Scripts\python.exe scripts\run_safe_tests.py
```

Every child is forced to `FL_BRIDGE_ENABLE_MIDI=0` and
`FL_BRIDGE_SANDBOXED=1`, with a bounded timeout. The explicit allowlist omits
`tests/test_midi_transport.py`, which is the only real shared-MIDI test.
