#!/usr/bin/env python3
"""Build a deterministic, non-secret LiteLLM runtime attestation."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from check_litellm_product_contract import (  # noqa: E402
    DEFAULT_CONTRACT,
    DEFAULT_SCHEMA,
    load_json,
    profile_source_findings,
    schema_findings,
    semantic_findings,
)

_CONFIG_ENV_REF = re.compile(r"os\.environ/([A-Z][A-Z0-9_]*)")
_PYTHON_ENV_REF = re.compile(
    r"(?:os\.environ\.get|os\.getenv)\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"
    r"|os\.environ\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*\]"
)
_SAFE_ATTESTATION_KEYS = {
    "schema_version",
    "product_version",
    "profile",
    "source_revision",
    "config_sha256",
    "bundle_sha256",
    "aliases",
    "router_aliases",
    "callbacks",
    "environment_variable_names",
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def build_attestation(
    contract: dict[str, Any],
    schema: dict[str, Any],
    proxy_config: dict[str, Any],
    *,
    profile: str,
    source_dir: Path,
    source_revision: str,
) -> dict[str, Any]:
    errors = schema_findings(contract, schema)
    semantic_errors, _warnings = semantic_findings(contract)
    source_errors, _source_warnings = profile_source_findings(contract, profile, proxy_config)
    errors.extend(semantic_errors)
    errors.extend(source_errors)
    if errors:
        raise ValueError("; ".join(errors))

    settings = proxy_config.get("litellm_settings", {})
    callbacks = sorted(set(settings.get("callbacks", [])))
    callback_files: list[Path] = []
    for callback in callbacks:
        module = callback.split(".", 1)[0]
        path = source_dir / f"{module}.py"
        if path.is_file():
            callback_files.append(path)

    config_bytes = _canonical_bytes(proxy_config)
    bundle = hashlib.sha256(config_bytes)
    for path in sorted(callback_files):
        bundle.update(path.name.encode() + b"\0" + path.read_bytes())

    serialized_config = proxy_config_path_text(proxy_config)
    environment_names = set(_CONFIG_ENV_REF.findall(serialized_config))
    for path in callback_files:
        callback_source = path.read_text(encoding="utf-8")
        environment_names.update(
            name
            for match in _PYTHON_ENV_REF.findall(callback_source)
            for name in match
            if name
        )
    model_aliases = {
        item["model_name"]
        for item in proxy_config.get("model_list", [])
        if isinstance(item, dict) and isinstance(item.get("model_name"), str)
    }
    router_aliases = proxy_config.get("router_settings", {}).get("model_group_alias", {}) or {}
    payload = {
        "schema_version": 2,
        "product_version": contract["product_version"],
        "profile": profile,
        "source_revision": source_revision,
        "config_sha256": _sha256(config_bytes),
        "bundle_sha256": bundle.hexdigest(),
        "aliases": sorted(model_aliases | set(router_aliases)),
        "router_aliases": dict(sorted(router_aliases.items())),
        "callbacks": callbacks,
        # Names only: this includes secrets, feature flags and runtime settings.
        # Values are never read or serialized into the attestation.
        "environment_variable_names": sorted(environment_names),
    }
    if set(payload) != _SAFE_ATTESTATION_KEYS:
        raise ValueError("attestation field allowlist drift")
    return payload


def proxy_config_path_text(proxy_config: dict[str, Any]) -> str:
    """Return a stable text form used only to discover environment-variable names."""
    return yaml.safe_dump(proxy_config, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--proxy-config", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--source-revision")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        proxy_config = yaml.safe_load(args.proxy_config.read_text(encoding="utf-8"))
        if not isinstance(proxy_config, dict):
            raise ValueError(f"{args.proxy_config}: expected a YAML object")
        payload = build_attestation(
            load_json(args.contract),
            load_json(args.schema),
            proxy_config,
            profile=args.profile,
            source_dir=args.source_dir or args.proxy_config.parent,
            source_revision=args.source_revision or _git_revision(ROOT),
        )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
