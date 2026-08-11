"""Build the exact bridge bytes that FL Studio should load.

FL Studio's embedded Python cannot read the bridge file to hash it at runtime.
The repository copy therefore contains one empty marker; the installer hashes
that original source and substitutes the digest into the deployed copy.
Keeping that transform here gives the installer, doctor, and tests one exact
definition of a current deployment.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Sequence


BRIDGE_SOURCE_MARKER = b'BRIDGE_SOURCE_SHA256 = ""  # injected-by-install'


class BridgeStampError(ValueError):
    """The repository bridge cannot be stamped unambiguously."""


def stamp_bridge_source(source: bytes) -> tuple[bytes, str]:
    """Return the deployed bridge bytes and SHA-256 of the original source."""
    marker_count = source.count(BRIDGE_SOURCE_MARKER)
    if marker_count != 1:
        raise BridgeStampError(
            "bridge source must contain exactly one install-time hash marker; "
            f"found {marker_count}"
        )

    digest = hashlib.sha256(source).hexdigest()
    replacement = (
        f'BRIDGE_SOURCE_SHA256 = "{digest}"  # injected-by-install'.encode("ascii")
    )
    return source.replace(BRIDGE_SOURCE_MARKER, replacement, 1), digest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stamp the repository bridge into its deployable form."
    )
    parser.add_argument("source", type=Path, help="unstamped repository bridge")
    parser.add_argument("output", type=Path, help="path for the stamped bridge")
    args = parser.parse_args(argv)

    try:
        source = args.source.read_bytes()
        deployed, digest = stamp_bridge_source(source)
        args.output.write_bytes(deployed)
    except (BridgeStampError, OSError) as exc:
        parser.error(str(exc))

    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
