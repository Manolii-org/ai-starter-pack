#!/usr/bin/env python3
"""Validate the shared LiteLLM product contract without reading secret values."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTRACT = ROOT / "config/litellm-product-contract.json"
DEFAULT_SCHEMA = ROOT / "schemas/litellm-product-contract.schema.json"
DEFAULT_PROXY_CONFIG = ROOT / "deploy/litellm-proxy/config.yaml"

MODULE_CALLBACKS = {
    "advisor_guard": {"sonnet_advisor_guardrail.sonnet_advisor_guardrail"},
    "authentication_fail_fast": {"auth_fail_fast.auth_fail_fast"},
    "budget_guard": {"budget_guard.budget_guard"},
    "cache_affinity": {"cache_affinity_inject.cache_affinity_inject"},
    "data_sensitivity_guard": {"data_sensitivity_guard.data_sensitivity_guard"},
    "openai_compat_sanitize": {"openai_compat_sanitize.openai_compat_sanitize"},
    "product_attestation": {"product_attestation.product_attestation"},
    "stream_monitoring": {"stream_velocity_monitor.stream_velocity_monitor"},
    "usage_telemetry": {"usage_telemetry.canonical_usage_telemetry"},
}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def schema_findings(contract: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    findings = []
    for error in sorted(validator.iter_errors(contract), key=lambda item: list(item.path)):
        location = ".".join(str(part) for part in error.path) or "<root>"
        findings.append(f"schema {location}: {error.message}")
    return findings


def semantic_findings(contract: dict[str, Any], *, today: date | None = None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    today = today or date.today()
    profiles = contract.get("profiles", {})
    modules = contract.get("modules", {})
    extensions = contract.get("extensions", {})

    for name, profile in profiles.items():
        enabled = set(profile.get("enabled_modules", []))
        gaps = set(profile.get("known_module_gaps", []))
        unknown = (enabled | gaps) - set(modules)
        if unknown:
            errors.append(f"profile {name}: unknown modules {sorted(unknown)}")
        overlap = enabled & gaps
        if overlap:
            errors.append(f"profile {name}: modules cannot be enabled and gaps: {sorted(overlap)}")
        for extension in profile.get("extensions", []):
            if extension not in extensions:
                errors.append(f"profile {name}: unknown extension {extension!r}")
            elif extensions[extension].get("owner") != name:
                errors.append(f"profile {name}: extension {extension!r} has a different owner")
        if profile.get("kind") == "logical":
            runtime_name = profile.get("runtime_profile")
            runtime = profiles.get(runtime_name)
            if not runtime or runtime.get("kind") != "runtime":
                errors.append(f"profile {name}: runtime_profile must reference a runtime profile")
            if "deployment" in profile:
                errors.append(f"profile {name}: logical profiles must not declare deployment")
            if profile.get("model_policy") == "inherit_runtime_with_entity_attribution":
                attribution = profile.get("attribution", {})
                if not isinstance(attribution, dict) or not attribution.get("field") or not attribution.get("value"):
                    errors.append(
                        f"profile {name}: inherited runtime policy requires nonempty attribution field and value"
                    )
        elif "deployment" not in profile:
            errors.append(f"profile {name}: runtime profiles must declare deployment")
        elif "source" not in profile:
            errors.append(f"profile {name}: runtime profiles must declare source ownership")

    for alias, spec in contract.get("core_aliases", {}).items():
        for profile in spec.get("required_profiles", []):
            if profile not in profiles:
                errors.append(f"alias {alias}: unknown required profile {profile!r}")
        for module in spec.get("required_modules", []):
            if module not in modules:
                errors.append(f"alias {alias}: unknown required module {module!r}")
                continue
            for profile_name in spec.get("required_profiles", []):
                profile = profiles.get(profile_name, {})
                accounted_for = set(profile.get("enabled_modules", [])) | set(
                    profile.get("known_module_gaps", [])
                )
                if module not in accounted_for:
                    errors.append(
                        f"alias {alias}: required module {module!r} is not enabled or a known gap "
                        f"for profile {profile_name}"
                    )

    for module, spec in modules.items():
        for profile_name in spec.get("mandatory_profiles", []):
            profile = profiles.get(profile_name)
            if profile is None:
                errors.append(f"module {module}: unknown mandatory profile {profile_name!r}")
                continue
            equivalents = {
                item.get("module") for item in profile.get("equivalent_controls", [])
            }
            if module not in profile.get("enabled_modules", []) and module not in profile.get("known_module_gaps", []) and module not in equivalents:
                errors.append(f"profile {profile_name}: mandatory module {module!r} is neither enabled nor a known gap")

    for profile_name, profile in profiles.items():
        equivalent_modules: set[str] = set()
        for control in profile.get("equivalent_controls", []):
            module = control.get("module")
            if module not in modules:
                errors.append(f"profile {profile_name}: equivalent control references unknown module {module!r}")
            if module in equivalent_modules:
                errors.append(f"profile {profile_name}: duplicate equivalent control for {module!r}")
            equivalent_modules.add(module)
            review_by = date.fromisoformat(control["review_by"])
            if review_by < today:
                errors.append(
                    f"profile {profile_name}: equivalent control for {module!r} expired on {review_by.isoformat()}"
                )
        overlap = equivalent_modules & (
            set(profile.get("enabled_modules", [])) | set(profile.get("known_module_gaps", []))
        )
        if overlap:
            errors.append(
                f"profile {profile_name}: equivalent controls conflict with enabled/gap modules: {sorted(overlap)}"
            )

    extension_aliases = {
        profile_name: {
            alias
            for extension_name in profile.get("extensions", [])
            for alias in extensions.get(extension_name, {}).get("aliases", [])
        }
        for profile_name, profile in profiles.items()
    }
    core_aliases = set(contract.get("core_aliases", {}))
    for extension_name, extension in extensions.items():
        collisions = core_aliases & set(extension.get("aliases", []))
        if collisions:
            errors.append(
                f"extension {extension_name}: aliases collide with core aliases: {sorted(collisions)}"
            )
    exceptions_by_profile: dict[str, int] = {}
    for exception in contract.get("exceptions", []):
        profile_name = exception.get("profile")
        profile = profiles.get(profile_name)
        if profile is None:
            errors.append(f"exception {exception.get('id')}: unknown profile {profile_name!r}")
            continue
        exceptions_by_profile[profile_name] = exceptions_by_profile.get(profile_name, 0) + 1
        if profile.get("model_policy") != exception.get("policy"):
            errors.append(f"exception {exception.get('id')}: policy does not match profile {profile_name}")
        denied = set(profile.get("denied_providers", []))
        forbidden = denied & set(exception.get("providers", []))
        if forbidden:
            errors.append(f"exception {exception.get('id')}: providers are denied by profile {profile_name}: {sorted(forbidden)}")
        unowned = set(exception.get("aliases", [])) - extension_aliases.get(profile_name, set())
        if unowned:
            errors.append(f"exception {exception.get('id')}: aliases are not owned extensions: {sorted(unowned)}")
        review_by = date.fromisoformat(exception["review_by"])
        if review_by < today:
            errors.append(f"exception {exception.get('id')}: expired on {review_by.isoformat()}")

    for name, profile in profiles.items():
        if profile.get("model_policy") == "named_proprietary_exceptions" and not exceptions_by_profile.get(name):
            errors.append(f"profile {name}: named_proprietary_exceptions requires at least one exception")
        if profile.get("model_policy") == "named_proprietary_exceptions":
            declared = {
                alias
                for exception in contract.get("exceptions", [])
                if exception.get("profile") == name
                for alias in exception.get("aliases", [])
            }
            proprietary = {
                alias
                for extension_name in profile.get("extensions", [])
                for alias in extensions.get(extension_name, {}).get("proprietary_aliases", [])
            }
            if declared != proprietary:
                errors.append(
                    f"profile {name}: proprietary exception coverage differs "
                    f"(missing={sorted(proprietary - declared)}, extra={sorted(declared - proprietary)})"
                )
        for gap in profile.get("known_module_gaps", []):
            warnings.append(f"profile {name}: mandatory module gap remains: {gap}")
    return errors, warnings


def _required_aliases(contract: dict[str, Any], profile_name: str) -> set[str]:
    required = {
        alias
        for alias, spec in contract["core_aliases"].items()
        if profile_name in spec.get("required_profiles", [])
    }
    profile = contract["profiles"][profile_name]
    for extension_name in profile.get("extensions", []):
        required.update(contract["extensions"][extension_name]["aliases"])
    return required


def _module_present(module: str, proxy_config: dict[str, Any]) -> bool:
    settings = proxy_config.get("litellm_settings", {})
    callbacks = set(settings.get("callbacks", []))
    if module == "error_reporting":
        return bool(callbacks & {"sentry_init.sentry_init"}) or bool(settings.get("failure_callback"))
    expected = MODULE_CALLBACKS.get(module)
    return expected is not None and bool(callbacks & expected)


def _forbidden_proxy_alias(alias: str) -> bool:
    return alias == "restricted" or alias == "opus" or alias.startswith("claude-opus-")


def _model_identifier_is_forbidden(model: str) -> bool:
    return _forbidden_proxy_alias(model.rsplit("/", 1)[-1])


def _resolving_router_aliases(model_aliases: set[str], router_aliases: dict[str, str]) -> set[str]:
    resolved: set[str] = set()

    def resolves(alias: str, trail: set[str]) -> bool:
        if alias in model_aliases:
            return True
        if alias in trail or alias not in router_aliases:
            return False
        return resolves(router_aliases[alias], trail | {alias})

    for alias in router_aliases:
        if resolves(alias, set()):
            resolved.add(alias)
    return resolved


def _transport_provider(params: dict[str, Any]) -> str:
    explicit = params.get("custom_llm_provider")
    if isinstance(explicit, str) and explicit:
        api_base = str(params.get("api_base", ""))
        if "openrouter.ai" in api_base:
            return "openrouter"
        return explicit
    model = str(params.get("model", ""))
    return model.split("/", 1)[0] if "/" in model else ""


def profile_source_findings(
    contract: dict[str, Any], profile_name: str, proxy_config: dict[str, Any]
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    profile = contract["profiles"].get(profile_name)
    if profile is None:
        return [f"unknown profile {profile_name!r}"], warnings
    if profile.get("kind") != "runtime":
        return [f"profile {profile_name!r} is logical and has no independent proxy source"], warnings
    model_aliases = {
        item.get("model_name")
        for item in proxy_config.get("model_list", [])
        if isinstance(item, dict) and isinstance(item.get("model_name"), str)
    }
    router_settings = proxy_config.get("router_settings", {})
    if not isinstance(router_settings, dict):
        errors.append(f"profile {profile_name}: router_settings must be an object")
        router_settings = {}
    router_aliases = router_settings.get("model_group_alias", {})
    if not isinstance(router_aliases, dict):
        errors.append(f"profile {profile_name}: router_settings.model_group_alias must be an object")
        router_aliases = {}
    invalid_router_aliases = {
        str(alias): target
        for alias, target in router_aliases.items()
        if not isinstance(alias, str) or not isinstance(target, str)
    }
    if invalid_router_aliases:
        errors.append(f"profile {profile_name}: router aliases and targets must be strings")
    valid_router_aliases = {
        alias: target
        for alias, target in router_aliases.items()
        if isinstance(alias, str) and isinstance(target, str)
    }
    resolving_router_aliases = _resolving_router_aliases(model_aliases, valid_router_aliases)
    unresolved_router_aliases = set(valid_router_aliases) - resolving_router_aliases
    if unresolved_router_aliases:
        errors.append(
            f"profile {profile_name}: router aliases do not resolve to model groups: "
            f"{sorted(unresolved_router_aliases)}"
        )
    aliases = model_aliases | resolving_router_aliases
    forbidden_aliases = {
        str(alias)
        for alias in model_aliases
        | set(valid_router_aliases)
        | set(valid_router_aliases.values())
        if _forbidden_proxy_alias(str(alias))
    }
    if forbidden_aliases:
        errors.append(
            f"profile {profile_name}: proxy source contains forbidden no-AI or Opus aliases: "
            f"{sorted(forbidden_aliases)}"
        )

    model_entries = [
        item
        for item in proxy_config.get("model_list", [])
        if isinstance(item, dict) and isinstance(item.get("litellm_params"), dict)
    ]
    forbidden_backends = {
        str(item["litellm_params"].get("model"))
        for item in model_entries
        if isinstance(item["litellm_params"].get("model"), str)
        and _model_identifier_is_forbidden(item["litellm_params"]["model"])
    }
    if forbidden_backends:
        errors.append(
            f"profile {profile_name}: proxy source contains forbidden Opus backend models: "
            f"{sorted(forbidden_backends)}"
        )
    required = _required_aliases(contract, profile_name)
    missing = required - aliases
    if missing:
        errors.append(f"profile {profile_name}: proxy source is missing declared aliases: {sorted(missing)}")

    enabled = profile.get("enabled_modules", [])
    for module in enabled:
        if not _module_present(module, proxy_config):
            errors.append(f"profile {profile_name}: enabled module {module!r} has no source evidence")
    for module in profile.get("known_module_gaps", []):
        if _module_present(module, proxy_config):
            errors.append(f"profile {profile_name}: known module gap {module!r} is now present; update the contract")

    denied = set(profile.get("denied_providers", []))
    for item in model_entries:
        provider = _transport_provider(item["litellm_params"])
        if provider in denied:
            errors.append(f"profile {profile_name}: alias {item.get('model_name')!r} uses denied provider {provider!r}")

    if profile.get("model_policy") == "named_proprietary_exceptions":
        exception_by_alias = {
            alias: exception
            for exception in contract.get("exceptions", [])
            if exception.get("profile") == profile_name
            for alias in exception.get("aliases", [])
        }
        for item in model_entries:
            alias = item.get("model_name")
            exception = exception_by_alias.get(alias)
            if exception is None:
                continue
            params = item["litellm_params"]
            provider = _transport_provider(params)
            model = str(params.get("model", ""))
            family = exception["model_family"]
            allowed_models = {family, *(f"{candidate}/{family}" for candidate in exception["providers"])}
            if provider not in exception["providers"] or model not in allowed_models:
                errors.append(
                    f"profile {profile_name}: exception alias {alias!r} uses unapproved "
                    f"provider/model {provider!r}/{model!r}"
                )

    fallback_sources: set[str] = set()
    settings = proxy_config.get("litellm_settings", {})
    for key in ("fallbacks", "content_policy_fallbacks"):
        for entry in settings.get(key, []) or []:
            if isinstance(entry, dict):
                fallback_sources.update(str(alias) for alias in entry)
    for extension_name in profile.get("extensions", []):
        extension = contract["extensions"][extension_name]
        if extension.get("fallback_owner") == "application":
            overlap = set(extension["aliases"]) & fallback_sources
            if overlap:
                errors.append(
                    f"profile {profile_name}: application-owned aliases have proxy fallbacks: {sorted(overlap)}"
                )
    return errors, warnings


def manolii_source_findings(contract: dict[str, Any], proxy_config: dict[str, Any]) -> list[str]:
    """Backward-compatible helper for focused callers and existing tests."""
    errors, _warnings = profile_source_findings(contract, "manolii", proxy_config)
    return errors


def validate(
    contract_path: Path = DEFAULT_CONTRACT,
    schema_path: Path = DEFAULT_SCHEMA,
    proxy_config_path: Path = DEFAULT_PROXY_CONFIG,
    *,
    profile: str = "manolii",
    today: date | None = None,
) -> tuple[list[str], list[str]]:
    contract = load_json(contract_path)
    schema = load_json(schema_path)
    errors = schema_findings(contract, schema)
    if errors:
        return errors, []
    semantic_errors, warnings = semantic_findings(contract, today=today)
    proxy_config = yaml.safe_load(proxy_config_path.read_text(encoding="utf-8"))
    if not isinstance(proxy_config, dict):
        semantic_errors.append(f"{proxy_config_path}: expected a YAML object")
    else:
        source_errors, source_warnings = profile_source_findings(contract, profile, proxy_config)
        semantic_errors.extend(source_errors)
        warnings.extend(source_warnings)
    return semantic_errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--proxy-config", type=Path, default=DEFAULT_PROXY_CONFIG)
    parser.add_argument("--profile", default="manolii", help="Runtime profile represented by --proxy-config")
    parser.add_argument("--json", action="store_true", help="Emit one compact machine-readable result")
    args = parser.parse_args(argv)
    try:
        errors, warnings = validate(args.contract, args.schema, args.proxy_config, profile=args.profile)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "profile": args.profile, "errors": [str(exc)], "warnings": []}, separators=(",", ":")))
            return 2
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"ok": not errors, "profile": args.profile, "errors": errors, "warnings": warnings}, separators=(",", ":")))
        return 1 if errors else 0
    for warning in warnings:
        print(f"WARN: {warning}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"LiteLLM product contract OK ({len(warnings)} known gap warnings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
