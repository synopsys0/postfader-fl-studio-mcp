"""Summarise a plug-in's parameter map into something shareable.

`plugins_scan_parameters` answers what a plug-in exposes *right now*, which
includes the settings someone chose. A control reading `Key = A` and
`Scale = Minor` states the key of the song; `Retune Speed = 10 ms` is a mixing
decision. None of that describes the plug-in, and none of it belongs in a
compatibility table.

So this module reduces a scan to shape and discards values. The sanitisation
is structural rather than a filter: :class:`ParameterShape` has no field for a
value or a display string, so there is nothing to accidentally forward. What
survives is the part that is a property of the plug-in -- how many controls
exist, where they sit, whether they are named, what kind of control they are,
and what unit they speak in -- which is exactly what tells the next person
whether their plug-in will work and which scan bounds it needs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

# Matches a display that is a bare number with an optional unit: "10 ms",
# "-3.5 dB", "100 %", "0". The unit is a property of the control; the number
# is a setting, so only the unit is kept.
_NUMERIC_DISPLAY = re.compile(r"^[-+]?\d+(?:\.\d+)?\s*(?P<unit>[^\d\s][^\d]*)?$")

_ON_OFF = {"on", "off", "yes", "no", "enabled", "disabled"}

ControlKind = Literal["on_off", "numeric", "enumerated", "unknown"]


@dataclass(frozen=True)
class ParameterShape:
    """What a control *is*, with no trace of how it is currently set."""

    index: int
    named: bool
    name: str | None          # from the plug-in's own vocabulary, not the user's
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
    highest_real_index: int | None
    largest_index_gap: int
    nameless_count: int
    display_available_count: int
    parameters: list[ParameterShape] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def enumerated(self) -> list[ParameterShape]:
        return [p for p in self.parameters if p.kind == "enumerated"]

    @property
    def units(self) -> list[str]:
        seen: list[str] = []
        for p in self.parameters:
            if p.unit and p.unit not in seen:
                seen.append(p.unit)
        return seen


def classify(display: str | None) -> tuple[ControlKind, str | None]:
    """Decide what kind of control a display string implies.

    Real plug-ins pad their display text -- every string observed on a live
    VST3 carried a trailing space -- so strip before deciding anything.
    """
    if display is None:
        return "unknown", None
    text = display.strip()
    if not text:
        return "unknown", None
    if text.casefold() in _ON_OFF:
        return "on_off", None
    match = _NUMERIC_DISPLAY.match(text)
    if match:
        unit = (match.group("unit") or "").strip() or None
        return "numeric", unit
    # Text that is not a number and not a switch is an enumeration: a key, a
    # scale, a mode. Its options can only be learned by sweeping the control,
    # which mutates, so a read-only report says one exists and stops there.
    return "enumerated", None


def summarise(scan: dict[str, Any]) -> PluginProfile:
    """Reduce a `plugins_scan_parameters` result to shareable shape."""
    plugin = scan.get("plugin") or {}
    rows = scan.get("parameters") or []

    shapes: list[ParameterShape] = []
    indices: list[int] = []
    nameless = 0
    with_display = 0

    for row in rows:
        index = int(row.get("index", -1))
        raw_name = (row.get("reported_name") or "").strip()
        display = row.get("display_text")
        if row.get("display_text_available"):
            with_display += 1
        kind, unit = classify(display)
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
        plugin_name=str(plugin.get("name") or "unknown"),
        reported_count=int(scan.get("reported_parameter_count") or 0),
        examined=int(scan.get("examined_count") or 0),
        real_count=int(scan.get("real_count") or len(rows)),
        padding_skipped=int(scan.get("padding_skipped") or 0),
        truncated=bool(scan.get("truncated")),
        truncated_by=scan.get("truncated_by"),
        highest_real_index=indices[-1] if indices else None,
        largest_index_gap=largest_gap,
        nameless_count=nameless,
        display_available_count=with_display,
        parameters=shapes,
    )
    profile.notes = _notes(profile)
    return profile


def _notes(profile: PluginProfile) -> list[str]:
    """Turn the numbers into the verdicts a reader actually needs."""
    notes: list[str] = []

    if profile.reported_count > profile.real_count * 4:
        notes.append(
            f"FL reports {profile.reported_count} slots for ~{profile.real_count} "
            "real controls, so this plug-in must be de-padded rather than paged."
        )
    if profile.nameless_count:
        share = profile.nameless_count / max(1, len(profile.parameters))
        notes.append(
            f"{profile.nameless_count} of {len(profile.parameters)} controls "
            f"({share:.0%}) report no name and are identifiable only by what "
            "they display, so address them with the display or option form."
        )
    if profile.enumerated:
        notes.append(
            f"{len(profile.enumerated)} controls display text rather than a "
            "number; their option lists can only be discovered by sweeping, "
            "which moves the control."
        )
    if profile.units:
        notes.append("Units seen in display text: " + ", ".join(profile.units) + ".")

    # The bound that decides whether a name search finds everything.
    from_bound = 256  # PARAM_SEARCH_RUN in the bridge
    if profile.largest_index_gap >= from_bound:
        notes.append(
            f"The widest gap between real controls is {profile.largest_index_gap}, "
            f"which meets or exceeds the {from_bound}-index search run: a name "
            "search can stop early on this plug-in. Use plugins_scan_parameters "
            "and address by index."
        )
    else:
        notes.append(
            f"Widest gap between real controls is {profile.largest_index_gap}, "
            f"well inside the {from_bound}-index search run."
        )
    if profile.truncated:
        notes.append(
            f"This scan was truncated by {profile.truncated_by}; the numbers "
            "above describe the range examined, not the whole plug-in."
        )
    # A genuine scan reports as many rows as it counted. If they disagree the
    # caller passed a partial set, and every row-derived figure above covers
    # only that subset -- say so rather than presenting them as the whole.
    if profile.real_count != len(profile.parameters):
        notes.append(
            f"The scan counted {profile.real_count} real controls but supplied "
            f"{len(profile.parameters)} rows; per-control figures cover the "
            "rows supplied only."
        )
    return notes


def render_markdown(profile: PluginProfile) -> str:
    """A table row for docs/plugin-support.md plus the reasoning behind it."""
    kinds: dict[str, int] = {}
    for p in profile.parameters:
        kinds[p.kind] = kinds.get(p.kind, 0) + 1
    kind_summary = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))

    lines = [
        "Paste this row into the table in docs/plugin-support.md:",
        "",
        f"| {profile.plugin_name} | VST/VST3 | ~{profile.reported_count} | "
        f"{profile.real_count} real, widest gap {profile.largest_index_gap}, "
        f"{profile.nameless_count} nameless |",
        "",
        "Detail:",
        "",
        f"- reported slots: {profile.reported_count}",
        f"- real controls found: {profile.real_count} "
        f"(examined {profile.examined}, padding skipped {profile.padding_skipped})",
        f"- highest real index: {profile.highest_real_index}",
        f"- control kinds: {kind_summary or 'none'}",
        f"- controls with a display string: {profile.display_available_count}",
        "",
    ]
    lines += [f"- {note}" for note in profile.notes]
    lines += [
        "",
        "No current value, display text, or setting appears above: those "
        "describe the session, not the plug-in.",
    ]
    return "\n".join(lines)

# Ceiling the bridge puts on a sweep. A control with more options than this
# cannot be fully enumerated by sweeping at all.
MAX_SWEEP_STEPS = 256

# Options partition 0..1 into roughly equal contiguous bands rather than
# needing fine sampling, so a sweep only has to land in each band once.
# Measured on a live VST3: a 29-option control resolved completely at 64 steps
# -- about 2.2 samples per option -- and 256 steps found nothing further. Two
# samples per option is therefore the working rule, with headroom.
SWEEP_SAMPLES_PER_OPTION = 2

# An option list is the plug-in's own vocabulary and is the same for everyone
# who owns it, so it is safe to publish -- unless the control enumerates things
# the user made. A preset or sample selector does exactly that.
_USER_CONTENT = re.compile(
    r"[/\\]"                     # a path separator
    r"|\.(wav|aiff?|mp3|flac|fxp|fxb|vstpreset|nks|nki)\b"  # a file extension
    r"|^(?:untitled|new preset|my )",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OptionSurvey:
    """What sweeping one enumerated control found."""

    index: int
    name: str | None
    option_count: int
    steps_used: int
    options: tuple[str, ...] = ()
    looks_user_generated: bool = False

    @property
    def steps_needed(self) -> int:
        """Sweep resolution this control wants, by the measured rule."""
        return self.option_count * SWEEP_SAMPLES_PER_OPTION

    @property
    def default_is_enough(self) -> bool:
        return 64 >= self.steps_needed

    @property
    def enumerable_at_all(self) -> bool:
        return self.steps_needed <= MAX_SWEEP_STEPS


def survey_options(index: int, name: str | None, options: list[str],
                   steps_used: int) -> OptionSurvey:
    """Reduce one sweep result, withholding option text that is user content."""
    cleaned = tuple(o.strip() for o in options if o and o.strip())
    suspect = any(_USER_CONTENT.search(o) for o in cleaned)
    return OptionSurvey(
        index=index,
        name=name,
        option_count=len(cleaned),
        steps_used=steps_used,
        # Withheld entirely when anything in the list looks like the user's
        # own content: one preset name is enough to make the list personal.
        options=() if suspect else cleaned,
        looks_user_generated=suspect,
    )


def render_option_survey(surveys: list[OptionSurvey]) -> str:
    """Format sweep findings, including where the default resolution fails."""
    if not surveys:
        return "No enumerated controls were surveyed."
    lines = ["", "Enumerated controls (discovered by sweeping):", ""]
    for s in sorted(surveys, key=lambda s: s.index):
        label = s.name or f"index {s.index}"
        verdict = (
            "default 64 steps is enough" if s.default_is_enough
            else f"needs sweep_steps>={min(s.steps_needed, MAX_SWEEP_STEPS)}"
            if s.enumerable_at_all
            else f"CANNOT be fully enumerated: {s.option_count} options exceeds "
                 f"what {MAX_SWEEP_STEPS} steps can resolve"
        )
        lines.append(f"- {label}: {s.option_count} options, {verdict}")
        if s.looks_user_generated:
            lines.append(
                "  Option text withheld: this control enumerates something "
                "that looks user-created, such as presets or samples."
            )
        elif s.options:
            lines.append("  " + ", ".join(s.options))
    return "\n".join(lines)

