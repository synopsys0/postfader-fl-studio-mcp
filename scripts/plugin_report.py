#!/usr/bin/env python3
"""Checkout wrapper for the installed ``postfader-plugin-report`` command."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fl_studio_mcp.plugin_report import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
