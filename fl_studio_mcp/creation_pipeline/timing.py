"""Local phase timing and performance reports for creation runs.

Timing is diagnostic state only.  The collector never uploads telemetry and
never changes execution decisions; soft targets produce warnings rather than
failures.  Reports are immutable snapshots so a completed receipt cannot be
rewritten by a later phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal, TypedDict, cast

from pydantic import AliasChoices, Field, model_validator

from .models import MAX_PHASES, MAX_TEXT, CreationPipelineModel


TimingPhase = Literal[
    "preflight",
    "palette",
    "composition",
    "note_application",
    "processing",
    "finalization",
    "custom",
]

MAX_PHASE_DURATION_MS = 86_400_000.0
MAX_TIMING_COUNTER = 1_000_000
DEFAULT_PHASE_SOFT_TARGET_MS = 120_000.0
DEFAULT_EXCESSIVE_REFRESH_COUNT = 8
DEFAULT_REPEATED_SCAN_COUNT = 1


class _TimingCounterValues(TypedDict):
    operation_count: int
    target_refresh_count: int
    full_inventory_scan_count: int
    preset_navigation_steps: int
    preset_enumeration_count: int
    piano_roll_dispatch_count: int
    piano_roll_preparation_count: int
    manual_wait_ms: float
    blocked_duration_ms: float
    write_mode_transition_count: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PerformanceWarning(CreationPipelineModel):
    """One non-fatal local performance warning."""

    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=MAX_TEXT)
    phase: str | None = Field(default=None, max_length=64)
    observed_value: float | None = Field(default=None, ge=0.0)
    soft_target: float | None = Field(default=None, ge=0.0)


class OperationTimingSummary(CreationPipelineModel):
    """Bounded aggregate counters for one run."""

    operation_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    target_refresh_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    full_inventory_scan_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    preset_navigation_steps: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    preset_enumeration_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    piano_roll_dispatch_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    piano_roll_preparation_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    manual_wait_ms: float = Field(default=0.0, ge=0.0, le=MAX_PHASE_DURATION_MS)
    blocked_duration_ms: float = Field(default=0.0, ge=0.0, le=MAX_PHASE_DURATION_MS)
    write_mode_transition_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)

    @property
    def full_scan_count(self) -> int:
        """Short compatibility spelling for full inventory scans."""

        return self.full_inventory_scan_count

    @property
    def preset_navigation_count(self) -> int:
        return self.preset_navigation_steps

    @property
    def piano_roll_dispatches(self) -> int:
        return self.piano_roll_dispatch_count


class SetupRecoveryTiming(CreationPipelineModel):
    """Timing attributable to setup and recovery rather than creative work."""

    setup_duration_ms: float = Field(default=0.0, ge=0.0, le=MAX_PHASE_DURATION_MS)
    recovery_duration_ms: float = Field(default=0.0, ge=0.0, le=MAX_PHASE_DURATION_MS)
    setup_wait_ms: float = Field(default=0.0, ge=0.0, le=MAX_PHASE_DURATION_MS)
    blocked_duration_ms: float = Field(default=0.0, ge=0.0, le=MAX_PHASE_DURATION_MS)
    setup_operation_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    recovery_operation_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)


class PhaseTiming(CreationPipelineModel):
    """Timing and bounded counters for one executed or skipped phase."""

    phase: str = Field(
        min_length=1,
        max_length=64,
        validation_alias=AliasChoices("phase", "phase_name"),
    )
    started_at: datetime | None = Field(
        default=None, validation_alias=AliasChoices("started_at", "start_at", "start")
    )
    ended_at: datetime | None = Field(
        default=None, validation_alias=AliasChoices("ended_at", "end_at", "end")
    )
    duration_ms: float = Field(default=0.0, ge=0.0, le=MAX_PHASE_DURATION_MS)
    operation_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    target_refresh_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    full_inventory_scan_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    preset_navigation_steps: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    preset_enumeration_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    piano_roll_dispatch_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    piano_roll_preparation_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    manual_wait_ms: float = Field(default=0.0, ge=0.0, le=MAX_PHASE_DURATION_MS)
    blocked_duration_ms: float = Field(default=0.0, ge=0.0, le=MAX_PHASE_DURATION_MS)
    write_mode_transition_count: int = Field(default=0, ge=0, le=MAX_TIMING_COUNTER)
    skipped: bool = False
    skip_reason: str | None = Field(default=None, max_length=MAX_TEXT)

    @model_validator(mode="before")
    @classmethod
    def infer_duration(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        started = data.get("started_at", data.get("start_at", data.get("start")))
        ended = data.get("ended_at", data.get("end_at", data.get("end")))
        if "duration_ms" not in data:
            if isinstance(started, datetime) and isinstance(ended, datetime):
                data["duration_ms"] = max(0.0, (ended - started).total_seconds() * 1000)
        return data

    @model_validator(mode="after")
    def validate_interval(self) -> "PhaseTiming":
        if self.started_at is None and self.ended_at is not None:
            raise ValueError("ended_at requires started_at")
        if self.started_at is not None and self.ended_at is not None:
            if self.ended_at < self.started_at:
                raise ValueError("phase ended_at cannot precede started_at")
        if self.skipped:
            if self.skip_reason is None or not self.skip_reason.strip():
                raise ValueError("a skipped phase needs skip_reason")
            if self.operation_count or self.duration_ms:
                raise ValueError("a skipped phase cannot contain timing counters")
        elif self.skip_reason is not None:
            raise ValueError("skip_reason is only valid for skipped phases")
        return self

    @property
    def phase_name(self) -> str:
        return self.phase

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def start(self) -> datetime | None:
        return self.started_at

    @property
    def end(self) -> datetime | None:
        return self.ended_at


class RunTimingReport(CreationPipelineModel):
    """Immutable local timing report for one complete creation run."""

    schema_version: Literal["1.0"] = "1.0"
    generated_at: datetime
    phase_timings: tuple[PhaseTiming, ...] = Field(
        default=(), validation_alias=AliasChoices("phase_timings", "phases"), max_length=MAX_PHASES
    )
    operation_summary: OperationTimingSummary = Field(default_factory=OperationTimingSummary)
    setup_recovery: SetupRecoveryTiming = Field(default_factory=SetupRecoveryTiming)
    total_duration_ms: float = Field(default=0.0, ge=0.0, le=MAX_PHASE_DURATION_MS)
    warnings: tuple[PerformanceWarning, ...] = Field(default=(), max_length=128)
    local_only: Literal[True] = True
    telemetry_uploaded: Literal[False] = False

    @model_validator(mode="before")
    @classmethod
    def infer_total_duration(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        phases = data.get("phase_timings", data.get("phases"))
        if "total_duration_ms" not in data and isinstance(phases, (tuple, list)):
            data["total_duration_ms"] = sum(
                item.duration_ms if isinstance(item, PhaseTiming) else float(item.get("duration_ms", 0.0))
                for item in phases
                if isinstance(item, (PhaseTiming, dict))
            )
        return data

    @model_validator(mode="after")
    def validate_phases(self) -> "RunTimingReport":
        names = [item.phase for item in self.phase_timings]
        if len(names) != len(set(names)):
            raise ValueError("phase timings must contain one row per phase")
        return self

    @property
    def phases(self) -> tuple[PhaseTiming, ...]:
        return self.phase_timings

    @property
    def performance_warnings(self) -> tuple[PerformanceWarning, ...]:
        return self.warnings

    @property
    def duration_seconds(self) -> float:
        return self.total_duration_ms / 1000.0


@dataclass
class _OpenPhase:
    phase: str
    started_at: datetime
    resume_index: int | None = None
    counters: dict[str, int | float] = field(default_factory=dict)


class RunTimingCollector:
    """Small deterministic collector used by a run executor.

    Pass a ``clock`` in tests to make timestamps and durations deterministic.
    The default clock is wall time only; no event is persisted outside the
    returned report.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        phase_soft_target_ms: float = DEFAULT_PHASE_SOFT_TARGET_MS,
        excessive_target_refresh_count: int = DEFAULT_EXCESSIVE_REFRESH_COUNT,
        existing_report: RunTimingReport | None = None,
    ) -> None:
        if type(phase_soft_target_ms) not in (int, float) or phase_soft_target_ms <= 0:
            raise ValueError("phase_soft_target_ms must be positive")
        if (
            type(excessive_target_refresh_count) is not int
            or not 1 <= excessive_target_refresh_count <= MAX_TIMING_COUNTER
        ):
            raise ValueError("excessive_target_refresh_count is out of bounds")
        if existing_report is not None and not isinstance(existing_report, RunTimingReport):
            raise TypeError("existing_report must be a RunTimingReport")
        self._clock = _now if clock is None else clock
        self.phase_soft_target_ms = phase_soft_target_ms
        self.excessive_target_refresh_count = excessive_target_refresh_count
        self._open: _OpenPhase | None = None
        self._phases: list[PhaseTiming] = (
            [] if existing_report is None else list(existing_report.phase_timings)
        )
        self._totals: dict[str, int | float] = self._empty_counters()
        if existing_report is not None:
            self._totals.update(
                {
                    key: getattr(existing_report.operation_summary, key)
                    for key in self._totals
                }
            )
        self._phase_soft_targets: dict[str, float] = {}

    @classmethod
    def from_report(
        cls,
        report: RunTimingReport,
        *,
        clock: Callable[[], datetime] | None = None,
        phase_soft_target_ms: float = DEFAULT_PHASE_SOFT_TARGET_MS,
        excessive_target_refresh_count: int = DEFAULT_EXCESSIVE_REFRESH_COUNT,
    ) -> "RunTimingCollector":
        """Reopen a collector from an immutable report for run continuation."""

        return cls(
            clock=clock,
            phase_soft_target_ms=phase_soft_target_ms,
            excessive_target_refresh_count=excessive_target_refresh_count,
            existing_report=report,
        )

    resume_from_report = from_report

    @staticmethod
    def _empty_counters() -> dict[str, int | float]:
        return {
            "operation_count": 0,
            "target_refresh_count": 0,
            "full_inventory_scan_count": 0,
            "preset_navigation_steps": 0,
            "preset_enumeration_count": 0,
            "piano_roll_dispatch_count": 0,
            "piano_roll_preparation_count": 0,
            "manual_wait_ms": 0.0,
            "blocked_duration_ms": 0.0,
            "write_mode_transition_count": 0,
        }

    def _timestamp(self) -> datetime:
        current = self._clock()
        if not isinstance(current, datetime):
            raise TypeError("timing clock must return datetime")
        return current

    def start_phase(
        self,
        phase: str,
        *,
        started_at: datetime | None = None,
        resume: bool = False,
    ) -> None:
        self.begin_phase(phase, started_at=started_at, resume=resume)

    def begin_phase(
        self,
        phase: str,
        *,
        started_at: datetime | None = None,
        resume: bool = False,
    ) -> None:
        if self._open is not None:
            raise RuntimeError(f"phase {self._open.phase!r} is still open")
        if not isinstance(phase, str) or not phase.strip() or len(phase) > 64:
            raise ValueError("phase must contain 1..64 characters")
        resume_index = next(
            (index for index, item in enumerate(self._phases) if item.phase == phase),
            None,
        )
        if resume_index is not None and not resume:
            raise ValueError(f"phase {phase!r} was already recorded")
        self._open = _OpenPhase(
            phase=phase,
            started_at=self._timestamp() if started_at is None else started_at,
            resume_index=resume_index,
            counters=self._empty_counters(),
        )

    def resume_phase(self, phase: str, *, started_at: datetime | None = None) -> None:
        """Reopen one closed phase for continuation and merge its next slice."""

        if not any(item.phase == phase for item in self._phases):
            raise ValueError(f"cannot resume unknown phase {phase!r}")
        self.begin_phase(phase, started_at=started_at, resume=True)

    reopen_phase = resume_phase

    def end_phase(self, phase: str | None = None, *, ended_at: datetime | None = None) -> PhaseTiming:
        if self._open is None:
            raise RuntimeError("no phase is open")
        if phase is not None and phase != self._open.phase:
            raise ValueError(f"open phase is {self._open.phase!r}, not {phase!r}")
        end = self._timestamp() if ended_at is None else ended_at
        duration = (end - self._open.started_at).total_seconds() * 1000.0
        if duration < 0:
            raise ValueError("phase ended before it started")
        counters = cast(_TimingCounterValues, dict(self._open.counters))
        timing = PhaseTiming(
            phase=self._open.phase,
            started_at=self._open.started_at,
            ended_at=end,
            duration_ms=duration,
            **counters,
        )
        if self._open.resume_index is None:
            result = timing
            self._phases.append(timing)
        else:
            prior = self._phases[self._open.resume_index]
            result = self._merge_phase_timings(
                prior, timing
            )
            self._phases[self._open.resume_index] = result
        self._open = None
        return result

    @staticmethod
    def _merge_phase_timings(prior: PhaseTiming, current: PhaseTiming) -> PhaseTiming:
        """Merge a continuation slice into one phase row."""

        counter_fields = (
            "operation_count",
            "target_refresh_count",
            "full_inventory_scan_count",
            "preset_navigation_steps",
            "preset_enumeration_count",
            "piano_roll_dispatch_count",
            "piano_roll_preparation_count",
            "manual_wait_ms",
            "blocked_duration_ms",
            "write_mode_transition_count",
        )
        return PhaseTiming(
            phase=prior.phase,
            started_at=prior.started_at or current.started_at,
            ended_at=current.ended_at,
            duration_ms=prior.duration_ms + current.duration_ms,
            **{
                name: getattr(prior, name) + getattr(current, name)
                for name in counter_fields
            },
        )

    finish_phase = end_phase

    def skip_phase(self, phase: str, reason: str) -> PhaseTiming:
        if self._open is not None:
            raise RuntimeError(f"phase {self._open.phase!r} is still open")
        timing = PhaseTiming(phase=phase, skipped=True, skip_reason=reason)
        if any(item.phase == phase for item in self._phases):
            raise ValueError(f"phase {phase!r} was already recorded")
        self._phases.append(timing)
        return timing

    def _increment(self, key: str, amount: int | float = 1) -> None:
        if self._open is None:
            raise RuntimeError("record a timing event inside an open phase")
        current = self._open.counters[key]
        self._open.counters[key] = current + amount
        self._totals[key] += amount

    def record_operation(self, count: int = 1) -> None:
        self._record_int("operation_count", count)

    def record_target_refresh(self, count: int = 1) -> None:
        self._record_int("target_refresh_count", count)

    def record_full_inventory_scan(self, count: int = 1) -> None:
        self._record_int("full_inventory_scan_count", count)

    # Alias used by inspectors that call a scan an inventory enumeration.
    record_full_scan = record_full_inventory_scan
    record_full_inventory_scans = record_full_inventory_scan

    def record_preset_navigation(self, steps: int = 1) -> None:
        self._record_int("preset_navigation_steps", steps)

    record_preset_navigation_steps = record_preset_navigation

    def record_preset_enumeration(self, count: int = 1) -> None:
        self._record_int("preset_enumeration_count", count)

    def record_piano_roll_dispatch(self, count: int = 1) -> None:
        self._record_int("piano_roll_dispatch_count", count)

    record_piano_roll_dispatches = record_piano_roll_dispatch

    def record_piano_roll_preparation(self, count: int = 1) -> None:
        self._record_int("piano_roll_preparation_count", count)

    # A preparation is often called "arming" by the bridge integration.
    record_piano_roll_arm = record_piano_roll_preparation

    def record_manual_wait(self, milliseconds: float) -> None:
        self._record_float("manual_wait_ms", milliseconds)

    def record_blocked_duration(self, milliseconds: float) -> None:
        self._record_float("blocked_duration_ms", milliseconds)

    def record_write_mode_transition(self, count: int = 1) -> None:
        self._record_int("write_mode_transition_count", count)

    def _record_int(self, key: str, count: int) -> None:
        if type(count) is not int or not 0 <= count <= MAX_TIMING_COUNTER:
            raise ValueError(f"{key} count is out of bounds")
        self._increment(key, count)

    def _record_float(self, key: str, milliseconds: float) -> None:
        if type(milliseconds) not in (int, float) or milliseconds < 0:
            raise ValueError(f"{key} must be non-negative")
        if milliseconds > MAX_PHASE_DURATION_MS:
            raise ValueError(f"{key} exceeds the timing bound")
        self._increment(key, float(milliseconds))

    def report(self, *, generated_at: datetime | None = None) -> RunTimingReport:
        return self.build_report(generated_at=generated_at)

    def build_report(self, *, generated_at: datetime | None = None) -> RunTimingReport:
        if self._open is not None:
            raise RuntimeError(f"phase {self._open.phase!r} is still open")
        summary = OperationTimingSummary(
            **cast(_TimingCounterValues, dict(self._totals))
        )
        setup_duration = sum(
            item.duration_ms
            for item in self._phases
            if item.phase in {"preflight", "setup", "recovery"}
        )
        recovery_duration = sum(
            item.duration_ms for item in self._phases if item.phase == "recovery"
        )
        setup_ops = sum(
            item.operation_count
            for item in self._phases
            if item.phase in {"preflight", "setup"}
        )
        recovery_ops = sum(item.operation_count for item in self._phases if item.phase == "recovery")
        setup_recovery = SetupRecoveryTiming(
            setup_duration_ms=setup_duration,
            recovery_duration_ms=recovery_duration,
            setup_wait_ms=sum(
                item.manual_wait_ms
                for item in self._phases
                if item.phase in {"preflight", "setup"}
            ),
            blocked_duration_ms=summary.blocked_duration_ms,
            setup_operation_count=setup_ops,
            recovery_operation_count=recovery_ops,
        )
        warnings = self._warnings(summary)
        return RunTimingReport(
            generated_at=self._timestamp() if generated_at is None else generated_at,
            phase_timings=tuple(self._phases),
            operation_summary=summary,
            setup_recovery=setup_recovery,
            total_duration_ms=sum(item.duration_ms for item in self._phases),
            warnings=tuple(warnings),
        )

    finalize = build_report

    def _warnings(self, summary: OperationTimingSummary) -> list[PerformanceWarning]:
        warnings: list[PerformanceWarning] = []
        if summary.full_inventory_scan_count > DEFAULT_REPEATED_SCAN_COUNT:
            warnings.append(
                PerformanceWarning(
                    code="repeated_full_inventory_scans",
                    message="The run performed more than one full inventory scan; reuse the preflight inventory.",
                    observed_value=summary.full_inventory_scan_count,
                    soft_target=1.0,
                )
            )
        if summary.piano_roll_preparation_count > 1:
            warnings.append(
                PerformanceWarning(
                    code="repeated_piano_roll_preparation",
                    message="Piano Roll preparation was repeated; retain the process-local arming state.",
                    observed_value=summary.piano_roll_preparation_count,
                    soft_target=1.0,
                )
            )
        if summary.preset_enumeration_count > 1:
            warnings.append(
                PerformanceWarning(
                    code="repeated_preset_enumeration",
                    message="Preset enumeration was repeated; reuse the bounded candidate inventory.",
                    observed_value=summary.preset_enumeration_count,
                    soft_target=1.0,
                )
            )
        if summary.target_refresh_count > self.excessive_target_refresh_count:
            warnings.append(
                PerformanceWarning(
                    code="excessive_target_refreshes",
                    message="Target-specific refreshes exceeded the configured soft bound.",
                    observed_value=summary.target_refresh_count,
                    soft_target=float(self.excessive_target_refresh_count),
                )
            )
        if summary.write_mode_transition_count > 1:
            warnings.append(
                PerformanceWarning(
                    code="unnecessary_write_mode_transitions",
                    message="The run changed write mode more than once; use one task-scoped transition.",
                    observed_value=summary.write_mode_transition_count,
                    soft_target=1.0,
                )
            )
        for phase in self._phases:
            target = self._phase_soft_targets.get(phase.phase, self.phase_soft_target_ms)
            if not phase.skipped and phase.duration_ms > target:
                warnings.append(
                    PerformanceWarning(
                        code="phase_soft_target_exceeded",
                        message=f"Phase {phase.phase!r} exceeded its soft timing target.",
                        phase=phase.phase,
                        observed_value=phase.duration_ms,
                        soft_target=target,
                    )
                )
        return warnings

    def set_phase_soft_target(self, phase: str, milliseconds: float) -> None:
        if not phase or len(phase) > 64 or milliseconds <= 0:
            raise ValueError("phase and positive soft target are required")
        self._phase_soft_targets[phase] = milliseconds


# Short alias retained for integrations that prefer the noun first.
TimingCollector = RunTimingCollector


__all__ = [
    "OperationTimingSummary",
    "PerformanceWarning",
    "PhaseTiming",
    "RunTimingCollector",
    "RunTimingReport",
    "SetupRecoveryTiming",
    "TimingCollector",
]
