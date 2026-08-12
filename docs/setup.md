# Setup and usage

Postfader is a macOS-only beta. It requires FL Studio 2026 version
26.1.3 build 5336 or newer, MIDI scripting API 44 or newer, Python 3.10 or
newer, and an MCP-compatible client. The current release is validated on Apple
silicon.

The MCP server communicates with a MIDI controller script loaded by FL Studio.
The production transport is local MIDI SysEx over a virtual CoreMIDI IAC port;
FL Studio's embedded Python environment cannot use the connector's socket or
file-mailbox test transports.

## 1. Prepare FL Studio and CoreMIDI

Launch FL Studio once so it creates its user settings folders, then quit it.

Open **Audio MIDI Setup → Window → Show MIDI Studio**, open **IAC Driver**,
enable **Device is online**, and select **Apply**. An existing virtual MIDI
input can also be used, but all examples assume the IAC Driver.

The IAC bus is shared and unauthenticated. Use it only on a trusted,
single-user Mac. The connector takes an exclusive process lock while connected
to prevent accidental duplicate clients, but that lock is not authentication.

## 2. Install the server and FL bridge

### Claude Desktop extension

Download the `postfader-fl-studio-mcp-0.12.0.mcpb` asset from the GitHub
Release and open it with Claude Desktop. The extension installs and registers
the local MCP server, but it cannot configure FL Studio's hardware-script
folder or CoreMIDI for you. Run the packaged bridge installer (or use the
source-install path below), then complete the IAC and FL Studio steps in this
guide. The extension does not enable writes.

### Python package or source checkout

Either install the published package:

```bash
pip install postfader-fl-studio-mcp
postfader-install-bridge
```

or work from a clone, which additionally gives you the test suite and the
read-only validation scripts:

```bash
git clone https://github.com/synopsys0/postfader-fl-studio-mcp.git
cd postfader-fl-studio-mcp
./scripts/install.sh
```

Both deploy identical bytes: `install.sh` shells out to the same
`fl_studio_mcp.bridge_install` module that backs `postfader-install-bridge`,
so there is one implementation of what "installed" means.

The default user-data folder is `~/Documents/Image-Line/FL Studio`. If yours
is elsewhere, use the same absolute override for installation and diagnosis:

```bash
FL_STUDIO_USER_DATA_DIR="/absolute/path/to/FL Studio" ./scripts/install.sh
FL_STUDIO_USER_DATA_DIR="/absolute/path/to/FL Studio" ./.venv/bin/python scripts/doctor.py
```

`scripts/install.sh`:

- creates `.venv` and installs the `postfader-fl-studio-mcp` distribution into it;
- deploys the packaged bridge into FL Studio's
  `Settings/Hardware/Universal Bridge/` folder;
- backs up a different bridge script already present at that destination;
- stamps the source hash into the deployed copy; and
- writes a repository-local `.mcp.json` for the server ID `fl-studio`.

Re-run `./scripts/install.sh` after updating the bridge, then reload the script
inside FL Studio. The source stamp lets the client detect a stale deployed
copy through the `bridge_source_sha256` value returned by `ping`.

## 3. Attach the bridge in FL Studio

Start FL Studio and open **Options → MIDI settings → Input**. Select the IAC
port, enable it, and set **Controller type** to `Universal Bridge`. Note the
**Port** number FL assigns it.

Then, in the **Output** list of the same dialog, select the same IAC port and
set its **Port** number to match the input's. This step is required, not
optional: the bridge sends its replies over MIDI output, and it refuses to
start without an assigned output. If it is missing, Script output reports:

```text
MIDI output is not assigned - in Options > MIDI settings give the output
device the same Port number as the input
```

Open **View → Script output** and press **Reload script**. The output should
end with:

```text
ready: MIDI SysEx
```

Reload the script whenever the installed bridge changes. If FL Studio does not
show `Universal Bridge`, confirm that it was launched once before installation
and run `./scripts/install.sh` again.

## 4. Check the connection

```bash
./.venv/bin/python scripts/doctor.py
./scripts/inspect_readonly.py --capabilities
```

`doctor.py` checks the application, Python environment, installed bridge,
CoreMIDI ports, compatibility handshake, and a read-only project query. The
inspection command prints strict JSON through the same read-only allowlist used
by the MCP server.

Additional read-only checks are available:

```bash
./scripts/inspect_readonly.py --selected-range
./scripts/inspect_readonly.py --parameter-limit 16 --max-plugins 16
./scripts/inspect_readonly.py --only-used
```

The complete mixer scan is the default. `--only-used` applies a heuristic and
can omit a musically relevant track that remains at default values.

## 5. Register the MCP server

The installer writes this repository-local configuration:

```json
{
  "mcpServers": {
    "fl-studio": {
      "command": "./.venv/bin/python",
      "args": ["-m", "fl_studio_mcp.mcp_server"],
      "cwd": ".",
      "env": {
        "FL_BRIDGE_ENABLE_MIDI": "1"
      }
    }
  }
}
```

An unchanged copy is available as `.mcp.json.example`. Both use relative paths
and therefore only work when the client is launched from this checkout. That is
the try-it-quickly case, not the way to live with the server.

### Registering it for use in your own projects

Register once at user scope with absolute paths so the connector is reachable
regardless of which project you are working in. For Claude Code:

```bash
claude mcp add fl-studio --scope user \
    --env FL_BRIDGE_ENABLE_MIDI=1 \
    -- /absolute/path/to/postfader-fl-studio-mcp/.venv/bin/python \
       -m fl_studio_mcp.mcp_server
```

Verify with `claude mcp list`. A project-scoped entry loads only inside its own
directory, which is why the installer's `.mcp.json` does not travel with you.
Note also that a project-scoped server requires a one-time approval prompt the
first time that project is opened.

Because only one process may own the IAC port, remove or disable a
project-scoped copy before relying on the user-scope one.

For a client with no CLI, use absolute paths and do not assume that `~` or
shell variables will be expanded:

```json
{
  "mcpServers": {
    "fl-studio": {
      "command": "/absolute/path/to/postfader-fl-studio-mcp/.venv/bin/python",
      "args": ["-m", "fl_studio_mcp.mcp_server"],
      "cwd": "/absolute/path/to/postfader-fl-studio-mcp",
      "env": {
        "FL_BRIDGE_ENABLE_MIDI": "1"
      }
    }
  }
}
```

Configuration locations differ between MCP clients, but the command,
arguments, working directory, and `FL_BRIDGE_ENABLE_MIDI=1` setting are the
same. Only one MCP server process can own the resolved IAC port at a time.

## Read-only mode and write mode

Read-only mode is the default. In that mode all 20 bridge mutation commands
(19 readback-verified state commands plus bounded live-note dispatch) are
absent from the active dispatch allowlist rather than merely rejected after
dispatch.

To enable writes, quit FL Studio and launch it from Terminal:

```bash
FL_BRIDGE_ENABLE_WRITES=1 open -a "FL Studio 2026"
```

The bridge reads the flag once when its script loads. A successful write-mode
handshake reports:

```text
bridge_mode: "write_test"
verified_writes_enabled: true
```

Launching FL Studio normally returns to read-only mode. Do not add
`FL_BRIDGE_ENABLE_WRITES` to the MCP server configuration: the flag must be in
FL Studio's own launch environment.

Mutation tools act immediately and do not add a second confirmation prompt.
Every result reports `undo_point_created`. Commands that request an undo point
report whether one was observed; transient transport actions and live-note
dispatch truthfully report `null`. False or null means undo cannot be relied
upon. The bridge never saves the project, but a later manual or application
save can persist its changes.

Every mutation also compares the bridge source digest reported by the running
FL script with the bridge packaged beside the server. A missing, malformed, or
stale digest fails closed before dispatch; inspection continues with a warning
so the mismatch can be diagnosed. Re-run the installer and reload the FL
script after every bridge update.

Mutation tools accept an optional bridge-lifetime `session_fingerprint` and,
except for the step sequencer's required digest guard, an optional typed
`expected_before`. The bridge checks supplied guards immediately before the
mutation and before any applicable undo request. Use values from a fresh read
when multiple people or clients might touch the project. The fingerprint is
not authentication and does not identify a project.

### Optional write validation

Use the validation script only in a new, blank, disposable FL Studio project:

```bash
./.venv/bin/python scripts/validate_writes.py
```

The default check finds an otherwise untouched non-Master mixer track, changes
its fader, pan, mute state, and one built-in EQ band, then attempts to restore
each captured value and verifies the restore by readback. A failed restore can
leave a value changed, so inspect every result before closing or saving.

The script also supports an explicitly selected plug-in parameter. That mode
mutates a real control and should be used only in a disposable project after
you have verified the track, slot, and parameter index yourself.

## Audio analysis

FL Studio's MIDI scripting API provides no audio buffers and no render command.
Export or record the required file in FL Studio, then supply its absolute path
to an audio tool. `audio_find_recent_bounces` searches a bounded set of normal
FL Studio output folders to help locate recent files.

Audio inputs are limited by format, size, and analysis duration, but a direct
measurement request can name an absolute audio path elsewhere on disk. Use the
server only with a trusted MCP client. Tool results contain measurements,
canonical paths, and hashes, not audio samples.

## Troubleshooting

**FL Studio does not list `Universal Bridge`.** Launch FL Studio once, quit it,
run `./scripts/install.sh`, then reopen FL Studio. Confirm the script exists in
FL Studio's user `Settings/Hardware/Universal Bridge/` folder.

**Script output never says `ready`.** Press **Reload script**. The bridge file
must contain ASCII only because FL Studio loads MIDI scripts using an ASCII
code path. Run `./.venv/bin/python -B tests/test_bridge.py` to check it.

**The client cannot reach the bridge.** Confirm that FL Studio is running, the
IAC port is online and enabled as an input, its controller type is
`Universal Bridge`, and Script output says `ready: MIDI SysEx`.

**`IAC already owned; owner pid N`.** Another connector process owns the port.
Close that process and retry. Do not run multiple MCP clients against the same
IAC port.

**Writes are refused.** Inspect `ping`. If `bridge_mode` is `read_only`, quit FL
Studio and relaunch it with `FL_BRIDGE_ENABLE_WRITES=1`. Starting it normally
from Finder or the Dock does not carry that variable.

**A write reports `verified: false`.** FL Studio accepted the call but the
later readback did not prove the requested result. Inspect `before`, `after`,
`verification_basis_detail`, and `warnings`; do not assume the control moved,
retry automatically, or assume rollback occurred.

**The installed bridge is stale.** Re-run `./scripts/install.sh`, then press
**Reload script** in FL Studio. The doctor compares the source stamp reported
by the bridge with the repository copy.

## Environment variables

Everything the connector reads. Only the first two are needed for an ordinary
install; the rest exist for automation and for hosts that cannot use CoreMIDI.

| Variable | Read by | Effect |
|---|---|---|
| `FL_BRIDGE_ENABLE_MIDI` | MCP server, scripts | `1` builds the MIDI SysEx transport. Without it that transport is never constructed, so the connector cannot reach FL Studio. Set in `.mcp.json`; the bundled scripts set it themselves |
| `FL_BRIDGE_ENABLE_WRITES` | **the FL Studio process** | `1` adds the narrowly allowlisted mutation commands to the bridge. Read once when the script loads, so it must be set on FL Studio itself, not on the MCP server |
| `FL_STUDIO_USER_DATA_DIR` | `install.sh`, `doctor.py` | FL Studio's user-data folder when it is not `~/Documents/Image-Line/FL Studio` |
| `FL_BRIDGE_SANDBOXED` | MCP server, `doctor.py` | `1` declares that this process cannot open CoreMIDI. The MIDI transport reports itself unavailable and no probe subprocess is created. Use it in CI and automation harnesses, where a native CoreMIDI abort would otherwise raise a crash report |
| `FL_BRIDGE_MIDI_PORT` | MCP server, scripts | CoreMIDI port name to use. Default `IAC Driver` |
| `FL_BRIDGE_TIMEOUT` | MCP server, scripts | Seconds to wait for a bridge reply. Default `30` |
| `FL_BRIDGE_HOST`, `FL_BRIDGE_PORT` | MCP server, scripts | Loopback TCP transport. Unavailable inside FL Studio's script sandbox; used by tests |
| `FL_BRIDGE_MAILBOX` | MCP server, scripts | Directory for the file-mailbox transport. Also unavailable inside FL Studio's sandbox; used by tests |

## Testing

```bash
./.venv/bin/python scripts/run_safe_tests.py
```

The safe suite is an explicit allowlist that uses a fake FL API and
deterministic synthetic audio. It does not require FL Studio, a MIDI device, a
user project, or user audio and never touches the physical IAC bus.

`tests/test_midi_transport.py` is intentionally excluded because it uses the
real shared MIDI transport. Run it manually only when you intend to exercise
that host resource.
