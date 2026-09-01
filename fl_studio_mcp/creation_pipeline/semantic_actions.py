"""Application of semantic processing plans through existing verified setters.

The executor in this module is intentionally boring at the bridge boundary:
it receives callbacks and invokes each callback at most once.  It does not
discover plug-ins, invent parameter mappings, call FL Studio in a background
task, retry an ambiguous write, or attempt a rollback.  A Production Run can
therefore inject its existing :class:`VerifiedWriter` methods (or equivalent
Track B verified callbacks) without creating a second writer implementation.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol, TypeAlias

from ..track_b_contracts import ChannelGeneratorTarget, MixerEffectTarget
from .processing import (
    MAX_PROCESSING_ACTIONS,
    ProcessingActionReceipt,
    ProcessingPlan,
    ProcessingPlanReceipt,
    ResolvedSemanticControl,
    SemanticControlResolution,
    SemanticControlValue,
    SemanticPluginAction,
    resolve_semantic_control,
)


class UnknownMutationOutcome(RuntimeError):
    """A setter dispatched a write but could not establish its outcome."""


class StaleTarget(RuntimeError):
    """A target fingerprint no longer describes the intended target."""


class ChangedSession(RuntimeError):
    """The captured session fingerprint no longer describes the session."""


class VerifiedSetter(Protocol):
    """Protocol for one existing readback-verified setter callback."""

    def __call__(self, **kwargs: Any) -> Any: ...


SetterCallback: TypeAlias = Callable[..., Any]


class SemanticSetterCallbacks:
    """Optional callback bundle accepted by :class:`SemanticActionExecutor`.

    The callbacks are ordinary callables rather than writer instances.  This
    keeps the semantic layer independent from bridge transport and makes it
    possible for a Production Run to inject the exact verified writer methods
    it already uses.
    """

    def __init__(
        self,
        *,
        display: SetterCallback | None = None,
        option: SetterCallback | None = None,
        normalized: SetterCallback | None = None,
    ) -> None:
        self.display = display
        self.option = option
        self.normalized = normalized

    def get(self, setter: str) -> SetterCallback | None:
        short = {
            "fl_set_plugin_param_display": "display",
            "fl_set_plugin_param_option": "option",
            "fl_set_plugin_param": "normalized",
        }.get(setter, setter)
        value = getattr(self, short, None)
        return value if callable(value) else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _callbacks(value: SemanticSetterCallbacks | Mapping[str, SetterCallback] | Any) -> SemanticSetterCallbacks:
    if isinstance(value, SemanticSetterCallbacks):
        return value
    if isinstance(value, Mapping):
        aliases = {
            "display": "display",
            "option": "option",
            "normalized": "normalized",
            "fl_set_plugin_param_display": "display",
            "fl_set_plugin_param_option": "option",
            "fl_set_plugin_param": "normalized",
            "set_plugin_parameter_display": "display",
            "set_plugin_parameter_option": "option",
            "set_plugin_parameter": "normalized",
        }
        found: dict[str, SetterCallback | None] = {
            "display": None,
            "option": None,
            "normalized": None,
        }
        for key, callback in value.items():
            name = aliases.get(key)
            if name is not None and callable(callback):
                found[name] = callback
        return SemanticSetterCallbacks(**found)
    return SemanticSetterCallbacks(
        display=getattr(value, "set_plugin_parameter_display", None)
        or getattr(value, "fl_set_plugin_param_display", None),
        option=getattr(value, "set_plugin_parameter_option", None)
        or getattr(value, "fl_set_plugin_param_option", None),
        normalized=getattr(value, "set_plugin_parameter", None)
        or getattr(value, "fl_set_plugin_param", None),
    )


def _target_arguments(action: SemanticPluginAction) -> dict[str, Any]:
    target = action.target
    if isinstance(target, MixerEffectTarget):
        # ``allow_master`` on an observed target describes how it was safely
        # read, not an authorization to mutate the Master.  Copy the target
        # with the action's explicit authorization bit for writer APIs that
        # accept the target object directly.
        target_for_write = target.model_copy(update={"allow_master": action.allow_master})
        return {
            "target": target_for_write,
            "track_index": target.track_index,
            "slot_index": target.slot_index,
            "allow_master": action.allow_master,
        }
    if isinstance(target, ChannelGeneratorTarget):
        # Existing verified plug-in callbacks may use ``channel_index`` for a
        # generator target.  We pass it only when the callback accepts it;
        # filtering happens in _invoke_setter below.
        return {
            "target": target,
            "channel_index": target.channel_index,
            "allow_master": False,
        }
    raise TypeError("semantic actions require a supported plug-in target")


def _invoke_with_supported_kwargs(callback: SetterCallback, arguments: dict[str, Any]) -> Any:
    """Call a callback once, filtering only arguments it cannot accept.

    Signature inspection avoids a speculative call followed by a retry.  A
    callback's own ``TypeError`` is allowed to propagate to the executor and is
    treated as an unknown mutation outcome, preserving the no-replay rule.
    """

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(**arguments)
    parameters = signature.parameters
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return callback(
            **{
                name: value
                for name, value in arguments.items()
                if not name.startswith("_")
            }
        )
    accepted = {
        name: value
        for name, value in arguments.items()
        if name in parameters
        and parameters[name].kind
        in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }
    # A narrow fake or adapter can intentionally accept one semantic action
    # object instead of writer-shaped kwargs.
    if not accepted and len(parameters) == 1:
        parameter = next(iter(parameters.values()))
        if parameter.name in {"action", "request", "semantic_action"}:
            return callback(**{parameter.name: arguments["_semantic_action"]})
    return callback(**accepted)


def _invoke_setter(
    callback: SetterCallback,
    action: SemanticPluginAction,
) -> Any:
    control = action.resolution.control
    if control is None:
        raise ValueError("cannot invoke a setter for an unresolved semantic control")
    arguments = _target_arguments(action)
    arguments.update(
        {
            "parameter": control.parameter_index,
            "parameter_index": control.parameter_index,
            "target_value": control.display_value,
            "display_value": control.display_value,
            "option": control.option,
            "normalized_value": control.normalized_value,
            "session_fingerprint": action.session_fingerprint,
            "target_fingerprint": action.target_fingerprint,
            "_semantic_action": action,
        }
    )
    # The marker is for an action-object callback only; existing setters never
    # see private integration arguments.
    return _invoke_with_supported_kwargs(callback, arguments)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _outcome(value: Any) -> tuple[str, bool, bool | None]:
    """Classify a writer result without weakening its verification proof."""

    if value is None:
        return "unknown", False, None
    if type(value) is bool:
        return ("verified" if value else "unverified"), True, value
    outcome = _field(value, "outcome")
    explicit_known = _field(value, "outcome_known")
    verified = _field(value, "verified")
    if outcome == "unknown" or explicit_known is False or verified is None:
        return "unknown", False, None
    if outcome in {"unverified", "not_verified"}:
        return "unverified", True, False
    if outcome == "verified":
        return "verified", True, True
    if type(verified) is bool:
        return ("verified" if verified else "unverified"), True, verified
    return "unknown", False, None


def _guard_result(
    checker: Callable[..., Any] | None,
    action: SemanticPluginAction,
    *,
    kind: str,
) -> bool | None:
    if checker is None:
        return True
    arguments = {
        "action": action,
        "target": action.target,
        "target_fingerprint": action.target_fingerprint,
        "session_fingerprint": action.session_fingerprint,
    }
    try:
        signature = inspect.signature(checker)
    except (TypeError, ValueError):
        value = checker(action)
    else:
        parameters = signature.parameters
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
            value = checker(**arguments)
        else:
            accepted = {
                name: item
                for name, item in arguments.items()
                if name in parameters
                and parameters[name].kind
                in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
            }
            if accepted:
                value = checker(**accepted)
            elif len(parameters) == 2:
                value = checker(action.target, action.target_fingerprint)
            elif len(parameters) == 1:
                value = checker(
                    action.session_fingerprint if kind == "session" else action.target
                )
            else:
                value = checker()
    if type(value) is bool:
        return value
    if isinstance(value, str):
        expected = action.session_fingerprint if kind == "session" else action.target_fingerprint
        return expected is not None and value == expected
    if value is None:
        return None
    # A checker may return a model carrying ``current_fingerprint``.
    current = _field(value, "current_fingerprint")
    expected = action.session_fingerprint if kind == "session" else action.target_fingerprint
    if isinstance(current, str) and expected is not None:
        return current == expected
    return bool(value)


class SemanticActionExecutor:
    """Apply one immutable plan through injected verified callbacks."""

    def __init__(
        self,
        setter_callbacks: SemanticSetterCallbacks | Mapping[str, SetterCallback] | Any = None,
        *,
        callbacks: SemanticSetterCallbacks | Mapping[str, SetterCallback] | Any = None,
        session_checker: Callable[..., Any] | None = None,
        target_checker: Callable[..., Any] | None = None,
        max_actions: int = MAX_PROCESSING_ACTIONS,
    ) -> None:
        if setter_callbacks is None:
            setter_callbacks = callbacks
        if setter_callbacks is None:
            raise TypeError("setter_callbacks or callbacks is required")
        if isinstance(max_actions, bool) or not isinstance(max_actions, int):
            raise ValueError("max_actions must be an integer")
        if not 1 <= max_actions <= MAX_PROCESSING_ACTIONS:
            raise ValueError("max_actions is outside the bounded processing range")
        self.callbacks = _callbacks(setter_callbacks)
        self.session_checker = session_checker
        self.target_checker = target_checker
        self.max_actions = max_actions

    def apply(self, plan: ProcessingPlan) -> ProcessingPlanReceipt:
        if not isinstance(plan, ProcessingPlan):
            raise TypeError("plan must be a ProcessingPlan")
        actions = plan.actions[: min(plan.max_actions, self.max_actions)]
        results: list[ProcessingActionReceipt] = []
        raw_receipts: list[Any] = []
        by_id: dict[str, ProcessingActionReceipt] = {}
        stopped_on: str | None = None
        attempted = 0
        for action in actions:
            blocked_by = tuple(
                dependency
                for dependency in action.depends_on
                if dependency not in by_id or by_id[dependency].status != "verified"
            )
            if blocked_by:
                result = ProcessingActionReceipt(
                    action_id=action.action_id,
                    status="blocked",
                    outcome_known=True,
                    verified=False,
                    blocked_by=blocked_by,
                    warning="dependent processing stopped after an earlier stale, unknown, unverified, or unresolved outcome",
                )
                results.append(result)
                by_id[action.action_id] = result
                stopped_on = stopped_on or action.action_id
                continue
            if action.resolution.status != "resolved" or action.resolution.control is None:
                result = ProcessingActionReceipt(
                    action_id=action.action_id,
                    status="unresolved_control",
                    outcome_known=True,
                    verified=False,
                    warning=action.resolution.reason or "semantic control could not be resolved",
                )
                results.append(result)
                by_id[action.action_id] = result
                stopped_on = stopped_on or action.action_id
                continue
            if action.session_fingerprint is not None:
                try:
                    session_ok = _guard_result(
                        self.session_checker, action, kind="session"
                    )
                except Exception as error:  # noqa: BLE001 - guard failure is safe stop
                    session_ok = False
                    warning = f"session guard failed before mutation: {error}"
                else:
                    warning = "session fingerprint changed before mutation"
                if session_ok is not True:
                    result = ProcessingActionReceipt(
                        action_id=action.action_id,
                        status="stale_session",
                        outcome_known=True,
                        verified=False,
                        warning=warning,
                    )
                    results.append(result)
                    by_id[action.action_id] = result
                    stopped_on = stopped_on or action.action_id
                    continue
            if action.target_fingerprint is not None:
                try:
                    target_ok = _guard_result(
                        self.target_checker, action, kind="target"
                    )
                except Exception as error:  # noqa: BLE001 - guard failure is safe stop
                    target_ok = False
                    warning = f"target guard failed before mutation: {error}"
                else:
                    warning = "target fingerprint is stale before mutation"
                if target_ok is not True:
                    result = ProcessingActionReceipt(
                        action_id=action.action_id,
                        status="stale_target",
                        outcome_known=True,
                        verified=False,
                        warning=warning,
                    )
                    results.append(result)
                    by_id[action.action_id] = result
                    stopped_on = stopped_on or action.action_id
                    continue
            callback = self.callbacks.get(action.resolution.control.setter)
            if callback is None:
                result = ProcessingActionReceipt(
                    action_id=action.action_id,
                    status="missing_setter",
                    outcome_known=True,
                    verified=False,
                    warning=(
                        f"no injected verified setter is available for "
                        f"{action.resolution.control.setter}"
                    ),
                )
                results.append(result)
                by_id[action.action_id] = result
                stopped_on = stopped_on or action.action_id
                continue
            attempted += 1
            try:
                receipt = _invoke_setter(callback, action)
            except StaleTarget as error:
                result = ProcessingActionReceipt(
                    action_id=action.action_id,
                    status="stale_target",
                    outcome_known=True,
                    verified=False,
                    warning=f"setter rejected a stale target before mutation: {error}",
                )
                results.append(result)
                by_id[action.action_id] = result
                stopped_on = stopped_on or action.action_id
                continue
            except ChangedSession as error:
                result = ProcessingActionReceipt(
                    action_id=action.action_id,
                    status="stale_session",
                    outcome_known=True,
                    verified=False,
                    warning=f"setter rejected a changed session before mutation: {error}",
                )
                results.append(result)
                by_id[action.action_id] = result
                stopped_on = stopped_on or action.action_id
                continue
            except UnknownMutationOutcome as error:
                result = ProcessingActionReceipt(
                    action_id=action.action_id,
                    status="unknown",
                    outcome_known=False,
                    verified=None,
                    warning=(
                        "verified setter reported an unknown outcome; nothing was "
                        f"replayed or rolled back ({error})"
                    ),
                )
                results.append(result)
                by_id[action.action_id] = result
                stopped_on = stopped_on or action.action_id
                continue
            except Exception as error:  # noqa: BLE001 - post-dispatch outcome is unknown
                result = ProcessingActionReceipt(
                    action_id=action.action_id,
                    status="unknown",
                    outcome_known=False,
                    verified=None,
                    warning=(
                        "verified setter raised after dispatch; outcome is unknown; "
                        f"nothing was replayed or rolled back ({error})"
                    ),
                )
                results.append(result)
                by_id[action.action_id] = result
                stopped_on = stopped_on or action.action_id
                continue
            status, known, verified = _outcome(receipt)
            result = ProcessingActionReceipt(
                action_id=action.action_id,
                status=status,  # type: ignore[arg-type]
                outcome_known=known,
                verified=verified,
                receipt=receipt,
                warning=(
                    None
                    if status == "verified"
                    else "setter outcome was not verified; dependent operations will stop"
                ),
            )
            results.append(result)
            by_id[action.action_id] = result
            if receipt is not None:
                raw_receipts.append(receipt)
            if status != "verified":
                stopped_on = stopped_on or action.action_id
        stopped = stopped_on is not None or len(actions) < len(plan.actions)
        completed = not stopped and len(actions) == len(plan.actions)
        verified: bool | None
        if not results:
            verified = True
        elif any(item.status == "unknown" for item in results):
            verified = None
        else:
            verified = all(item.status == "verified" for item in results)
        outcome_known = not any(item.status == "unknown" for item in results)
        warnings = list(plan.warnings)
        warnings.append(
            "No semantic action was replayed or rolled back; earlier verified receipts are preserved."
        )
        if stopped:
            warnings.append("Dependent semantic processing stopped at the first unsafe outcome.")
        return ProcessingPlanReceipt(
            plan_id=plan.plan_id,
            requested_count=len(plan.actions),
            attempted_count=attempted,
            completed=completed,
            stopped=stopped,
            stopped_on=stopped_on,
            verified=verified,
            outcome_known=outcome_known,
            results=tuple(results),
            receipts=tuple(raw_receipts),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    # ``execute`` is an integration-friendly synonym; it still invokes the
    # same one-shot application path and therefore cannot introduce a replay.
    execute = apply


def apply_processing_plan(
    plan: ProcessingPlan,
    *,
    setter_callbacks: SemanticSetterCallbacks | Mapping[str, SetterCallback] | Any = None,
    callbacks: SemanticSetterCallbacks | Mapping[str, SetterCallback] | Any = None,
    session_checker: Callable[..., Any] | None = None,
    target_checker: Callable[..., Any] | None = None,
    max_actions: int = MAX_PROCESSING_ACTIONS,
) -> ProcessingPlanReceipt:
    """Convenience API for Production Runs and MCP integration callers."""

    return SemanticActionExecutor(
        setter_callbacks,
        callbacks=callbacks,
        session_checker=session_checker,
        target_checker=target_checker,
        max_actions=max_actions,
    ).apply(plan)


# Integration vocabulary aliases.  There is still one executor and one
# mutation path; these names only make the closed operation easy to discover.
ProcessingPlanExecutor = SemanticActionExecutor
SemanticProcessingExecutor = SemanticActionExecutor


def apply_semantic_plugin_action(
    action_or_plan: SemanticPluginAction | ProcessingPlan,
    *,
    setter_callbacks: SemanticSetterCallbacks | Mapping[str, SetterCallback] | Any = None,
    callbacks: SemanticSetterCallbacks | Mapping[str, SetterCallback] | Any = None,
    session_checker: Callable[..., Any] | None = None,
    target_checker: Callable[..., Any] | None = None,
    max_actions: int = MAX_PROCESSING_ACTIONS,
) -> ProcessingPlanReceipt:
    """Apply one semantic action or an already batched processing plan."""

    if isinstance(action_or_plan, SemanticPluginAction):
        action_or_plan = ProcessingPlan(
            plan_id=f"action-{action_or_plan.action_id}",
            request_id=action_or_plan.goal_id,
            completion_target="restrained_first_pass",
            actions=(action_or_plan,),
        )
    return apply_processing_plan(
        action_or_plan,
        setter_callbacks=setter_callbacks,
        callbacks=callbacks,
        session_checker=session_checker,
        target_checker=target_checker,
        max_actions=max_actions,
    )


__all__ = [
    "ChangedSession",
    "ProcessingPlanExecutor",
    "ProcessingActionReceipt",
    "ProcessingPlan",
    "ProcessingPlanReceipt",
    "SemanticActionExecutor",
    "SemanticProcessingExecutor",
    "SemanticSetterCallbacks",
    "ResolvedSemanticControl",
    "SemanticControlResolution",
    "SemanticControlValue",
    "StaleTarget",
    "UnknownMutationOutcome",
    "VerifiedSetter",
    "apply_processing_plan",
    "apply_semantic_plugin_action",
    "resolve_semantic_control",
]
