#!/usr/bin/env python3
"""Resolve a session capability manifest to the env-var NAMES it may hold.

Contract: docs/contracts/credential-capabilities.md. Names only — this
script never sees, fetches or prints a credential value. Fail-closed: an
unknown capability, a malformed manifest or a broken table exits 2.

Usage:
  python3 scripts/lib/agent_capabilities.py --manifest M [--table T]
      [--names-only | --report]
"""
import argparse
import json
import sys

VALID_SOURCES = {"env", "doppler", "broker", "mcp"}
VALID_RETRIEVAL = {"eager", "deferred"}
VALID_PRIVILEGE = {"standard", "managed", "privileged"}
VALID_MODES = {"capabilities", "legacy"}

CANONICAL_TABLE = {
    # capability: (secret_names, source, retrieval, privilege)
    "git-read": ([], "env", "eager", "standard"),
    "github-actions-read": (["GH_TOKEN"], "doppler", "deferred", "managed"),
    "github-actions-dispatch": ([], "broker", "deferred", "privileged"),
    "doppler-read": (["DOPPLER_TOKEN_PRD"], "doppler", "eager", "managed"),
    "mcp-knowledge": (["MCP_API_KEY"], "doppler", "eager", "managed"),
    "llm-proxy": (["LLM_API_KEY"], "doppler", "eager", "managed"),
    "llm-anthropic": (["ANTHROPIC_API_KEY"], "doppler", "eager", "managed"),
    "deploy-vercel": (["VERCEL_TOKEN"], "doppler", "deferred", "managed"),
    "deploy-fly": (["FLY_API_TOKEN"], "doppler", "deferred", "managed"),
    "db-admin-supabase": (["SUPABASE_ACCESS_TOKEN"], "doppler", "deferred", "managed"),
    "agent-telemetry": (
        ["MCP_FINANCIAL_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"],
        "doppler", "deferred", "managed"),
    "external-browser": (["BROWSERBASE_API_KEY"], "doppler", "deferred", "managed"),
    "billing-read": (["GH_BILLING_TOKEN"], "doppler", "deferred", "managed"),
}


def _fail(msg):
    print(f"capability-resolve error: {msg}", file=sys.stderr)
    return 2


def _load_json(path, label):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ValueError(f"{label} unreadable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TypeError(f"{label} must be a JSON object")
    return data


def _load_table(extra_path=None):
    table = {k: {"secret_names": list(v[0]), "source": v[1],
                 "retrieval": v[2], "privilege": v[3]}
             for k, v in CANONICAL_TABLE.items()}
    if extra_path:
        ext = _load_json(extra_path, "capability table")
        caps = ext.get("capabilities", ext)
        if not isinstance(caps, dict):
            raise TypeError("capability table: 'capabilities' must be an object")
        for name, spec in caps.items():
            if name in CANONICAL_TABLE:
                raise ValueError(
                    f"capability table: '{name}' redefines a canonical capability — "
                    "extensions must not narrow pack capabilities")
            if not isinstance(spec, dict) or "source" not in spec:
                raise ValueError(f"capability table: '{name}' missing required 'source'")
            source = spec["source"]
            if source not in VALID_SOURCES:
                raise ValueError(
                    f"capability table: '{name}' invalid source '{source}' "
                    f"(want one of {sorted(VALID_SOURCES)})")
            retrieval = spec.get("retrieval", "deferred")
            if retrieval not in VALID_RETRIEVAL:
                raise ValueError(
                    f"capability table: '{name}' invalid retrieval '{retrieval}'")
            privilege = spec.get("privilege", "managed")
            if privilege not in VALID_PRIVILEGE:
                raise ValueError(
                    f"capability table: '{name}' invalid privilege '{privilege}'")
            names = spec.get("secret_names", [])
            if (not isinstance(names, list)
                    or not all(isinstance(n, str) and n for n in names)):
                raise TypeError(
                    f"capability table: '{name}'.secret_names must be an array "
                    "of nonempty strings")
            if source == "broker" and names:
                raise ValueError(
                    f"capability table: '{name}' declares secret_names on a "
                    "broker source — broker capabilities dispatch, they never inject")
            table[name] = {
                "secret_names": list(names),
                "source": source,
                "retrieval": retrieval,
                "privilege": privilege,
            }
    return table


def resolve(manifest_path, table_path=None):
    """Return {env_name: {'retrieval':..., 'source':..., 'capability':...}}.
    Raises (TypeError, ValueError) on any contract violation."""
    manifest = _load_json(manifest_path, "manifest")
    table = _load_table(table_path)
    overrides = manifest.get("retrieval_overrides", {})
    if not isinstance(overrides, dict):
        raise TypeError("manifest.retrieval_overrides must be an object")
    for name, value in overrides.items():
        if value not in VALID_RETRIEVAL:
            raise ValueError(
                f"manifest.retrieval_overrides['{name}'] must be 'eager' or "
                f"'deferred', got '{value}'")

    mode = manifest.get("mode", "capabilities")
    if mode not in VALID_MODES:
        raise ValueError(
            f"manifest.mode must be 'capabilities' or 'legacy', got '{mode}'")
    if mode == "legacy":
        wanted = sorted(table)  # union — documented migration path only
    else:
        wanted = manifest.get("capabilities")
        if not isinstance(wanted, list) or not all(isinstance(c, str) for c in wanted):
            raise TypeError("manifest.capabilities must be a list of strings")

    out = {}
    for cap in wanted:
        spec = table.get(cap)
        if spec is None:
            raise ValueError(f"unknown capability '{cap}' — fail closed, no fallback")
        if spec["source"] == "broker":
            continue  # dispatch-only: injects nothing even if names were declared
        for name in spec["secret_names"]:
            mapping = {
                "source": spec["source"],
                "retrieval": overrides.get(name, spec["retrieval"]),
                "privilege": spec["privilege"],
                "capability": cap,
            }
            prior = out.get(name)
            if prior is not None:
                differing = {k for k in ("source", "retrieval", "privilege")
                             if prior[k] != mapping[k]}
                if differing:
                    raise ValueError(
                        f"conflicting mappings for '{name}' from capabilities "
                        f"'{prior['capability']}' and '{cap}' "
                        f"({', '.join(sorted(differing))} differ) — "
                        "manifest order must not decide")
                continue
            out[name] = mapping
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--table")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--names-only", action="store_true")
    group.add_argument("--report", action="store_true")
    args = ap.parse_args()

    try:
        resolved = resolve(args.manifest, args.table)
    except (TypeError, ValueError) as exc:
        return _fail(str(exc))

    if args.report:
        print(f"{'env name':<32} {'retrieval':<10} {'source':<9} {'privilege':<10} capability")
        for name in sorted(resolved):
            r = resolved[name]
            print(f"{name:<32} {r['retrieval']:<10} {r['source']:<9} {r['privilege']:<10} {r['capability']}")
        print(f"\n{len(resolved)} env names resolved ({sum(1 for r in resolved.values() if r['retrieval'] == 'eager')} eager)")
    else:
        for name in sorted(resolved):
            print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
