#!/usr/bin/env python3
"""Validate entity-local release evidence; never approve or execute a deployment.

The owning trusted adapter authenticates evidence and observes the expected
subject. This offline contract is shared; provider commands stay in that adapter.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

HEX = re.compile(r"[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
NAME = re.compile(r"[a-zA-Z0-9_.:/@-]{1,200}\Z")


class Held(ValueError):
    """Incomplete, stale or incompatible evidence holds the affected release."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Held(message)


def strict_json(text: str) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise Held("non-JSON constant")

    result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    require(isinstance(result, dict), "release input must be an object")
    return result


def fields(value, required: set[str], optional: set[str] | None = None) -> None:
    require(isinstance(value, dict), "invalid release object")
    require(required <= value.keys() <= required | (optional or set()), "missing or unknown release fields")


def names(value, *, nonempty=False) -> list[str]:
    require(isinstance(value, list), "expected an explicit identity list")
    require(all(isinstance(v, str) and NAME.fullmatch(v) for v in value), "invalid release identity")
    require(len(value) == len(set(value)), "duplicate release identity")
    require(not nonempty or bool(value), "release identity list is empty")
    return value


def subject(value: dict) -> None:
    fields(value, {"entity", "release_group", "target", "sources", "artifacts",
                   "migration_digest", "policy_digest", "client_policy_digest", "baseline", "recovery"})
    for key in ("entity", "release_group", "target"):
        require(isinstance(value[key], str) and NAME.fullmatch(value[key]), "invalid release scope")
    for key, pattern in (("sources", SHA), ("artifacts", HEX)):
        require(isinstance(value[key], dict), "release sources/artifacts must be explicit maps")
        require(key != "sources" or bool(value[key]), "release has no source")
        for name, identity in value[key].items():
            require(isinstance(name, str) and NAME.fullmatch(name) and isinstance(identity, str)
                    and pattern.fullmatch(identity), "release requires exact source/artifact identities")
    for key in ("migration_digest", "policy_digest", "client_policy_digest"):
        require(isinstance(value[key], str) and HEX.fullmatch(value[key]), "release digest unavailable")
    require(isinstance(value["baseline"], dict), "observed baseline must be explicit")
    for name, identity in value["baseline"].items():
        require(isinstance(name, str) and NAME.fullmatch(name) and isinstance(identity, str)
                and NAME.fullmatch(identity), "invalid baseline identity")
    fields(value["recovery"], {"mode", "artifact_digest", "migration_digest"})
    require(value["recovery"]["mode"] in {"fix-forward", "restore-artifact", "none"}, "unknown recovery mode")
    for key in ("artifact_digest", "migration_digest"):
        digest = value["recovery"][key]
        require(digest is None or isinstance(digest, str) and HEX.fullmatch(digest), "invalid recovery digest")
    if value["recovery"]["mode"] == "restore-artifact":
        require(value["recovery"]["artifact_digest"] is not None, "named recovery artifact required")


def release_identity(bundle: dict) -> str:
    # Qualification runs/timestamps can change; the approved plan cannot.
    plan = {key: value for key, value in bundle.items() if key != "evidence"}
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate(bundle: dict, expected_subject: dict, phase: str, *, approved_release_id=None, now=None) -> dict:
    fields(bundle, {"schema_version", "subject", "supported_clients", "pending_migrations",
                    "features", "required_checks", "evidence"})
    require(type(bundle["schema_version"]) is int and bundle["schema_version"] == 1,
            "unsupported coordinated release contract")
    require(phase in {"qualify", "prepare", "activate"}, "unknown release phase")
    subject(bundle["subject"])
    subject(expected_subject)
    require(bundle["subject"] == expected_subject, "release subject changed; qualify and approve the new plan")
    clients = set(names(bundle["supported_clients"]))
    required = set(names(bundle["required_checks"], nonempty=True))
    evidence = bundle["evidence"]
    fields(evidence, {"subject", "release_id", "completed_at", "run_id", "run_attempt", "checks", "ready_components"})
    require(evidence["subject"] == expected_subject, "evidence belongs to another release subject")
    identity = release_identity(bundle)
    require(evidence["release_id"] == identity, "evidence belongs to another release plan")
    if phase != "qualify":
        require(approved_release_id == identity, "execution requires the exact approved release identity")
    for key in ("run_id", "run_attempt"):
        require(type(evidence[key]) is int and evidence[key] > 0, "exact evidence run/attempt required")
    try:
        completed = datetime.fromisoformat(evidence["completed_at"].replace("Z", "+00:00"))
        age = (now or datetime.now(timezone.utc)) - completed
    except (AttributeError, TypeError, ValueError):
        raise Held("invalid release evidence time") from None
    require(timedelta(minutes=-5) <= age <= timedelta(hours=24), "release evidence is stale or future dated")
    require(isinstance(evidence["checks"], list), "executable check evidence missing")
    checks = {}
    for check in evidence["checks"]:
        fields(check, {"id", "result", "clients"})
        names([check["id"]])
        require(check["id"] not in checks, "ambiguous check evidence")
        require(check["result"] in {"success", "failure", "skipped"}, "unknown check conclusion")
        names(check["clients"])
        checks[check["id"]] = check

    def passed(check_id: str, covered: set[str]) -> None:
        check = checks.get(check_id)
        require(check is not None and check["result"] == "success", f"required check did not succeed: {check_id}")
        require(covered <= set(check["clients"]), f"check omits supported clients: {check_id}")

    for check_id in required:
        passed(check_id, clients)
    require(isinstance(bundle["features"], list), "feature inventory missing")
    features = {}
    for feature in bundle["features"]:
        fields(feature, {"id", "active", "activate", "requires", "client_policy", "required_checks"})
        names([feature["id"]])
        require(feature["id"] not in features, "duplicate feature policy")
        require(type(feature["active"]) is bool and type(feature["activate"]) is bool,
                "feature activation state must be observed explicitly")
        dependencies = names(feature["requires"])
        require(set(dependencies) <= bundle["subject"]["sources"].keys(), "feature requires an unbound component")
        require(feature["client_policy"] in {"all-supported", "verified-targeting", "verified-minimum"},
                "explicit client behavior policy required")
        feature_checks = names(feature["required_checks"], nonempty=True)
        if feature["active"] or feature["activate"]:
            for check_id in feature_checks:
                passed(check_id, clients)
            if feature["client_policy"] != "all-supported":
                passed(feature["id"] + ":version-policy", clients)
                passed(feature["id"] + ":unknown-client-safe", clients)
        features[feature["id"]] = feature
    require(isinstance(evidence["ready_components"], dict), "component readiness evidence missing")
    for component, sha in evidence["ready_components"].items():
        require(component in bundle["subject"]["sources"] and sha == bundle["subject"]["sources"][component],
                "component readiness does not match the approved source")
    if phase == "activate":
        for feature in features.values():
            if feature["activate"]:
                require(set(feature["requires"]) <= evidence["ready_components"].keys(),
                        "activation requires verified component readiness")

    require(isinstance(bundle["pending_migrations"], list), "exact pending migration inventory missing")
    seen = set()
    for migration in bundle["pending_migrations"]:
        fields(migration, {"id", "sha256", "effect", "feature"})
        names([migration["id"]])
        require(migration["id"] not in seen, "duplicate pending migration")
        seen.add(migration["id"])
        require(isinstance(migration["sha256"], str) and HEX.fullmatch(migration["sha256"]), "unbound migration content")
        require(migration["effect"] in {"schema-preparation", "behavior-change", "feature-activation"},
                "unclassified database change")
        if migration["effect"] == "schema-preparation":
            require(migration["feature"] is None, "schema preparation cannot implicitly activate a feature")
        else:
            require(isinstance(migration["feature"], str) and migration["feature"] in features,
                    "database behavior change has no feature/client policy")
            feature = features[migration["feature"]]
            require(feature["active"] or feature["activate"], "database change contradicts disabled feature policy")
            if phase == "activate":
                require(set(feature["requires"]) <= evidence["ready_components"].keys(),
                        "database behavior requires verified component readiness")
            if migration["effect"] == "feature-activation":
                require(feature["activate"], "activation is absent from the approved release intent")
                require(phase != "prepare", "activation SQL must be separated from the preparation batch")
            elif phase == "prepare":
                require(not feature["requires"], "app-dependent behavior belongs after component readiness")
    return {"schema_version": 1, "release_id": release_identity(bundle), "phase": phase,
            "qualified": True, "approval_granted": False,
            "activation_features": sorted(f["id"] for f in features.values() if f["activate"])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--expected-subject", type=Path, required=True)
    parser.add_argument("--phase", choices=("qualify", "prepare", "activate"), required=True)
    parser.add_argument("--approved-release-id", help="Identity bound by the existing protected production decision")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing output")
    args = parser.parse_args(argv)
    try:
        for path in (args.bundle, args.expected_subject):
            require(path.stat().st_size <= 4 * 1024 * 1024, "release evidence exceeds size bound")
        result = validate(strict_json(args.bundle.read_text()), strict_json(args.expected_subject.read_text()), args.phase,
                          approved_release_id=args.approved_release_id)
        text = json.dumps(result, sort_keys=True) + "\n"
        if args.output and not args.dry_run:
            args.output.write_text(text)
        print(text, end="")
        return 0
    except (Held, ValueError, OSError, TypeError, KeyError) as exc:
        print("coordinated release held: " + (str(exc) if isinstance(exc, Held) else type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
