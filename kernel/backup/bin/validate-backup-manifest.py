#!/usr/bin/env python3
"""Validate a backup tenant manifest against the draft schema + cross-field rules.

Usage: validate-backup-manifest.py <manifest.yaml> [...]
Exit 0 = all valid; 1 = any invalid. Names/metadata only — this validator also
FAILS if any value looks like an inlined secret (defence in depth: manifests
must never carry secret values).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("ERROR: PyYAML required (pip install pyyaml)", file=sys.stderr)
    sys.exit(1)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "manifest" / "backup-tenant.schema.json"
CRON_RE = re.compile(r"^\S+ \S+ \S+ \S+ \S+$")
# Anything that looks like credential material rather than a name.
SECRETY_RE = re.compile(
    r"(dp\.(pt|st|sa)\.|postgres(ql)?://[^ ]*:[^ ]*@|AKIA[0-9A-Z]{16}|-----BEGIN|Bearer\s+\S{16,})"
)


def _schema_validate(manifest: dict, schema: dict, errors: list[str]) -> None:
    # Fail closed: without jsonschema the nested constraints (storage/systems/
    # monitoring/drill) cannot be enforced, and a shallow fallback would let
    # invalid manifests pass outside CI.
    try:
        import jsonschema
    except ImportError:
        errors.append(
            "jsonschema is required for full schema validation (pip install jsonschema) — refusing to validate without it"
        )
        return

    for err in sorted(
        jsonschema.Draft202012Validator(schema).iter_errors(manifest),
        key=lambda e: list(e.absolute_path),
    ):
        errors.append(f"schema: {'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}")


def _cross_field(manifest: dict, errors: list[str]) -> None:
    def walk(node, path="$"):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and SECRETY_RE.search(node):
            errors.append(f"secret-shaped value at {path} — manifests carry names only")

    walk(manifest)

    # Defensive typing throughout: this validator must REPORT structural
    # problems, never crash on them (schema errors are already collected above).
    def check_cron(path: str, cron) -> None:
        if cron is not None and not CRON_RE.match(str(cron)):
            errors.append(f"{path}: not a 5-field cron expression")

    drill = manifest.get("drill")
    if isinstance(drill, dict):
        check_cron("drill.cadence", drill.get("cadence"))

    systems = manifest.get("systems")
    if isinstance(systems, list):
        for i, s in enumerate(systems):
            if not isinstance(s, dict):
                continue
            split_exports = s.get("split_exports")
            if isinstance(split_exports, list):
                for j, se in enumerate(split_exports):
                    if isinstance(se, dict):
                        check_cron(f"systems[{i}].split_exports[{j}].cadence", se.get("cadence"))
        names = [s.get("name") for s in systems if isinstance(s, dict) and s.get("name") is not None]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            errors.append(f"duplicate system names: {sorted(dupes)}")


def validate_file(path: Path, schema: dict) -> list[str]:
    errors: list[str] = []
    try:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"yaml parse error: {exc}"]
    if not isinstance(manifest, dict):
        return ["manifest root must be a mapping"]
    _schema_validate(manifest, schema, errors)
    _cross_field(manifest, errors)
    return errors


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 1
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    rc = 0
    for arg in argv:
        errs = validate_file(Path(arg), schema)
        if errs:
            rc = 1
            for e in errs:
                print(f"FAIL {arg}: {e}", file=sys.stderr)
        else:
            print(f"OK   {arg}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
