"""Lazy, transactional local journal for Production Runs.

Each row is a complete typed run snapshot. SQLite commits the operation-start
marker before dispatch and the receipt together with its generated outputs.
Revision checks prevent a stale registry from overwriting a newer checkpoint.
The store never imports the bridge, opens FL Studio, or creates files on reads.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .host_config import fl_studio_user_data_dir


PRODUCTION_RUN_PATH_ENV = "POSTFADER_PRODUCTION_RUN_PATH"
DEFAULT_PRODUCTION_RUN_FILENAME = "production-runs-v1.sqlite3"


class ProductionRunStoreError(RuntimeError):
    """A run checkpoint could not be read or committed."""


class ProductionRunConflictError(ProductionRunStoreError):
    """Another registry already advanced this run."""


def resolve_production_run_path(path: str | os.PathLike[str] | None = None) -> Path:
    raw = os.fspath(path) if path is not None else os.environ.get(
        PRODUCTION_RUN_PATH_ENV, ""
    ).strip()
    if raw:
        resolved = Path(raw).expanduser()
        if not resolved.is_absolute():
            raise ValueError("Production Run journal path must be absolute")
        return resolved
    return (
        fl_studio_user_data_dir()
        / "Settings"
        / "PostFader"
        / DEFAULT_PRODUCTION_RUN_FILENAME
    )


class LocalProductionRunStore:
    """Disk-backed snapshots with no I/O until the first operation.

    ``path`` overrides the environment and platform default. SQLite's rollback
    journal and FULL synchronous mode make each snapshot an atomic commit;
    there is no separate receipt/output file to become out of sync.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path_override = path

    @property
    def path(self) -> Path:
        return resolve_production_run_path(self._path_override)

    def load(self, run_id: str) -> tuple[int, str] | None:
        path = self.path
        if not path.exists():
            return None
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute(
                    "SELECT revision, payload FROM production_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ProductionRunStoreError(f"Cannot read Production Run journal: {exc}") from exc
        return None if row is None else (int(row[0]), str(row[1]))

    def list_ids(self, *, limit: int = 64) -> tuple[str, ...]:
        if not 1 <= limit <= 64:
            raise ValueError("Production Run listing limit must be within 1..64")
        path = self.path
        if not path.exists():
            return ()
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                rows = db.execute(
                    "SELECT run_id FROM production_runs ORDER BY updated_at DESC, run_id LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ProductionRunStoreError(f"Cannot list Production Run journal: {exc}") from exc
        return tuple(str(row[0]) for row in rows)

    def save(
        self,
        run_id: str,
        payload: str,
        *,
        expected_revision: int,
        updated_at: str,
    ) -> int:
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path, timeout=10)) as db, db:
                db.execute("PRAGMA synchronous = FULL")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS production_runs ("
                    "run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
                    "updated_at TEXT NOT NULL, payload TEXT NOT NULL)"
                )
                next_revision = expected_revision + 1
                if expected_revision == 0:
                    db.execute(
                        "INSERT INTO production_runs VALUES (?, ?, ?, ?)",
                        (run_id, next_revision, updated_at, payload),
                    )
                else:
                    cursor = db.execute(
                        "UPDATE production_runs SET revision = ?, updated_at = ?, payload = ? "
                        "WHERE run_id = ? AND revision = ?",
                        (next_revision, updated_at, payload, run_id, expected_revision),
                    )
                    if cursor.rowcount != 1:
                        raise ProductionRunConflictError(
                            "This Production Run was advanced by another registry; "
                            "reload its current state before continuing."
                        )
        except (OSError, sqlite3.Error) as exc:
            raise ProductionRunStoreError(f"Cannot commit Production Run checkpoint: {exc}") from exc
        return next_revision
