"""Reduce a live plug-in scan to privacy-safe compatibility evidence.

The bridge has to read current parameter values and display strings to decide
which reported slots are real.  Those values are session data: a key, preset,
timing value, or custom option can describe somebody's project.  The local
reducer uses a small closed vocabulary to classify controls, while public
reports retain only structural counts.  They never emit values, units, display
strings, parameter names, option text, track/slot locations, paths, dates, or
project metadata.

Both bridge-wire and typed MCP scan shapes are accepted.  Keeping that adapter
here prevents the command-line reporter from drifting from the bridge schema
again while its tests continue to exercise only one representation.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Literal


_NUMERIC_DISPLAY = re.compile(
    r"^[-+]?\d+(?:\.\d+)?\s*(?P<unit>[^\d\s][^\d]*)?$"
)
_ON_OFF = {"on", "off", "yes", "no", "enabled", "disabled"}

# Display text is untrusted session data.  Only units from this closed list
# survive reduction; an arbitrary suffix such as a preset or client label is
# deliberately discarded rather than guessed to be a unit.
_KNOWN_UNITS = {
    "%": "%",
    "bpm": "BPM",
    "cent": "cent",
    "cents": "cents",
    "db": "dB",
    "degree": "degree",
    "degrees": "degrees",
    "hz": "Hz",
    "khz": "kHz",
    "ms": "ms",
    "oct": "oct",
    "octave": "octave",
    "octaves": "octaves",
    "s": "s",
    "sample": "sample",
    "samples": "samples",
    "semitone": "semitone",
    "semitones": "semitones",
    "st": "st",
    "x": "x",
}

ControlKind = Literal["on_off", "numeric", "enumerated", "unknown"]


def _integer(value: Any, field: str) -> int:
    """Accept exact JSON integers only; evidence must never be coerced."""
    if type(value) is not int:
        raise ValueError(f"plug-in scan {field} must be an integer")
    return value


def _optional_integer(value: Any, field: str) -> int | None:
    return None if value is None else _integer(value, field)


def _mapping(scan: Any) -> dict[str, Any]:
    if hasattr(scan, "model_dump"):
        scan = scan.model_dump(mode="python")
    if not isinstance(scan, dict):
        raise ValueError("plug-in scan must be an object")
    return scan


_MISSING = object()


def _same_json_value(left: Any, right: Any) -> bool:
    """Compare untrusted JSON-like values without bool/int equivalence."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json_value(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json_value(a, b) for a, b in zip(left, right)
        )
    return bool(left == right)


def _alias_value(
    source: dict[str, Any], typed_key: str, raw_key: str, field: str
) -> Any:
    """Read one raw/typed alias pair, rejecting contradictory duplicates."""
    typed = source.get(typed_key, _MISSING)
    raw = source.get(raw_key, _MISSING)
    if typed is not _MISSING and raw is not _MISSING:
        if not _same_json_value(typed, raw):
            raise ValueError(f"plug-in scan carries conflicting {field} fields")
        return typed
    if typed is not _MISSING:
        return typed
    if raw is not _MISSING:
        return raw
    return None


def _normalise_scan(scan: Any) -> dict[str, Any]:
    """Return one internal shape from bridge-wire or typed MCP input."""
    source = _mapping(scan)
    plugin = source.get("plugin")
    if isinstance(plugin, dict):
        plugin_name = plugin.get("name")
    elif isinstance(plugin, str):
        plugin_name = plugin
    else:
        raise ValueError("plug-in scan plugin must be a string or object")
    if not isinstance(plugin_name, str):
        raise ValueError("plug-in scan plugin name must be a string")

    raw_rows = _alias_value(source, "parameters", "params", "parameter-list")
    if not isinstance(raw_rows, list):
        raise ValueError("plug-in scan parameters must be a list")

    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise ValueError("each plug-in parameter must be an object")
        typed = "reported_name" in raw or "display_text" in raw
        if (
            "reported_name" in raw
            and "name" in raw
            and not _same_json_value(raw["reported_name"], raw["name"])
        ):
            raise ValueError("plug-in parameter carries conflicting name fields")
        if (
            "display_text" in raw
            and "display" in raw
            and not _same_json_value(raw["display_text"], raw["display"])
        ):
            raise ValueError("plug-in parameter carries conflicting display fields")
        name = raw.get("reported_name", raw.get("name"))
        display = raw.get("display_text", raw.get("display"))
        if name is not None and not isinstance(name, str):
            raise ValueError("plug-in parameter name must be a string or null")
        if display is not None and not isinstance(display, str):
            raise ValueError("plug-in parameter display must be a string or null")
        availability = raw.get("display_text_available")
        if availability is not None and type(availability) is not bool:
            raise ValueError("plug-in parameter display availability must be a boolean")
        if typed and availability is None:
            raise ValueError("typed plug-in parameter is missing display availability")
        if availability is not None and availability != bool(display):
            raise ValueError("plug-in parameter display availability is inconsistent")
        rows.append(
            {
                "index": raw.get("index"),
                "reported_name": name,
                "display_text": display,
                "display_text_available": (
                    bool(display) if availability is None else availability
                ),
            }
        )

    reported = _alias_value(
        source, "reported_parameter_count", "reported_count", "reported-count"
    )
    examined = _alias_value(
        source, "examined_count", "examined", "examined-count"
    )
    real = _alias_value(source, "real_count", "real", "real-count")
    if isinstance(plugin, dict):
        nested_reported = plugin.get("reported_parameter_count")
        if (
            nested_reported is not None
            and not _same_json_value(reported, nested_reported)
        ):
            raise ValueError(
                "plug-in scan carries conflicting aggregate and plug-in reported counts"
            )
    start = source.get("scan_start")
    end = source.get("scan_end")
    if "truncated" not in source or type(source["truncated"]) is not bool:
        raise ValueError("plug-in scan truncated must be a boolean")
    truncated_by = source.get("truncated_by")
    if truncated_by is not None and not isinstance(truncated_by, str):
        raise ValueError("plug-in scan truncated_by must be a string or null")
    return {
        "plugin_name": plugin_name or "unknown",
        "reported_count": _integer(reported, "reported count"),
        "examined": _integer(examined, "examined count"),
        "real_count": _integer(real, "real count"),
        "padding_skipped": _integer(source.get("padding_skipped"), "padding count"),
        "truncated": source["truncated"],
        "truncated_by": truncated_by or None,
        "scan_start": _optional_integer(start, "scan start"),
        "scan_end": _optional_integer(end, "scan end"),
        "parameters": rows,
    }


@dataclass(frozen=True)
class ParameterShape:
    """What a control is, with no trace of how it is currently set."""

    index: int
    named: bool
    # Retained in memory for local diagnostics, but never rendered into a
    # community report.  Some hosts can surface user-defined labels here.
    name: str | None
    kind: ControlKind
    unit: str | None = None


@dataclass
class PluginProfile:
    plugin_name: str
    reported_count: int
    examined: int
    real_count: int
    padding_skipped: int
    truncated: bool
    truncated_by: str | None
    scan_start: int | None
    scan_end: int | None
    highest_real_index: int | None
    largest_index_gap: int
    nameless_count: int
    display_available_count: int
    parameters: list[ParameterShape] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def enumerated(self) -> list[ParameterShape]:
        return [
            parameter
            for parameter in self.parameters
            if parameter.kind == "enumerated"
        ]

    @property
    def units(self) -> list[str]:
        seen: list[str] = []
        for parameter in self.parameters:
            if parameter.unit and parameter.unit not in seen:
                seen.append(parameter.unit)
        return seen

    @property
    def internally_consistent(self) -> bool:
        indices = [parameter.index for parameter in self.parameters]
        bounds_known = self.scan_start is not None and self.scan_end is not None
        bounded_width = (
            self.scan_end - self.scan_start if bounds_known else None
        )
        return (
            self.reported_count >= 0
            and self.examined >= 0
            and self.real_count >= 0
            and self.padding_skipped >= 0
            and self.real_count == len(self.parameters)
            and self.examined == self.real_count + self.padding_skipped
            and len(indices) == len(set(indices))
            and all(index >= 0 for index in indices)
            and all(index < self.reported_count for index in indices)
            and (self.scan_start is None or self.scan_start >= 0)
            and (self.scan_end is None or self.scan_end >= 0)
            and (
                self.scan_start is None
                or self.scan_end is None
                or self.scan_start <= self.scan_end <= self.reported_count
            )
            and (
                not bounds_known
                or all(self.scan_start <= index < self.scan_end for index in indices)
            )
            and (bounded_width is None or self.examined <= bounded_width)
            and (
                self.truncated
                or bounded_width is None
                or self.examined == bounded_width
            )
            and self.truncated_by in {
                None, "max_indices", "max_results", "start", "end"
            }
            and (self.truncated or self.truncated_by is None)
        )

    @property
    def complete(self) -> bool:
        """Whether every index FL reported was examined consistently."""
        bounds_cover_all = (
            self.scan_start == 0 and self.scan_end == self.reported_count
        )
        return (
            self.internally_consistent
            and not self.truncated
            and bounds_cover_all
            and self.examined == self.reported_count
        )

    @property
    def kind_counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for parameter in self.parameters:
            result[parameter.kind] = result.get(parameter.kind, 0) + 1
        return result


def classify(display: str | None) -> tuple[ControlKind, str | None]:
    """Classify a display without allowing its free text into public output."""
    if display is None:
        return "unknown", None
    text = str(display).strip()
    if not text:
        return "unknown", None
    if text.casefold() in _ON_OFF:
        return "on_off", None
    match = _NUMERIC_DISPLAY.match(text)
    if match:
        raw_unit = (match.group("unit") or "").strip()
        unit = _KNOWN_UNITS.get(raw_unit.casefold()) if raw_unit else None
        return "numeric", unit
    # The actual text is deliberately not retained. It may be a product-owned
    # enumeration, a user preset, a sample, or a project-specific label.
    return "enumerated", None


def summarise(scan: Any) -> PluginProfile:
    """Reduce a raw or typed ``plugin.scan_params`` result to safe shape."""
    normal = _normalise_scan(scan)
    shapes: list[ParameterShape] = []
    indices: list[int] = []
    nameless = 0
    with_display = 0

    for row in normal["parameters"]:
        index = _integer(row.get("index"), "parameter index")
        raw_name = str(row.get("reported_name") or "").strip()
        display = row.get("display_text")
        if row.get("display_text_available"):
            with_display += 1
        kind, unit = classify(None if display is None else str(display))
        if not raw_name:
            nameless += 1
        shapes.append(
            ParameterShape(
                index=index,
                named=bool(raw_name),
                name=raw_name or None,
                kind=kind,
                unit=unit,
            )
        )
        indices.append(index)

    indices.sort()
    largest_gap = 0
    for earlier, later in zip(indices, indices[1:]):
        largest_gap = max(largest_gap, later - earlier - 1)

    profile = PluginProfile(
        plugin_name=normal["plugin_name"],
        reported_count=normal["reported_count"],
        examined=normal["examined"],
        real_count=normal["real_count"],
        padding_skipped=normal["padding_skipped"],
        truncated=normal["truncated"],
        truncated_by=normal["truncated_by"],
        scan_start=normal["scan_start"],
        scan_end=normal["scan_end"],
        highest_real_index=indices[-1] if indices else None,
        largest_index_gap=largest_gap,
        nameless_count=nameless,
        display_available_count=with_display,
        parameters=shapes,
    )
    profile.notes = _notes(profile)
    return profile


def _notes(profile: PluginProfile) -> list[str]:
    notes: list[str] = []
    if profile.reported_count > profile.real_count * 4:
        notes.append(
            f"FL reports {profile.reported_count} slots for {profile.real_count} "
            "real-looking controls; de-padding is required."
        )
    if profile.nameless_count:
        notes.append(
            f"{profile.nameless_count} of {len(profile.parameters)} controls "
            "report no name."
        )
    if profile.enumerated:
        notes.append(
            f"{len(profile.enumerated)} controls display text rather than a number; "
            "option text is withheld from public reports."
        )
    if profile.units:
        notes.append("Recognised units: " + ", ".join(profile.units) + ".")

    search_bound = 256
    if profile.largest_index_gap >= search_bound:
        notes.append(
            f"The widest gap is {profile.largest_index_gap}, meeting or exceeding "
            f"the {search_bound}-index name-search run; address later controls "
            "by index."
        )
    else:
        notes.append(
            f"Widest gap between real-looking controls: {profile.largest_index_gap}."
        )
    if profile.truncated:
        notes.append(
            "The scan is partial (stopped by "
            f"{profile.truncated_by or 'an unknown bound'})."
        )
    if not profile.internally_consistent:
        notes.append(
            "The supplied scan is internally inconsistent and is not "
            "publishable evidence."
        )
    return notes


def markdown_text(value: Any, *, maximum: int | None = 160) -> str:
    """Collapse and escape one untrusted label for a Markdown table cell."""
    text = " ".join(str(value or "unknown").split())
    if maximum is not None:
        text = text[:maximum]
    text = html.escape(text, quote=False).replace("|", "&#124;")
    return text or "unknown"
