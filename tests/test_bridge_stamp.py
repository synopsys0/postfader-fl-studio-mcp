#!/usr/bin/env python3
"""Hermetic checks for the install-time FL bridge source stamp."""

from __future__ import annotations

import hashlib
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fl_studio_mcp.bridge_stamp import (  # noqa: E402
    BRIDGE_SOURCE_MARKER,
    BridgeStampError,
    main,
    stamp_bridge_source,
)


BRIDGE_SOURCE = ROOT / "fl_studio_mcp" / "_bridge" / "device_UniversalBridge.py"


class BridgeStampTests(unittest.TestCase):
    def test_repository_bridge_is_stamped_with_its_original_source_hash(self) -> None:
        source = BRIDGE_SOURCE.read_bytes()
        deployed, digest = stamp_bridge_source(source)

        self.assertEqual(digest, hashlib.sha256(source).hexdigest())
        self.assertEqual(len(digest), 64)
        self.assertNotIn(BRIDGE_SOURCE_MARKER, deployed)

        stamped_line = (
            f'BRIDGE_SOURCE_SHA256 = "{digest}"  # injected-by-install'.encode(
                "ascii"
            )
        )
        self.assertEqual(deployed.count(stamped_line), 1)
        self.assertEqual(deployed.replace(stamped_line, BRIDGE_SOURCE_MARKER), source)

    def test_missing_marker_is_refused(self) -> None:
        with self.assertRaisesRegex(BridgeStampError, "found 0"):
            stamp_bridge_source(b"print('bridge without a marker')\n")

    def test_duplicate_markers_are_refused(self) -> None:
        source = BRIDGE_SOURCE_MARKER + b"\n" + BRIDGE_SOURCE_MARKER + b"\n"
        with self.assertRaisesRegex(BridgeStampError, "found 2"):
            stamp_bridge_source(source)

    def test_cli_writes_the_same_bytes_used_for_deployment_checks(self) -> None:
        source = BRIDGE_SOURCE.read_bytes()
        expected, digest = stamp_bridge_source(source)

        with tempfile.TemporaryDirectory(prefix="flmcp-bridge-stamp-") as raw:
            output = Path(raw) / "device_UniversalBridge.py"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = main([str(BRIDGE_SOURCE), str(output)])

            self.assertEqual(result, 0)
            self.assertEqual(stdout.getvalue().strip(), digest)
            self.assertEqual(output.read_bytes(), expected)
            self.assertNotEqual(output.read_bytes(), source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
