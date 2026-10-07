import hashlib
import json
import math
import re
from importlib.resources import files
from typing import Any

from jsonschema import Draft7Validator

from .types import Catalog, Feature, ReleaseBundle

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
    if kind == "json":
        try:
            json.dumps(value, allow_nan=False)
            return True
        except (TypeError, ValueError):
            return False
    if kind == "number":
        return type(value) in (int, float) and math.isfinite(value)
    return type(value) is {"boolean": bool, "string": str}[kind]

def validate_catalog(catalog: Catalog) -> None:
    validate("catalog", catalog)
    def walk(node: str, path: set[str]) -> None:
        if node in path or node not in catalog["features"] or len(path) > 32:
            raise ValueError("invalid ancestor graph")
        for ancestor in catalog["features"][node]["ancestors"]:
            walk(ancestor, path | {node})
    for key, feature in catalog["features"].items():
        if not matches(feature["baseline"], feature):
            raise ValueError("invalid baseline")
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
        if type(native) is not dict or "defaultValue" not in native or not matches(native["defaultValue"], feature):
            raise ValueError("invalid native feature")
        if type(native.get("rules", [])) is not list:
            raise ValueError("invalid rules")
        for rule in native.get("rules", []):
            _validate_rule(rule, feature)
    return payload

def _validate_rule(rule: Any, feature: Feature) -> None:
    allowed = {"force", "coverage", "seed", "hashVersion", "key", "variations", "weights", "meta", "bucketVersion", "hashAttribute"}
    if type(rule) is not dict or set(rule) - allowed:
        raise ValueError("unsupported rule capability")
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
    s = bundle["scope"]
    values = ["feature-controls/release/v1", s["namespace"], s["application"], s["environment"],
              bundle["revision"], bundle["catalog_revision"], bundle["created_at"], bundle["expires_at"],
              bundle["provider_semantics"], bundle["payload_sha256"], bundle["approval_ref"]]
    return json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
