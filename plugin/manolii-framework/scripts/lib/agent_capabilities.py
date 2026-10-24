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
import os
from pathlib import Path
import re
import subprocess
import sys

VALID_SOURCES = {"env", "doppler", "broker", "mcp"}
VALID_RETRIEVAL = {"eager", "deferred"}
VALID_PRIVILEGE = {"standard", "managed", "privileged"}
VALID_MODES = {"capabilities", "legacy"}

class RouteAdmissionError(ValueError):
    """The requested transport has no admitted consumer policy (no secrets)."""


def assessment_context(environ=None):
    """Local artifact provenance under an existing trusted Actions launcher.

    This is not provider execution acknowledgement or standalone authentication.
    The workflow restores producers from base, clears candidate files, and binds
    artifact transfer to this run. Candidate/model fields cannot choose the roster.
    Local legacy callers remain compatible; hosted missing context fails closed.
    """
    env = os.environ if environ is None else environ
    if env.get("GITHUB_ACTIONS") != "true":
        return None
    try:
        event = _load_json(env.get("GITHUB_EVENT_PATH", ""), "launch event")
        repository = event["repository"]["full_name"]
        head = event["pull_request"]["head"]["sha"]
        base = event["pull_request"]["base"]["sha"]
        run, attempt = env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_RUN_ATTEMPT", "")
        if (repository != env.get("GITHUB_REPOSITORY")
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or not all(isinstance(x, str) and re.fullmatch(r"[0-9a-f]{40}", x) for x in (head, base))
                or not all(isinstance(x, str) and re.fullmatch(r"[1-9][0-9]*", x) for x in (run, attempt))):
            raise ValueError("invalid context")
        return {"version": 1, "repository": repository, "head": head, "base": base,
                "run": run, "attempt": attempt}
    except (OSError, UnicodeError, TypeError, ValueError, KeyError, AttributeError):
        raise ValueError("assessment launcher context unavailable") from None


def bind_review_candidate(data):
    """Stamp runner-owned provenance after parsing; never trust model metadata."""
    context = assessment_context()
    if context is not None:
        data = {**data, "_assessment": context}
    return data


def selected_review_roles(environ=None):
    """Read both phase selections from trusted workflow control, not artifacts."""
    env = os.environ if environ is None else environ
    if env.get("GITHUB_ACTIONS") != "true":
        return None
    if any(key not in env for key in ("REVIEW_SKILLS", "REVIEW_AGENTS")):
        raise ValueError("assessment selected-role roster unavailable")
    result = set()
    for key in ("REVIEW_SKILLS", "REVIEW_AGENTS"):
        raw = env[key]
        if raw == "":
            continue
        roles = raw.split(",")
        if any(not re.fullmatch(r"[a-z][a-z0-9-]*", role) for role in roles):
            raise ValueError("assessment selected-role roster invalid")
        if result.intersection(roles) or len(set(roles)) != len(roles):
            raise ValueError("assessment selected-role roster ambiguous")
        result.update(roles)
    return result


def require_direct_llm_admission(repo_root=None, environ=None):
    """Admit the external-consumer lane before looking up any credential.

    `first_party` is a reviewer requirement, never spending authority. Estate
    and unknown consumers cannot use direct credits. An external launch must
    explicitly select llm-anthropic in the existing capability manifest and
    bind its external ownership declaration to the launcher's repository.
    Policy comes from the PR base (CI) or checked-in HEAD (local), never a PR
    working tree. This is local admission under a trusted launcher, not proof
    of account entitlement or a security boundary against a hostile shell.
    No manifest is created and no credential is read by this function.
    """
    env = os.environ if environ is None else environ
    repository = env.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RouteAdmissionError("direct review denied: missing consumer repository evidence")
    root = Path.cwd() if repo_root is None else Path(repo_root)
    revision = "HEAD"
    if env.get("GITHUB_ACTIONS") == "true":
        try:
            event = _load_json(env.get("GITHUB_EVENT_PATH", ""), "launch event")
            if event["repository"]["full_name"].lower() != repository.lower():
                raise ValueError("repository mismatch")
            revision = (event["pull_request"]["base"]["sha"] if "pull_request" in event
                        else env.get("GITHUB_SHA", ""))
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
                raise ValueError("missing immutable policy revision")
        except (OSError, UnicodeError, TypeError, ValueError, KeyError, AttributeError):
            raise RouteAdmissionError("direct review denied: invalid launcher policy provenance") from None
    def read_git(*args):
        try:
            result = subprocess.run(
                ["git", "--no-pager", "--no-replace-objects", "--no-lazy-fetch",
                 "-C", str(root), "-c", "core.fsmonitor=false", *args],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
                # Never pass provider credentials or allow implicit object fetch.
                env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1",
                     "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.SubprocessError):
            raise RouteAdmissionError("direct review denied: policy source unavailable") from None
        if result.returncode:
            raise RouteAdmissionError("direct review denied: policy source unavailable")
        return result.stdout

    paths = {".ai/capabilities.json", "config/agent-capabilities.json"}
    # Prove absence with a successful immutable-tree enumeration. A failed
    # blob read must never mean "absent" after another policy was admitted.
    entries = read_git("ls-tree", "-rz", revision, "--", *sorted(paths))
    if not entries:
        raise RouteAdmissionError("direct review denied: missing committed capability policy evidence")
    for entry in entries.rstrip(b"\0").split(b"\0"):
        try:
            metadata, path = entry.decode("ascii").split("\t", 1)
            mode, kind, oid = metadata.split()
            if (path not in paths or mode not in {"100644", "100755"} or kind != "blob"
                    or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid)):
                raise ValueError("invalid policy tree entry")
            manifest = json.loads(read_git("cat-file", "blob", oid).decode("utf-8"))
            resolve_manifest(manifest)
            admitted = (
                type(manifest.get("version")) is int and manifest["version"] == 1
                and manifest.get("mode") == "capabilities"
                and "llm-anthropic" in manifest.get("capabilities", [])
                and manifest.get("consumer_scope") == "external"
                and manifest.get("consumer_repository", "").lower() == repository.lower()
            )
        except (UnicodeError, TypeError, ValueError, AttributeError):
            admitted = False
        if not admitted:
            raise RouteAdmissionError("direct review denied: invalid or conflicting consumer policy evidence")

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
    return resolve_manifest(manifest, table_path)


def resolve_manifest(manifest, table_path=None):
    """Validate an already-loaded manifest with the same name-only resolver."""
    if not isinstance(manifest, dict):
        raise TypeError("manifest must be a JSON object")
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
        if "llm-proxy" in wanted and "llm-anthropic" in wanted:
            raise ValueError(
                "manifest selects both 'llm-proxy' and 'llm-anthropic' — the "
                "LLM lanes are mutually exclusive per consumer")

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
