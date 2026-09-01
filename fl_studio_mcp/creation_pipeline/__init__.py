"""Task-scoped creation readiness, execution, and outcome primitives.

The public MCP integration lives in :mod:`fl_studio_mcp.production_runs`.
This package keeps the reusable creation services independent from the server
registration layer and does not own an FL Studio bridge or an AI model.
"""

from .models import CREATION_PIPELINE_SCHEMA_VERSION


__all__ = ["CREATION_PIPELINE_SCHEMA_VERSION"]
