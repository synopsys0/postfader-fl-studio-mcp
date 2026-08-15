"""Durable create-only evidence and fail-closed script transport setup."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .host_config import FL_BRIDGE_MIDI_PORT_ENV, HostConfigurationError


# Cloud-sync and antivirus filters can briefly retain a handle to the old
# evidence snapshot on Windows. Keep the write atomic, but tolerate that
# bounded transient instead of abandoning a supervised run after one race.
ATOMIC_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4)


class EvidenceOutputError(RuntimeError):
    """A caller-selected evidence destination could not be reserved safely."""


class EvidenceDestination:
    """One create-only destination updated through durable atomic snapshots."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        initial = {
            "schema_version": 1,
            "overall": "started",
            "phase": "output_reserved",
            "contact_started": False,
            "project_saved": False,
        }
        journal = [{"sequence": 0, "state": self._journal_state(initial)}]
        rendered = self._render_snapshot(initial, journal)
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise EvidenceOutputError(
                "could not exclusively create new evidence output %r: %s"
                % (os.fspath(self.path), exc)
            ) from exc
        self._latest = initial
        self._journal = journal
        self._closed = False

    @staticmethod
    def _normalise(value: Any) -> Any:
        return json.loads(json.dumps(value, default=str))

    @classmethod
    def _journal_state(cls, value: Any) -> Any:
        """Keep phase evidence without recursively retaining full snapshots."""

        normalised = cls._normalise(value)
        if not isinstance(normalised, dict):
            return {"value": normalised}
        keys = (
            "schema_version",
            "kind",
            "overall",
            "phase",
            "contact_started",
            "project_saved",
            "last_checkpoint",
            "error",
            "evidence_output_failures",
        )
        return {key: normalised[key] for key in keys if key in normalised}

    @classmethod
    def _render_snapshot(cls, latest: Any, journal: list[dict[str, Any]]) -> str:
        latest = cls._normalise(latest)
        if isinstance(latest, dict):
            snapshot = dict(latest)
        else:
            snapshot = {"value": latest}
        snapshot.update(
            {
                "evidence_format": "atomic_snapshot_compact_journal_v2",
                "latest_sequence": len(journal) - 1,
                "evidence_journal": cls._normalise(journal),
            }
        )
        return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"

    def _replace_durably(self, rendered: str) -> None:
        descriptor = -1
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".%s." % self.path.name,
                suffix=".tmp",
                dir=os.fspath(self.path.parent),
                text=True,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                descriptor = -1
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(len(ATOMIC_REPLACE_RETRY_DELAYS) + 1):
                try:
                    os.replace(temporary_name, self.path)
                    break
                except PermissionError:
                    if attempt >= len(ATOMIC_REPLACE_RETRY_DELAYS):
                        raise
                    time.sleep(ATOMIC_REPLACE_RETRY_DELAYS[attempt])
            temporary_name = None
            if os.name != "nt":
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as exc:
            raise EvidenceOutputError(
                "could not atomically checkpoint evidence output %r: %s"
                % (os.fspath(self.path), exc)
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    def write(self, value: Any) -> None:
        """Append one logical checkpoint through same-directory replacement."""

        if self._closed:
            raise EvidenceOutputError("evidence output is already closed")
        latest = self._normalise(value)
        journal = [
            *self._journal,
            {"sequence": len(self._journal), "state": self._journal_state(latest)},
        ]
        self._replace_durably(self._render_snapshot(latest, journal))
        self._latest = latest
        self._journal = journal

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "EvidenceDestination":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


def reserve_evidence_output(
    path: str | os.PathLike[str] | None,
    *,
    required: bool,
) -> EvidenceDestination | None:
    if path is None:
        if required:
            raise EvidenceOutputError(
                "live acceptance requires --output naming a new evidence file"
            )
        return None
    return EvidenceDestination(path)


def configure_acceptance_transport(
    midi_port: str | None,
    *,
    live: bool,
) -> str | None:
    """Configure MIDI before server import; defaults always remain fail-closed."""

    os.environ["FL_BRIDGE_ENABLE_MIDI"] = "0"
    os.environ.pop(FL_BRIDGE_MIDI_PORT_ENV, None)
    if midi_port is None:
        return None
    query = midi_port.strip()
    if not query:
        raise HostConfigurationError("--midi-port must not be empty")
    if not live:
        return query
    os.environ["FL_BRIDGE_ENABLE_MIDI"] = "1"
    os.environ[FL_BRIDGE_MIDI_PORT_ENV] = query
    return query


def structured_failure(
    *,
    kind: str,
    phase: str,
    error: BaseException | str,
    contact_started: bool,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": kind,
        "overall": "fail" if contact_started else "refused",
        "phase": phase,
        "contact_started": contact_started,
        "project_saved": False,
        "error": str(error),
    }
