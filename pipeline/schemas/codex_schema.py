"""Small JSON Schema boundary used for Codex structured results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class SchemaError(ValueError):
    """A JSON value does not satisfy the supported schema subset."""

    path: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}: {self.detail}"


def _schema_object(value: JsonValue, path: str) -> JsonObject:
    if not isinstance(value, dict):
        raise SchemaError(path, "schema object required")
    return value


def validate_json(value: JsonValue, schema: JsonObject, path: str = "$") -> None:
    """Validate the deterministic subset emitted by repository schemas."""
    declared_type = schema.get("type")
    if isinstance(declared_type, str) and not _matches_type(value, declared_type):
        raise SchemaError(path, f"expected {declared_type}")
    if "const" in schema and value != schema["const"]:
        raise SchemaError(path, "const mismatch")
    choices = schema.get("enum")
    if isinstance(choices, list) and value not in choices:
        raise SchemaError(path, "enum mismatch")
    if isinstance(value, str):
        _validate_string(value, schema, path)
    if isinstance(value, list):
        _validate_array(value, schema, path)
    if isinstance(value, dict):
        _validate_object(value, schema, path)


def _matches_type(value: JsonValue, declared: str) -> bool:
    matches = {
        "array": isinstance(value, list),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "null": value is None,
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "object": isinstance(value, dict),
        "string": isinstance(value, str),
    }
    if declared not in matches:
        raise SchemaError("$schema", f"unsupported type {declared}")
    return matches[declared]


def _validate_string(value: str, schema: JsonObject, path: str) -> None:
    minimum = schema.get("minLength")
    maximum = schema.get("maxLength")
    if isinstance(minimum, int) and len(value) < minimum:
        raise SchemaError(path, "shorter than minLength")
    if isinstance(maximum, int) and len(value) > maximum:
        raise SchemaError(path, "longer than maxLength")


def _validate_array(value: list[JsonValue], schema: JsonObject, path: str) -> None:
    maximum = schema.get("maxItems")
    if isinstance(maximum, int) and len(value) > maximum:
        raise SchemaError(path, "longer than maxItems")
    item_schema = schema.get("items")
    if isinstance(item_schema, dict):
        for index, item in enumerate(value):
            validate_json(item, item_schema, f"{path}[{index}]")


def _validate_object(value: JsonObject, schema: JsonObject, path: str) -> None:
    properties = _schema_object(schema.get("properties", {}), f"{path}.properties")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise SchemaError(f"{path}.required", "string list required")
    missing = [item for item in required if item not in value]
    if missing:
        raise SchemaError(path, f"missing required property {missing[0]}")
    if schema.get("additionalProperties") is False:
        extras = sorted(set(value).difference(properties))
        if extras:
            raise SchemaError(path, f"unexpected property {extras[0]}")
    for name, item in value.items():
        child = properties.get(name)
        if isinstance(child, dict):
            validate_json(item, child, f"{path}.{name}")
