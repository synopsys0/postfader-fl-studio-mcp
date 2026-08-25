"""Deploy the MIDI bridge script into FL Studio's controller-script folder.

FL Studio loads its MIDI controller scripts from a fixed folder under the
user's data directory. The MCP server cannot reach a project until a copy of
``device_UniversalBridge.py`` is sitting there, so an installed package that
only ships the server is not usable on its own -- hence this module and the
``postfader-install-bridge`` command it backs.

The bridge travels inside the package as data rather than as an importable
module. It is written against FL Studio's embedded API (``plugins``, ``mixer``,
``general``) and would fail on import anywhere else, so it must never be
reachable by ``import``.

The native scripts in ``scripts/install.ps1`` and ``scripts/install.sh``
perform the same deployment for someone working from a clone. All paths call
:func:`deploy` so there is one implementation of what
"installed" means, and one definition of when a backup is warranted.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .bridge_stamp import BridgeStampError, stamp_bridge_source
from .host_config import (
    HostConfigurationError,
    default_fl_studio_user_data_dir,
    fl_studio_user_data_dir,
    midi_port_query,
    platform_family,
)

BRIDGE_FILENAME = "device_UniversalBridge.py"
CONTROLLER_DIR_NAME = "Universal Bridge"
DEFAULT_USER_DATA_DIR = default_fl_studio_user_data_dir()


class BridgeInstallError(RuntimeError):
    """Deployment could not be completed; the message says what to fix."""


def bridge_source_path() -> Path:
    """Absolute path to the bridge script shipped with this package."""
    path = Path(__file__).resolve().parent / "_bridge" / BRIDGE_FILENAME
    if not path.is_file():
        raise BridgeInstallError(
            "the packaged bridge script is missing at %s; reinstall the "
            "package, or run the native bootstrap script from a clone" % path
        )
    return path


def user_data_dir(override: str | None = None) -> Path:
    """FL Studio's shared, platform-aware user-data selection.

    Resolution order is explicit argument, ``FL_STUDIO_USER_DATA_DIR``, then
    Windows Known Documents or the macOS home Documents default.
    """
    return fl_studio_user_data_dir(override)


def hardware_dir(override: str | None = None) -> Path:
    return user_data_dir(override) / "Settings" / "Hardware"


def target_path(override: str | None = None) -> Path:
    return hardware_dir(override) / CONTROLLER_DIR_NAME / BRIDGE_FILENAME


def midi_setup_epilog(platform_name: str | None = None) -> str:
    """Return setup guidance without inventing a Windows endpoint default."""

    if platform_family(platform_name) == "windows":
        selected = midi_port_query(platform_name=platform_name)
        port_line = (
            "Use the configured FL_BRIDGE_MIDI_PORT value %r." % selected
            if selected
            else "Set FL_BRIDGE_MIDI_PORT to its exact endpoint name first."
        )
        return (
            "After this, configure a virtual MIDI endpoint yourself; PostFader "
            "does not install or configure a MIDI driver. %s In FL Studio: "
            "Options > MIDI settings > Input, select that endpoint, enable it, "
            "set Controller type to 'Universal Bridge', and note its Port "
            "number. Under Output, give the paired endpoint the SAME Port "
            "number. Then reload the script and verify with: postfader-doctor"
            % port_line
        )
    return (
        "After this, in FL Studio: Options > MIDI settings > Input, select "
        "your IAC port, enable it, set Controller type to 'Universal Bridge', "
        "and note its Port number. Under Output, give the same port the SAME "
        "Port number -- the bridge replies over MIDI output and refuses to "
        "start without it. Then View > Script output > Reload script. Verify "
        "with: postfader-doctor"
    )


def expected_bridge_deployment() -> tuple[bytes, str]:
    """Return the exact installed bytes and source hash this version ships."""
    try:
        return stamp_bridge_source(bridge_source_path().read_bytes())
    except (BridgeStampError, OSError) as exc:
        raise BridgeInstallError("could not prepare the bridge: %s" % exc) from exc


def deploy(override: str | None = None) -> dict:
    """Install the packaged bridge, returning what happened.

    The comparison is against the *stamped* bytes rather than the raw source.
    Comparing against the unstamped file would make every correct deployment
    look stale and would leave a backup behind on every run.

    Keys returned: ``target``, ``digest``, ``changed``, ``backup``.
    """
    hardware = hardware_dir(override)
    if not hardware.is_dir():
        raise BridgeInstallError(
            "FL Studio's Hardware folder was not found at:\n  %s\n"
            "Launch FL Studio once so it creates its settings folders, then "
            "re-run. For a relocated data folder, pass --user-data-dir or set "
            "FL_STUDIO_USER_DATA_DIR." % hardware
        )

    stamped, digest = expected_bridge_deployment()

    target = target_path(override)
    target.parent.mkdir(parents=True, exist_ok=True)

    backup = None
    changed = True
    if target.is_file():
        current = target.read_bytes()
        if current == stamped:
            changed = False
        else:
            backup = target.with_name(
                "%s.bak-%s" % (BRIDGE_FILENAME, datetime.now().strftime("%Y%m%d-%H%M%S"))
            )
            shutil.copy2(target, backup)

    if changed:
        target.write_bytes(stamped)
        target.chmod(0o644)

    return {"target": target, "digest": digest, "changed": changed, "backup": backup}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="postfader-install-bridge",
        description=(
            "Install the PostFader MIDI bridge into FL Studio so the MCP "
            "server can reach a running project."
        ),
        epilog=midi_setup_epilog(),
    )
    parser.add_argument(
        "--user-data-dir",
        default=None,
        metavar="PATH",
        help="FL Studio user-data folder (default: %s, or "
             "FL_STUDIO_USER_DATA_DIR)" % DEFAULT_USER_DATA_DIR,
    )
    parser.add_argument(
        "--print-source", action="store_true",
        help="print the path of the packaged bridge script and exit",
    )
    args = parser.parse_args(argv)

    try:
        if args.print_source:
            print(bridge_source_path())
            return 0
        outcome = deploy(args.user_data_dir)
    except (BridgeInstallError, HostConfigurationError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if outcome["changed"]:
        print("Installed the bridge at:\n  %s" % outcome["target"])
    else:
        print("Bridge already up to date at:\n  %s" % outcome["target"])
    print("  source SHA-256 %s" % outcome["digest"])
    if outcome["backup"] is not None:
        print("  backed up the previous bridge to %s" % outcome["backup"])
    print(parser.epilog)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
