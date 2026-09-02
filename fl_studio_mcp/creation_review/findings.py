"""Evaluation finding extraction and evidence-aware prioritisation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .assets import _model, _value


FINDINGS_ANALYZER_VERSION = "creation-review-findings-2"
MAX_FINDINGS = 64
MAX_TOP_PRIORITIES = 8

FINDING_CATEGORIES = frozenset(
    {
        "technical_export",
        "clipping_or_headroom",
        "dynamics",
        "tonal_balance",
        "low_end",
        "stereo",
        "masking",
        "section_contrast",
        "arrangement_development",
        "sound_selection",
        "composition",
        "processing",
        "delivery",
        "user_feedback",
        "insufficient_evidence",
    }
)

EVIDENCE_SOURCES = frozenset(
    {
        "decoded_audio_measurement",
        "synchronized_stem_measurement",
        "reference_comparison",
        "production_run_receipt",
        "sound_palette_metadata",
        "explicit_user_feedback",
        "connected_ai_interpretation",
    }
)


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=False)
    return {
        key: getattr(value, key)
        for key in (
            "source", "text", "message", "feedback", "note", "overall_note",
            "bounded_note", "overall_verdict",
            "section_id", "role_id", "category", "severity", "confidence",
            "actionability", "measurement", "requested_target", "explanation",
            "goal", "origin", "state", "classification", "domain", "evidence",
            "required_additional_evidence",
        )
        if hasattr(value, key)
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _finding_id(category: str, evidence_source: str, section_id: str | None, role_id: str | None, measurement: Any, explanation: str) -> str:
    material = {
        "category": category,
        "evidence_source": evidence_source,
        "section_id": section_id,
        "role_id": role_id,
        "measurement": measurement,
        "explanation": explanation,
    }
    return "finding-" + _digest(material)[:24]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _nested(payload: Any, *keys: str) -> Any:
    current = payload
    for key in keys:
        if isinstance(current, Mapping):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
        if current is None:
            return None
    return current


def _severity(value: str | None, *, fallback: str = "medium") -> str:
    allowed = {"critical", "high", "medium", "low", "info"}
    return value if value in allowed else fallback


def _confidence(value: Any, fallback: str = "medium") -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if 0.0 <= numeric <= 1.0:
            return numeric
    if isinstance(value, Mapping):
        value = value.get("level", fallback)
    level_value = getattr(value, "level", None)
    if level_value is not None:
        value = level_value
    level = str(value) if str(value) in {"high", "medium", "low"} else fallback
    return {"high": 0.9, "medium": 0.65, "low": 0.35}[level]


def _finding(
    *,
    category: str,
    evidence_source: str,
    severity: str,
    confidence: Any,
    section_id: str | None = None,
    role_id: str | None = None,
    asset_id: str | None = None,
    measurement: Mapping[str, Any] | None = None,
    expected_target: Any = None,
    explanation: str,
    actionability: str = "actionable",
    candidate_techniques: Sequence[str] = (),
    required_additional_evidence: Sequence[str] = (),
    limitations: Sequence[str] = (),
) -> Any:
    category = category if category in FINDING_CATEGORIES else "insufficient_evidence"
    evidence_source = evidence_source if evidence_source in EVIDENCE_SOURCES else "connected_ai_interpretation"
    allowed_actionability = {
        "actionable",
        "informational",
        "requires_user_judgment",
        "insufficient_evidence",
        "not_actionable",
    }
    if actionability not in allowed_actionability:
        actionability = "actionable"
    material_measurement = dict(measurement or {})
    fid = _finding_id(category, evidence_source, section_id, role_id, material_measurement, explanation)
    payload = {
        "finding_id": fid,
        "category": category,
        "evidence_source": evidence_source,
        "severity": _severity(severity),
        "confidence": _confidence(confidence),
        "section_id": section_id,
        "role_id": role_id,
        "asset_id": asset_id,
        "measurement": material_measurement,
        "expected_or_requested_target": expected_target,
        "explanation": explanation,
        "actionability": actionability,
        "candidate_techniques": tuple(str(value) for value in candidate_techniques),
        "required_additional_evidence": tuple(str(value) for value in required_additional_evidence),
        "limitations": tuple(str(value) for value in limitations),
    }
    return _model("EvaluationFinding", payload)


def _feedback_entries(feedback: Any) -> list[dict[str, Any]]:
    """Flatten only structured feedback fields; natural language stays external."""

    if feedback is None:
        return []
    if isinstance(feedback, (str, bytes)):
        values: list[Any] = [feedback]
    elif isinstance(feedback, Sequence) and not isinstance(feedback, Mapping):
        values = list(feedback)
    else:
        values = [feedback]
    entries: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, str):
            text = value.strip()
            if text:
                entries.append({"text": text, "source": "user_explicit"})
            continue
        item = dict(_mapping(value))
        # CreationFeedback can carry grouped lists. Keep each group scoped so
        # a complaint about one role is not broadcast to every role.
        grouped_keys = (
            ("section_feedback", "section_id"),
            ("role_feedback", "role_id"),
            ("palette_feedback", "role_id"),
            ("arrangement_feedback", None),
            ("processing_feedback", None),
        )
        expanded = False
        for key, scope_key in grouped_keys:
            grouped = item.get(key)
            if grouped is None:
                continue
            expanded = True
            values_group = grouped if isinstance(grouped, Sequence) and not isinstance(grouped, (str, bytes, Mapping)) else [grouped]
            for entry in values_group:
                detail: dict[str, Any] = (
                    dict(_mapping(entry))
                    if not isinstance(entry, str)
                    else {"text": entry}
                )
                if scope_key and item.get(scope_key) is not None:
                    detail.setdefault(scope_key, item.get(scope_key))
                detail.setdefault("source", item.get("source", "user_explicit"))
                if not any(detail.get(key) for key in ("text", "message", "feedback", "note", "overall_note", "bounded_note")) and detail.get("verdict"):
                    detail["text"] = f"Producer verdict: {detail['verdict']}"
                    if detail["verdict"] in {"approved", "accepted", "neutral"}:
                        detail["actionability"] = "informational"
                entries.append(detail)
        if not expanded:
            entries.append(item)
        overall_note = item.get("overall_note", item.get("bounded_note"))
        if overall_note:
            entries.append({
                "text": str(overall_note),
                "source": item.get("source", "user_explicit"),
                "category": "user_feedback",
            })
        elif item.get("overall_verdict"):
            # A verdict without a note remains explicit context. Keep it
            # informational instead of interpreting "approved" as a sonic
            # complaint or silently dropping the user's decision.
            entries.append({
                "text": f"Overall producer verdict: {item['overall_verdict']}",
                "source": item.get("source", "user_explicit"),
                "category": "user_feedback",
                "actionability": "informational",
            })
    return entries


def _feedback_finding(entry: Mapping[str, Any]) -> Any | None:
    text = str(
        entry.get(
            "text",
            entry.get(
                "message",
                entry.get(
                    "feedback",
                    entry.get(
                        "note",
                        entry.get("overall_note", entry.get("bounded_note", "")),
                    ),
                ),
            ),
        )
    ).strip()
    if not text:
        return None
    source_raw = str(entry.get("source", "user_explicit"))
    source = "explicit_user_feedback" if source_raw in {"user_explicit", "user", "explicit_user_feedback"} else "connected_ai_interpretation"
    category = str(entry.get("category", "user_feedback"))
    role_id = entry.get("role_id")
    section_id = entry.get("section_id")
    # Natural-language interpretation belongs to the connected AI.  This
    # boundary records only explicit structured category/role fields and never
    # guesses a role from words such as "bass" or "lead" in an overall note.
    severity = _severity(str(entry.get("severity", "high" if source == "explicit_user_feedback" else "medium")))
    actionability = entry.get("actionability", "actionable")
    if actionability == "actionable" and entry.get("verdict") in {"approved", "accepted", "neutral"}:
        actionability = "informational"
    return _finding(
        category=category,
        evidence_source=source,
        severity=severity,
        confidence=_confidence(entry.get("confidence", "high" if source == "explicit_user_feedback" else "medium")),
        section_id=str(section_id) if section_id is not None else None,
        role_id=str(role_id) if role_id is not None else None,
        measurement=entry.get("measurement") if isinstance(entry.get("measurement"), Mapping) else {"feedback": text},
        expected_target=entry.get("expected_target", entry.get("requested_target")),
        explanation=text,
        actionability=str(actionability),
        candidate_techniques=entry.get("candidate_techniques", ()),
        required_additional_evidence=entry.get("required_additional_evidence", ()),
        limitations=entry.get("limitations", ()),
    )


def build_evaluation_findings(
    *,
    global_measurements: Mapping[str, Any] | Any | None = None,
    section_measurements: Mapping[str, Any] | Sequence[Any] = (),
    stem_measurements: Mapping[str, Any] | Sequence[Any] = (),
    masking_analysis: Any | None = None,
    reference_comparisons: Any | None = None,
    contrast_analysis: Any | None = None,
    goal_evaluations: Sequence[Any] | None = None,
    user_feedback: Any | None = None,
    brief: str | None = None,
    max_findings: int = MAX_FINDINGS,
) -> tuple[Any, ...]:
    """Create bounded findings and rank direct feedback before proxies."""

    if max_findings < 1:
        raise ValueError("max_findings must be positive")
    max_findings = min(int(max_findings), MAX_FINDINGS)
    findings: list[tuple[int, int, Any]] = []
    sequence = 0

    # Priority rank 0 is explicit user feedback, ahead of every automatic
    # measurement even when the latter is technically severe.
    for entry in _feedback_entries(user_feedback):
        finding = _feedback_finding(entry)
        if finding is not None:
            findings.append((0, sequence, finding))
            sequence += 1

    global_map = _mapping(global_measurements or {})
    loudness = _nested(global_map, "loudness") or {}
    spectrum = _nested(global_map, "spectrum") or {}
    dynamics = _nested(global_map, "dynamics") or {}
    stereo = _nested(global_map, "stereo") or {}
    asset_id = _value(global_measurements, "asset_id")

    # Keep each original/requested goal visible with its evidence boundary.
    # These are informational records: a proxy or a technical measurement is
    # not silently converted into an artistic pass/fail judgment.
    for goal in tuple(goal_evaluations or ())[:MAX_FINDINGS]:
        goal_map = _mapping(goal)
        state = str(_value(goal_map, "state", _value(goal_map, "classification", "")))
        if state not in {
            "technically_evaluable",
            "proxy_evaluable",
            "requires_user_judgment",
            "not_evaluable_from_supplied_assets",
        }:
            continue
        domain = str(_value(goal_map, "domain", "requested_goal"))
        category = {
            "technical_audio": "technical_export",
            "arrangement_development": "arrangement_development",
            "processing": "processing",
            "composition": "composition",
            "sound_selection": "sound_selection",
            "audible_quality": "insufficient_evidence",
            "reference_comparison": "insufficient_evidence",
        }.get(domain, "insufficient_evidence")
        evidence_values = _value(goal_map, "evidence", ()) or ()
        evidence_text = " ".join(str(value) for value in evidence_values)
        evidence_source = (
            "reference_comparison" if "reference" in evidence_text.casefold()
            else "production_run_receipt" if "processing" in evidence_text.casefold()
            else "production_run_receipt" if "persisted" in evidence_text.casefold()
            else "decoded_audio_measurement" if "audio" in evidence_text.casefold()
            else "connected_ai_interpretation"
        )
        actionability = {
            "technically_evaluable": "informational",
            "proxy_evaluable": "informational",
            "requires_user_judgment": "requires_user_judgment",
            "not_evaluable_from_supplied_assets": "insufficient_evidence",
        }[state]
        confidence = {
            "technically_evaluable": "high",
            "proxy_evaluable": "medium",
            "requires_user_judgment": "high",
            "not_evaluable_from_supplied_assets": "low",
        }[state]
        explanation = str(
            _value(goal_map, "rationale", "The requested goal was classified against supplied evidence.")
        )[:4096]
        if state == "requires_user_judgment":
            explanation += " Producer listening judgment is required; no automatic verdict is asserted."
        elif state == "not_evaluable_from_supplied_assets":
            explanation += " No automatic verdict is asserted from missing evidence."
        findings.append((8, sequence, _finding(
            category=category,
            evidence_source=evidence_source,
            severity="info" if state in {"technically_evaluable", "proxy_evaluable"} else "low",
            confidence=confidence,
            measurement={
                "goal_state": state,
                "origin": _value(goal_map, "origin"),
                "evidence": evidence_values,
            },
            expected_target=_value(goal_map, "goal", _value(goal_map, "requested_target")),
            explanation=explanation,
            actionability=actionability,
            required_additional_evidence=_value(goal_map, "required_additional_evidence", ()) or (),
            limitations=("Goal classification describes evidence availability, not artistic approval.",),
        )))
        sequence += 1
    # Critical technical findings are automatic but remain ahead of softer
    # proxies.  Values are facts; candidate techniques are hints for the
    # connected AI and are not mutations.
    clipped = _number(_value(loudness, "clipped_samples"))
    true_peak = _number(_value(loudness, "true_peak_dbtp", _value(loudness, "true_peak_db")))
    if (clipped is not None and clipped > 0) or (true_peak is not None and true_peak >= -0.1):
        findings.append((1, sequence, _finding(
            category="clipping_or_headroom",
            evidence_source="decoded_audio_measurement",
            severity="critical",
            confidence="high",
            asset_id=str(asset_id) if asset_id else None,
            measurement={"clipped_samples": clipped, "true_peak_dbtp": true_peak},
            expected_target="no clipped samples and true peak below full scale",
            explanation="The supplied full mix reaches or exceeds full scale; headroom/clipping is technically measurable.",
            candidate_techniques=("reduce_master_gain", "inspect_limiter_ceiling"),
        )))
        sequence += 1
    dc = _number(_value(loudness, "dc_offset"))
    if dc is not None and abs(dc) > 0.01:
        findings.append((2, sequence, _finding(
            category="technical_export",
            evidence_source="decoded_audio_measurement",
            severity="high",
            confidence="high",
            asset_id=str(asset_id) if asset_id else None,
            measurement={"dc_offset": dc},
            expected_target="DC offset near zero",
            explanation="The decoded audio has a measurable DC offset.",
            candidate_techniques=("remove_dc_offset",),
        )))
        sequence += 1

    crest = _number(_value(loudness, "crest_factor_db"))
    spread = _number(_value(dynamics, "dynamic_spread_db"))
    if crest is not None and crest < 4:
        findings.append((3, sequence, _finding(
            category="dynamics",
            evidence_source="decoded_audio_measurement",
            severity="medium",
            confidence="high",
            asset_id=str(asset_id) if asset_id else None,
            measurement={"crest_factor_db": crest},
            expected_target="retain useful transient/body contrast",
            explanation="The measured crest factor is low; this is a compression proxy, not an artistic verdict.",
            candidate_techniques=("reduce_compression", "inspect_limiter"),
            limitations=("A full-mix metric cannot identify which role caused the result.",),
        )))
        sequence += 1
    if spread is not None and spread < 2 and _number(_value(loudness, "rms_db")) is not None:
        findings.append((3, sequence, _finding(
            category="dynamics",
            evidence_source="decoded_audio_measurement",
            severity="low",
            confidence="medium",
            asset_id=str(asset_id) if asset_id else None,
            measurement={"dynamic_spread_db": spread},
            expected_target="preserve section-level movement",
            explanation="The analyzed windows show little level spread; section context is needed before acting.",
            candidate_techniques=("compare_section_levels",),
        )))
        sequence += 1

    bands = _value(spectrum, "bands", {}) or {}
    low_share = sum((_number(_value(bands.get(name, {}), "energy_share")) or 0.0) for name in ("sub", "low")) if isinstance(bands, Mapping) else None
    if low_share is not None and low_share > 0.55:
        findings.append((4, sequence, _finding(
            category="low_end",
            evidence_source="decoded_audio_measurement",
            severity="medium",
            confidence="high",
            asset_id=str(asset_id) if asset_id else None,
            measurement={"sub_plus_low_energy_share": round(low_share, 5)},
            expected_target="directional low-end balance",
            explanation="The full mix contains a high share of sub/low spectral energy; role attribution requires synchronized stems.",
            candidate_techniques=("inspect_bass_sub_ownership", "request_bass_and_drum_stems"),
            required_additional_evidence=("synchronized bass and drum stems",),
        )))
        sequence += 1
    centroid = _number(_value(spectrum, "spectral_centroid_hz"))
    low_mid = _number(_value(bands.get("low_mid", {}), "energy_share")) if isinstance(bands, Mapping) else None
    if (low_mid is not None and low_mid > 0.4) or (centroid is not None and 0 < centroid < 500):
        findings.append((4, sequence, _finding(
            category="tonal_balance",
            evidence_source="decoded_audio_measurement",
            severity="medium",
            confidence="medium",
            asset_id=str(asset_id) if asset_id else None,
            measurement={"low_mid_energy_share": low_mid, "spectral_centroid_hz": centroid},
            expected_target="directional broad spectral balance",
            explanation="The full mix has concentrated low-mid energy; this is a broad tonal proxy and does not identify a channel.",
            candidate_techniques=("inspect_low_mid_roles", "request_role_stems_if_attribution_is_needed"),
        )))
        sequence += 1
    corr = _number(_value(stereo, "correlation"))
    if corr is not None and corr < 0.3:
        findings.append((4, sequence, _finding(
            category="stereo",
            evidence_source="decoded_audio_measurement",
            severity="high",
            confidence="high",
            asset_id=str(asset_id) if asset_id else None,
            measurement={"stereo_correlation": corr},
            expected_target="mono-compatible stereo correlation",
            explanation="The measured stereo correlation is low; mono compatibility is technically testable.",
            candidate_techniques=("inspect_wide_layers", "check_polarity"),
        )))
        sequence += 1

    # Section and arrangement objects can be produced by contrast.py or
    # supplied directly by an evaluator.  Preserve their confidence labels.
    if contrast_analysis is not None:
        contrast_map = _mapping(contrast_analysis)
        drop_proxy = _value(contrast_map, "drop_impact_proxy", _value(contrast_map, "build_to_drop"))
        energy_delta = _number(_value(drop_proxy, "energy_delta_db")) if drop_proxy else None
        if energy_delta is not None and energy_delta < 2.0:
            findings.append((5, sequence, _finding(
                category="section_contrast",
                evidence_source="decoded_audio_measurement",
                severity="medium",
                confidence="low",
                section_id=_value(drop_proxy, "to_section_id"),
                measurement={"build_to_drop_energy_delta_db": energy_delta},
                expected_target="clearer build-to-drop energy movement",
                explanation="The measured build-to-drop energy movement is modest; this is a drop-impact proxy, not proof of how the drop feels.",
                actionability="actionable",
                candidate_techniques=("increase_drop_density", "review_drop_transients"),
                limitations=("Proxy does not capture musical expectation, timbre, or listener taste.",),
            )))
            sequence += 1
        similarity = _value(contrast_map, "drop_a_drop_b_similarity")
        similarity_score = _number(_value(similarity, "similarity_score")) if similarity else None
        if similarity_score is not None and similarity_score > 0.9:
            findings.append((6, sequence, _finding(
                category="arrangement_development",
                evidence_source="decoded_audio_measurement",
                severity="low",
                confidence="low",
                section_id=_value(similarity, "section_b_id"),
                measurement={"drop_a_drop_b_similarity": similarity_score},
                expected_target="intentional development between Drop A and Drop B",
                explanation="Drop A and Drop B are measured as similar on the available proxies; this remains a low-confidence arrangement suggestion.",
                actionability="actionable",
                candidate_techniques=("create_section_note_variation", "increase_drop_b_density"),
            )))
            sequence += 1

    if masking_analysis is not None:
        mask_map = _mapping(masking_analysis)
        if not bool(_value(mask_map, "context_ready", False)):
            findings.append((7, sequence, _finding(
                category="insufficient_evidence",
                evidence_source="synchronized_stem_measurement",
                severity="low",
                confidence="low",
                measurement={"context_ready": False, "readiness_reasons": _value(mask_map, "readiness_reasons", ())},
                expected_target="synchronized stem context for masking attribution",
                explanation="Masking attribution is unavailable because supplied stems are not verified as synchronized.",
                actionability="insufficient_evidence",
                required_additional_evidence=("matching-start vocal/instrumental stems",),
            )))
        else:
            masking = _value(mask_map, "masking") or mask_map
            score = _number(_value(masking, "possible_masking_index"))
            if score is not None and score > 0.45:
                findings.append((4, sequence, _finding(
                    category="masking",
                    evidence_source="synchronized_stem_measurement",
                    severity="high",
                    confidence="high",
                    measurement={"possible_masking_index": score},
                    expected_target="role separation in supplied synchronized stems",
                    explanation="Synchronized stem analysis shows likely spectral overlap; attribution is limited to the supplied stem mapping.",
                    candidate_techniques=("adjust_role_level", "apply_semantic_processing"),
                )))
        sequence += 1

    if reference_comparisons is not None:
        reference_values = (
            list(reference_comparisons)
            if isinstance(reference_comparisons, Sequence)
            and not isinstance(reference_comparisons, (str, bytes, Mapping))
            else [reference_comparisons]
        )
        for reference_value in reference_values:
            reference_map = _mapping(reference_value)
            reference_measurements = _value(reference_map, "measurements", {}) or {}
            comparison_ready = _value(reference_measurements, "comparison_ready")
            alignment_state = _value(reference_map, "alignment_state", "unknown")
            if comparison_ready is None:
                comparison_ready = alignment_state == "aligned"
            if not bool(comparison_ready):
                findings.append((7, sequence, _finding(
                    category="insufficient_evidence",
                    evidence_source="reference_comparison",
                    severity="low",
                    confidence="low",
                    explanation="The supplied reference could not be aligned with sufficient confidence; directional comparison is withheld.",
                    actionability="insufficient_evidence",
                    required_additional_evidence=("matching export start and duration",),
                )))
                sequence += 1

    # Subjective brief goals remain visibly subjective. Do not invent pass/fail
    # results from their words.
    if not goal_evaluations and brief and any(word in brief.casefold() for word in ("emotional", "feel", "character", "natural", "vibe", "impact")):
        findings.append((8, sequence, _finding(
            category="insufficient_evidence",
            evidence_source="connected_ai_interpretation",
            severity="low",
            confidence="low",
            expected_target="producer judgment for subjective creative goals",
            explanation="Part of the requested goal requires user listening judgment and cannot be established by these measurements alone.",
            actionability="requires_user_judgment",
            required_additional_evidence=("explicit user feedback after listening",),
            limitations=("Measured audio quality is not artistic approval.",),
        )))

    findings.sort(key=lambda value: (value[0], value[1]))
    return tuple(value[2] for value in findings[:max_findings])


def rank_findings(findings: Sequence[Any], *, max_findings: int = MAX_FINDINGS) -> tuple[Any, ...]:
    """Rank already-constructed findings with explicit evidence precedence."""

    if max_findings < 1:
        raise ValueError("max_findings must be positive")
    max_findings = min(int(max_findings), MAX_FINDINGS)
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

    def evidence_rank(item: Any) -> int:
        source = str(_value(item, "evidence_source", "connected_ai_interpretation"))
        if source == "explicit_user_feedback":
            return 0
        if source == "production_run_receipt":
            return 2
        if source == "synchronized_stem_measurement":
            return 3
        if source == "decoded_audio_measurement":
            # Critical export failures outrank all softer evidence.  Other
            # full-mix values are split by confidence so low-confidence
            # arrangement proxies cannot bury synchronized-stem findings.
            category = str(_value(item, "category", ""))
            severity = str(_value(item, "severity", "medium"))
            confidence = _number(_value(item, "confidence")) or 0.0
            if severity == "critical" or category in {"technical_export", "clipping_or_headroom"}:
                return 1
            return 4 if confidence >= 0.65 else 6
        if source == "reference_comparison":
            return 5
        if source == "sound_palette_metadata":
            return 6
        return 7

    ordered = sorted(
        findings,
        key=lambda item: (
            evidence_rank(item),
            severity_rank.get(str(_value(item, "severity", "medium")), 9),
            str(_value(item, "finding_id", "")),
        ),
    )
    return tuple(ordered[:max_findings])


__all__ = [
    "EVIDENCE_SOURCES",
    "FINDINGS_ANALYZER_VERSION",
    "FINDING_CATEGORIES",
    "MAX_FINDINGS",
    "MAX_TOP_PRIORITIES",
    "build_evaluation_findings",
    "rank_findings",
]
