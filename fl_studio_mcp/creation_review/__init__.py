"""Creation Review contracts, bounded session state, and local persistence."""

# The package re-exports the versioned model list programmatically so aliases
# added to ``models.__all__`` cannot silently disappear from this namespace.
# pyright: reportUnsupportedDunderAll=false

from . import models as _models
from .models import *  # noqa: F403
from .persistence import (
    DEFAULT_MAX_REVIEW_ASSETS,
    DEFAULT_MAX_REVIEW_ASSET_SETS,
    DEFAULT_MAX_REVIEW_COMPARISONS,
    DEFAULT_MAX_REVIEW_EVALUATIONS,
    DEFAULT_MAX_REVIEW_FEEDBACK,
    DEFAULT_MAX_REVIEW_FINDINGS,
    DEFAULT_MAX_REVIEW_MANIFESTS,
    DEFAULT_MAX_REVIEW_PASSES,
    DEFAULT_MAX_REVIEW_SESSIONS,
    DEFAULT_REVIEW_SESSION_FILENAME,
    REVIEW_SESSION_PATH_ENV,
    REVIEW_SESSION_PATH_ENV_ALIASES,
    REVIEW_SESSION_SCHEMA_VERSION,
    BoundedReviewSessionStore,
    CreationReviewSessionStore,
    LocalReviewSessionStore,
    ReviewSessionCorruptionError,
    ReviewSessionStore,
    ReviewSessionWriteError,
    resolve_review_session_path,
)
from .sessions import (
    CreationReviewSessionRegistry,
    InvalidReviewSessionTransition,
    ReviewAudioAssetError,
    ReviewSessionError,
    ReviewSessionLimitError,
    ReviewSessionRegistry,
    UnknownReviewSessionError,
    UnknownSourceRunError,
    snapshot_source_run,
    validate_audio_asset,
)


__all__ = [
    *_models.__all__,
    "BoundedReviewSessionStore",
    "CreationReviewSessionRegistry",
    "CreationReviewSessionStore",
    "DEFAULT_MAX_REVIEW_ASSETS",
    "DEFAULT_MAX_REVIEW_ASSET_SETS",
    "DEFAULT_MAX_REVIEW_COMPARISONS",
    "DEFAULT_MAX_REVIEW_EVALUATIONS",
    "DEFAULT_MAX_REVIEW_FEEDBACK",
    "DEFAULT_MAX_REVIEW_FINDINGS",
    "DEFAULT_MAX_REVIEW_MANIFESTS",
    "DEFAULT_MAX_REVIEW_PASSES",
    "DEFAULT_MAX_REVIEW_SESSIONS",
    "DEFAULT_REVIEW_SESSION_FILENAME",
    "InvalidReviewSessionTransition",
    "LocalReviewSessionStore",
    "REVIEW_SESSION_PATH_ENV",
    "REVIEW_SESSION_PATH_ENV_ALIASES",
    "REVIEW_SESSION_SCHEMA_VERSION",
    "ReviewAudioAssetError",
    "ReviewSessionCorruptionError",
    "ReviewSessionError",
    "ReviewSessionLimitError",
    "ReviewSessionRegistry",
    "ReviewSessionStore",
    "ReviewSessionWriteError",
    "UnknownReviewSessionError",
    "UnknownSourceRunError",
    "resolve_review_session_path",
    "snapshot_source_run",
    "validate_audio_asset",
]

# Keep wildcard imports stable if a compatibility alias is listed twice by a
# future module addition.
__all__ = list(dict.fromkeys(__all__))
