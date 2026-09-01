"""Deterministic, read-only creation readiness evaluation.

``CreationReadinessService`` consumes observations that have already been
captured by an integration layer.  It intentionally has no bridge dependency:
calling it cannot arm Piano Roll, enable write mode, enumerate a catalog, or
otherwise mutate FL Studio.  All independently detectable issues are
collected before the report is returned.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from ..contracts import ConnectionInfo
from .context import (
    ContextTargetIdentity,
    CreationRunContextSnapshot,
    PianoRollArmingReceipt,
    build_context_snapshot,
)
from .models import (
    CompletionTarget,
    ConnectionReadiness,
    CreationReadinessInput,
    CreationReadinessReport,
    EffectCoverageReport,
    PatternCoverage,
    ReadinessBlocker,
    ReadinessDimension,
    ReadinessDimensionName,
    ReadinessEvidence,
    ReadinessLimitation,
    ReadinessManualAction,
    ReadinessState,
    ScopeReadiness,
    _canonical_digest,
)


_DIMENSION_ORDER: tuple[ReadinessDimensionName, ...] = (
    "connection_bridge",
    "piano_roll",
    "instrument_pool",
    "drum_coverage",
    "patterns_arrangement",
    "mixer_effects",
    "scope_manual_work",
)


def _as_connection_info(connection: ConnectionReadiness) -> ConnectionInfo | None:
    """Read the repository connection contract without copying or changing it."""

    if connection.connection_info is not None:
        return connection.connection_info
    if connection.connected is None and connection.compatible is None:
        return None
    return ConnectionInfo(
        connected=bool(connection.connected),
        compatible=bool(connection.compatible),
        compatibility_reason=connection.compatibility_reason or "unspecified",
        session_fingerprint=connection.session_fingerprint,
        runtime_write_mode_control=bool(connection.runtime_write_mode_control),
    )


class CreationReadinessService:
    """Evaluate one bounded set of observations without performing writes."""

    def __init__(self, *, max_issues: int = 128, max_actions: int = 64) -> None:
        if type(max_issues) is not int or not 1 <= max_issues <= 128:
            raise ValueError("max_issues must be between 1 and 128")
        if type(max_actions) is not int or not 1 <= max_actions <= 64:
            raise ValueError("max_actions must be between 1 and 64")
        self.max_issues = max_issues
        self.max_actions = max_actions

    def evaluate(
        self,
        facts: CreationReadinessInput | None = None,
        *,
        observed_at: datetime | None = None,
        **fact_values: object,
    ) -> CreationReadinessReport:
        """Return one aggregate scorecard and reusable context snapshot.

        ``fact_values`` is a convenience for integrations that have not yet
        assembled a ``CreationReadinessInput`` object.  It is still validated
        by the same strict contract, and supplying both ``facts`` and keyword
        facts is rejected to avoid silently combining observations.
        """

        if facts is not None and fact_values:
            raise TypeError("pass either facts or keyword readiness facts, not both")
        if facts is None:
            facts = CreationReadinessInput.model_validate(fact_values)
        elif not isinstance(facts, CreationReadinessInput):
            if not isinstance(facts, dict):
                raise TypeError("facts must be a CreationReadinessInput or mapping")
            facts = CreationReadinessInput.model_validate(facts)
        observed = facts.observed_at if observed_at is None else observed_at

        blockers: list[ReadinessBlocker] = []
        limitations: list[ReadinessLimitation] = []
        manual_actions: list[ReadinessManualAction] = []
        optional_enhancements: list[ReadinessLimitation] = []
        dimensions: list[ReadinessDimension] = []
        warnings: list[str] = []

        self._evaluate_connection(
            facts.connection,
            blockers,
            limitations,
            manual_actions,
            dimensions,
        )
        self._evaluate_piano_roll(
            facts.piano_roll,
            blockers,
            limitations,
            manual_actions,
            dimensions,
        )
        self._evaluate_instruments(
            facts,
            blockers,
            limitations,
            manual_actions,
            dimensions,
        )
        self._evaluate_drums(
            facts,
            blockers,
            limitations,
            manual_actions,
            dimensions,
        )
        self._evaluate_patterns(
            facts.patterns,
            blockers,
            limitations,
            manual_actions,
            dimensions,
        )
        self._evaluate_effects(
            facts.effects,
            self._effective_completion_target(facts),
            facts.processing_required,
            blockers,
            limitations,
            manual_actions,
            dimensions,
        )
        self._evaluate_scope(
            facts.scope,
            blockers,
            limitations,
            manual_actions,
            dimensions,
        )

        warnings.extend(facts.piano_roll.warnings)
        warnings.extend(facts.instrument_pool.warnings)
        warnings.extend(facts.drum_coverage.warnings)
        warnings.extend(facts.patterns.warnings)
        warnings.extend(facts.effects.warnings)
        warnings.extend(facts.scope.warnings)

        blockers = self._dedupe_blockers(blockers)
        limitations = self._dedupe_limitations(limitations)
        manual_actions = self._dedupe_actions(manual_actions)
        optional_enhancements = self._dedupe_limitations(optional_enhancements)
        blockers = blockers[: self.max_issues]
        limitations = limitations[: self.max_issues]
        manual_actions = manual_actions[: self.max_actions]
        optional_enhancements = optional_enhancements[: self.max_actions]
        warnings_tuple = tuple(dict.fromkeys(warnings))[: self.max_issues]

        overall_state = self._overall_state(blockers, limitations, manual_actions)
        score = self._score(dimensions)
        context = self._context_for(facts)
        return CreationReadinessReport(
            observed_at=observed,
            overall_state=overall_state,
            score=score,
            dimensions=tuple(dimensions),
            blockers=tuple(blockers),
            limitations=tuple(limitations),
            manual_actions=tuple(manual_actions),
            optional_enhancements=tuple(optional_enhancements),
            context_snapshot=context,
            warnings=warnings_tuple,
        )

    @staticmethod
    def _effective_completion_target(
        facts: CreationReadinessInput,
    ) -> CompletionTarget:
        """Choose the request target, retaining a richer effect observation.

        The top-level request is authoritative when it is explicit.  Older
        integrations often only pass an ``EffectCoverageReport`` and leave the
        input default in place, so a non-default target captured by that report
        is retained in that compatibility case.
        """

        if (
            facts.completion_target == "playable_draft"
            and facts.effects.completion_target != "playable_draft"
        ):
            return facts.effects.completion_target
        return facts.completion_target

    # Common service spellings make the foundation convenient to embed while
    # keeping one implementation and one deterministic ordering policy.
    assess = evaluate
    preflight = evaluate
    check = evaluate
    run = evaluate

    def _evaluate_connection(
        self,
        connection: ConnectionReadiness,
        blockers: list[ReadinessBlocker],
        limitations: list[ReadinessLimitation],
        actions: list[ReadinessManualAction],
        dimensions: list[ReadinessDimension],
    ) -> None:
        info = _as_connection_info(connection)
        local_blockers: list[ReadinessBlocker] = []
        local_limits: list[ReadinessLimitation] = []
        local_actions: list[ReadinessManualAction] = []

        if info is None:
            local_blockers.append(
                self._blocker(
                    "connection_unavailable",
                    "connection_bridge",
                    "No captured FL Studio connection information is available.",
                    "bridge ping/project summary",
                )
            )
        else:
            if not info.connected:
                local_blockers.append(
                    self._blocker(
                        "bridge_disconnected",
                        "connection_bridge",
                        "The FL Studio bridge is not connected.",
                        "ConnectionInfo.connected",
                    )
                )
            if not info.compatible:
                local_blockers.append(
                    self._blocker(
                        "bridge_incompatible",
                        "connection_bridge",
                        info.compatibility_reason or "The bridge is not compatible with this run.",
                        "ConnectionInfo.compatible",
                    )
                )
            if info.session_fingerprint is None:
                local_blockers.append(
                    self._blocker(
                        "session_fingerprint_unavailable",
                        "connection_bridge",
                        "The running bridge did not provide a session fingerprint.",
                        "ConnectionInfo.session_fingerprint",
                    )
                )
            if info.bridge_transport == "none":
                local_blockers.append(
                    self._blocker(
                        "midi_transport_unavailable",
                        "connection_bridge",
                        "No supported bridge transport is available.",
                        "ConnectionInfo.bridge_transport",
                    )
                )
            if info.runtime_write_mode_control is False:
                local_blockers.append(
                    self._blocker(
                        "runtime_write_mode_unavailable",
                        "connection_bridge",
                        "The running bridge cannot control run-scoped write mode.",
                        "ConnectionInfo.runtime_write_mode_control",
                    )
                )
            expected_source = info.expected_bridge_source_sha256
            actual_source = info.bridge_source_sha256
            if expected_source is not None and actual_source != expected_source:
                local_blockers.append(
                    self._blocker(
                        "bridge_source_revision_mismatch",
                        "connection_bridge",
                        "The deployed bridge source revision does not match the expected revision.",
                        "ConnectionInfo.bridge_source_sha256",
                    )
                )
            if info.bridge_provenance == "mismatched":
                local_blockers.append(
                    self._blocker(
                        "bridge_provenance_mismatch",
                        "connection_bridge",
                        "Bridge provenance verification reported a revision mismatch.",
                        "ConnectionInfo.bridge_provenance",
                    )
                )
            if not info.bridge_provenance_verified:
                local_limits.append(
                    self._limitation(
                        "bridge_provenance_unverified",
                        "connection_bridge",
                        "Bridge/source provenance was not independently verified in this observation.",
                        "ConnectionInfo.bridge_provenance",
                    )
                )

        if self._revisions_comparable(
            connection.package_source_revision,
            connection.deployed_bridge_revision,
        ) and connection.package_source_revision != connection.deployed_bridge_revision:
            local_blockers.append(
                self._blocker(
                    "package_bridge_revision_mismatch",
                    "connection_bridge",
                    "The package source revision and deployed bridge revision differ.",
                    "CreationReadinessInput.connection",
                )
            )
        if (
            connection.deployed_bridge_revision is not None
            and connection.running_bridge_revision is not None
            and connection.deployed_bridge_revision != connection.running_bridge_revision
        ):
            local_blockers.append(
                self._blocker(
                    "running_bridge_revision_mismatch",
                    "connection_bridge",
                    "The running bridge revision differs from the deployed bridge revision.",
                    "CreationReadinessInput.connection",
                )
            )
        if connection.require_process_identity and connection.mcp_process_identity is None:
            local_blockers.append(
                self._blocker(
                    "mcp_process_identity_unavailable",
                    "connection_bridge",
                    "The MCP process identity required for a process-local run is unavailable.",
                    "CreationReadinessInput.connection",
                )
            )
        elif connection.mcp_process_identity is None:
            local_limits.append(
                self._limitation(
                    "mcp_process_identity_unrecorded",
                    "connection_bridge",
                    "The process identity was not recorded; continuation must revalidate the process-local context.",
                    "CreationReadinessInput.connection",
                )
            )
        if connection.require_midi or any(
            value is False
            for value in (connection.midi_input_available, connection.midi_output_available)
        ):
            if connection.midi_endpoint is None:
                local_blockers.append(
                    self._blocker(
                        "midi_endpoint_unavailable",
                        "connection_bridge",
                        "No MIDI endpoint was selected for this run.",
                        "CreationReadinessInput.connection.midi_endpoint",
                    )
                )
            if connection.midi_input_available is False:
                local_blockers.append(
                    self._blocker(
                        "midi_input_unavailable",
                        "connection_bridge",
                        "The selected MIDI input is unavailable.",
                        "CreationReadinessInput.connection.midi_input_available",
                    )
                )
            if connection.midi_output_available is False:
                local_blockers.append(
                    self._blocker(
                        "midi_output_unavailable",
                        "connection_bridge",
                        "The selected MIDI output is unavailable.",
                        "CreationReadinessInput.connection.midi_output_available",
                    )
                )
        if connection.queue_healthy is False:
            local_blockers.append(
                self._blocker(
                    "bridge_queue_unhealthy",
                    "connection_bridge",
                    "The bridge queue or transport health check failed.",
                    "CreationReadinessInput.connection.queue_healthy",
                )
            )
        if connection.supported_fl_build is False:
            local_blockers.append(
                self._blocker(
                    "unsupported_fl_build",
                    "connection_bridge",
                    "The connected FL Studio build is not supported for this run.",
                    "CreationReadinessInput.connection.supported_fl_build",
                )
            )
        if connection.current_write_state == "enabled":
            local_limits.append(
                self._limitation(
                    "write_mode_already_enabled",
                    "connection_bridge",
                    "Write mode was already enabled before preflight; the integration must preserve its ownership boundary.",
                    "CreationReadinessInput.connection.current_write_state",
                )
            )

        blockers.extend(local_blockers)
        limitations.extend(local_limits)
        actions.extend(local_actions)
        dimensions.append(
            self._dimension(
                "connection_bridge",
                local_blockers,
                local_limits,
                local_actions,
                "Connection and bridge checks are complete.",
            )
        )

    @staticmethod
    def _revisions_comparable(left: str | None, right: str | None) -> bool:
        """Avoid comparing package labels with bridge source hashes.

        The live collector records a package revision such as
        ``postfader-0.13`` alongside the expected/running bridge source
        SHA-256.  Those are both useful provenance observations, but they are
        different namespaces and cannot truthfully be declared mismatched.
        Equal values (or two values from the same namespace) remain directly
        comparable.
        """

        if left is None or right is None:
            return False
        left_is_sha = len(left) == 64 and all(
            character in "0123456789abcdef" for character in left
        )
        right_is_sha = len(right) == 64 and all(
            character in "0123456789abcdef" for character in right
        )
        return left_is_sha == right_is_sha

    def _evaluate_piano_roll(
        self,
        piano_roll,
        blockers: list[ReadinessBlocker],
        limitations: list[ReadinessLimitation],
        actions: list[ReadinessManualAction],
        dimensions: list[ReadinessDimension],
    ) -> None:
        local_blockers: list[ReadinessBlocker] = []
        local_limits: list[ReadinessLimitation] = []
        local_actions: list[ReadinessManualAction] = []
        if piano_roll.required:
            checks = (
                (
                    "piano_roll_apply_script_missing",
                    not piano_roll.apply_script_present,
                    "The generated PostFader Apply script is not present.",
                ),
                (
                    "piano_roll_not_armed",
                    not piano_roll.armed_this_process,
                    "Piano Roll is not armed in the current MCP process.",
                ),
                (
                    "piano_roll_arming_receipt_missing",
                    not piano_roll.authenticated_arming_receipt,
                    "No authenticated Piano Roll arming receipt is available.",
                ),
                (
                    "piano_roll_target_selection_unavailable",
                    not piano_roll.target_selection_supported,
                    "The required Piano Roll target-selection capability is unavailable.",
                ),
                (
                    "piano_roll_persistence_receipt_unavailable",
                    not piano_roll.persistence_receipt_supported,
                    "The required Piano Roll persistence receipt capability is unavailable.",
                ),
            )
            for code, missing, message in checks:
                if missing:
                    local_blockers.append(
                        self._blocker(code, "piano_roll", message, "PianoRollReadiness")
                    )
            pending_actions = tuple(
                item
                for item in (
                    *((piano_roll.manual_action,) if piano_roll.manual_action is not None else ()),
                    *piano_roll.manual_actions,
                )
                if not item.completed
            )
            for manual_action in pending_actions:
                local_actions.append(manual_action)
                if manual_action.blocking:
                    local_blockers.append(
                        self._blocker(
                            "piano_roll_manual_action_required",
                            "piano_roll",
                            manual_action.instruction,
                            "PianoRollReadiness.manual_action",
                            manual_action_id=manual_action.action_id,
                        )
                    )
                else:
                    local_limits.append(
                        self._limitation(
                            "piano_roll_manual_action_pending",
                            "piano_roll",
                            manual_action.instruction,
                            "PianoRollReadiness.manual_action",
                        )
                    )
        blockers.extend(local_blockers)
        limitations.extend(local_limits)
        actions.extend(local_actions)
        dimensions.append(
            self._dimension(
                "piano_roll",
                local_blockers,
                local_limits,
                local_actions,
                "Piano Roll arming, targeting, and persistence capabilities are checked.",
            )
        )

    def _evaluate_instruments(
        self,
        facts: CreationReadinessInput,
        blockers: list[ReadinessBlocker],
        limitations: list[ReadinessLimitation],
        actions: list[ReadinessManualAction],
        dimensions: list[ReadinessDimension],
    ) -> None:
        pool = facts.instrument_pool
        requested = tuple(
            dict.fromkeys(
                (*facts.requested_roles, *pool.requested_roles, *pool.required_roles)
            )
        )
        covered = {item.casefold() for item in pool.covered_roles}
        missing = {
            item.casefold(): item
            for item in (*pool.missing_roles, *requested)
            if item.casefold() not in covered
        }
        local_blockers: list[ReadinessBlocker] = []
        local_limits: list[ReadinessLimitation] = []
        local_actions: list[ReadinessManualAction] = []
        for role in requested:
            if role.casefold() in missing:
                local_blockers.append(
                    self._blocker(
                        "instrument_role_missing",
                        "instrument_pool",
                        f"No usable loaded generator candidate covers requested role {role!r}.",
                        "InstrumentPoolCoverage.missing_roles",
                    )
                )
        for product in pool.missing_products:
            local_blockers.append(
                self._blocker(
                    "required_instrument_product_missing",
                    "instrument_pool",
                    f"Required instrument product {product!r} is not loaded.",
                    "InstrumentPoolCoverage.missing_products",
                )
            )
        for target in pool.loaded_generators:
            target_roles = {item.casefold() for item in target.requested_roles}
            if target_roles.intersection(item.casefold() for item in requested):
                if not target.usable_preset_candidate:
                    local_blockers.append(
                        self._blocker(
                            "preset_candidate_unavailable",
                            "instrument_pool",
                            f"Loaded generator {target.product_name!r} has no usable preset candidate for its requested role.",
                            "InstrumentTargetCoverage.usable_preset_candidate",
                        )
                    )
                if target.target_fingerprint is None:
                    local_blockers.append(
                        self._blocker(
                            "instrument_target_fingerprint_missing",
                            "instrument_pool",
                            f"Loaded generator {target.product_name!r} has no target fingerprint proof.",
                            "InstrumentTargetCoverage.target_fingerprint",
                        )
                    )
        if requested and not pool.preset_navigation_supported:
            local_limits.append(
                self._limitation(
                    "preset_navigation_limited",
                    "instrument_pool",
                    "Preset navigation is unavailable; only already-observed candidates can be considered.",
                    "InstrumentPoolCoverage.preset_navigation_supported",
                )
            )
        if requested and not pool.usable_preset_discovery:
            local_limits.append(
                self._limitation(
                    "preset_discovery_limited",
                    "instrument_pool",
                    "Usable preset discovery is not fully available for the loaded instrument pool.",
                    "InstrumentPoolCoverage.usable_preset_discovery",
                )
            )
        blockers.extend(local_blockers)
        limitations.extend(local_limits)
        actions.extend(local_actions)
        dimensions.append(
            self._dimension(
                "instrument_pool",
                local_blockers,
                local_limits,
                local_actions,
                "Loaded generator, target identity, preset, and requested-role coverage are checked.",
            )
        )

    def _evaluate_drums(
        self,
        facts: CreationReadinessInput,
        blockers: list[ReadinessBlocker],
        limitations: list[ReadinessLimitation],
        actions: list[ReadinessManualAction],
        dimensions: list[ReadinessDimension],
    ) -> None:
        drums = facts.drum_coverage
        required_roles = tuple(dict.fromkeys((*facts.required_drum_roles, *drums.required_roles)))
        required = drums.required or bool(required_roles)
        local_blockers: list[ReadinessBlocker] = []
        local_limits: list[ReadinessLimitation] = []
        local_actions: list[ReadinessManualAction] = []
        if required:
            if not drums.loaded_drum_generator:
                local_blockers.append(
                    self._blocker(
                        "drum_generator_missing",
                        "drum_coverage",
                        "No loaded drum-capable generator is available.",
                        "DrumCoverage.loaded_drum_generator",
                    )
                )
            mapped = {item.casefold() for item in drums.mapped_roles}
            if drums.drum_map is not None:
                mapped.update(item.role.casefold() for item in drums.drum_map.mappings)
            missing = {
                item.casefold(): item
                for item in (*drums.missing_roles, *required_roles)
                if item.casefold() not in mapped
            }
            if drums.drum_map is not None:
                missing.update(
                    {
                        item.casefold(): item
                        for item in drums.drum_map.missing_required(required_roles)
                    }
                )
            if not drums.pad_map_available and drums.drum_map is None:
                if drums.general_midi_fallback_available:
                    local_limits.append(
                        self._limitation(
                            "drum_pad_map_missing_using_general_midi",
                            "drum_coverage",
                            "No reported drum pad map is available; the bounded General MIDI fallback may be used.",
                            "DrumCoverage.general_midi_fallback_available",
                        )
                    )
                else:
                    local_blockers.append(
                        self._blocker(
                            "drum_pad_map_missing",
                            "drum_coverage",
                            "The required drum generator has no reported semantic pad map.",
                            "DrumCoverage.pad_map_available",
                        )
                    )
            for role in missing.values():
                if drums.general_midi_fallback_available:
                    local_limits.append(
                        self._limitation(
                            "drum_role_fallback",
                            "drum_coverage",
                            f"Required drum role {role!r} is not in the reported map; General MIDI fallback is available.",
                            "DrumCoverage.missing_roles",
                        )
                    )
                else:
                    local_blockers.append(
                        self._blocker(
                            "drum_role_missing",
                            "drum_coverage",
                            f"Required semantic drum role {role!r} is not mapped.",
                            "DrumCoverage.missing_roles",
                        )
                    )
            if not drums.exact_kit_preset_supported:
                local_limits.append(
                    self._limitation(
                        "drum_kit_identity_limited",
                        "drum_coverage",
                        "Exact drum-kit preset identity is not fully supported by the captured observations.",
                        "DrumCoverage.exact_kit_preset_supported",
                    )
                )
        blockers.extend(local_blockers)
        limitations.extend(local_limits)
        actions.extend(local_actions)
        dimensions.append(
            self._dimension(
                "drum_coverage",
                local_blockers,
                local_limits,
                local_actions,
                "Drum generator, semantic pad-map, and requested-role coverage are checked.",
            )
        )

    def _evaluate_patterns(
        self,
        patterns: PatternCoverage,
        blockers: list[ReadinessBlocker],
        limitations: list[ReadinessLimitation],
        actions: list[ReadinessManualAction],
        dimensions: list[ReadinessDimension],
    ) -> None:
        local_blockers: list[ReadinessBlocker] = []
        local_limits: list[ReadinessLimitation] = []
        local_actions: list[ReadinessManualAction] = []
        empty = set(patterns.empty_pattern_numbers)
        for number in patterns.required_empty_patterns:
            if patterns.empty_patterns_available is False or number not in empty:
                local_blockers.append(
                    self._blocker(
                        "empty_pattern_missing",
                        "patterns_arrangement",
                        f"Required empty pattern {number} is not available.",
                        "PatternCoverage.empty_pattern_numbers",
                    )
                )
        material = set(patterns.patterns_with_existing_material)
        for number in patterns.intended_pattern_numbers:
            if number in material:
                local_blockers.append(
                    self._blocker(
                        "intended_pattern_contains_material",
                        "patterns_arrangement",
                        f"Intended pattern {number} already contains material and cannot be treated as empty.",
                        "PatternCoverage.patterns_with_existing_material",
                    )
                )
        for section in patterns.missing_sections:
            if section.casefold() in {item.casefold() for item in patterns.required_sections}:
                local_blockers.append(
                    self._blocker(
                        "arrangement_section_missing",
                        "patterns_arrangement",
                        f"Required arrangement section {section!r} is unavailable.",
                        "PatternCoverage.missing_sections",
                    )
                )
        if patterns.manual_playlist_placement_required:
            requested = patterns.expected_manual_playlist_actions or (
                "Place generated patterns in the Playlist for the intended sections.",
            )
            for index, instruction in enumerate(requested, start=1):
                local_actions.append(
                    ReadinessManualAction(
                        action_id=f"playlist-placement-{index}",
                        dimension="patterns_arrangement",
                        instruction=instruction,
                        required=True,
                        blocking=False,
                    )
                )
            local_limits.append(
                self._limitation(
                    "manual_playlist_placement",
                    "patterns_arrangement",
                    "Pattern creation can proceed, but Playlist placement requires a known manual handoff.",
                    "PatternCoverage.manual_playlist_placement_required",
                )
            )
        elif not patterns.playlist_placement_supported and patterns.required_sections:
            local_limits.append(
                self._limitation(
                    "playlist_delivery_limited",
                    "patterns_arrangement",
                    "Playlist placement is not exposed; created patterns may require manual delivery.",
                    "PatternCoverage.playlist_placement_supported",
                )
            )
        blockers.extend(local_blockers)
        limitations.extend(local_limits)
        actions.extend(local_actions)
        dimensions.append(
            self._dimension(
                "patterns_arrangement",
                local_blockers,
                local_limits,
                local_actions,
                "Pattern identity, empty-pattern targets, arrangement sections, and Playlist delivery are checked.",
            )
        )

    def _evaluate_effects(
        self,
        effects: EffectCoverageReport,
        completion_target: CompletionTarget,
        processing_required: bool,
        blockers: list[ReadinessBlocker],
        limitations: list[ReadinessLimitation],
        actions: list[ReadinessManualAction],
        dimensions: list[ReadinessDimension],
    ) -> None:
        requires = (
            processing_required
            or effects.processing_required_for_completion
            or completion_target in {"mix_ready", "polished_mix_ready"}
        )
        local_blockers: list[ReadinessBlocker] = []
        local_limits: list[ReadinessLimitation] = []
        local_actions: list[ReadinessManualAction] = []
        missing = list(effects.missing_capabilities)
        for role in effects.roles:
            missing.extend(role.missing_capabilities)
            if role.state in {
                "missing_requested_effect",
                "loaded_but_unresolved",
                "unresolved_effect",
            }:
                missing.append(
                    # This synthetic record is local to the scorecard and does
                    # not claim a product is available.
                    self._missing_from_role(role.role_id, role.state, role.processing_required_for_completion)
                )
        if effects.state in {
            "missing_requested_effect",
            "loaded_but_unresolved",
            "unresolved_effect",
        } and not missing:
            missing.append(
                self._missing_from_role(
                    "project",
                    effects.state,
                    effects.required_processing_missing,
                )
            )
        seen: set[tuple[str | None, str]] = set()
        for item in missing:
            key = (item.role_id, item.category)
            if key in seen:
                continue
            seen.add(key)
            required = requires or item.required_for_completion
            if required:
                local_blockers.append(
                    self._blocker(
                        "required_processing_missing",
                        "mixer_effects",
                        f"Required processing capability {item.category!r} is unavailable: {item.reason}",
                        "EffectCoverageReport.missing_capabilities",
                    )
                )
            else:
                local_limits.append(
                    self._limitation(
                        "missing_requested_effect",
                        "mixer_effects",
                        f"Requested processing capability {item.category!r} is unavailable: {item.reason}",
                        "EffectCoverageReport.missing_capabilities",
                    )
                )
        if effects.has_unresolved_controls:
            message = (
                "One or more loaded effects have no resolved semantic control adapter."
            )
            if requires and not effects.dry_by_design:
                local_blockers.append(
                    self._blocker(
                        "required_processing_controls_unresolved",
                        "mixer_effects",
                        message,
                        "EffectCoverageReport.roles",
                    )
                )
            else:
                local_limits.append(
                    self._limitation(
                        "loaded_effect_controls_unresolved",
                        "mixer_effects",
                        message,
                        "EffectCoverageReport.roles",
                    )
                )
        if effects.dry_by_design and not effects.requested_categories:
            # This is useful explanatory state, not a blocker or a warning.
            pass
        elif not effects.loaded_capabilities and not effects.requested_categories:
            message = "No loaded effects were observed; a dry draft remains possible."
            if requires and not effects.dry_by_design:
                local_blockers.append(
                    self._blocker(
                        "required_processing_missing",
                        "mixer_effects",
                        message,
                        "EffectCoverageReport.loaded_capabilities",
                    )
                )
            else:
                local_limits.append(
                    self._limitation(
                        "no_loaded_effects",
                        "mixer_effects",
                        message,
                        "EffectCoverageReport.loaded_capabilities",
                    )
                )
        blockers.extend(local_blockers)
        limitations.extend(local_limits)
        actions.extend(local_actions)
        dimensions.append(
            self._dimension(
                "mixer_effects",
                local_blockers,
                local_limits,
                local_actions,
                "Loaded effects, Atlas/adapters, requested techniques, and dry-draft capability are checked.",
            )
        )

    def _evaluate_scope(
        self,
        scope: ScopeReadiness,
        blockers: list[ReadinessBlocker],
        limitations: list[ReadinessLimitation],
        actions: list[ReadinessManualAction],
        dimensions: list[ReadinessDimension],
    ) -> None:
        local_blockers: list[ReadinessBlocker] = []
        local_limits: list[ReadinessLimitation] = []
        local_actions: list[ReadinessManualAction] = []
        allowed = {item.casefold() for item in scope.allowed_mutation_categories}
        for category in scope.required_mutation_categories:
            if category.casefold() not in allowed:
                local_blockers.append(
                    self._blocker(
                        "mutation_category_not_allowed",
                        "scope_manual_work",
                        f"Required mutation category {category!r} is outside the authorized scope.",
                        "ScopeReadiness.allowed_mutation_categories",
                    )
                )
        for operation in scope.unavailable_operations:
            local_blockers.append(
                self._blocker(
                    "required_operation_unavailable",
                    "scope_manual_work",
                    f"Required operation {operation!r} is not exposed by the current runtime.",
                    "ScopeReadiness.unavailable_operations",
                )
            )
        for index, instruction in enumerate(scope.expected_manual_playlist_actions, start=1):
            local_actions.append(
                ReadinessManualAction(
                    action_id=f"scope-playlist-placement-{index}",
                    dimension="scope_manual_work",
                    instruction=instruction,
                    required=True,
                    blocking=False,
                )
            )
        if scope.expected_export_action is not None:
            local_actions.append(
                ReadinessManualAction(
                    action_id="scope-export",
                    dimension="scope_manual_work",
                    instruction=scope.expected_export_action,
                    required=True,
                    blocking=False,
                )
            )
        if local_actions:
            local_limits.append(
                self._limitation(
                    "manual_scope_handoff",
                    "scope_manual_work",
                    "One or more known manual setup or delivery actions remain outside the bridge.",
                    "ScopeReadiness",
                )
            )
        blockers.extend(local_blockers)
        limitations.extend(local_limits)
        actions.extend(local_actions)
        dimensions.append(
            self._dimension(
                "scope_manual_work",
                local_blockers,
                local_limits,
                local_actions,
                "Preservation, allowed mutation categories, unavailable operations, and manual handoffs are checked.",
            )
        )

    def _context_for(self, facts: CreationReadinessInput) -> CreationRunContextSnapshot | None:
        supplied = facts.context_snapshot
        if isinstance(supplied, CreationRunContextSnapshot):
            return supplied
        connection_info = _as_connection_info(facts.connection)
        session = facts.connection.session_fingerprint or (
            None if connection_info is None else connection_info.session_fingerprint
        )
        if session is None:
            return None
        targets: list[ContextTargetIdentity] = []
        for index, item in enumerate(facts.instrument_pool.loaded_generators):
            if item.target_fingerprint is not None:
                targets.append(
                    ContextTargetIdentity(
                        target_id=f"instrument-{index}",
                        kind="channel_generator",
                        fingerprint=item.target_fingerprint,
                        target=item.target,
                    )
                )
        if facts.drum_coverage.target_fingerprint is not None:
            targets.append(
                ContextTargetIdentity(
                    target_id="drum-generator",
                    kind="channel_generator",
                    fingerprint=facts.drum_coverage.target_fingerprint,
                    target=(
                        None
                        if facts.drum_coverage.drum_map is None
                        else facts.drum_coverage.drum_map.target
                    ),
                )
            )
        arming = None
        piano = facts.piano_roll
        if piano.arming_receipt_id is not None or (
            piano.required
            and piano.armed_this_process
            and piano.authenticated_arming_receipt
        ):
            arming = PianoRollArmingReceipt(
                receipt_id=piano.arming_receipt_id or "piano-roll-arming",
                process_identity=facts.connection.mcp_process_identity,
                authenticated=piano.authenticated_arming_receipt,
                script_present=piano.apply_script_present,
            )
        project_digest = facts.project_checkpoint_digest
        palette_digest = facts.palette_inventory_digest or _canonical_digest(
            facts.instrument_pool.model_dump(mode="json")
        )
        preset_digest = facts.preset_inventory_digest or _canonical_digest(
            tuple(item.current_preset for item in facts.instrument_pool.loaded_generators)
        )
        drum_digest = facts.drum_map_digest or _canonical_digest(
            facts.drum_coverage.model_dump(mode="json")
        )
        effect_digest = facts.effect_coverage_digest or _canonical_digest(
            facts.effects.model_dump(mode="json")
        )
        return build_context_snapshot(
            session_fingerprint=session,
            project=facts.project,
            target_fingerprints=tuple(targets),
            pattern_identities=facts.patterns.existing_patterns,
            palette_inventory_digest=palette_digest,
            preset_inventory_digest=preset_digest,
            drum_map_digest=drum_digest,
            effect_coverage_digest=effect_digest,
            piano_roll_arming_receipt=arming,
            mcp_process_identity=facts.connection.mcp_process_identity,
            package_source_revision=(
                facts.connection.package_source_revision
                or (
                    None
                    if connection_info is None
                    else connection_info.expected_bridge_source_sha256
                )
            ),
            deployed_bridge_revision=(
                facts.connection.deployed_bridge_revision
                or (
                    None
                    if connection_info is None
                    else connection_info.expected_bridge_source_sha256
                )
            ),
            running_bridge_revision=(
                facts.connection.running_bridge_revision
                or (
                    None
                    if connection_info is None
                    else connection_info.bridge_source_sha256
                )
            ),
            midi_endpoint=facts.connection.midi_endpoint,
            captured_at=facts.observed_at,
            project_checkpoint_digest=project_digest,
        )

    @staticmethod
    def _missing_from_role(role_id: str, state: str, required: bool):
        from .models import MissingProcessingCapability

        return MissingProcessingCapability(
            role_id=role_id,
            category="requested_effect",
            reason=f"role coverage state is {state}",
            required_for_completion=required,
        )

    @staticmethod
    def _blocker(
        code: str,
        dimension: ReadinessDimensionName,
        message: str,
        source: str,
        *,
        manual_action_id: str | None = None,
    ) -> ReadinessBlocker:
        return ReadinessBlocker(
            code=code,
            dimension=dimension,
            message=message,
            evidence=(ReadinessEvidence(source=source, detail=message),),
            manual_action_id=manual_action_id,
        )

    @staticmethod
    def _limitation(
        code: str,
        dimension: ReadinessDimensionName,
        message: str,
        source: str,
    ) -> ReadinessLimitation:
        return ReadinessLimitation(
            code=code,
            dimension=dimension,
            message=message,
            evidence=(ReadinessEvidence(source=source, detail=message),),
        )

    @staticmethod
    def _dimension(
        name: ReadinessDimensionName,
        blockers: list[ReadinessBlocker],
        limitations: list[ReadinessLimitation],
        actions: list[ReadinessManualAction],
        summary: str,
    ) -> ReadinessDimension:
        if blockers:
            state: ReadinessState = "blocked"
        elif limitations or any(not item.completed for item in actions):
            state = "ready_with_limitations"
        else:
            state = "ready"
        return ReadinessDimension(
            name=name,
            state=state,
            summary=summary,
            evidence=(ReadinessEvidence(source="creation-readiness", detail=summary),),
            blocker_codes=tuple(item.code for item in blockers),
            limitation_codes=tuple(item.code for item in limitations),
            manual_action_ids=tuple(item.action_id for item in actions),
        )

    @staticmethod
    def _overall_state(
        blockers: Iterable[ReadinessBlocker],
        limitations: Iterable[ReadinessLimitation],
        actions: Iterable[ReadinessManualAction],
    ) -> ReadinessState:
        if any(item.classification == "blocking" for item in blockers):
            return "blocked"
        if any(item.classification != "optional_enhancement" for item in limitations) or any(
            not item.completed for item in actions
        ):
            return "ready_with_limitations"
        return "ready"

    @staticmethod
    def _score(dimensions: Iterable[ReadinessDimension]) -> float:
        values = {
            "ready": 1.0,
            "ready_with_limitations": 0.75,
            "blocked": 0.0,
        }
        rows = tuple(dimensions)
        if not rows:
            return 0.0
        return round(100.0 * sum(values[item.state] for item in rows) / len(rows), 2)

    @staticmethod
    def _dedupe_blockers(items: Iterable[ReadinessBlocker]) -> list[ReadinessBlocker]:
        seen: set[tuple[str, str, str]] = set()
        result: list[ReadinessBlocker] = []
        for item in items:
            key = (item.dimension, item.code, item.message)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    @staticmethod
    def _dedupe_limitations(items: Iterable[ReadinessLimitation]) -> list[ReadinessLimitation]:
        seen: set[tuple[str, str, str]] = set()
        result: list[ReadinessLimitation] = []
        for item in items:
            key = (item.dimension, item.code, item.message)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    @staticmethod
    def _dedupe_actions(items: Iterable[ReadinessManualAction]) -> list[ReadinessManualAction]:
        seen: set[str] = set()
        result: list[ReadinessManualAction] = []
        for item in items:
            if item.action_id not in seen:
                seen.add(item.action_id)
                result.append(item)
        return result


CreationReadinessEvaluator = CreationReadinessService


def evaluate_creation_readiness(
    facts: CreationReadinessInput | None = None,
    **fact_values: object,
) -> CreationReadinessReport:
    """Convenience function for integrations and read-only MCP handlers."""

    service = CreationReadinessService()
    if fact_values:
        if facts is not None:
            raise TypeError("pass either facts or keyword readiness facts, not both")
        return service.evaluate(CreationReadinessInput.model_validate(fact_values))
    return service.evaluate(facts)


assess_creation_readiness = evaluate_creation_readiness
build_creation_readiness_report = evaluate_creation_readiness
check_creation_readiness = evaluate_creation_readiness
preflight_creation_readiness = evaluate_creation_readiness


__all__ = [
    "CreationReadinessEvaluator",
    "CreationReadinessService",
    "assess_creation_readiness",
    "build_creation_readiness_report",
    "check_creation_readiness",
    "evaluate_creation_readiness",
    "preflight_creation_readiness",
]
