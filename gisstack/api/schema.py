"""A small JSON-Schema validator (the subset the API needs) with no dependencies.

Supported keywords: type (one name or a list), enum, const, minimum, maximum, minLength, maxLength, pattern, items, minItems, maxItems,
properties, required, additionalProperties (bool or schema), nullable via type lists, $ref into `#/components/schemas/<Name>`.
The same dictionaries are embedded verbatim in the OpenAPI 3.1 document (which uses JSON Schema 2020-12), so validation and
documentation cannot drift apart."""
from __future__ import annotations

import re
from typing import Any, Optional

Schema = dict[str, Any]
_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}
MAX_DEPTH = 40


class SchemaError(ValueError):
    """The schema itself is malformed (a programming error, never user input)."""


def _resolve(schema: Schema, components: dict[str, Schema]) -> Schema:
    ref = schema.get("$ref")
    if ref is None:
        return schema
    m = re.fullmatch(r"#/components/schemas/([A-Za-z0-9_.-]+)", str(ref))
    if not m or m.group(1) not in components:
        raise SchemaError(f"unresolved $ref {ref!r}")
    return components[m.group(1)]


def validate(value: Any, schema: Schema, components: Optional[dict[str, Schema]] = None, path: str = "", _depth: int = 0) -> list[str]:
    """Return a list of human-readable problems (empty = valid).  Never raises for bad *data*."""
    comps = components or {}
    if _depth > MAX_DEPTH:
        return [f"{path or '$'}: nested too deeply"]
    schema = _resolve(schema, comps)
    here = path or "$"
    errors: list[str] = []
    t = schema.get("type")
    if t is not None:
        names = [t] if isinstance(t, str) else list(t)
        for n in names:
            if n not in _TYPES:
                raise SchemaError(f"unknown type {n!r}")
        if not any(_TYPES[n](value) for n in names):
            return [f"{here}: expected {' or '.join(names)}, got {type(value).__name__ if value is not None else 'null'}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{here}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{here}: must be one of {schema['enum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{here}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{here}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{here}: does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{here}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{here}: must be <= {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{here}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{here}: at most {schema['maxItems']} item(s)")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += validate(item, schema["items"], comps, f"{path}[{i}]", _depth + 1)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                errors.append(f"{path + '.' if path else ''}{k}: is required")
        for k, v in value.items():
            if k in props:
                errors += validate(v, props[k], comps, f"{path + '.' if path else ''}{k}", _depth + 1)
            else:
                extra = schema.get("additionalProperties", True)
                if extra is False:
                    errors.append(f"{path + '.' if path else ''}{k}: unknown field")
                elif isinstance(extra, dict):
                    errors += validate(v, extra, comps, f"{path + '.' if path else ''}{k}", _depth + 1)
    return errors


def coerce_query(args: dict[str, str], schema: Schema) -> tuple[dict[str, Any], list[str]]:
    """Query strings are text: convert to the declared scalar types (integer / number / boolean), then validate."""
    props = schema.get("properties", {})
    out: dict[str, Any] = {}
    errors: list[str] = []
    for k, raw in args.items():
        want = props.get(k, {}).get("type")
        want = want[0] if isinstance(want, list) else want
        try:
            if want == "integer":
                out[k] = int(raw)
            elif want == "number":
                out[k] = float(raw)
            elif want == "boolean":
                if raw.lower() not in ("true", "false", "1", "0"):
                    raise ValueError
                out[k] = raw.lower() in ("true", "1")
            else:
                out[k] = raw
        except ValueError:
            errors.append(f"{k}: expected {want}")
    if errors:
        return out, errors
    return out, validate(out, schema)
