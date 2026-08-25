#!/usr/bin/env bash
# Install the FL Studio MCP server and its bridge script.
# Safe to re-run: it updates in place.
# Set FL_STUDIO_USER_DATA_DIR when FL Studio uses a custom user data folder.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
SKIP_BRIDGE_DEPLOYMENT=0

if [ "${1:-}" = "--skip-bridge-deployment" ]; then
  SKIP_BRIDGE_DEPLOYMENT=1
  shift
fi
if [ "$#" -ne 0 ]; then
  echo "usage: $0 [--skip-bridge-deployment]" >&2
  exit 2
fi

echo "FL Studio MCP — install"
echo

python_is_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 15) else 1)' \
    >/dev/null 2>&1
}

BOOTSTRAP_PYTHON=""
if [ -x "$VENV/bin/python" ]; then
  if ! python_is_supported "$VENV/bin/python"; then
    echo "The existing $VENV uses an unsupported Python."
    echo "Remove that .venv, install Python 3.10 through 3.14, and rerun this installer."
    exit 1
  fi
  BOOTSTRAP_PYTHON="$VENV/bin/python"
else
  for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    candidate_path="$(command -v "$candidate" 2>/dev/null || true)"
    if [ -n "$candidate_path" ] && python_is_supported "$candidate_path"; then
      BOOTSTRAP_PYTHON="$candidate_path"
      break
    fi
  done
  if [ -z "$BOOTSTRAP_PYTHON" ]; then
    echo "Python 3.10 through 3.14 is required, but no compatible interpreter was found."
    exit 1
  fi
fi

echo "Using Python: $BOOTSTRAP_PYTHON"

# --- python environment ----------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating virtualenv at .venv"
  "$BOOTSTRAP_PYTHON" -m venv "$VENV"
fi

echo "Installing Python packages"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --editable "$ROOT"
echo "  done"

# --- bridge script ---------------------------------------------------------
if [ "$SKIP_BRIDGE_DEPLOYMENT" -eq 1 ]; then
  echo "Bridge deployment deferred to guided setup."
else
  # Resolve this through the package contract used by every consumer. On
  # macOS this retains ~/Documents by default; an explicit override still wins.
  FL_USER_DATA="$("$VENV/bin/python" -c \
    'from fl_studio_mcp.host_config import fl_studio_user_data_dir; print(fl_studio_user_data_dir())')"
  echo "FL Studio user data: $FL_USER_DATA"

  # The packaged deployer owns stamping, same-bytes comparison, and backups.
  echo
  echo "Installing bridge script into FL Studio"
  if ! "$VENV/bin/python" -m fl_studio_mcp.bridge_install \
        --user-data-dir "$FL_USER_DATA"; then
    exit 1
  fi
fi

echo
echo "No MCP client configuration was changed."
echo "Continue with the guided first-time setup:"
printf '  %q setup\n' "$VENV/bin/postfader"

echo
echo "Next steps:"
echo
echo "  1. Run the guided setup above. It lists only exact bidirectional"
echo "     MIDI endpoints and generates your selected client configuration."
echo "  2. If no endpoint is listed, enable an IAC port first:"
echo "     Audio MIDI Setup > Window > Show MIDI Studio > double-click"
echo "     'IAC Driver' > tick 'Device is online' > Apply."
echo
echo "  3. Start FL Studio. Options > MIDI settings > Input:"
echo "     select the port, enable it, and set Controller type to 'Universal Bridge'."
echo "     Note the Port number FL gives it."
echo
echo "  4. In the same dialog under Output, select that same port and give it"
echo "     the SAME Port number. The bridge replies over MIDI output and will"
echo "     refuse to start without it."
echo
echo "  5. Rerun the guided setup or postfader-doctor to verify the live bridge."
