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
from functools import lru_cache
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


@lru_cache(maxsize=4)
def _read_stamped_bridge_source(
    path: Path, identity: tuple[int, int, int, int, int]
) -> tuple[bytes, str]:
    # The identity is a cache key, not a second content-integrity check.
    return stamp_bridge_source(path.read_bytes())


def read_stamped_bridge_source(path: Path) -> tuple[bytes, str]:
    """Read and stamp each source revision once, including editable installs.

    Handshakes need the packaged source digest for diagnostics. Reopening,
    hashing, and copying the entire controller on every ping adds no new
    information while the file is unchanged. A cheap stat also lets a running
    development server notice edits, replacements, and missing source files.
    """
    path = path.absolute()
    metadata = path.stat()
    identity = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
    return _read_stamped_bridge_source(path, identity)


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
