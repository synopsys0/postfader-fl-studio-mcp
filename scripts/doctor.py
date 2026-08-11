#!/usr/bin/env python3
"""Check every piece of the FL Studio MCP setup and say what to fix.

The checks themselves live in ``fl_studio_mcp.diagnostics`` so that an
installed package can run them as ``postfader-doctor`` without a clone. This
wrapper only exists so the documented ``./scripts/doctor.py`` keeps working
from a checkout, including before the package is on sys.path.

Set FL_STUDIO_USER_DATA_DIR when FL Studio uses a custom user data folder.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fl_studio_mcp.diagnostics import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
