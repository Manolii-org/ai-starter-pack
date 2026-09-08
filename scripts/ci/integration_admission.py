#!/usr/bin/env python3
"""Plan deterministic integration admission without executing repository commands.

The planner is intentionally standard-library-only. It validates the portable JSON
configuration, expands affected-surface dependencies, computes deterministic contract and
per-surface input-closure digests, and accepts provenance-bound evidence. A caller executes
the returned lane commands; this module never evaluates PR-authored strings as shell code.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ENGINE_VERSION = "1"


class AdmissionError(ValueError):
    """Configuration or evidence cannot safely produce an admission plan."""


def canonical_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _patterns(value: Any, where: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise AdmissionError(f"{where} must be {'a non-empty' if nonempty else 'an'} array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise AdmissionError(f"{where} entries must be non-empty strings")
    if len(value) != len(set(value)):
        raise AdmissionError(f"{where} contains duplicates")
    return value


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdmissionError(f"cannot load config {path}: {exc}") from exc
    if not isinstance(config, dict) or config.get("schema_version") != SCHEMA_VERSION:
        raise AdmissionError(f"config schema_version must equal {SCHEMA_VERSION}")
    if set(config) - {
        "schema_version", "required_context", "unknown_paths", "global_invalidators", "surfaces"
    }:
        raise AdmissionError(f"unknown top-level keys: {sorted(set(config) - {'schema_version', 'required_context', 'unknown_paths', 'global_invalidators', 'surfaces'})}")
    if not isinstance(config.get("required_context"), str) or not config["required_context"].strip():
        raise AdmissionError("required_context must be a non-empty string")
    if config.get("unknown_paths", "block") not in {"block", "all"}:
        raise AdmissionError("unknown_paths must be block or all")
    config.setdefault("unknown_paths", "block")
    config.setdefault("global_invalidators", [])
    _patterns(config["global_invalidators"], "global_invalidators")
    surfaces = config.get("surfaces")
    if not isinstance(surfaces, dict) or not surfaces:
        raise AdmissionError("surfaces must be a non-empty object")
    names = set(surfaces)
    allowed = {"paths", "commands", "depends_on", "invalidates", "inputs", "lane", "working_directory"}
    for name, surface in surfaces.items():
        if not isinstance(name, str) or not name or not isinstance(surface, dict):
            raise AdmissionError("surface names must be non-empty strings with object values")
        if set(surface) - allowed:
            raise AdmissionError(f"surface {name} has unknown keys: {sorted(set(surface) - allowed)}")
        _patterns(surface.get("paths"), f"surfaces.{name}.paths", nonempty=True)
        _patterns(surface.get("commands"), f"surfaces.{name}.commands", nonempty=True)
        for key in ("depends_on", "invalidates", "inputs"):
            surface.setdefault(key, [])
            _patterns(surface[key], f"surfaces.{name}.{key}")
        surface.setdefault("lane", "default")
        surface.setdefault("working_directory", ".")
        if not isinstance(surface["lane"], str) or not surface["lane"]:
            raise AdmissionError(f"surfaces.{name}.lane must be a non-empty string")
        if not isinstance(surface["working_directory"], str) or not surface["working_directory"]:
            raise AdmissionError(f"surfaces.{name}.working_directory must be a non-empty string")
        missing = (set(surface["depends_on"]) | set(surface["invalidates"])) - names
        if missing:
            raise AdmissionError(f"surface {name} references unknown surfaces: {sorted(missing)}")
    _reject_dependency_cycles(surfaces)
    return config


def _reject_dependency_cycles(surfaces: dict[str, Any]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise AdmissionError(f"surface dependency cycle includes {name}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in surfaces[name]["depends_on"]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in surfaces:
        visit(name)


def matches(path: str, pattern: str) -> bool:
    # fnmatch does not give `dir/**` the intuitive match for the directory itself.
    prefix = pattern[:-3] if pattern.endswith("/**") else None
    return fnmatch.fnmatchcase(path, pattern) or (prefix is not None and path == prefix)


def affected_surfaces(config: dict[str, Any], changed: list[str]) -> tuple[set[str], list[str]]:
    surfaces = config["surfaces"]
    all_names = set(surfaces)
    if any(matches(path, pattern) for path in changed for pattern in config["global_invalidators"]):
        return all_names, []
    direct = {
        name
        for name, surface in surfaces.items()
        if any(matches(path, pattern) for path in changed for pattern in surface["paths"])
    }
    unknown = [
        path
        for path in changed
        if not any(matches(path, pattern) for surface in surfaces.values() for pattern in surface["paths"])
    ]
    if unknown and config["unknown_paths"] == "all":
        direct = all_names
        unknown = []
    closure = set(direct)
    changed_set = True
    while changed_set:
        before = set(closure)
        for name in list(closure):
            closure.update(surfaces[name]["depends_on"])
            closure.update(surfaces[name]["invalidates"])
        for name, surface in surfaces.items():
            if set(surface["depends_on"]) & closure:
                closure.add(name)
        changed_set = closure != before
    return closure, unknown


def git_files(root: Path) -> dict[str, tuple[str, str]]:
    proc = subprocess.run(
        ["git", "ls-files", "--stage", "-z"], cwd=root, capture_output=True, check=False
    )
    if proc.returncode:
        raise AdmissionError(proc.stderr.decode(errors="replace").strip() or "git ls-files failed")
    entries: dict[str, tuple[str, str]] = {}
    for item in proc.stdout.split(b"\0"):
        if not item:
            continue
        metadata, raw_path = item.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        if stage != "0":
            raise AdmissionError("unmerged index entries cannot produce reusable evidence")
        entries[raw_path.decode(errors="surrogateescape")] = (mode, oid)
    return entries


def file_digest(root: Path, patterns: list[str], files: dict[str, tuple[str, str]]) -> str:
    selected = sorted({path for path in files if any(matches(path, pattern) for pattern in patterns)})
    entries = []
    for relative in selected:
        mode, object_oid = files[relative]
        path = root / relative
        if mode in {"120000", "160000"}:
            content_identity = {"git_object": object_oid}
        elif path.is_file():
            content_identity = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        else:
            raise AdmissionError(f"tracked input is absent from the working tree: {relative}")
        entries.append({"path": relative, "mode": mode, **content_identity})
    return canonical_digest(entries)


def contract_digest(config: dict[str, Any]) -> str:
    return canonical_digest({"engine_version": ENGINE_VERSION, "config": config})


def surface_input_digest(
    root: Path, config: dict[str, Any], name: str,
    files: dict[str, tuple[str, str]],
) -> str:
    surfaces = config["surfaces"]
    influencers = {name}
    changed = True
    while changed:
        before = set(influencers)
        for current in list(influencers):
            influencers.update(surfaces[current]["depends_on"])
        for candidate, candidate_config in surfaces.items():
            if set(candidate_config["invalidates"]) & influencers:
                influencers.add(candidate)
        changed = influencers != before
    patterns = sorted({
        pattern
        for current in influencers
        for pattern in surfaces[current]["paths"] + surfaces[current]["inputs"]
    } | set(config["global_invalidators"]))
    surface = surfaces[name]
    return canonical_digest({
        "contract": contract_digest(config),
        "surface": name,
        "influencers": sorted(influencers),
        "commands": surface["commands"],
        "files": file_digest(root, patterns, files),
    })


@dataclass(frozen=True)
class EvidenceDecision:
    reusable: bool
    reason: str


def validate_evidence(
    evidence: dict[str, Any], *, repository: str, installation_id: str, tree_oid: str,
    contract: str, surface: str, input_digest: str, accepted_producers: set[str]
) -> EvidenceDecision:
    required = {
        "schema_version", "repository", "installation_id", "tree_oid", "test_contract_digest",
        "producer_version", "surface", "input_closure_digest", "verdict"
    }
    missing = required - set(evidence)
    if missing:
        return EvidenceDecision(False, f"missing fields: {sorted(missing)}")
    checks = (
        (evidence["schema_version"] == SCHEMA_VERSION, "schema version mismatch"),
        (evidence["repository"] == repository, "repository mismatch"),
        (str(evidence["installation_id"]) == installation_id, "installation mismatch"),
        (evidence["test_contract_digest"] == contract, "test contract mismatch"),
        (evidence["producer_version"] in accepted_producers, "producer not accepted"),
        (evidence["surface"] == surface, "surface mismatch"),
        (evidence["input_closure_digest"] == input_digest, "input closure mismatch"),
        (evidence["verdict"] == "executed_pass", "evidence is not an executed pass"),
    )
    for valid, reason in checks:
        if not valid:
            return EvidenceDecision(False, reason)
    mode = "exact tree" if evidence["tree_oid"] == tree_oid else "composed input closure"
    return EvidenceDecision(True, mode)


def build_plan(
    root: Path, config: dict[str, Any], changed: list[str], evidence_items: list[dict[str, Any]],
    *, repository: str, installation_id: str, tree_oid: str, accepted_producers: set[str]
) -> dict[str, Any]:
    affected, unknown = affected_surfaces(config, changed)
    contract = contract_digest(config)
    files = git_files(root)
    surfaces: dict[str, Any] = {}
    lanes: dict[str, list[dict[str, Any]]] = {}
    for name in sorted(config["surfaces"]):
        if name not in affected:
            surfaces[name] = {"verdict": "not_applicable", "reason": "surface not affected"}
            continue
        digest = surface_input_digest(root, config, name, files)
        decision = EvidenceDecision(False, "no evidence")
        for evidence in evidence_items:
            candidate = validate_evidence(
                evidence, repository=repository, installation_id=installation_id,
                tree_oid=tree_oid, contract=contract, surface=name, input_digest=digest,
                accepted_producers=accepted_producers,
            )
            if candidate.reusable:
                decision = candidate
                break
        if decision.reusable:
            verdict = "reused_exact_pass" if decision.reason == "exact tree" else "reused_composed_pass"
            surfaces[name] = {"verdict": verdict, "input_closure_digest": digest}
        else:
            surface = config["surfaces"][name]
            work = {
                "surface": name, "commands": surface["commands"],
                "working_directory": surface["working_directory"], "input_closure_digest": digest,
            }
            lanes.setdefault(surface["lane"], []).append(work)
            surfaces[name] = {"verdict": "execution_required", "input_closure_digest": digest}
    return {
        "schema_version": SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "required_context": config["required_context"],
        "repository": repository,
        "installation_id": installation_id,
        "tree_oid": tree_oid,
        "test_contract_digest": contract,
        "changed_files": changed,
        "unknown_files": unknown,
        "blocked": bool(unknown),
        "surfaces": surfaces,
        "lanes": lanes,
    }


def _read_evidence(directory: Path | None) -> list[dict[str, Any]]:
    if directory is None or not directory.exists():
        return []
    result = []
    for path in sorted(directory.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AdmissionError(f"invalid evidence {path}: {exc}") from exc
        if not isinstance(item, dict):
            raise AdmissionError(f"evidence {path} must be an object")
        result.append(item)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("contract")
    plan = sub.add_parser("plan")
    plan.add_argument("--changed-file", action="append", default=[])
    plan.add_argument("--changed-files-json", type=Path)
    plan.add_argument("--evidence-dir", type=Path)
    plan.add_argument("--repository", required=True)
    plan.add_argument("--installation-id", required=True)
    plan.add_argument("--tree-oid", required=True)
    plan.add_argument("--accepted-producer", action="append", required=True)
    plan.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "contract":
            print(contract_digest(config))
            return 0
        changed = list(args.changed_file)
        if args.changed_files_json:
            extra = json.loads(args.changed_files_json.read_text(encoding="utf-8"))
            if not isinstance(extra, list) or any(not isinstance(item, str) for item in extra):
                raise AdmissionError("changed-files JSON must be an array of strings")
            changed.extend(extra)
        result = build_plan(
            args.root.resolve(), config, sorted(set(changed)), _read_evidence(args.evidence_dir),
            repository=args.repository, installation_id=args.installation_id,
            tree_oid=args.tree_oid, accepted_producers=set(args.accepted_producer),
        )
        payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(payload, encoding="utf-8")
        else:
            print(payload, end="")
        return 2 if result["blocked"] else 0
    except (AdmissionError, OSError, json.JSONDecodeError) as exc:
        print(f"integration-admission: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
