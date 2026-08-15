"""Shared host paths and transport configuration for Postfader entry points.

This module performs no MIDI or FL Studio I/O.  It only resolves local process
configuration so the installer, server, diagnostics, and offline discovery
surfaces cannot silently disagree about where FL Studio's user data lives or
which MIDI endpoint was selected.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence
from uuid import UUID


FL_STUDIO_USER_DATA_ENV = "FL_STUDIO_USER_DATA_DIR"
FL_BRIDGE_MIDI_PORT_ENV = "FL_BRIDGE_MIDI_PORT"
MACOS_MIDI_PORT_DEFAULT = "IAC Driver"
MAX_MIDI_MATCH_CANDIDATES = 8
MAX_MIDI_PORT_NAME_CHARS = 160

PlatformFamily = Literal["windows", "macos", "other"]
UserDataSource = Literal[
    "explicit",
    "environment",
    "windows-known-documents",
    "home-documents",
]


class HostConfigurationError(RuntimeError):
    """Required host configuration is missing or unusable."""


class MidiPortMatchError(HostConfigurationError):
    """A configured MIDI query did not resolve to exactly one endpoint."""

    def __init__(
        self,
        message: str,
        *,
        reason: Literal["missing", "ambiguous"],
        direction: str,
        candidates: Sequence[str],
    ):
        super().__init__(message)
        self.reason = reason
        self.direction = direction
        self.candidates = tuple(candidates)


@dataclass(frozen=True)
class UserDataSelection:
    path: Path
    source: UserDataSource


def platform_family(platform_name: str | None = None) -> PlatformFamily:
    """Normalize Python's platform label to the host distinctions we use."""

    value = (platform_name or sys.platform).lower()
    if value.startswith("win"):
        return "windows"
    if value == "darwin":
        return "macos"
    return "other"


class _GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def from_uuid(cls, value: UUID) -> "_GUID":
        fields = value.fields
        node = fields[5].to_bytes(6, "big")
        data4 = (ctypes.c_ubyte * 8)(fields[3], fields[4], *node)
        return cls(fields[0], fields[1], fields[2], data4)


_FOLDERID_DOCUMENTS = _GUID.from_uuid(
    UUID("fdd39ad0-238f-46af-adb4-6c85480369c7")
)


def windows_known_documents_dir() -> Path | None:
    """Return the redirected Windows Documents known folder when available."""

    if os.name != "nt":
        return None
    raw_path = ctypes.c_void_p()
    try:
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = (
            ctypes.POINTER(_GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_void_p),
        )
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(_FOLDERID_DOCUMENTS), 0, None, ctypes.byref(raw_path)
        )
        if result != 0 or not raw_path.value:
            return None
        value = ctypes.wstring_at(raw_path.value)
        return Path(value) if value else None
    except (AttributeError, OSError, ValueError):
        return None
    finally:
        if raw_path.value:
            try:
                ctypes.windll.ole32.CoTaskMemFree(raw_path)
            except (AttributeError, OSError):
                pass


def default_documents_dir(
    *,
    platform_name: str | None = None,
    home: Path | None = None,
) -> tuple[Path, UserDataSource]:
    """Resolve the platform's Documents folder and record the evidence source."""

    if platform_family(platform_name) == "windows":
        redirected = windows_known_documents_dir()
        if redirected is not None:
            return redirected, "windows-known-documents"
    resolved_home = Path.home() if home is None else Path(home)
    return resolved_home / "Documents", "home-documents"


def default_fl_studio_user_data_dir(
    *,
    platform_name: str | None = None,
    home: Path | None = None,
) -> Path:
    documents, _source = default_documents_dir(
        platform_name=platform_name, home=home
    )
    return documents / "Image-Line" / "FL Studio"


def fl_studio_user_data_selection(
    explicit: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    home: Path | None = None,
) -> UserDataSelection:
    """Resolve FL user data with explicit > environment > platform default."""

    environment = os.environ if environ is None else environ
    if explicit is not None and os.fspath(explicit).strip():
        return UserDataSelection(
            _absolute_configured_path(
                os.fspath(explicit).strip(), "explicit --user-data-dir"
            ),
            "explicit",
        )
    configured = environment.get(FL_STUDIO_USER_DATA_ENV, "").strip()
    if configured:
        return UserDataSelection(
            _absolute_configured_path(
                configured, FL_STUDIO_USER_DATA_ENV
            ),
            "environment",
        )
    documents, source = default_documents_dir(
        platform_name=platform_name, home=home
    )
    return UserDataSelection(
        documents / "Image-Line" / "FL Studio", source
    )


def _absolute_configured_path(value: str, source: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise HostConfigurationError(
            "%s must be an absolute FL Studio user-data path; got %r. "
            "Use the complete path so installer and MCP processes resolve "
            "the same directory regardless of their working directories."
            % (source, value)
        )
    return path


def fl_studio_user_data_dir(
    explicit: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    home: Path | None = None,
) -> Path:
    return fl_studio_user_data_selection(
        explicit,
        environ=environ,
        platform_name=platform_name,
        home=home,
    ).path


def midi_port_query(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> str | None:
    """Return the configured port query, defaulting only on macOS."""

    environment = os.environ if environ is None else environ
    if explicit is not None and explicit.strip():
        return explicit.strip()
    configured = environment.get(FL_BRIDGE_MIDI_PORT_ENV, "").strip()
    if configured:
        return configured
    if platform_family(platform_name) == "macos":
        return MACOS_MIDI_PORT_DEFAULT
    return None


def _bounded_port_names(names: Sequence[str]) -> str:
    limited = []
    for name in names[:MAX_MIDI_MATCH_CANDIDATES]:
        rendered = name
        if len(rendered) > MAX_MIDI_PORT_NAME_CHARS:
            rendered = rendered[: MAX_MIDI_PORT_NAME_CHARS - 3] + "..."
        limited.append(repr(rendered))
    suffix = ""
    if len(names) > MAX_MIDI_MATCH_CANDIDATES:
        suffix = ", ... (+%d more)" % (
            len(names) - MAX_MIDI_MATCH_CANDIDATES
        )
    return "[" + ", ".join(limited) + suffix + "]"


def match_midi_port(
    query: str,
    candidates: Sequence[object],
    *,
    direction: str,
) -> tuple[int, str]:
    """Resolve one endpoint by exact match, then unique substring fallback."""

    normalized_query = query.casefold()
    names = [str(candidate) for candidate in candidates]
    exact = [
        (index, name)
        for index, name in enumerate(names)
        if name.casefold() == normalized_query
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        matches = [name for _index, name in exact]
        raise MidiPortMatchError(
            "MIDI %s query %r is ambiguous: %d case-insensitive exact "
            "matches %s. Set FL_BRIDGE_MIDI_PORT to a uniquely named endpoint."
            % (
                direction,
                query,
                len(matches),
                _bounded_port_names(matches),
            ),
            reason="ambiguous",
            direction=direction,
            candidates=matches,
        )

    substring = [
        (index, name)
        for index, name in enumerate(names)
        if normalized_query in name.casefold()
    ]
    if len(substring) == 1:
        return substring[0]
    if len(substring) > 1:
        matches = [name for _index, name in substring]
        raise MidiPortMatchError(
            "MIDI %s query %r is ambiguous: %d case-insensitive substring "
            "matches %s. Set FL_BRIDGE_MIDI_PORT to a unique exact endpoint name."
            % (
                direction,
                query,
                len(matches),
                _bounded_port_names(matches),
            ),
            reason="ambiguous",
            direction=direction,
            candidates=matches,
        )
    raise MidiPortMatchError(
        "MIDI %s query %r matched no endpoint; available candidates: %s"
        % (direction, query, _bounded_port_names(names)),
        reason="missing",
        direction=direction,
        candidates=names,
    )


def require_midi_port_query(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> str:
    query = midi_port_query(
        explicit, environ=environ, platform_name=platform_name
    )
    if query is None:
        raise HostConfigurationError(
            "FL_BRIDGE_MIDI_PORT must name the user-configured virtual MIDI "
            "endpoint on Windows; Postfader does not install or select a MIDI "
            "driver automatically"
        )
    return query


def mcp_server_environment(
    *,
    user_data_dir: str | os.PathLike[str] | None = None,
    midi_port: str | None = None,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> dict[str, str]:
    """Build the transport environment written into a generated MCP config."""

    selected_user_data = fl_studio_user_data_dir(
        user_data_dir, environ=environ, platform_name=platform_name
    )
    selected_port = require_midi_port_query(
        midi_port, environ=environ, platform_name=platform_name
    )
    return {
        "FL_BRIDGE_ENABLE_MIDI": "1",
        FL_BRIDGE_MIDI_PORT_ENV: selected_port,
        FL_STUDIO_USER_DATA_ENV: os.fspath(selected_user_data),
    }
