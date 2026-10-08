import hashlib
import json
import math
import re
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft7Validator, SchemaError

from .types import AssignmentBoundary, Catalog, Context, Feature, ReleaseBundle

_SCHEMA = json.loads(files(__package__).joinpath("schema.json").read_text())
_VALIDATORS = {name: Draft7Validator({**_SCHEMA, "$ref": f"#/$defs/{name}"}) for name in _SCHEMA["$defs"]}

def validate(kind: str, value: Any) -> None:
    if not _VALIDATORS[kind].is_valid(value):
        raise ValueError(f"invalid {kind}")

def ascii_id(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is None:
        raise ValueError("invalid ASCII identifier")

def matches(value: Any, feature: Feature) -> bool:
    kind = feature["value_type"]
    if not _valid_json(value):
        return False
    if kind == "number" and type(value) not in (int, float):
        return False
    if kind in ("boolean", "string") and type(value) is not {"boolean": bool, "string": str}[kind]:
        return False
    constraints = [feature.get("value_schema", {})]
    if "allowed_values" in feature:
        constraints.append({"enum": feature["allowed_values"]})
    key = json.dumps({"allOf": constraints}, sort_keys=True, separators=(",", ":"))
    return _value_validator(key).is_valid(value)

@lru_cache(maxsize=256)
def _value_validator(key: str) -> Draft7Validator:
    return Draft7Validator(json.loads(key))

def _valid_json(value: Any, depth: int = 0) -> bool:
    if depth > 20:
        return False
    if value is None or type(value) in (bool, str):
        return True
    if type(value) is int:
        return abs(value) <= 9007199254740991
    if type(value) is float:
        return math.isfinite(value) and (not value.is_integer() or abs(value) <= 9007199254740991)
    if type(value) is list:
        return len(value) <= 1000 and all(_valid_json(v, depth + 1) for v in value)
    return type(value) is dict and len(value) <= 1000 and all(type(k) is str and _valid_json(v, depth + 1) for k, v in value.items())

def validate_catalog(catalog: Catalog) -> None:
    validate("catalog", catalog)
    heights: dict[str, int] = {}
    def walk(node: str, path: set[str]) -> int:
        if node in path or node not in catalog["features"] or len(path) > 32:
            raise ValueError("invalid ancestor graph")
        if node in heights:
            return heights[node]
        height = max([0] + [1 + walk(ancestor, path | {node}) for ancestor in catalog["features"][node]["ancestors"]])
        if height > 32:
            raise ValueError("ancestor depth exceeded")
        heights[node] = height
        return height
    for key, feature in catalog["features"].items():
        apps = feature.get("applications", catalog["applications"])
        shared = feature.get("experiment", {}).get("assignment_boundary")
        if set(apps) - set(catalog["applications"]) or (shared and set(shared["applications"]) - set(apps)):
            raise ValueError("invalid application boundary")
        if "value_schema" in feature:
            _value_schema(feature["value_schema"])
        if "allowed_values" in feature and not _valid_json(feature["allowed_values"]):
            raise ValueError("invalid allowed values")
        if not matches(feature["baseline"], feature) or not matches(feature["disabled_value"], feature) or \
                (feature["disabled_value"] is True):
            raise ValueError("invalid baseline or disabled value")
        walk(key, set())

def payload_of(bundle: ReleaseBundle, catalog: Catalog) -> dict[str, Any]:
    validate("bundle", bundle)
    raw = bundle["payload_bytes"].encode("utf-8")
    if bundle["scope"] != catalog["scope"] or bundle["catalog_revision"] != catalog["revision"] or \
            bundle["created_at"] >= bundle["expires_at"] or len(raw) > 1048576 or \
            hashlib.sha256(raw).hexdigest() != bundle["payload_sha256"]:
        raise ValueError("bundle binding mismatch")
    payload = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON number")))
    if type(payload) is not dict or set(payload) != {"features", "experiments", "savedGroups", "contextualBandits"} or \
            any(type(payload[k]) is not dict for k in ("features", "savedGroups", "contextualBandits")) or \
            type(payload["experiments"]) is not list or payload["experiments"] or payload["savedGroups"] or payload["contextualBandits"]:
        raise ValueError("incomplete or unsupported payload")
    if set(payload["features"]) != set(catalog["features"]):
        raise ValueError("incomplete feature catalog")
    for key, native in payload["features"].items():
        feature = catalog["features"][key]
        if type(native) is not dict or set(native) - {"defaultValue", "rules"} or "defaultValue" not in native or not matches(native["defaultValue"], feature):
            raise ValueError("invalid native feature")
        if type(native.get("rules", [])) is not list:
            raise ValueError("invalid rules")
        for rule in native.get("rules", []):
            _validate_rule(rule, feature)
            for field in ("hashVersion", "bucketVersion"):
                if field in rule:
                    rule[field] = int(rule[field])
    return payload

def _validate_rule(rule: Any, feature: Feature) -> None:
    allowed = {"force", "coverage", "seed", "hashVersion", "key", "variations", "weights", "meta", "bucketVersion", "hashAttribute", "condition"}
    if type(rule) is not dict or set(rule) - allowed:
        raise ValueError("unsupported rule capability")
    if "hashVersion" in rule and (type(rule["hashVersion"]) not in (int, float) or rule["hashVersion"] != 2):
        raise ValueError("invalid hash version")
    if "bucketVersion" in rule and (type(rule["bucketVersion"]) not in (int, float) or
                                   not 0 <= rule["bucketVersion"] <= 9007199254740991 or int(rule["bucketVersion"]) != rule["bucketVersion"]):
        raise ValueError("invalid bucket version")
    if "condition" in rule:
        _native_condition(rule["condition"])
    for name in ("seed", "key"):
        if name in rule:
            ascii_id(rule[name])
    if rule.get("hashAttribute", "id") != "id":
        raise ValueError("unsupported hash attribute")
    if "force" in rule and not matches(rule["force"], feature):
        raise ValueError("invalid forced value")
    if "variations" in rule:
        exp = feature.get("experiment")
        variations, weights, meta = rule["variations"], rule.get("weights"), rule.get("meta")
        if not exp or rule.get("key") != exp["key"] or rule.get("hashVersion") != 2 or "seed" not in rule or \
                rule.get("bucketVersion") != exp["epoch"] or type(variations) is not list or not 2 <= len(variations) <= 128 or \
                not all(matches(v, feature) for v in variations) or type(weights) is not list or len(weights) != len(variations) or \
                not all(type(w) in (int, float) and math.isfinite(w) and 0 <= w <= 1 for w in weights) or \
                abs(sum(weights) - 1) > 1e-9 or type(meta) is not list or len(meta) != len(variations):
            raise ValueError("invalid experiment")
        keys = []
        for item in meta:
            if type(item) is not dict or set(item) != {"key"}:
                raise ValueError("invalid metadata")
            ascii_id(item["key"])
            keys.append(item["key"])
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate variation key")
    if "coverage" in rule and (type(rule["coverage"]) not in (int, float) or not 0 <= rule["coverage"] <= 1 or rule.get("hashVersion") != 2 or "seed" not in rule):
        raise ValueError("invalid coverage")

def approval_message(bundle: ReleaseBundle) -> bytes:
    validate("bundle", bundle)
    s = bundle["scope"]
    values = ["feature-controls/release/v1", s["ecosystem_id"], s["deployment_id"], s["environment_id"], s["feature_namespace"],
              int(bundle["revision"]), int(bundle["catalog_revision"]), int(bundle["created_at"]), int(bundle["expires_at"]),
              bundle["provider_semantics"], bundle["payload_sha256"], bundle["approval_ref"]]
    return json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode("utf-8")

def attributes_of(context: Context) -> dict[str, Any]:
    graph = context["group_ancestors"]
    closures: dict[str, set[str]] = {}
    heights: dict[str, int] = {}
    def walk(key: str, path: set[str]) -> set[str]:
        if key in path or key not in graph or len(path) > 32:
            raise ValueError("invalid membership graph")
        if key in closures:
            return closures[key]
        result, height = {key}, 0
        for parent in graph[key]:
            result |= walk(parent, path | {key})
            height = max(height, 1 + heights[parent])
        if height > 32:
            raise ValueError("membership depth exceeded")
        closures[key], heights[key] = result, height
        return result
    for key in graph:
        walk(key, set())
    groups: set[str] = set()
    for key in context["groups"]:
        groups |= walk(key, set())
    return {"id": context["assignment_key"], "groups": sorted(groups), "roles": list(context["roles"]), "tenant": context["tenant_key"]}

def _native_condition(value: Any) -> None:
    if type(value) is not dict or not 1 <= len(value) <= 3:
        raise ValueError("invalid condition")
    for attribute, condition in value.items():
        if attribute not in ("groups", "roles", "tenant") or type(condition) is not dict or len(condition) != 1:
            raise ValueError("unsupported condition")
        operator, operand = next(iter(condition.items()))
        if attribute == "tenant":
            if operator not in ("$eq", "$ne"):
                raise ValueError("unsupported condition operator")
            ascii_id(operand)
        else:
            if operator not in ("$in", "$nin") or type(operand) is not list or not 1 <= len(operand) <= 128:
                raise ValueError("unsupported condition operator")
            for key in operand:
                ascii_id(key)
            if len(set(operand)) != len(operand):
                raise ValueError("duplicate condition value")

def _value_schema(value: Any, depth: int = 0) -> None:
    keywords = {"type", "enum", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "items", "properties", "required", "additionalProperties"}
    if depth > 8 or type(value) is not dict or not _valid_json(value) or len(json.dumps(value)) > 16384 or set(value) - keywords or \
            value.get("type") not in ("null", "boolean", "number", "integer", "string", "array", "object"):
        raise ValueError("unsupported value schema")
    if "enum" in value and (type(value["enum"]) is not list or not 1 <= len(value["enum"]) <= 128 or not _valid_json(value["enum"])):
        raise ValueError("invalid value enum")
    if ("additionalProperties" in value and value["additionalProperties"] is not False) or (value["type"] == "object" and value.get("additionalProperties") is not False):
        raise ValueError("object schema must be closed")
    allowed_by_type = {
        "null": set(), "boolean": set(), "number": {"minimum", "maximum"}, "integer": {"minimum", "maximum"},
        "string": {"minLength", "maxLength"}, "array": {"minItems", "maxItems", "items"},
        "object": {"properties", "required", "additionalProperties"},
    }
    structural = {"minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "items", "properties", "required", "additionalProperties"}
    if any(field in value and field not in allowed_by_type[value["type"]] for field in structural):
        raise ValueError("schema keyword does not apply to type")
    for field in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        if field in value and (type(value[field]) not in (int, float) or not math.isfinite(value[field])):
            raise ValueError("invalid value bound")
    if "properties" in value:
        if type(value["properties"]) is not dict or len(value["properties"]) > 64:
            raise ValueError("invalid schema properties")
        for key, child in value["properties"].items():
            ascii_id(key)
            _value_schema(child, depth + 1)
    if "items" in value:
        _value_schema(value["items"], depth + 1)
    try:
        Draft7Validator.check_schema(value)
    except SchemaError as error:
        raise ValueError("invalid value schema") from error

def boundary_of(feature: Feature, context: Context) -> AssignmentBoundary:
    boundary = feature.get("experiment", {}).get("assignment_boundary")
    if boundary and context["application_id"] in boundary["applications"]:
        return {"mode": "shared", "key": boundary["key"]}
    return {"mode": "application", "key": context["application_id"]}

def allocation_id(context: Context, boundary: AssignmentBoundary) -> str:
    scope = context["scope"]
    parts = [scope["ecosystem_id"], scope["deployment_id"], scope["environment_id"], scope["feature_namespace"], boundary["mode"], boundary["key"], context["assignment_key"]]
    return ".".join(f"{len(part)}.{part}" for part in parts)
