"""Installed, privacy-safe plug-in compatibility reporter.

The default path is read-only.  An optional representative write check exists
for a blank disposable project only; it requires an explicit acknowledgement,
refuses Master/playback/recording, and does not claim success unless the move
and exact restoration are independently read back.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from . import __version__
from .plugin_profile import PluginProfile, markdown_text, summarise


PluginFormat = Literal["native", "VST", "VST3", "AU", "unknown"]
PluginOrigin = Literal["stock", "third-party", "unknown"]
Edition = Literal["Fruity", "Producer", "Signature", "All Plugins", "unknown"]
EvidenceLevel = Literal["detected", "read-profiled", "write-validated"]


class ReportClient(Protocol):
    def ping(self) -> dict[str, Any]: ...
    def call(self, command: str, **arguments: Any) -> dict[str, Any]: ...
    def close(self) -> None: ...

_HOME_MARKERS = (
    "/" + "Users" + "/",
    "/" + "home" + "/",
    ":\\" + "Users" + "\\",
)
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_FL_VERSION = re.compile(
    r"(?:^|\s)v?(\d+)\.(\d+)\.(\d+)(?:\s*\[?build\s+(\d+)\]?)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WriteValidationEvidence:
    attempted: bool = False
    move_verified: bool | None = None
    move_verification_basis: str = "not-run"
    restore_command_verified: bool | None = None
    restore_readback_verified: bool | None = None
    outcome: str = "not-run"

    @property
    def successful(self) -> bool:
        return (
            self.attempted
            and self.move_verified is True
            and self.move_verification_basis in {"value_readback", "display_change_only"}
            and self.restore_command_verified is True
            and self.restore_readback_verified is True
            and self.outcome == "write-and-exact-restore-verified"
        )


@dataclass(frozen=True)
class PublicPluginReport:
    schema_version: str
    plugin_name: str
    plugin_version: str
    plugin_origin: PluginOrigin
    plugin_format: PluginFormat
    scope: Literal["mixer-effect"]
    evidence_level: EvidenceLevel
    evidence_source: Literal["community-candidate"]
    fl_studio_version: str
    fl_studio_edition: Edition
    postfader_version: str
    platform: str
    scan_complete: bool
    scan_truncated_by: str | None
    reported_slots: int
    examined_indices: int
    real_controls: int
    padding_skipped: int
    highest_real_index: int | None
    widest_index_gap: int
    nameless_controls: int
    write_validation: WriteValidationEvidence
    limitations: tuple[str, ...]

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plugin": {
                "name": self.plugin_name,
                "version": self.plugin_version,
                "origin": self.plugin_origin,
                "format": self.plugin_format,
                "scope": self.scope,
            },
            "evidence": {
                "level": self.evidence_level,
                "source": self.evidence_source,
                "read_scan": "complete" if self.scan_complete else "partial",
                "truncated_by": self.scan_truncated_by,
            },
            "environment": {
                "fl_studio_version": self.fl_studio_version,
                "fl_studio_edition": self.fl_studio_edition,
                "postfader_version": self.postfader_version,
                "platform": self.platform,
            },
            "structure": {
                "reported_slots": self.reported_slots,
                "examined_indices": self.examined_indices,
                "real_controls": self.real_controls,
                "padding_skipped": self.padding_skipped,
                "highest_real_index": self.highest_real_index,
                "widest_index_gap": self.widest_index_gap,
                "nameless_controls": self.nameless_controls,
            },
            "representative_write": {
                "attempted": self.write_validation.attempted,
                "move_verified": self.write_validation.move_verified,
                "verification_basis": self.write_validation.move_verification_basis,
                "restore_command_verified": (
                    self.write_validation.restore_command_verified
                ),
                "exact_restore_readback_verified": (
                    self.write_validation.restore_readback_verified
                ),
                "outcome": self.write_validation.outcome,
            },
            "limitations": list(self.limitations),
            "privacy": {
                "current_values_included": False,
                "display_text_included": False,
                "parameter_names_included": False,
                "option_text_included": False,
                "track_or_slot_included": False,
                "project_metadata_included": False,
                "paths_or_timestamps_included": False,
            },
        }


def _public_label(value: Any, field: str, maximum: int = 160) -> str:
    raw = str(value or "").strip()
    text = " ".join(raw.split())
    if not text or text.casefold() == "unknown":
        raise ValueError(f"{field} was not reported")
    if len(text) > maximum:
        raise ValueError(f"{field} is too long for a public report")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in text):
        raise ValueError(f"{field} contains unsafe control or formatting characters")
    folded = text.casefold()
    if any(marker.casefold() in folded for marker in _HOME_MARKERS):
        raise ValueError(f"{field} looks like a private home path")
    if _EMAIL.search(text) or _UUID.search(text):
        raise ValueError(f"{field} looks like host or account metadata")
    return text


def _safe_fl_version(value: Any) -> str:
    match = _FL_VERSION.search(str(value or ""))
    if not match:
        return "unknown"
    major, minor, patch, build = match.groups()
    result = f"{major}.{minor}.{patch}"
    return result if build is None else f"{result} build {build}"


def _safe_platform() -> str:
    if platform.system() != "Darwin":
        return "unknown"
    architecture = platform.machine().casefold()
    architecture = architecture if architecture in {"arm64", "x86_64"} else "unknown"
    return f"macOS {architecture}"


def build_public_report(
    profile: PluginProfile,
    *,
    plugin_version: str,
    plugin_origin: PluginOrigin,
    plugin_format: PluginFormat,
    fl_studio_version: str,
    fl_studio_edition: Edition,
    write_validation: WriteValidationEvidence | None = None,
) -> PublicPluginReport:
    """Validate and freeze the exact fields allowed into a public report."""
    if not profile.internally_consistent:
        raise ValueError(
            "the scan is internally inconsistent; refusing to publish misleading evidence"
        )
    name = _public_label(profile.plugin_name, "plug-in name")
    version = (
        "unknown"
        if not plugin_version or plugin_version.casefold() == "unknown"
        else _public_label(plugin_version, "plug-in version", maximum=64)
    )
    write = write_validation or WriteValidationEvidence()
    level: EvidenceLevel = (
        "write-validated"
        if profile.complete and profile.real_count > 0 and write.successful
        else "read-profiled"
        if profile.complete and profile.real_count > 0
        else "detected"
    )
    limitations: list[str] = []
    if not profile.complete:
        limitations.append(
            "Parameter scan was partial; unexamined controls may exist."
        )
        if write.attempted:
            limitations.append(
                "A representative write cannot promote partial read evidence to write-validated."
            )
    if profile.real_count == 0:
        limitations.append(
            "FL exposed no real-looking controls; this proves detection only."
        )
    if profile.largest_index_gap >= 256:
        limitations.append(
            "A gap reaches the bounded name-search limit; later controls should be addressed by index."
        )
    if profile.nameless_count:
        noun = "control" if profile.nameless_count == 1 else "controls"
        verb = "was" if profile.nameless_count == 1 else "were"
        limitations.append(
            f"{profile.nameless_count} {noun} {verb} nameless in FL's report."
        )
    if write.move_verification_basis == "display_change_only":
        limitations.append(
            "The representative write proved control movement, not the exact requested normalized destination."
        )
    if not write.attempted:
        limitations.append("No representative write was attempted.")
    elif not write.successful:
        limitations.append("Representative write validation did not complete successfully.")
    limitations.append("Representative validation does not test every parameter or audible result.")

    return PublicPluginReport(
        schema_version="1.0",
        plugin_name=name,
        plugin_version=version,
        plugin_origin=plugin_origin,
        plugin_format=plugin_format,
        scope="mixer-effect",
        evidence_level=level,
        evidence_source="community-candidate",
        fl_studio_version=_safe_fl_version(fl_studio_version),
        fl_studio_edition=fl_studio_edition,
        postfader_version=__version__,
        platform=_safe_platform(),
        scan_complete=profile.complete,
        scan_truncated_by=profile.truncated_by,
        reported_slots=profile.reported_count,
        examined_indices=profile.examined,
        real_controls=profile.real_count,
        padding_skipped=profile.padding_skipped,
        highest_real_index=profile.highest_real_index,
        widest_index_gap=profile.largest_index_gap,
        nameless_controls=profile.nameless_count,
        write_validation=write,
        limitations=tuple(limitations),
    )


def _environment(report: PublicPluginReport) -> str:
    pieces: list[str] = []
    if report.fl_studio_version != "unknown":
        pieces.append(f"FL Studio {report.fl_studio_version}")
    if report.fl_studio_edition != "unknown":
        pieces.append(report.fl_studio_edition)
    pieces.append(f"Postfader {report.postfader_version}")
    if report.platform != "unknown":
        pieces.append(report.platform)
    return "; ".join(pieces)


def render_public_markdown(report: PublicPluginReport) -> str:
    """Produce an issue-ready report and a pasteable matrix row."""
    read_result = (
        f"complete; {report.real_controls}/{report.reported_slots} real-looking; "
        f"{report.padding_skipped} padding"
        if report.scan_complete
        else f"partial; examined {report.examined_indices}/{report.reported_slots}; "
        f"{report.real_controls} real-looking"
    )
    write = report.write_validation
    write_result = (
        f"verified ({write.move_verification_basis})"
        if write.move_verified is True
        else "not verified"
        if write.attempted
        else "not run"
    )
    restore_result = (
        "exact restore confirmed by independent readback"
        if write.restore_readback_verified is True
        else "restore not confirmed"
        if write.attempted
        else "not run"
    )
    product = (
        report.plugin_name
        if report.plugin_version == "unknown"
        else f"{report.plugin_name} {report.plugin_version}"
    )
    name = markdown_text(product)
    origin_format = markdown_text(
        f"{report.plugin_origin} / {report.plugin_format}", maximum=48
    )
    row = (
        f"| {name} | {origin_format} | {report.evidence_level} | "
        f"{markdown_text(read_result, maximum=180)} | "
        f"{markdown_text(write_result, maximum=100)} | "
        f"{markdown_text(restore_result, maximum=100)} | "
        f"{markdown_text(_environment(report), maximum=180)} | "
        "community candidate | "
        f"See full limitations below ({len(report.limitations)}). |"
    )
    # Keep the matrix readable, but never abbreviate safety or evidence caveats.
    # They are escaped like table cells without applying a length limit.
    limitation_lines = [
        f"- {markdown_text(item, maximum=None)}"
        for item in report.limitations
    ] or ["- None reported."]
    structure = report.as_public_dict()["structure"]
    lines = [
        "## Postfader plug-in validation report",
        "",
        f"Report schema: `{report.schema_version}`",
        "",
        "Submission status: **community candidate** — contributor-generated "
        "evidence that has not yet been reviewed or merged into Postfader's "
        "maintained compatibility matrix.",
        "",
        "### Matrix candidate",
        "",
        "| Plug-in | Origin / format | Evidence | Read result | Write check | "
        "Restore | Environment | Source | Limitations |",
        "|---|---|---|---|---|---|---|---|---|",
        row,
        "",
        "### Full limitations",
        "",
        *limitation_lines,
        "",
        "### Structural evidence",
        "",
        f"- reported slots: {structure['reported_slots']}",
        f"- indices examined: {structure['examined_indices']}",
        f"- real-looking controls: {structure['real_controls']}",
        f"- padding skipped: {structure['padding_skipped']}",
        f"- highest real index: {structure['highest_real_index']}",
        f"- widest index gap: {structure['widest_index_gap']}",
        f"- nameless controls: {structure['nameless_controls']}",
        "",
        "### Privacy boundary",
        "",
        "This generated report contains no current values, display-derived kinds or "
        "units, display text, parameter "
        "names, option text, mixer location, project metadata, path, or timestamp. "
        "Review it before posting; do not attach the source scan, logs, screenshots, "
        "presets, project files, or audio.",
    ]
    return "\n".join(lines)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"FL bridge returned a malformed {label}")
    return value


def _parameter_page(client: ReportClient, track: int, slot: int, index: int) -> dict[str, Any]:
    page = _object(client.call(
        "plugin.params", track=track, slot=slot, offset=index, limit=1,
        skip_padding=False,
    ), "parameter page")
    for row in page.get("params") or []:
        if row.get("index") == index:
            return row
    raise ValueError("the selected parameter is not currently readable")


def _normalised_value(row: dict[str, Any]) -> float:
    value = row.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("the selected parameter has no numeric normalized value")
    result = float(value)
    if not math.isfinite(result) or result < 0.0 or result > 1.0:
        raise ValueError("the selected parameter reported an invalid normalized value")
    return result


def validate_representative_write(
    client: ReportClient,
    profile: PluginProfile,
    *,
    track: int,
    slot: int,
    parameter_index: int,
) -> WriteValidationEvidence:
    """Move one known control, restore it, and independently verify restoration."""
    if track <= 0:
        raise ValueError("representative write validation refuses the Master track")
    if slot < 0 or slot > 9 or parameter_index < 0:
        raise ValueError("invalid mixer slot or parameter index")
    if parameter_index not in {parameter.index for parameter in profile.parameters}:
        raise ValueError("the selected index was not a real-looking control in this scan")
    if not profile.complete or not profile.internally_consistent:
        raise ValueError("representative write validation requires a complete read profile")
    selected = next(
        parameter for parameter in profile.parameters if parameter.index == parameter_index
    )
    if selected.kind != "numeric":
        raise ValueError(
            "representative write validation requires a numeric control; "
            "switches, enumerations, and unknown controls are not generic test targets"
        )

    ping = _object(client.ping(), "handshake")
    if ping.get("verified_writes_enabled") is not True:
        raise ValueError("the running FL bridge has verified writes disabled")
    state = _object(client.call("project.info"), "project state")
    if state.get("playing") not in (False, 0):
        raise ValueError("write validation refuses while FL Studio is playing")
    if state.get("recording") not in (False, 0):
        raise ValueError("write validation refuses while FL Studio is recording")
    if state.get("safe_to_edit") not in (True, 1):
        raise ValueError("FL Studio does not currently report that it is safe to edit")

    before = _parameter_page(client, track, slot, parameter_index)
    original = _normalised_value(before)
    original_display = str(before.get("display") or "")
    if not original_display.strip():
        raise ValueError(
            "representative write validation requires a nonblank display for exact restore proof"
        )
    target = 0.25 if original >= 0.5 else 0.75

    moved: dict[str, Any] | None = None
    restored: dict[str, Any] | None = None
    reread: dict[str, Any] | None = None
    move_error = False
    restore_error = False
    reread_error = False
    try:
        try:
            moved = _object(client.call(
                "plugin.set_param",
                track=track,
                slot=slot,
                index=parameter_index,
                value=target,
            ), "write result")
        except (OSError, RuntimeError, TimeoutError, ValueError):
            move_error = True
    finally:
        # Restoration is a new request for the captured original value, never
        # a replay of the possibly ambiguous test write.
        try:
            restored = _object(client.call(
                "plugin.set_param",
                track=track,
                slot=slot,
                index=parameter_index,
                value=original,
            ), "restore result")
        except (OSError, RuntimeError, TimeoutError, ValueError):
            restore_error = True
        try:
            reread = _parameter_page(client, track, slot, parameter_index)
        except (OSError, RuntimeError, TimeoutError, ValueError):
            reread_error = True

    raw_basis = str((moved or {}).get("verification_basis") or "unknown")
    basis = (
        raw_basis
        if raw_basis in {"value_readback", "display_change_only", "none"}
        else "unknown"
    )
    move_verified = (
        moved is not None
        and moved.get("verified") is True
        and basis in {"value_readback", "display_change_only"}
    )
    restore_command_verified = (
        restored is not None and restored.get("verified") is True
    )
    restore_readback_verified = False
    if reread is not None:
        try:
            reread_value = _normalised_value(reread)
        except ValueError:
            reread_value = math.nan
        reread_display = str(reread.get("display") or "")
        restore_readback_verified = (
            math.isfinite(reread_value)
            and abs(reread_value - original) <= 1e-6
            and (not original_display or reread_display == original_display)
        )

    if (
        not move_error
        and not restore_error
        and not reread_error
        and move_verified
        and restore_command_verified
        and restore_readback_verified
    ):
        outcome = "write-and-exact-restore-verified"
    elif not restore_readback_verified:
        outcome = "restore-not-confirmed"
    elif move_error or not move_verified:
        outcome = "write-not-verified"
    elif restore_error or reread_error:
        outcome = "validation-incomplete"
    else:
        outcome = "validation-incomplete"
    return WriteValidationEvidence(
        attempted=True,
        move_verified=move_verified,
        move_verification_basis=basis,
        restore_command_verified=restore_command_verified,
        restore_readback_verified=restore_readback_verified,
        outcome=outcome,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="postfader-plugin-report",
        description=(
            "Generate privacy-safe compatibility evidence for one mixer effect. "
            "Read-only unless --validate-write is explicitly used. Output is a "
            "community candidate for maintainer review, not maintained compatibility "
            "evidence."
        ),
    )
    parser.add_argument("--track", type=int, help="mixer track index (live mode)")
    parser.add_argument("--slot", type=int, help="effect slot index, 0-9 (live mode)")
    parser.add_argument(
        "--from-json",
        metavar="PATH",
        help="reduce a saved raw or MCP scan without contacting FL Studio",
    )
    parser.add_argument(
        "--max-indices",
        type=int,
        default=None,
        help="live read bound, 1-8192 (default: bridge maximum)",
    )
    parser.add_argument(
        "--plugin-version",
        default="unknown",
        help="contributor-asserted product version, or unknown",
    )
    parser.add_argument(
        "--plugin-origin",
        choices=("stock", "third-party", "unknown"),
        default="unknown",
    )
    parser.add_argument(
        "--plugin-format",
        choices=("native", "VST", "VST3", "AU", "unknown"),
        default="unknown",
        help="contributor-asserted format; FL's API does not report it",
    )
    parser.add_argument(
        "--fl-edition",
        choices=("Fruity", "Producer", "Signature", "All Plugins", "unknown"),
        default="unknown",
        help="installed FL Studio edition",
    )
    parser.add_argument(
        "--output", choices=("markdown", "json"), default="markdown"
    )
    parser.add_argument(
        "--validate-write",
        type=int,
        metavar="PARAMETER_INDEX",
        help="move and exactly restore one representative normalized control",
    )
    parser.add_argument(
        "--confirm-disposable-project",
        action="store_true",
        help="required with --validate-write; confirms a blank disposable project",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.from_json:
        if args.track is not None or args.slot is not None:
            parser.error("--from-json cannot be combined with --track or --slot")
        if args.validate_write is not None:
            parser.error("--validate-write requires live FL Studio, not --from-json")
        if args.max_indices is not None:
            parser.error("--max-indices applies only to a live scan, not --from-json")
    elif args.track is None or args.slot is None:
        parser.error("--track and --slot are required unless --from-json is used")
    if args.track == 0 and args.validate_write is not None:
        parser.error("--validate-write refuses the Master track")
    if args.slot is not None and not 0 <= args.slot <= 9:
        parser.error("--slot must be 0 through 9")
    if args.max_indices is not None and not 1 <= args.max_indices <= 8192:
        parser.error("--max-indices must be 1 through 8192")
    if args.validate_write is not None and args.validate_write < 0:
        parser.error("--validate-write index must be non-negative")
    if args.validate_write is not None and not args.confirm_disposable_project:
        parser.error(
            "--validate-write requires --confirm-disposable-project; it moves a control"
        )
    if args.confirm_disposable_project and args.validate_write is None:
        parser.error("--confirm-disposable-project is only valid with --validate-write")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    client: ReportClient | None = None
    fl_version = "unknown"
    try:
        if args.from_json:
            with Path(args.from_json).open(encoding="utf-8") as handle:
                scan: Any = json.load(handle)
        else:
            # The installed command is an explicitly supported IAC entry
            # point. Set the opt-in before importing bridge_client, which
            # freezes transport availability at import time. Merely importing
            # this reporting module does not change process state or touch
            # CoreMIDI.
            os.environ.setdefault("FL_BRIDGE_ENABLE_MIDI", "1")
            from .bridge_client import get_client
            from .readonly_inspector import (
                IncompatibleFLStudio,
                ReadOnlyGateway,
                ReadOnlyInspector,
            )

            client = get_client()
            inspector = ReadOnlyInspector(ReadOnlyGateway(client))
            connection = inspector.connection_info()
            if not connection.connected or not connection.compatible:
                raise IncompatibleFLStudio(
                    connection.error or connection.compatibility_reason
                )
            scan = inspector.scan_plugin_parameters(
                track_index=args.track,
                slot_index=args.slot,
                max_indices=args.max_indices,
            )
            fl_version = connection.fl_app_version or "unknown"

        profile = summarise(scan)
        write = WriteValidationEvidence()
        if args.validate_write is not None:
            if client is None or args.track is None or args.slot is None:
                raise RuntimeError(
                    "write validation requires a live FL Studio client and explicit "
                    "track/slot"
                )
            if not profile.complete:
                raise ValueError(
                    "write validation requires a complete, internally consistent read profile"
                )
            write = validate_representative_write(
                client,
                profile,
                track=args.track,
                slot=args.slot,
                parameter_index=args.validate_write,
            )
        report = build_public_report(
            profile,
            plugin_version=args.plugin_version,
            plugin_origin=args.plugin_origin,
            plugin_format=args.plugin_format,
            fl_studio_version=fl_version,
            fl_studio_edition=args.fl_edition,
            write_validation=write,
        )
        if args.output == "json":
            print(json.dumps(report.as_public_dict(), indent=2, sort_keys=True))
        else:
            print(render_public_markdown(report))

        if write.attempted and not write.successful:
            if write.restore_readback_verified is not True:
                print(
                    "RESTORE NOT CONFIRMED: inspect the disposable project in FL Studio "
                    "before closing it.",
                    file=sys.stderr,
                )
            else:
                print(
                    "Representative write was not verified; the exact restore was confirmed.",
                    file=sys.stderr,
                )
            return 2
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
