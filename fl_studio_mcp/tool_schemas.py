"""Advertise compact MCP tool schemas without loosening argument validation.

Every tool argument is still parsed by its full pydantic model with unknown
fields rejected, and every result is built from its full model. This module
only shapes the input and output JSON Schemas that clients receive from
``tools/list``. Many clients load that listing into the model's context in
full, and before these reductions the four Production Run tools advertised
roughly 240 KB of input schema each.

The reductions, applied to every input and output schema:

* pydantic's generated ``title`` keywords are dropped; property and definition
  names already carry them;
* a union whose members share every field apart from their ``operation``
  constant becomes one object with an ``operation`` enum, which is exact; and
* the Production Run operation union, whose members take different fields, is
  advertised as an object with an ``operation`` enum and the fields every
  operation shares. ``describe_operations`` returns the exact schema of any
  operation on demand, and the server still validates the full union.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .production_runs import ProductionOperation, UnavailableProductionOperation


DESCRIBE_OPERATIONS_TOOL = "run_describe_operations"
MAX_DESCRIBED_OPERATIONS = 8

# Keywords whose value is one subschema, a list of them, or a name -> subschema
# map. Everything else (examples, defaults, enums, const values) is data and
# must never be rewritten.
_SUBSCHEMA_KEYS = (
    "items",
    "additionalProperties",
    "not",
    "if",
    "then",
    "else",
    "contains",
    "propertyNames",
    "unevaluatedItems",
    "unevaluatedProperties",
)
_SUBSCHEMA_LIST_KEYS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SUBSCHEMA_MAP_KEYS = ("properties", "patternProperties", "$defs", "dependentSchemas")
_DEF_PREFIX = "#/$defs/"


def _operation_classes() -> tuple[type[BaseModel], ...]:
    union = get_args(ProductionOperation)[0]
    return tuple(get_args(union))


def _operation_names(model: type[BaseModel]) -> tuple[str, ...]:
    return tuple(get_args(model.model_fields["operation"].annotation))


PRODUCTION_OPERATIONS: dict[str, type[BaseModel]] = {
    name: model
    for model in _operation_classes()
    if model is not UnavailableProductionOperation
    for name in _operation_names(model)
}
UNAVAILABLE_PRODUCTION_OPERATIONS = _operation_names(UnavailableProductionOperation)
_ALL_PRODUCTION_OPERATION_NAMES = frozenset(PRODUCTION_OPERATIONS) | frozenset(
    UNAVAILABLE_PRODUCTION_OPERATIONS
)

ProductionOperationName = Literal[
    "generate_chord_progression",
    "generate_melody",
    "generate_bassline",
    "generate_drums",
    "adapt_note_sequence",
    "prepare_pattern",
    "select_pattern",
    "write_note_sequence",
    "transform_piano_roll",
    "add_section_markers",
    "record_automation_value",
    "apply_verified_batch",
    "plan_sound_palette",
    "apply_sound_palette",
    "create_sound_palette_variation",
    "select_plugin_preset",
    "inspect_drum_map",
    "select_drum_kit",
    "record_sound_feedback",
    "plan_processing",
    "apply_processing_plan",
    "apply_semantic_plugin_action",
    "start_review_session",
    "attach_review_assets",
    "evaluate_creation",
    "record_creation_feedback",
    "plan_creation_revision",
    "apply_creation_revision",
    "compare_revision_bounces",
    "create_playlist_handoff",
    "create_delivery_manifest",
]
if set(get_args(ProductionOperationName)) != set(PRODUCTION_OPERATIONS):
    raise RuntimeError("ProductionOperationName must list every available operation")


def _transform(node: Any, visit) -> Any:
    """Rebuild a schema bottom-up, calling ``visit`` on every schema object."""

    if not isinstance(node, dict):
        return node
    rebuilt: dict[str, Any] = {}
    for key, value in node.items():
        if key in _SUBSCHEMA_KEYS and isinstance(value, dict):
            rebuilt[key] = _transform(value, visit)
        elif key in _SUBSCHEMA_LIST_KEYS and isinstance(value, list):
            rebuilt[key] = [_transform(item, visit) for item in value]
        elif key in _SUBSCHEMA_MAP_KEYS and isinstance(value, dict):
            rebuilt[key] = {name: _transform(item, visit) for name, item in value.items()}
        else:
            rebuilt[key] = value
    return visit(rebuilt)


def _definition(root: dict[str, Any], reference: Any) -> dict[str, Any] | None:
    if not isinstance(reference, dict) or set(reference) != {"$ref"}:
        return None
    target = reference["$ref"]
    if not isinstance(target, str) or not target.startswith(_DEF_PREFIX):
        return None
    found = root.get("$defs", {}).get(target[len(_DEF_PREFIX):])
    return found if isinstance(found, dict) else None


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _operation_shape(definition: dict[str, Any]) -> str | None:
    """Return an object's shape without its operation constant, if it has one."""

    properties = definition.get("properties", {})
    if definition.get("type") != "object" or "const" not in properties.get("operation", {}):
        return None
    return _canonical(
        {
            "properties": {
                name: value for name, value in properties.items() if name != "operation"
            },
            "required": sorted(
                name for name in definition.get("required", []) if name != "operation"
            ),
            "additionalProperties": definition.get("additionalProperties"),
        }
    )


def _merge_uniform_operations(root: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Collapse union members that differ only in their operation constant.

    Members with the same fields become one object whose ``operation`` is an
    enum of their constants; any member with its own fields is kept as is.
    The result accepts exactly the inputs the original union accepted.
    """

    for keyword in ("oneOf", "anyOf"):
        members = node.get(keyword)
        if not isinstance(members, list) or len(members) < 2:
            continue
        groups: dict[str, list[tuple[Any, dict[str, Any]]]] = {}
        for member in members:
            definition = _definition(root, member)
            shape = None if definition is None else _operation_shape(definition)
            if definition is None or shape is None:
                groups = {}
                break
            groups.setdefault(shape, []).append((member, definition))
        if not groups or all(len(group) < 2 for group in groups.values()):
            continue
        rebuilt = []
        for group in groups.values():
            if len(group) == 1:
                rebuilt.append(group[0][0])
                continue
            merged = copy.deepcopy(group[0][1])
            merged.pop("description", None)
            merged["properties"]["operation"] = {
                "type": "string",
                "enum": [definition["properties"]["operation"]["const"] for _, definition in group],
            }
            required = [name for name in merged.get("required", []) if name != "operation"]
            merged["required"] = ["operation", *required]
            rebuilt.append(merged)
        rest = {key: value for key, value in node.items() if key not in {keyword, "discriminator"}}
        if len(rebuilt) == 1:
            return {**rebuilt[0], **rest}
        return {**rest, keyword: rebuilt}
    return node


def _compact_production_operations(root: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Advertise the heterogeneous Production Run union by name only."""

    discriminator = node.get("discriminator")
    if not isinstance(discriminator, dict) or discriminator.get("propertyName") != "operation":
        return node
    mapping = discriminator.get("mapping")
    if not isinstance(mapping, dict) or set(mapping) != _ALL_PRODUCTION_OPERATION_NAMES:
        return node
    shared: dict[str, Any] = {}
    first = _definition(root, {"$ref": next(iter(mapping.values()))})
    if first is not None:
        for name in ("operation_id", "after"):
            if name in first.get("properties", {}):
                shared[name] = copy.deepcopy(first["properties"][name])
    return {
        "type": "object",
        "description": (
            "One Production Run operation. Its other fields depend on `operation`: "
            f"call {DESCRIBE_OPERATIONS_TOOL} with the operation names you plan to use "
            "to get their exact fields. `after` lists earlier operation_ids this one "
            "waits for. A field can refer to an earlier operation's output with "
            '{"reference": "operation_output", "operation_id": ..., "output": ...}. '
            "save_project, render_project, insert_plugin and create_playlist_clip are "
            "not available in FL Studio's scripting API and are refused."
        ),
        "properties": {
            "operation": {"type": "string", "enum": sorted(PRODUCTION_OPERATIONS)},
            **shared,
        },
        "required": ["operation", "operation_id"],
        "additionalProperties": True,
    }


def _strip_title(node: dict[str, Any]) -> dict[str, Any]:
    if isinstance(node.get("title"), str):
        node = {key: value for key, value in node.items() if key != "title"}
    return node


def _references(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith(_DEF_PREFIX):
                found.add(value[len(_DEF_PREFIX):])
            elif key != "$defs":
                _references(value, found)
    elif isinstance(node, list):
        for value in node:
            _references(value, found)


def _prune_definitions(schema: dict[str, Any]) -> dict[str, Any]:
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return schema
    reachable: set[str] = set()
    pending: set[str] = set()
    _references({key: value for key, value in schema.items() if key != "$defs"}, pending)
    while pending:
        name = pending.pop()
        if name in reachable or name not in definitions:
            continue
        reachable.add(name)
        _references(definitions[name], pending)
    kept = {name: value for name, value in definitions.items() if name in reachable}
    result = {key: value for key, value in schema.items() if key != "$defs"}
    if kept:
        result["$defs"] = kept
    return result


def compact_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the advertised form of one tool's input or output schema."""

    root = schema
    root = _transform(root, lambda node: _compact_production_operations(schema, node))
    root = _prune_definitions(root)
    snapshot = root
    root = _transform(root, lambda node: _merge_uniform_operations(snapshot, node))
    root = _prune_definitions(root)
    return _transform(root, _strip_title)


class OperationDescription(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str
    summary: str
    required_fields: tuple[str, ...]
    json_schema: dict[str, Any] | None = Field(
        default=None,
        description="Exact JSON Schema for this operation; included only when requested.",
    )


class OperationCatalog(BaseModel):
    """Production Run operations, and the exact schema of the ones requested."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operations: tuple[OperationDescription, ...]
    unavailable_operations: tuple[str, ...]
    guidance: str


_CATALOG_GUIDANCE = (
    "Build a plan as {plan_id, operations: [...]}; every operation needs a unique "
    "operation_id and runs in order. Request exact schemas only for the operations "
    "you will use. Validate with run_validate when the user wants a plan "
    "reviewed, otherwise call run_execute directly; validation errors name "
    "the exact field to fix."
)


def _summary(model: type[BaseModel]) -> str:
    text = (model.__doc__ or "").strip()
    return text.splitlines()[0] if text else model.__name__


def describe_operations(
    operations: tuple[str, ...] = (),
) -> OperationCatalog:
    """List every operation and return exact schemas for the requested ones."""

    unknown = sorted(set(operations) - set(PRODUCTION_OPERATIONS))
    if unknown:
        raise ValueError(
            "unknown Production Run operation(s): %s" % ", ".join(unknown)
        )
    if len(set(operations)) > MAX_DESCRIBED_OPERATIONS:
        raise ValueError(
            "request at most %d operation schemas per call" % MAX_DESCRIBED_OPERATIONS
        )
    requested = set(operations)
    entries = []
    for name in sorted(PRODUCTION_OPERATIONS):
        model = PRODUCTION_OPERATIONS[name]
        entries.append(
            OperationDescription(
                operation=name,
                summary=_summary(model),
                required_fields=tuple(
                    field
                    for field, info in model.model_fields.items()
                    if info.is_required() and field != "operation"
                ),
                json_schema=(
                    compact_schema(TypeAdapter(model).json_schema())
                    if name in requested
                    else None
                ),
            )
        )
    return OperationCatalog(
        operations=tuple(entries),
        unavailable_operations=tuple(sorted(UNAVAILABLE_PRODUCTION_OPERATIONS)),
        guidance=_CATALOG_GUIDANCE,
    )
