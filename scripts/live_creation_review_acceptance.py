#!/usr/bin/env python3
"""Plan or run the maintainer Creation Review acceptance workflow.

The public Creation Review API works from a completed Production Run and
caller-selected audio exports.  This script is the deliberately small
maintainer boundary around that API.  It is safe to invoke with no arguments:
the default is an offline plan and validation report, with no MCP import, FL
Studio contact, audio read, or filesystem scan.

``--live`` opts into one bounded sequence against an already prepared,
disposable project.  The sequence can be resumed at a named review step by
passing the Review Session ID (or a private checkpoint).  It only calls the
Creation Review tools; rendering, project-save, and UI automation are not
part of this workflow.  A revision apply is never inferred from a plan or a
feedback record: ``--authorize-apply`` and the three live safety confirmations
are required on the invocation that applies it.

Evidence is create-only and is accepted only below this checkout's
``.private`` directory.  The script does not inspect Git or attempt to commit
private artifacts.  The evidence report contains bounded metadata, hashes,
timings, and exact blockers; it does not retain audio bytes or raw tool
transcripts.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import math
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT))

ACCEPTANCE_KIND = "postfader_creation_review_acceptance"
SCHEMA_VERSION = 1
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
ASSET_KINDS = (
    "candidate_full_mix",
    "before_full_mix",
    "after_full_mix",
    "reference_full_mix",
    "instrumental_stem",
    "vocal_stem",
    "drum_stem",
    "bass_stem",
    "chord_stem",
    "lead_stem",
    "role_stem",
    "section_bounce",
)
STEPS = (
    "all",
    "start",
    "evaluate",
    "feedback",
    "plan",
    "apply",
    "compare",
    "approve",
    "reject",
    "delivery",
)
REVIEW_TOOLS = {
    "start": "postfader_review_start",
    "attach": "postfader_review_attach_assets",
    "evaluate": "postfader_review_evaluate",
    "get": "postfader_review_get",
    "feedback": "postfader_review_record_feedback",
    "plan": "postfader_review_plan_revision",
    "apply": "postfader_review_apply_revision",
    "compare": "postfader_review_compare",
    "export_handoff": "postfader_review_export_handoff",
    "delivery": "postfader_delivery_manifest",
    "delivery_export": "postfader_delivery_export_manifest",
}
FORBIDDEN_TOOL_FRAGMENTS = ("render", "save", "click")
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_ASSETS = 16
MAX_STEPS = 16
MAX_REVISION_OPERATIONS = 8
MAX_FEEDBACK_NOTES = 16


class AcceptanceConfigurationError(RuntimeError):
    """The requested acceptance workflow is incomplete or unsafe."""

    def __init__(self, message: str, *, blockers: tuple[dict[str, Any], ...] = ()):
        super().__init__(message)
        self.blockers = blockers


class EvidenceOutputError(RuntimeError):
    """A create-only evidence path is outside the private boundary."""


class AcceptanceStepError(AcceptanceConfigurationError):
    """One bounded step failed; dependent steps must not be replayed."""

    def __init__(
        self,
        tool: str,
        message: str,
        *,
        unknown_outcome: bool = False,
        blockers: tuple[dict[str, Any], ...] = (),
    ):
        super().__init__(message, blockers=blockers)
        self.tool = tool
        self.unknown_outcome = unknown_outcome


Caller = Callable[[str, dict[str, Any]], Awaitable[Any]]
Checkpoint = Callable[[Mapping[str, Any]], None]


def positive_finite_seconds(value: str) -> float:
    """Argparse converter for a finite, positive timeout."""

    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return result


def bounded_integer(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 0 <= result <= 4096:
        raise argparse.ArgumentTypeError("must be between 0 and 4096")
    return result


def bounded_positive_integer(value: str) -> int:
    result = bounded_integer(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments; absence of ``--live`` intentionally means plan."""

    parser = argparse.ArgumentParser(
        description=(
            "Plan or run bounded Creation Review acceptance. The default is "
            "offline plan-only validation; use --live for selected-bounce work."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan",
        dest="plan",
        action="store_true",
        help="offline plan and validation only (the default)",
    )
    mode.add_argument(
        "--live",
        dest="plan",
        action="store_false",
        help="execute the selected bounded workflow against the current MCP process",
    )
    parser.set_defaults(plan=True)
    parser.add_argument(
        "--step",
        "--resume-step",
        dest="step",
        choices=STEPS,
        default="all",
        help="step to run or resume (default: all requested steps)",
    )
    parser.add_argument(
        "--resume",
        dest="resume_step",
        choices=STEPS,
        help="alias for --resume-step, kept explicit for maintainer handoffs",
    )
    parser.add_argument(
        "--resume-from",
        metavar="PRIVATE_CHECKPOINT",
        help="read one prior private checkpoint to recover IDs; never scans a directory",
    )
    parser.add_argument(
        "--source-run-id",
        "--source-run",
        dest="source_run_id",
        help="completed Production Run ID (required for a new live session)",
    )
    parser.add_argument(
        "--review-session-id",
        "--session-id",
        dest="review_session_id",
        help="existing Review Session ID for a resumed step",
    )
    parser.add_argument(
        "--bounce",
        "--candidate-bounce",
        "--selected-bounce",
        dest="bounce",
        help="explicit caller-selected candidate/full-mix bounce",
    )
    parser.add_argument("--before-bounce", help="explicit before-revision full-mix bounce")
    parser.add_argument("--after-bounce", help="explicit after-revision full-mix bounce")
    parser.add_argument("--reference-bounce", help="optional explicit reference full mix")
    parser.add_argument(
        "--asset",
        action="append",
        default=[],
        metavar="KIND=PATH",
        help="optional selected stem/section asset; may be repeated (bounded)",
    )
    parser.add_argument("--asset-set-id", help="existing attached asset-set ID")
    parser.add_argument("--evaluation-id", help="evaluation ID for a resumed plan/apply")
    parser.add_argument("--revision-plan-id", help="existing or requested Revision Plan ID")
    parser.add_argument(
        "--revision-json",
        metavar="JSON_OR_PRIVATE_FILE",
        help="closed revision spec as JSON or one bounded private JSON file",
    )
    parser.add_argument(
        "--revision-objective",
        default="Run one bounded acceptance revision while preserving accepted elements.",
        help="objective used when --revision-json is omitted",
    )
    parser.add_argument("--role-id", default="main_lead")
    parser.add_argument("--section-id")
    parser.add_argument("--channel-index", type=bounded_integer, default=0)
    parser.add_argument("--pattern-number", type=bounded_positive_integer, default=1)
    parser.add_argument(
        "--feedback",
        action="append",
        default=[],
        metavar="NOTE",
        help="explicit producer note; may be repeated and is bounded",
    )
    parser.add_argument("--feedback-json", metavar="JSON_OR_PRIVATE_FILE")
    parser.add_argument("--feedback-id", default="acceptance-feedback")
    parser.add_argument("--approval-note")
    approval = parser.add_mutually_exclusive_group()
    approval.add_argument("--approve", action="store_true")
    approval.add_argument("--reject", action="store_true")
    parser.add_argument(
        "--apply",
        "--apply-revision",
        dest="apply",
        action="store_true",
        help="apply the closed revision in --step all (requires explicit authorization)",
    )
    parser.add_argument(
        "--authorize-apply",
        "--confirm-apply",
        dest="authorize_apply",
        action="store_true",
        help="explicitly authorize this invocation's one revision apply",
    )
    parser.add_argument(
        "--export-delivery",
        action="store_true",
        help="also create JSON/Markdown delivery files below .private",
    )
    parser.add_argument(
        "--delivery-output-directory",
        help="private directory for optional delivery files (defaults beside evidence)",
    )
    parser.add_argument(
        "--midi-port",
        help="exact configured virtual MIDI endpoint for a live revision apply",
    )
    parser.add_argument("--confirm-user-present", action="store_true")
    parser.add_argument("--confirm-disposable-project", action="store_true")
    parser.add_argument("--confirm-safe-to-edit", action="store_true")
    parser.add_argument(
        "--per-step-timeout-seconds",
        type=positive_finite_seconds,
        default=180.0,
        help="hard deadline for each MCP step (default: 180)",
    )
    parser.add_argument(
        "--overall-timeout-seconds",
        type=positive_finite_seconds,
        default=600.0,
        help="hard deadline for this workflow (default: 600)",
    )
    parser.add_argument("--tempo-bpm", type=positive_finite_seconds)
    parser.add_argument("--time-signature-numerator", type=bounded_positive_integer, default=4)
    parser.add_argument("--time-signature-denominator", type=bounded_positive_integer, default=4)
    parser.add_argument(
        "--output",
        help="new JSON evidence file; live output is required and must be below .private",
    )
    return parser.parse_args(argv)


def _normalise(value: Any, *, depth: int = 0) -> Any:
    """Convert typed responses to bounded JSON-compatible values."""

    if depth > 12:
        return "<depth-limit>"
    if hasattr(value, "model_dump"):
        try:
            return _normalise(value.model_dump(mode="json", exclude_none=False), depth=depth + 1)
        except TypeError:
            return _normalise(value.model_dump(), depth=depth + 1)
    if isinstance(value, Mapping):
        return {
            str(key): _normalise(item, depth=depth + 1)
            for key, item in list(value.items())[:256]
        }
    if isinstance(value, (tuple, list)):
        return [_normalise(item, depth=depth + 1) for item in list(value)[:256]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _body(value: Any) -> dict[str, Any]:
    value = _normalise(value)
    if not isinstance(value, dict):
        return {}
    # ``tool_payload`` normally removes this wrapper, but retaining this
    # compatibility branch makes fake MCP callers and older MCP clients safe.
    nested = value.get("result")
    if isinstance(nested, dict) and len(value) <= 3:
        return nested
    return value


def _session_body(value: Any) -> dict[str, Any]:
    """Return a ReviewSession from either a session or lookup envelope."""

    body = _body(value)
    nested = body.get("session")
    return nested if isinstance(nested, dict) else body


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _normalise(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or SOURCE_ID_PATTERN.fullmatch(value) is None:
        raise AcceptanceConfigurationError(
            f"{label} must match {SOURCE_ID_PATTERN.pattern!r}; received {value!r}"
        )
    return value


def _private_root() -> Path:
    return (ROOT / ".private").resolve()


def _private_output(path: str | os.PathLike[str] | None) -> Path | None:
    """Resolve one output and prove it remains below ``ROOT/.private``.

    No directory traversal is performed.  Existing parent components are
    checked one at a time to reject symlink escapes; missing parents are
    created only by :func:`_reserve_output` after this check succeeds.
    """

    if path is None:
        return None
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    private_anchor = ROOT / ".private"
    if private_anchor.is_symlink():
        raise EvidenceOutputError("repository .private must not be a symlink")
    private = _private_root()
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(private)
    except ValueError as exc:
        raise EvidenceOutputError(
            "Creation Review acceptance evidence must be stored below the repository .private directory"
        ) from exc
    if not relative.parts:
        raise EvidenceOutputError("evidence output must name a file below .private")
    if ".." in candidate.parts or "\x00" in os.fspath(candidate):
        raise EvidenceOutputError("evidence output must not contain traversal or null-byte components")
    # A private evidence file may be nested for a maintainer's own grouping,
    # but keep the accepted path shape bounded and never walk the tree.
    if len(relative.parts) > 4:
        raise EvidenceOutputError("evidence output path is deeper than the four-component private bound")
    parent = candidate.parent
    while parent != private and private in parent.parents:
        if parent.is_symlink():
            raise EvidenceOutputError("evidence output parent must not be a symlink")
        parent = parent.parent
    if resolved == private or candidate.exists() and candidate.is_dir():
        raise EvidenceOutputError("evidence output must be a file, not a directory")
    return resolved


def _private_directory(path: str | os.PathLike[str] | None) -> Path:
    """Resolve an optional delivery directory below ``.private``."""

    if path is None:
        return _private_root()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    if (ROOT / ".private").is_symlink():
        raise EvidenceOutputError("repository .private must not be a symlink")
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(_private_root())
    except ValueError as exc:
        raise EvidenceOutputError(
            "delivery artifacts must be written below the repository .private directory"
        ) from exc
    if ".." in candidate.parts or len(relative.parts) > 4:
        raise EvidenceOutputError("delivery output directory has an unsafe bounded path")
    if candidate.exists() and not candidate.is_dir():
        raise EvidenceOutputError("delivery output path is not a directory")
    return resolved


def _load_json_input(value: str | None, *, label: str) -> Any:
    """Parse inline JSON or one bounded, caller-selected JSON file."""

    if value is None:
        return None
    text = value.strip()
    if text.startswith("{") or text.startswith("["):
        if len(text.encode("utf-8")) > MAX_JSON_BYTES:
            raise AcceptanceConfigurationError(f"{label} exceeds the {MAX_JSON_BYTES}-byte bound")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AcceptanceConfigurationError(f"{label} is invalid JSON: {exc}") from exc
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        raise AcceptanceConfigurationError(f"{label} JSON file does not exist: {path}")
    try:
        size = path.stat().st_size
        if size > MAX_JSON_BYTES:
            raise AcceptanceConfigurationError(
                f"{label} JSON file is {size} bytes, over the {MAX_JSON_BYTES}-byte bound"
            )
        raw = path.read_bytes()
    except OSError as exc:
        raise AcceptanceConfigurationError(f"could not read {label} JSON file: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceConfigurationError(f"{label} is invalid UTF-8 JSON: {exc}") from exc


def _parse_asset(value: str) -> tuple[str, str]:
    kind, separator, path = value.partition("=")
    if not separator or kind not in ASSET_KINDS or not path.strip():
        raise AcceptanceConfigurationError(
            f"asset must use KIND=PATH with KIND in {', '.join(ASSET_KINDS)}"
        )
    return kind, path


def _selected_assets(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Build explicit selected asset inputs without opening any audio file."""

    values: list[tuple[str, str, str]] = []
    if args.bounce:
        values.append(("candidate_full_mix", args.bounce, "candidate-bounce"))
    if args.before_bounce:
        values.append(("before_full_mix", args.before_bounce, "before-bounce"))
    if args.after_bounce:
        values.append(("after_full_mix", args.after_bounce, "after-bounce"))
    if args.reference_bounce:
        values.append(("reference_full_mix", args.reference_bounce, "reference-bounce"))
    for index, raw in enumerate(args.asset):
        kind, path = _parse_asset(raw)
        values.append((kind, path, f"asset-{index + 1}"))
    if len(values) > MAX_ASSETS:
        raise AcceptanceConfigurationError(f"at most {MAX_ASSETS} selected assets are supported")
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for kind, path, default_id in values:
        if "\x00" in path or ".." in Path(path).parts:
            raise AcceptanceConfigurationError(f"selected {kind} path contains an unsafe component")
        if not Path(path).is_absolute():
            raise AcceptanceConfigurationError(f"selected {kind} path must be absolute: {path!r}")
        suffix = Path(path).suffix.casefold()
        if suffix not in {".wav", ".wave", ".aif", ".aiff", ".flac", ".ogg", ".oga", ".mp3"}:
            raise AcceptanceConfigurationError(
                f"selected {kind} path has unsupported audio extension {suffix or '<none>'!r}"
            )
        asset_id = default_id
        if asset_id in seen_ids:
            asset_id = f"{default_id}-{len(result) + 1}"
        # Keep plan mode filesystem-free: duplicate detection is lexical here;
        # the review service performs the authoritative symlink/path checks
        # when it validates the selected audio file.
        canonical = os.path.normcase(os.path.normpath(path))
        if canonical in seen_paths and kind in {"before_full_mix", "after_full_mix"}:
            raise AcceptanceConfigurationError("before and after bounces must be distinct selected files")
        seen_ids.add(asset_id)
        seen_paths.add(canonical)
        row: dict[str, Any] = {
            "path": path,
            "asset_kind": kind,
            "asset_id": asset_id,
            "display_label": Path(path).name or default_id,
        }
        result.append(row)
    return result


def _asset_metadata_for_report(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep evidence useful without retaining selected absolute paths."""

    return [
        {
            "asset_id": item["asset_id"],
            "asset_kind": item["asset_kind"],
            "display_label": item["display_label"],
            "path_sha256": hashlib.sha256(item["path"].encode("utf-8")).hexdigest(),
        }
        for item in assets
    ]


def _feedback_payload(args: argparse.Namespace, session_id: str, *, verdict: str = "neutral") -> dict[str, Any]:
    raw = _load_json_input(args.feedback_json, label="feedback")
    if raw is None:
        notes = tuple(args.feedback)
        if len(notes) > MAX_FEEDBACK_NOTES:
            raise AcceptanceConfigurationError(
                f"at most {MAX_FEEDBACK_NOTES} feedback notes are supported"
            )
        if not notes and verdict == "neutral":
            raise AcceptanceConfigurationError(
                "feedback step requires --feedback or --feedback-json; silence is not feedback"
            )
        payload: dict[str, Any] = {
            "feedback_id": args.feedback_id,
            "review_session_id": session_id,
            "source": "user_explicit",
            "overall_verdict": verdict,
            "overall_note": "\n".join(notes) if notes else args.approval_note,
            "approval_level": "overall" if verdict == "approved" else "rejected" if verdict == "rejected" else "none",
            "persist": False,
        }
    elif isinstance(raw, Mapping):
        payload = dict(raw)
        payload.setdefault("feedback_id", args.feedback_id)
        payload.setdefault("source", "user_explicit")
        payload["review_session_id"] = session_id
        if verdict != "neutral":
            payload["overall_verdict"] = verdict
            payload["approval_level"] = "overall" if verdict == "approved" else "rejected"
    else:
        raise AcceptanceConfigurationError("feedback JSON must be an object")
    return payload


def _session_id(value: Any) -> str | None:
    body = _body(value)
    candidate = body.get("review_session_id")
    return candidate if isinstance(candidate, str) and SOURCE_ID_PATTERN.fullmatch(candidate) else None


def _asset_set_id(value: Any) -> str | None:
    body = _session_body(value)
    asset_sets = body.get("asset_sets")
    if isinstance(asset_sets, list) and asset_sets:
        row = asset_sets[-1]
        if isinstance(row, dict) and isinstance(row.get("asset_set_id"), str):
            return row["asset_set_id"]
    candidate = body.get("asset_set_id")
    return candidate if isinstance(candidate, str) else None


def _latest_id(value: Any, collection: str, key: str) -> str | None:
    body = _session_body(value)
    rows = body.get(collection)
    if isinstance(rows, list) and rows:
        candidate = rows[-1]
        if isinstance(candidate, dict) and isinstance(candidate.get(key), str):
            return candidate[key]
    return None


def _response_blockers(value: Any, *, step: str, tool: str) -> list[dict[str, Any]]:
    """Extract only explicit response blockers with their source context."""

    body = _body(value)
    rows = body.get("blockers")
    if not isinstance(rows, list):
        rows = []
    status = body.get("status")
    if status in {"blocked", "failed", "fail", "refused", "unknown"} and not rows:
        rows = [f"{tool} returned status {status!r}"]
    blockers: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:64], start=1):
        if isinstance(row, Mapping):
            code = row.get("code") or row.get("kind") or f"{step}_blocker_{index}"
            message = row.get("message") or row.get("reason") or row.get("error") or str(row)
            detail = {str(key): _normalise(item) for key, item in list(row.items())[:16]}
        else:
            code = f"{step}_blocker_{index}"
            message = str(row)
            detail = {}
        blockers.append(
            {
                "code": str(code),
                "message": str(message),
                "step": step,
                "tool": tool,
                "detail": detail,
            }
        )
    return blockers


def _error_blocker(*, code: str, message: str, step: str, tool: str | None = None) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "step": step,
        "tool": tool,
        "detail": {},
    }


def _revision_spec(
    args: argparse.Namespace,
    *,
    session_id: str,
    source_run_id: str,
    evaluation_id: str,
    feedback_ids: tuple[str, ...] = (),
    authorized: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Build one closed typed revision request and operation list.

    A caller-provided JSON object wins.  Otherwise the default is a single
    low-risk Piano Roll transform, bounded to the selected channel/pattern.
    It is intentionally only a plan until the apply step receives explicit
    authorization.
    """

    raw = _load_json_input(args.revision_json, label="revision")
    if raw is not None:
        if not isinstance(raw, Mapping):
            raise AcceptanceConfigurationError("revision JSON must be an object")
        data = dict(raw)
        request = data.get("request", data.get("revision_request"))
        operations = data.get("operations", data.get("ordered_operations"))
        if not isinstance(request, Mapping) or not isinstance(operations, list):
            raise AcceptanceConfigurationError("revision JSON needs object request and list operations")
        if len(operations) > MAX_REVISION_OPERATIONS:
            raise AcceptanceConfigurationError(
                f"at most {MAX_REVISION_OPERATIONS} revision operations are supported per acceptance pass"
            )
        request = dict(request)
        request["source_run_id"] = source_run_id
        request["source_evaluation_id"] = evaluation_id
        request["authorized_to_modify"] = authorized
        revision_plan_id = str(data.get("revision_plan_id") or args.revision_plan_id or "acceptance-revision-plan")
        envelope = {
            "revision_plan_id": revision_plan_id,
            "targeted_findings": list(data.get("targeted_findings", [])),
            "expected_objectives": list(data.get("expected_objectives", [])),
            "subjective_objectives": list(data.get("subjective_objectives", [])),
            "manual_actions": list(data.get("manual_actions", [])),
        }
        return request, list(operations), envelope

    request = {
        "source_evaluation_id": evaluation_id,
        "source_run_id": source_run_id,
        "requested_objective": args.revision_objective,
        "section_scope": [args.section_id] if args.section_id else [],
        "role_scope": [args.role_id] if args.role_id else [],
        "allowed_changes": ["transform_generated_sequence"],
        "preserved_elements": ["accepted sound assignments and unrelated roles"],
        "accepted_element_locks": [],
        "maximum_changed_roles": 1,
        "maximum_changed_sections": 1 if args.section_id else 0,
        "maximum_operations": 1,
        "maximum_risk_level": "low",
        "revision_strength": "subtle",
        "authorized_to_modify": authorized,
        "regenerate_versus_transform": "prefer_transform",
        "processing_policy": "explicit_only",
        "manual_handoff_allowance": True,
    }
    operation: dict[str, Any] = {
        "operation_id": "acceptance-revision-operation",
        "operation": "transform_generated_sequence",
        "finding_ids": [],
        "feedback_ids": list(feedback_ids[:1]),
        "section_id": args.section_id,
        "role_id": args.role_id,
        "preserves": ["accepted source identity"],
        "subjective_objective": args.revision_objective,
        "confidence": 1.0,
        "fallback_behavior": "stop if the selected Piano Roll target is not verifiable",
        "verification_method": "existing_verified_writer_readback",
        "parameters": {
            "channel_index": args.channel_index,
            "pattern_number": args.pattern_number,
            "transform": {
                "operation": "transpose",
                "scope": "all",
                "semitones": 1,
            },
        },
    }
    # The operation must trace to either a real finding or explicit feedback.
    # When neither exists, a stable synthetic feedback reference keeps the
    # offline plan structurally closed; the live planner will reject it if the
    # persisted session does not contain matching evidence.
    if not feedback_ids:
        operation["feedback_ids"] = [args.feedback_id]
    envelope = {
        "revision_plan_id": args.revision_plan_id or "acceptance-revision-plan",
        "targeted_findings": [],
        "expected_objectives": [],
        "subjective_objectives": [args.revision_objective],
        "manual_actions": [
            "Export the revised full mix manually with matching settings before comparison."
        ],
    }
    return request, [operation], envelope


def _offline_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Return a deterministic, fully inspectable plan without MCP imports."""

    source_run_id = args.source_run_id or "source-run-placeholder"
    session_id = args.review_session_id or "review-session-placeholder"
    evaluation_id = args.evaluation_id or "evaluation-placeholder"
    assets = _selected_assets(args)
    feedback_requested = bool(args.feedback or args.feedback_json)
    requested_apply = args.apply or args.step == "apply"
    requested_approval = args.approve or args.reject or args.step in {"approve", "reject"}
    revision_request, operations, revision_envelope = _revision_spec(
        args,
        session_id=session_id,
        source_run_id=source_run_id,
        evaluation_id=evaluation_id,
        feedback_ids=(args.feedback_id,) if feedback_requested else (),
        authorized=False,
    )
    planned_steps: list[str] = []
    if args.step in {"all", "start"}:
        planned_steps.append("start")
    if assets:
        planned_steps.append("attach")
    if args.step in {"all", "evaluate"}:
        planned_steps.append("evaluate")
    if feedback_requested or args.step == "feedback":
        planned_steps.append("feedback")
    if args.revision_json or args.step in {"all", "plan", "apply"} or args.apply:
        planned_steps.append("plan")
    if requested_apply:
        planned_steps.append("apply")
    if args.before_bounce and args.after_bounce:
        planned_steps.append("compare")
    if requested_approval:
        planned_steps.append("approve" if args.approve or args.step == "approve" else "reject")
    if args.step in {"all", "delivery"}:
        planned_steps.append("delivery")
    # Preserve order while bounding the printed plan.
    planned_steps = list(dict.fromkeys(planned_steps))[:MAX_STEPS]
    blockers: list[dict[str, Any]] = []
    explicit_workflow_request = bool(
        args.source_run_id
        or args.review_session_id
        or args.bounce
        or args.before_bounce
        or args.after_bounce
        or args.feedback
        or args.feedback_json
        or args.revision_json
        or args.apply
        or args.step != "all"
    )
    if explicit_workflow_request and args.step != "start" and not args.review_session_id and not args.source_run_id:
        blockers.append(
            _error_blocker(
                code="source_run_id_required",
                message="a completed --source-run-id is required to start or resume live review work",
                step=args.step,
            )
        )
    if explicit_workflow_request and args.step in {"evaluate", "all"} and not assets and not args.review_session_id:
        blockers.append(
            _error_blocker(
                code="selected_bounce_required",
                message="evaluation needs an explicit selected bounce or an existing attached Review Session asset set",
                step="evaluate",
            )
        )
    if requested_apply and not args.authorize_apply:
        blockers.append(
            _error_blocker(
                code="apply_authorization_required",
                message="apply is planned but not authorized; pass --authorize-apply on the live apply invocation",
                step="apply",
                tool=REVIEW_TOOLS["apply"],
            )
        )
    if args.before_bounce and not args.after_bounce or args.after_bounce and not args.before_bounce:
        blockers.append(
            _error_blocker(
                code="before_after_pair_required",
                message="comparison requires both --before-bounce and --after-bounce",
                step="compare",
            )
        )
    if args.export_delivery and args.step not in {"all", "delivery"}:
        blockers.append(
            _error_blocker(
                code="delivery_export_step_required",
                message="--export-delivery is only valid with --step delivery or --step all",
                step=args.step,
            )
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": ACCEPTANCE_KIND,
        "mode": "plan_only",
        "overall": "blocked" if blockers else "pass",
        "phase": "offline_validation",
        "contact_started": False,
        "physical_io_performed": False,
        "fl_project_io_performed": False,
        "project_saved": False,
        "render_attempted": False,
        "save_attempted": False,
        "click_attempted": False,
        "private_artifact_commit_attempted": False,
        "private_artifact_committed": False,
        "automatic_replay_attempted": False,
        "automatic_replay": False,
        "rollback_attempted": False,
        "target_assessment": "not_claimed",
        "acceptance_targets": {
            "under_five_minutes_from_armed_ready": "not_claimed",
            "under_ten_minutes_with_one_manual_action": "not_claimed",
            "one_task_scoped_authorization": "not_claimed",
            "zero_surprise_setup_blockers": "not_claimed",
            "manual_playlist_handoffs_at_most_one": "not_claimed",
        },
        "source_run_id": source_run_id,
        "review_session_id": session_id,
        "selected_assets": _asset_metadata_for_report(assets),
        "planned_steps": planned_steps,
        "planned_tools": [
            REVIEW_TOOLS[name]
            for name in (
                "start",
                "attach",
                "evaluate",
                "feedback",
                "plan",
                "apply",
                "compare",
                "export_handoff",
                "delivery",
            )
            if name in planned_steps or (name == "export_handoff" and "delivery" in planned_steps)
        ],
        "request": {
            "source_run_id": source_run_id,
            "brief": "Maintainer acceptance of selected Creation Review bounce evidence.",
            "interaction_policy": "analyze_only",
            "authorized_to_modify": False,
            "persist_session": True,
            "persist_asset_paths": False,
        },
        "revision_request": revision_request,
        "revision_operations": operations,
        "revision_envelope": revision_envelope,
        "checks": {
            "offline_only": {"status": "passed", "mcp_imported": False, "fl_contacted": False},
            "no_render_save_click": {
                "status": "passed",
                "render_attempted": False,
                "save_attempted": False,
                "click_attempted": False,
            },
            "private_evidence": {"status": "not_requested", "required_root": os.fspath(_private_root())},
            "no_private_artifact_commit": {"status": "passed", "commit_attempted": False},
            "explicit_apply_authorization": {
                "status": "required" if requested_apply else "not_requested",
                "authorized": False,
            },
        },
        "blockers": blockers,
        "blocker_messages": [item["message"] for item in blockers],
        "timings": {"overall_elapsed_seconds": 0.0, "steps": []},
        "requested_midi_port": args.midi_port,
        "per_step_timeout_seconds": args.per_step_timeout_seconds,
        "overall_timeout_seconds": args.overall_timeout_seconds,
    }


def _load_resume_context(args: argparse.Namespace) -> dict[str, Any]:
    if args.resume_from is None:
        return {}
    path = _private_output(args.resume_from)
    assert path is not None
    if not path.is_file():
        raise AcceptanceConfigurationError(f"resume checkpoint does not exist: {path}")
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise AcceptanceConfigurationError("resume checkpoint exceeds the bounded JSON size")
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceConfigurationError(f"could not read resume checkpoint: {exc}") from exc
    if not isinstance(value, Mapping) or value.get("kind") != ACCEPTANCE_KIND:
        raise AcceptanceConfigurationError("resume checkpoint is not Creation Review acceptance evidence")
    return dict(value)


def _apply_resume_context(args: argparse.Namespace, previous: Mapping[str, Any]) -> None:
    """Fill only missing IDs from a private report; CLI values always win."""

    if args.review_session_id is None and isinstance(previous.get("review_session_id"), str):
        args.review_session_id = previous["review_session_id"]
    if args.source_run_id is None and isinstance(previous.get("source_run_id"), str):
        args.source_run_id = previous["source_run_id"]
    if args.evaluation_id is None:
        candidate = previous.get("evaluation_id")
        if isinstance(candidate, str):
            args.evaluation_id = candidate
    if args.revision_plan_id is None:
        candidate = previous.get("revision_plan_id")
        if isinstance(candidate, str):
            args.revision_plan_id = candidate


async def _call(name: str, arguments: dict[str, Any], timeout: float) -> Any:
    """Call one public MCP tool only after live mode and transport setup."""

    from fl_studio_mcp import mcp_server
    from fl_studio_mcp.acceptance import tool_payload

    result = await asyncio.wait_for(mcp_server.mcp.call_tool(name, arguments), timeout=timeout)
    return tool_payload(result)


def _review_request(args: argparse.Namespace, source_run_id: str) -> dict[str, Any]:
    return {
        "source_run_id": source_run_id,
        "brief": "Maintainer acceptance of a caller-selected Creation Review bounce.",
        "interaction_policy": "analyze_and_plan",
        "requested_focus": ["technical export evidence", "bounded revision traceability"],
        "section_scope": [],
        "role_scope": [],
        "reference_goals": [],
        "max_revision_passes": 1,
        "max_revision_operations": 1,
        "authorized_to_modify": False,
        "persist_session": True,
        "persist_asset_paths": False,
        "evaluation_policy": {
            "mode": "balanced",
            "compare_reference": bool(args.reference_bounce),
            "include_stems": bool(args.asset),
            "include_generated_content": True,
            "max_findings": 32,
        },
    }


def _augment_assets(assets: list[dict[str, Any]], source_run_id: str) -> list[dict[str, Any]]:
    del source_run_id  # The review service supplies source_run_id from the session.
    result: list[dict[str, Any]] = []
    for item in assets:
        row = dict(item)
        result.append(row)
    return result


def _find_asset_id(value: Any, kind: str) -> str | None:
    body = _session_body(value)
    rows = body.get("assets")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("asset_kind") == kind and isinstance(row.get("asset_id"), str):
                return row["asset_id"]
    for collection in ("asset_sets",):
        rows = body.get(collection)
        if isinstance(rows, list):
            for row in reversed(rows):
                if not isinstance(row, dict):
                    continue
                nested = row.get("assets")
                if isinstance(nested, list):
                    for asset in nested:
                        if isinstance(asset, dict) and asset.get("asset_kind") == kind and isinstance(asset.get("asset_id"), str):
                            return asset["asset_id"]
    return None


async def async_main(
    args: argparse.Namespace,
    *,
    checkpoint: Checkpoint | None = None,
    caller: Caller | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Execute one bounded live sequence, or return the offline plan."""

    previous = _load_resume_context(args) if args.resume_from else {}
    _apply_resume_context(args, previous)
    if args.resume_step is not None:
        args.step = args.resume_step
    if args.plan:
        return _offline_plan(args)

    # A named step is a resume operation and must identify the existing
    # Review Session.  ``all`` is the only mode that can open a new session.
    if args.step == "start" and not args.source_run_id:
        raise AcceptanceConfigurationError(
            "--step start requires --source-run-id for the completed Production Run"
        )
    if args.step not in {"all", "start"} and args.review_session_id is None:
        raise AcceptanceConfigurationError(
            f"--step {args.step} requires --review-session-id to resume an existing session"
        )
    source_run_id = _identifier(
        args.source_run_id or "source-run-resumed",
        label="--source-run-id",
    )
    if args.step == "all" and args.review_session_id is None and not args.source_run_id:
        raise AcceptanceConfigurationError(
            "a new live review needs --source-run-id for the completed Production Run"
        )
    if args.step == "all" and args.review_session_id is None and not (
        args.bounce or args.before_bounce
    ):
        raise AcceptanceConfigurationError(
            "a new live review needs one explicit selected bounce (--bounce or --before-bounce)"
        )
    if not args.confirm_user_present or not args.confirm_disposable_project:
        raise AcceptanceConfigurationError(
            "live Creation Review acceptance requires --confirm-user-present and --confirm-disposable-project"
        )
    requested_apply = args.apply or args.step == "apply"
    if requested_apply and not args.authorize_apply:
        raise AcceptanceConfigurationError(
            "revision apply was requested without --authorize-apply",
            blockers=(
                _error_blocker(
                    code="apply_authorization_required",
                    message="explicit authorization is required immediately before apply",
                    step="apply",
                    tool=REVIEW_TOOLS["apply"],
                ),
            ),
        )
    if requested_apply and not args.confirm_safe_to_edit:
        raise AcceptanceConfigurationError(
            "revision apply requires --confirm-safe-to-edit for the disposable project"
        )

    assets = _selected_assets(args)
    if args.before_bounce and not args.after_bounce or args.after_bounce and not args.before_bounce:
        raise AcceptanceConfigurationError("comparison requires both --before-bounce and --after-bounce")
    if args.export_delivery and args.step not in {"all", "delivery"}:
        raise AcceptanceConfigurationError("--export-delivery requires --step delivery or --step all")

    from fl_studio_mcp.evidence import configure_acceptance_transport

    configure_acceptance_transport(args.midi_port, live=True)
    started = clock()
    steps: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": ACCEPTANCE_KIND,
        "mode": "live",
        "overall": "pass",
        "phase": "contact_started",
        "contact_started": True,
        "physical_io_performed": bool(assets),
        "fl_project_io_performed": False,
        "project_saved": False,
        "render_attempted": False,
        "save_attempted": False,
        "click_attempted": False,
        "private_artifact_commit_attempted": False,
        "private_artifact_committed": False,
        "automatic_replay_attempted": False,
        "automatic_replay": False,
        "rollback_attempted": False,
        "target_assessment": "not_claimed",
        "acceptance_targets": {
            "under_five_minutes_from_armed_ready": "not_claimed",
            "under_ten_minutes_with_one_manual_action": "not_claimed",
            "one_task_scoped_authorization": "not_claimed",
            "zero_surprise_setup_blockers": "not_claimed",
            "manual_playlist_handoffs_at_most_one": "not_claimed",
        },
        "source_run_id": source_run_id,
        "review_session_id": args.review_session_id,
        "selected_assets": _asset_metadata_for_report(assets),
        "steps": steps,
        "blockers": blockers,
        "blocker_messages": [item["message"] for item in blockers],
        "timings": {"steps": []},
        "checks": {
            "private_evidence": {
                "status": "passed",
                "required_root": os.fspath(_private_root()),
            },
            "no_render_save_click": {
                "status": "passed",
                "render_attempted": False,
                "save_attempted": False,
                "click_attempted": False,
                "forbidden_tool_calls": [],
            },
            "no_private_artifact_commit": {"status": "passed", "commit_attempted": False},
            "explicit_apply_authorization": {
                "status": "passed" if requested_apply and args.authorize_apply else "not_requested",
                "authorized": bool(requested_apply and args.authorize_apply),
                "authorization_count": 0,
            },
        },
        "requested_midi_port": args.midi_port,
        "per_step_timeout_seconds": args.per_step_timeout_seconds,
        "overall_timeout_seconds": args.overall_timeout_seconds,
    }
    caller_target: Callable[[str, dict[str, Any]], Any] = caller or (
        lambda tool, arguments: _call(tool, arguments, args.per_step_timeout_seconds)
    )

    def elapsed() -> float:
        return round(max(0.0, clock() - started), 6)

    def save_checkpoint(entry: Mapping[str, Any]) -> None:
        if checkpoint is None:
            return
        checkpoint(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": ACCEPTANCE_KIND,
                "overall": report["overall"],
                "phase": report["phase"],
                "contact_started": True,
                "project_saved": False,
                "render_attempted": False,
                "save_attempted": False,
                "click_attempted": False,
                "private_artifact_commit_attempted": False,
                "private_artifact_committed": False,
                "automatic_replay_attempted": False,
                "automatic_replay": False,
                "rollback_attempted": False,
                "target_assessment": "not_claimed",
                "source_run_id": report["source_run_id"],
                "review_session_id": report.get("review_session_id"),
                "last_checkpoint": _normalise(entry),
            }
        )

    def record_timing(row: Mapping[str, Any]) -> None:
        """Retain one compact timing row even when a step fails."""

        report["timings"]["steps"].append(
            {
                "step": row.get("step"),
                "tool": row.get("tool"),
                "status": row.get("status"),
                "duration_seconds": row.get("duration_seconds"),
            }
        )

    async def invoke(step: str, tool: str, arguments: dict[str, Any], *, mutating: bool = False) -> Any:
        if any(fragment in tool.casefold() for fragment in FORBIDDEN_TOOL_FRAGMENTS):
            forbidden = _error_blocker(
                code="forbidden_tool_call",
                message=f"workflow refuses forbidden tool {tool!r}; render/save/UI automation are outside this harness",
                step=step,
                tool=tool,
            )
            blockers.append(forbidden)
            report["checks"]["no_render_save_click"]["forbidden_tool_calls"].append(tool)
            raise AcceptanceStepError(tool, forbidden["message"], blockers=(forbidden,))
        now = elapsed()
        remaining = args.overall_timeout_seconds - now
        if remaining <= 0:
            blocker = _error_blocker(
                code="overall_timeout",
                message="overall Creation Review acceptance deadline expired before this step",
                step=step,
                tool=tool,
            )
            blockers.append(blocker)
            raise AcceptanceStepError(tool, blocker["message"], blockers=(blocker,), unknown_outcome=mutating)
        row: dict[str, Any] = {
            "step": step,
            "tool": tool,
            "status": "in_flight",
            "mutating": mutating,
            "arguments_sha256": _digest(arguments),
            "automatic_replay_attempted": False,
            "started_elapsed_seconds": now,
        }
        if mutating:
            # The review apply delegates to the Production Run writer.  Mark
            # project contact before the call so an unknown result cannot be
            # mistaken for a read-only outcome.
            report["fl_project_io_performed"] = True
        steps.append(row)
        report["phase"] = f"{step}_in_flight"
        save_checkpoint(row)
        try:
            result = caller_target(tool, arguments)
            if inspect.isawaitable(result):
                value = await asyncio.wait_for(
                    result,
                    timeout=min(args.per_step_timeout_seconds, remaining),
                )
            else:
                value = result
        except asyncio.TimeoutError as exc:
            row.update(
                {
                    "status": "timed_out",
                    "outcome": "unknown" if mutating else "not_reached",
                    "error": f"step exceeded {min(args.per_step_timeout_seconds, remaining):g} seconds",
                    "finished_elapsed_seconds": elapsed(),
                    "duration_seconds": round(max(0.0, elapsed() - now), 6),
                }
            )
            blocker = _error_blocker(
                code="step_timeout",
                message=row["error"],
                step=step,
                tool=tool,
            )
            blockers.append(blocker)
            report["phase"] = "unknown_outcome" if mutating else "blocked"
            record_timing(row)
            save_checkpoint(row)
            raise AcceptanceStepError(tool, row["error"], unknown_outcome=mutating, blockers=(blocker,)) from exc
        except Exception as exc:
            row.update(
                {
                    "status": "failed",
                    "outcome": "unknown" if mutating else "not_reached",
                    "error": f"{type(exc).__name__}: {exc}",
                    "finished_elapsed_seconds": elapsed(),
                    "duration_seconds": round(max(0.0, elapsed() - now), 6),
                }
            )
            blocker = _error_blocker(
                code="step_failed_unknown" if mutating else "step_failed",
                message=row["error"],
                step=step,
                tool=tool,
            )
            blockers.append(blocker)
            report["phase"] = "unknown_outcome" if mutating else "blocked"
            record_timing(row)
            save_checkpoint(row)
            raise AcceptanceStepError(tool, row["error"], unknown_outcome=mutating, blockers=(blocker,)) from exc
        response_blockers = _response_blockers(value, step=step, tool=tool)
        row.update(
            {
                "status": "blocked" if response_blockers else "passed",
                "response_sha256": _digest(value),
                "finished_elapsed_seconds": elapsed(),
                "duration_seconds": round(max(0.0, elapsed() - now), 6),
            }
        )
        if response_blockers:
            blockers.extend(response_blockers)
            row["blockers"] = response_blockers
            report["phase"] = "unknown_outcome" if mutating and _body(value).get("status") == "unknown" else "blocked"
            record_timing(row)
            save_checkpoint(row)
            raise AcceptanceStepError(
                tool,
                "; ".join(item["message"] for item in response_blockers),
                unknown_outcome=mutating and _body(value).get("status") == "unknown",
                blockers=tuple(response_blockers),
            )
        report["phase"] = "workflow_execution"
        record_timing(row)
        save_checkpoint(row)
        return value

    def selected_step(name: str) -> bool:
        if args.step == name:
            return True
        if args.step != "all":
            return False
        optional = {
            "attach": bool(assets),
            "feedback": bool(args.feedback or args.feedback_json),
            "plan": bool(args.revision_json or args.revision_plan_id or args.apply),
            "apply": requested_apply,
            "compare": bool(args.before_bounce and args.after_bounce),
            "approve": bool(args.approve),
            "reject": bool(args.reject),
            "delivery": True,
            "start": args.review_session_id is None,
            "evaluate": True,
        }
        return optional.get(name, False)

    session_value: Any = None
    evaluation_value: Any = None
    plan_value: Any = None
    try:
        if args.review_session_id is not None:
            _identifier(args.review_session_id, label="--review-session-id")
            session_value = await invoke("resume", REVIEW_TOOLS["get"], {"review_session_id": args.review_session_id})
            lookup_body = _body(session_value)
            if lookup_body.get("found") is False:
                blocker = _error_blocker(
                    code="review_session_not_found",
                    message=f"Review Session {args.review_session_id!r} was not found in the current or persisted review registry",
                    step="resume",
                    tool=REVIEW_TOOLS["get"],
                )
                blockers.append(blocker)
                raise AcceptanceStepError(
                    REVIEW_TOOLS["get"],
                    blocker["message"],
                    blockers=(blocker,),
                )
            report["review_session"] = _normalise(session_value)
            resumed_source = _session_body(session_value).get("source_run_id")
            if isinstance(resumed_source, str):
                if args.source_run_id and args.source_run_id != resumed_source:
                    blocker = _error_blocker(
                        code="source_run_mismatch",
                        message=f"provided source run {args.source_run_id!r} does not match Review Session source run {resumed_source!r}",
                        step="resume",
                        tool=REVIEW_TOOLS["get"],
                    )
                    blockers.append(blocker)
                    raise AcceptanceStepError(
                        REVIEW_TOOLS["get"],
                        blocker["message"],
                        blockers=(blocker,),
                    )
                source_run_id = _identifier(resumed_source, label="Review Session source_run_id")
                report["source_run_id"] = source_run_id
        if selected_step("start") and args.review_session_id is None:
            session_value = await invoke("start", REVIEW_TOOLS["start"], {"request": _review_request(args, source_run_id)})
            resolved_session = _session_id(session_value)
            if resolved_session is None:
                blocker = _error_blocker(
                    code="review_session_id_missing",
                    message="postfader_review_start returned no valid review_session_id",
                    step="start",
                    tool=REVIEW_TOOLS["start"],
                )
                blockers.append(blocker)
                raise AcceptanceStepError(REVIEW_TOOLS["start"], blocker["message"], blockers=(blocker,))
            args.review_session_id = resolved_session
            report["review_session_id"] = resolved_session
            report["review_session"] = _normalise(session_value)
        if args.review_session_id is None:
            raise AcceptanceConfigurationError("workflow did not establish a Review Session ID")
        session_id = args.review_session_id
        if assets:
            attach_args = {
                "review_session_id": session_id,
                "assets": _augment_assets(assets, source_run_id),
            }
            session_value = await invoke("attach", REVIEW_TOOLS["attach"], attach_args)
            report["review_session"] = _normalise(session_value)
            report["asset_set_id"] = args.asset_set_id or _asset_set_id(session_value)
        if selected_step("evaluate"):
            evaluate_args: dict[str, Any] = {"review_session_id": session_id}
            if args.asset_set_id or report.get("asset_set_id"):
                evaluate_args["asset_set_id"] = args.asset_set_id or report["asset_set_id"]
            if args.tempo_bpm is not None:
                evaluate_args["tempo_bpm"] = args.tempo_bpm
            evaluate_args["time_signature_numerator"] = args.time_signature_numerator
            evaluate_args["time_signature_denominator"] = args.time_signature_denominator
            evaluation_value = await invoke("evaluate", REVIEW_TOOLS["evaluate"], evaluate_args)
            report["evaluation"] = _normalise(evaluation_value)
            evaluation_id = _body(evaluation_value).get("evaluation_id")
            if isinstance(evaluation_id, str):
                report["evaluation_id"] = evaluation_id
        if selected_step("feedback"):
            feedback_value = await invoke(
                "feedback",
                REVIEW_TOOLS["feedback"],
                _feedback_payload(args, session_id),
            )
            report["feedback"] = _normalise(feedback_value)
            report["feedback_id"] = args.feedback_id
        reusing_recorded_plan = bool(
            requested_apply and args.revision_plan_id
        )
        if (selected_step("plan") and not reusing_recorded_plan) or (
            requested_apply and not reusing_recorded_plan
        ):
            evaluation_id = args.evaluation_id or report.get("evaluation_id")
            if not isinstance(evaluation_id, str):
                evaluation_id = _latest_id(report.get("review_session", {}), "evaluations", "evaluation_id")
            if not isinstance(evaluation_id, str):
                blocker = _error_blocker(
                    code="evaluation_id_required",
                    message="revision planning requires an evaluation_id from the selected Review Session",
                    step="plan",
                    tool=REVIEW_TOOLS["plan"],
                )
                blockers.append(blocker)
                raise AcceptanceStepError(REVIEW_TOOLS["plan"], blocker["message"], blockers=(blocker,))
            feedback_ids = (args.feedback_id,) if report.get("feedback_id") else ()
            revision_request, operations, envelope = _revision_spec(
                args,
                session_id=session_id,
                source_run_id=source_run_id,
                evaluation_id=evaluation_id,
                feedback_ids=feedback_ids,
                authorized=False,
            )
            plan_args = {
                "review_session_id": session_id,
                "request": revision_request,
                "operations": operations,
                **envelope,
            }
            plan_value = await invoke("plan", REVIEW_TOOLS["plan"], plan_args)
            report["revision_plan"] = _normalise(plan_value)
            report["revision_plan_id"] = _body(plan_value).get("revision_plan_id") or envelope["revision_plan_id"]
        if requested_apply:
            report["checks"]["explicit_apply_authorization"]["authorization_count"] = 1
            evaluation_id = args.evaluation_id or report.get("evaluation_id") or _latest_id(report.get("review_session", {}), "evaluations", "evaluation_id")
            if not isinstance(evaluation_id, str):
                plans = _body(report.get("review_session", {})).get("revision_plans")
                if isinstance(plans, list) and plans:
                    source_evaluation = plans[-1]
                    if isinstance(source_evaluation, Mapping):
                        candidate = source_evaluation.get("source_evaluation_id")
                        if isinstance(candidate, str):
                            evaluation_id = candidate
            if not isinstance(evaluation_id, str):
                raise AcceptanceConfigurationError("apply needs the source evaluation ID")
            revision_request, _operations, _envelope = _revision_spec(
                args,
                session_id=session_id,
                source_run_id=source_run_id,
                evaluation_id=evaluation_id,
                feedback_ids=((args.feedback_id,) if report.get("feedback_id") else ()),
                authorized=True,
            )
            plan_id = args.revision_plan_id or report.get("revision_plan_id")
            if not isinstance(plan_id, str):
                plan_id = _latest_id(report.get("review_session", {}), "revision_plans", "revision_plan_id")
            if not isinstance(plan_id, str):
                raise AcceptanceConfigurationError("apply needs a recorded revision_plan_id")
            apply_args = {
                "review_session_id": session_id,
                "revision_plan_id": plan_id,
                "request": revision_request,
                "authorized_to_modify": True,
            }
            pass_value = await invoke("apply", REVIEW_TOOLS["apply"], apply_args, mutating=True)
            report["revision_pass"] = _normalise(pass_value)
            pass_body = _body(pass_value)
            report["revision_pass_id"] = pass_body.get("revision_pass_id")
            observed_authorizations = pass_body.get("authorization_count")
            if observed_authorizations is not None:
                report["checks"]["explicit_apply_authorization"]["observed_authorization_count"] = observed_authorizations
                if observed_authorizations != 1:
                    blocker = _error_blocker(
                        code="authorization_count_unverified",
                        message=f"revision pass reported authorization_count={observed_authorizations!r}; expected exactly one",
                        step="apply",
                        tool=REVIEW_TOOLS["apply"],
                    )
                    blockers.append(blocker)
                    raise AcceptanceStepError(
                        REVIEW_TOOLS["apply"],
                        blocker["message"],
                        blockers=(blocker,),
                    )
            if pass_body.get("status") not in {"awaiting_rebounce", "completed"}:
                blocker = _error_blocker(
                    code="revision_pass_not_complete",
                    message=f"revision apply returned non-complete status {pass_body.get('status')!r}",
                    step="apply",
                    tool=REVIEW_TOOLS["apply"],
                )
                blockers.append(blocker)
                raise AcceptanceStepError(REVIEW_TOOLS["apply"], blocker["message"], blockers=(blocker,))
        if selected_step("compare") or (args.step == "all" and args.before_bounce and args.after_bounce):
            before_id = _find_asset_id(report.get("review_session", {}), "before_full_mix") or "before-bounce"
            after_id = _find_asset_id(report.get("review_session", {}), "after_full_mix") or "after-bounce"
            compare_args = {
                "review_session_id": session_id,
                "before_asset_id": before_id,
                "after_asset_id": after_id,
                "user_approval_state": "not_requested",
            }
            if args.revision_plan_id or report.get("revision_plan_id"):
                compare_args["revision_plan_id"] = args.revision_plan_id or report["revision_plan_id"]
            comparison_value = await invoke("compare", REVIEW_TOOLS["compare"], compare_args)
            report["comparison"] = _normalise(comparison_value)
        if selected_step("approve") or (args.step == "all" and args.approve):
            approved = await invoke(
                "approve",
                REVIEW_TOOLS["feedback"],
                _feedback_payload(args, session_id, verdict="approved"),
            )
            report["approval"] = "approved"
            report["approval_response"] = _normalise(approved)
        elif selected_step("reject") or (args.step == "all" and args.reject):
            rejected = await invoke(
                "reject",
                REVIEW_TOOLS["feedback"],
                _feedback_payload(args, session_id, verdict="rejected"),
            )
            report["approval"] = "rejected"
            report["approval_response"] = _normalise(rejected)
        if selected_step("delivery"):
            handoff = await invoke(
                "export_handoff",
                REVIEW_TOOLS["export_handoff"],
                {"review_session_id": session_id},
            )
            report["export_handoff"] = _normalise(handoff)
            delivery = await invoke("delivery", REVIEW_TOOLS["delivery"], {"review_session_id": session_id})
            report["delivery_manifest"] = _normalise(delivery)
            if args.export_delivery:
                output_directory = _private_directory(args.delivery_output_directory)
                output_directory.mkdir(parents=True, exist_ok=True)
                exported = await invoke(
                    "delivery_export",
                    REVIEW_TOOLS["delivery_export"],
                    {
                        "review_session_id": session_id,
                        "formats": ["json", "markdown"],
                        "output_directory": os.fspath(output_directory),
                    },
                )
                report["delivery_export"] = _normalise(exported)
        report["phase"] = "complete"
    except AcceptanceStepError as exc:
        for blocker in exc.blockers:
            if blocker not in blockers:
                blockers.append(blocker)
        report["overall"] = "fail"
        report["phase"] = "unknown_outcome" if exc.unknown_outcome else "blocked"
        report["failed_tool"] = exc.tool
        report["error"] = str(exc)
        report["automatic_replay_attempted"] = False
    except AcceptanceConfigurationError as exc:
        blockers.extend(item for item in exc.blockers if item not in blockers)
        if not exc.blockers:
            blockers.append(
                _error_blocker(
                    code="configuration_error",
                    message=str(exc),
                    step=args.step,
                )
            )
        report["overall"] = "fail"
        report["phase"] = "blocked"
        report["error"] = str(exc)
    finally:
        report["blockers"] = blockers
        report["blocker_messages"] = [item["message"] for item in blockers]
        report["observed_elapsed_seconds"] = elapsed()
        report["timings"]["overall_elapsed_seconds"] = report["observed_elapsed_seconds"]
        report["checks"]["no_render_save_click"]["status"] = (
            "passed" if not report["checks"]["no_render_save_click"]["forbidden_tool_calls"] else "failed"
        )
        save_checkpoint({
            "step": "complete",
            "status": report["overall"],
            "blockers": blockers,
            "observed_elapsed_seconds": report["observed_elapsed_seconds"],
        })
    return report


def _failure(
    *,
    phase: str,
    error: BaseException | str,
    contact_started: bool,
    blockers: tuple[dict[str, Any], ...] = (),
    mode: str | None = None,
) -> dict[str, Any]:
    resolved_blockers = list(blockers)
    if not resolved_blockers:
        resolved_blockers.append(
            _error_blocker(code="acceptance_error", message=str(error), step=phase)
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": ACCEPTANCE_KIND,
        "mode": mode or ("live" if contact_started else "plan_only"),
        "overall": "fail" if contact_started else "refused",
        "phase": phase,
        "contact_started": contact_started,
        "physical_io_performed": False,
        "fl_project_io_performed": False,
        "project_saved": False,
        "render_attempted": False,
        "save_attempted": False,
        "click_attempted": False,
        "private_artifact_commit_attempted": False,
        "private_artifact_committed": False,
        "automatic_replay_attempted": False,
        "automatic_replay": False,
        "rollback_attempted": False,
        "target_assessment": "not_claimed",
        "acceptance_targets": {
            "under_five_minutes_from_armed_ready": "not_claimed",
            "under_ten_minutes_with_one_manual_action": "not_claimed",
            "one_task_scoped_authorization": "not_claimed",
            "zero_surprise_setup_blockers": "not_claimed",
            "manual_playlist_handoffs_at_most_one": "not_claimed",
        },
        "error": str(error),
        "blockers": resolved_blockers,
        "blocker_messages": [item["message"] for item in resolved_blockers],
        "timings": {"overall_elapsed_seconds": 0.0, "steps": []},
        "checks": {
            "no_render_save_click": {
                "status": "passed",
                "render_attempted": False,
                "save_attempted": False,
                "click_attempted": False,
            },
            "no_private_artifact_commit": {"status": "passed", "commit_attempted": False},
        },
    }


def _reserve_output(path: str | None, *, required: bool) -> tuple[Any, Path | None]:
    if path is None:
        if required:
            raise EvidenceOutputError("live Creation Review acceptance requires --output below .private")
        return None, None
    resolved = _private_output(path)
    assert resolved is not None
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EvidenceOutputError(f"could not create private evidence parent: {exc}") from exc
    from fl_studio_mcp.evidence import reserve_evidence_output

    return reserve_evidence_output(resolved, required=True), resolved


def _finish(destination: Any, value: dict[str, Any], *, contact_started: bool) -> int:
    output_failures: list[dict[str, Any]] = []
    if destination is not None:
        checks = value.setdefault("checks", {})
        checks["private_evidence"] = {
            "status": "passed",
            "required_root": os.fspath(_private_root()),
            "path": os.fspath(destination.path),
            "create_only": True,
        }
        try:
            destination.write(value)
        except Exception as exc:
            output_failures.append({"stage": "final_evidence_write", "reason": str(exc)})
        try:
            destination.close()
        except Exception as exc:
            output_failures.append({"stage": "final_evidence_close", "reason": str(exc)})
    if output_failures:
        failure = _failure(
            phase="final_evidence_output",
            error="one or more evidence output operations failed",
            contact_started=contact_started,
        )
        failure["evidence_output_failures"] = output_failures
        print(json.dumps(failure, indent=2, sort_keys=True), file=sys.stderr)
        return 1 if contact_started else 2
    if destination is not None:
        print(destination.path)
    else:
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
    if value.get("overall") == "fail":
        return 1
    if value.get("overall") in {"refused", "blocked"}:
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    previous: dict[str, Any] = {}
    try:
        if args.resume_from:
            previous = _load_resume_context(args)
            _apply_resume_context(args, previous)
        if args.resume_step is not None:
            args.step = args.resume_step
        if args.plan:
            # Plan mode intentionally has no evidence side effect unless the
            # maintainer explicitly asks for a private output snapshot.
            if args.output is None:
                try:
                    value = asyncio.run(async_main(args))
                except AcceptanceConfigurationError as exc:
                    value = _failure(
                        phase="offline_validation",
                        error=exc,
                        contact_started=False,
                        blockers=exc.blockers,
                        mode="plan_only",
                    )
                    print(json.dumps(value, indent=2, sort_keys=True), file=sys.stderr)
                    return 2
                return _finish(None, value, contact_started=False)
            destination, _path = _reserve_output(args.output, required=False)
            try:
                value = asyncio.run(async_main(args, checkpoint=destination.write if destination is not None else None))
            except AcceptanceConfigurationError as exc:
                value = _failure(
                    phase="offline_validation",
                    error=exc,
                    contact_started=False,
                    blockers=exc.blockers,
                    mode="plan_only",
                )
            return _finish(destination, value, contact_started=False)
        destination, _path = _reserve_output(args.output, required=True)
    except (AcceptanceConfigurationError, EvidenceOutputError) as exc:
        value = _failure(
            phase="argument_or_output_validation",
            error=exc,
            contact_started=False,
            blockers=getattr(exc, "blockers", ()),
            mode="live" if not args.plan else "plan_only",
        )
        print(json.dumps(value, indent=2, sort_keys=True), file=sys.stderr)
        return 2

    contact_started = False
    try:
        # ``async_main`` configures the transport immediately before its lazy
        # MCP import.  The checkpoint below marks the boundary for a resumed
        # private evidence file without claiming project mutation.
        if destination is not None:
            destination.write(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": ACCEPTANCE_KIND,
                    "mode": "live",
                    "overall": "started",
                    "phase": "contact_started",
                    "contact_started": True,
                    "project_saved": False,
                    "render_attempted": False,
                    "save_attempted": False,
                    "click_attempted": False,
                    "private_artifact_commit_attempted": False,
                    "private_artifact_committed": False,
                    "automatic_replay_attempted": False,
                    "automatic_replay": False,
                    "rollback_attempted": False,
                    "target_assessment": "not_claimed",
                    "source_run_id": args.source_run_id,
                    "review_session_id": args.review_session_id,
                }
            )
            contact_started = True
        value = asyncio.run(
            async_main(
                args,
                checkpoint=destination.write if destination is not None else None,
            )
        )
        contact_started = contact_started or bool(value.get("contact_started"))
    except (AcceptanceConfigurationError, OSError, ValueError, TimeoutError) as exc:
        value = _failure(
            phase="workflow_validation_or_execution",
            error=exc,
            contact_started=contact_started,
            blockers=getattr(exc, "blockers", ()),
            mode="live",
        )
    except BaseException as exc:
        value = _failure(
            phase="interrupted_execution",
            error=f"{type(exc).__name__}: {exc}",
            contact_started=contact_started,
            mode="live",
        )
    return _finish(destination, value, contact_started=contact_started)


if __name__ == "__main__":
    raise SystemExit(main())
