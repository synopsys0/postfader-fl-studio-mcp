#!/usr/bin/env bash
# Install the FL Studio MCP server and its bridge script.
# Safe to re-run: it updates in place.
# Set FL_STUDIO_USER_DATA_DIR when FL Studio uses a custom user data folder.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"

echo "FL Studio MCP — install"
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

# Resolve this once through the package contract used by every consumer. On
# macOS this retains ~/Documents by default; an explicit environment override
# still wins. The Windows bootstrap added in a later phase will use Known
# Documents through the same function.
FL_USER_DATA="$("$VENV/bin/python" -c \
  'from fl_studio_mcp.host_config import fl_studio_user_data_dir; print(fl_studio_user_data_dir())')"
echo "FL Studio user data: $FL_USER_DATA"

# --- bridge script ---------------------------------------------------------
# Deployment lives in fl_studio_mcp.bridge_install so that this script and the
# postfader-install-bridge command a pip user gets cannot drift apart. That
# module owns the stamping, the same-bytes comparison that avoids a needless
# backup on every re-run, and the Hardware-folder error message.
echo
echo "Installing bridge script into FL Studio"
if ! "$VENV/bin/python" -m fl_studio_mcp.bridge_install \
      --user-data-dir "$FL_USER_DATA"; then
  exit 1
fi

echo
echo "No MCP client configuration was changed."
echo "Generate an explicit Codex or Claude example with:"
printf '  %q %q --help\n' \
  "$VENV/bin/python" "$ROOT/scripts/generate_mcp_config.py"

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
