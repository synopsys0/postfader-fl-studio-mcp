#!/usr/bin/env bash
# Install the FL Studio MCP server and its bridge script.
# Safe to re-run: it updates in place.
# Set FL_STUDIO_USER_DATA_DIR when FL Studio uses a custom user data folder.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
FL_USER_DATA="${FL_STUDIO_USER_DATA_DIR:-$HOME/Documents/Image-Line/FL Studio}"
HARDWARE="$FL_USER_DATA/Settings/Hardware"
TARGET="$HARDWARE/Universal Bridge"

echo "FL Studio MCP — install"
echo "FL Studio user data: $FL_USER_DATA"
echo

# --- python environment ----------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating virtualenv at .venv"
  python3 -m venv "$VENV"
fi

echo "Installing Python packages"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --editable "$ROOT"
echo "  done"

# --- bridge script ---------------------------------------------------------
if [ ! -d "$HARDWARE" ]; then
  echo
  echo "ERROR: FL Studio's Hardware folder was not found at:"
  echo "  $HARDWARE"
  echo "Launch FL Studio once so it creates its settings folders, then re-run."
  echo "For a custom location, set FL_STUDIO_USER_DATA_DIR to FL Studio's user data folder."
  exit 1
fi

echo
echo "Installing bridge script into FL Studio"
mkdir -p "$TARGET"

# Build the exact stamped bytes first. Comparing the installed bridge with the
# unstamped repository source would make every correct deployment look stale
# and would create a needless backup every time the installer was re-run.
STAGED_BRIDGE="$(mktemp "${TMPDIR:-/tmp}/flmcp-bridge.XXXXXX")"
trap 'rm -f "$STAGED_BRIDGE"' EXIT
BRIDGE_SHA="$(
  "$VENV/bin/python" -m fl_studio_mcp.bridge_stamp \
    "$ROOT/bridge/device_UniversalBridge.py" "$STAGED_BRIDGE"
)"
if [ -f "$TARGET/device_UniversalBridge.py" ] && \
   ! cmp -s "$STAGED_BRIDGE" "$TARGET/device_UniversalBridge.py"; then
  BACKUP="$TARGET/device_UniversalBridge.py.bak-$(date +%Y%m%d-%H%M%S)"
  cp "$TARGET/device_UniversalBridge.py" "$BACKUP"
  echo "  backed up previous bridge to $BACKUP"
fi
cp "$STAGED_BRIDGE" "$TARGET/device_UniversalBridge.py"
chmod 0644 "$TARGET/device_UniversalBridge.py"
rm -f "$STAGED_BRIDGE"
trap - EXIT
echo "  $TARGET/device_UniversalBridge.py (stamped $BRIDGE_SHA)"

# --- client config ---------------------------------------------------------
cat > "$ROOT/.mcp.json" <<JSON
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
JSON
echo
echo "Wrote .mcp.json for the local MCP server"

echo
echo "Next steps:"
echo
echo "  1. Enable a MIDI port for FL to attach the script to."
echo "     Audio MIDI Setup > Window > Show MIDI Studio > double-click"
echo "     'IAC Driver' > tick 'Device is online' > Apply."
echo
echo "  2. Start FL Studio. Options > MIDI settings > Input:"
echo "     select the port, enable it, and set Controller type to 'Universal Bridge'."
echo "     Note the Port number FL gives it."
echo
echo "  3. In the same dialog under Output, select that same port and give it"
echo "     the SAME Port number. The bridge replies over MIDI output and will"
echo "     refuse to start without it."
echo
echo "  4. Verify:  ./scripts/doctor.py"
echo
echo "  5. Register the server with your client - see README.md."
