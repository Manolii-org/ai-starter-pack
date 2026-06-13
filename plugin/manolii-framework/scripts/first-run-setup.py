#!/usr/bin/env python3
"""First-run interactive setup for the AI Starter Pack.

Asks 4 yes/no questions to configure optional features, then writes
.ai/setup-complete with the user's choices and next steps.

Also includes a required-secrets verifier (--verify-secrets flag or automatic
after setup) that checks pack-components.yml against the local environment
and fails loudly if required Doppler secrets are missing.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml  # PyYAML — optional; secrets verification degrades gracefully without it
except Exception:
    yaml = None

SETUP_COMPLETE_FILE = Path(".ai/setup-complete")
COMPONENTS_FILE = Path("pack-components.yml")

# Secrets each interactive setup choice requires (Codex review, PR #2185):
# the rendered contract reflects render-time flags, so keys for features
# enabled interactively must be merged into verification explicitly.
CHOICE_REQUIRED_KEYS = {
    "oss_routing": ["LITELLM_MASTER_KEY"],
    "langfuse_telemetry": ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"],
    "browserbase": ["BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID"],
    "remote_memory": ["MCP_API_KEY"],
}


def _parse_components_fallback(text: str) -> dict:
    """Minimal dependency-free parser for the MACHINE-EMITTED pack-components.yml.

    Understands only the exact shape the Copier template produces. Used when
    PyYAML is absent so the verifier still fails loudly instead of silently
    passing (Codex review, PR #2185). Hand-edited exotic YAML is out of scope —
    install PyYAML for full parsing.
    """
    rs = {"github": [], "doppler": {"project": "unknown", "keys": []}, "fly": []}
    instance = {"repo_vars": [], "required_secrets": rs}
    section = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "repo_vars:":
            section = "repo_vars"
        elif stripped == "github:":
            section = "github"
        elif stripped == "keys:":
            section = "doppler_keys"
        elif stripped == "fly:":
            section = "fly"
        elif stripped.startswith("project:"):
            val = stripped.split(":", 1)[1].strip().strip("\"'")
            rs["doppler"]["project"] = val or "unknown"
        elif stripped.startswith("- ") and section:
            item = stripped[2:].strip().strip("\"'")
            target = {
                "repo_vars": instance["repo_vars"],
                "github": rs["github"],
                "doppler_keys": rs["doppler"]["keys"],
                "fly": rs["fly"],
            }[section]
            target.append(item)
        elif stripped == "[]":
            pass  # empty list under the current section
        elif ":" in stripped:
            section = None  # any other mapping key closes the open list
    return {"instance": instance}


def ask_yn(prompt: str, default: bool = False) -> bool:
    """Prompt for a yes/no answer. Returns True for yes."""
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        try:
            answer = input(prompt + suffix + " ").strip().lower()
        except EOFError:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        if answer == "":
            return default
        print("  Please enter y or n.")


def print_separator():
    print("-" * 60)


def parse_env_file(path: Path) -> set[str]:
    """Parse a .env file and return set of KEY names with non-empty values.

    Skips blank lines and comments; splits on first '=' and strips quotes.
    Returns empty set if file doesn't exist.
    """
    if not path.exists():
        return set()

    keys = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes if present
            if value and value[0] in ('"', "'") and value[0] == value[-1]:
                value = value[1:-1]
            if key and value:
                keys.add(key)
    except OSError:
        # Unreadable .env (permissions/race) — treat as no local keys rather
        # than crashing first-run setup; missing keys surface in the report.
        return keys

    return keys


def verify_required_secrets(extra_doppler_keys=()) -> int:
    """Verify required secrets against environment and .env file.

    extra_doppler_keys: additional keys to verify beyond the rendered
    contract (derived from interactive setup choices).
    Returns 0 on success, 2 if any required keys are missing.
    Returns 0 (graceful degradation) only when pack-components.yml is absent.
    """
    # Graceful degradation: pre-1.2.0 render without pack-components.yml
    if not COMPONENTS_FILE.exists():
        print()
        print("  WARNING: pack-components.yml not found (pre-1.2.0 render)")
        print("  Secrets verification skipped.")
        print()
        return 0

    try:
        raw_text = COMPONENTS_FILE.read_text(encoding="utf-8")
    except OSError as e:
        print()
        print(f"  ERROR: Could not read {COMPONENTS_FILE}: {e}")
        print()
        return 2

    if yaml is None:
        # No skip: the verifier's contract is to fail loudly, so fall back to
        # the dependency-free parser for the machine-emitted format.
        print()
        print("  NOTE: PyYAML not installed — using built-in minimal parser")
        print("  (pip install pyyaml for full YAML support)")

    try:
        components_data = (yaml.safe_load(raw_text) if yaml is not None
                           else _parse_components_fallback(raw_text))
        # Defensive extraction: yaml.safe_load returns None for an empty file,
        # and hand-edited contracts may have unexpected shapes.
        if not isinstance(components_data, dict):
            components_data = {}
        instance = components_data.get("instance") or {}
        if not isinstance(instance, dict):
            instance = {}
        required_secrets = instance.get("required_secrets") or {}
        if not isinstance(required_secrets, dict):
            required_secrets = {}

        github_secrets = required_secrets.get("github")
        if not isinstance(github_secrets, list):
            github_secrets = []
        github_secrets = [str(s) for s in github_secrets]

        fly_secrets = required_secrets.get("fly")
        if not isinstance(fly_secrets, list):
            fly_secrets = []
        fly_secrets = [str(s) for s in fly_secrets]

        repo_vars = instance.get("repo_vars")
        if not isinstance(repo_vars, list):
            repo_vars = []
        repo_vars = [str(v) for v in repo_vars]

        doppler_info = required_secrets.get("doppler") or {}
        if not isinstance(doppler_info, dict):
            doppler_info = {}
        doppler_project = doppler_info.get("project", "unknown")
        doppler_keys = doppler_info.get("keys")
        if not isinstance(doppler_keys, list):
            doppler_keys = []
        doppler_keys = [str(k) for k in doppler_keys]
        if extra_doppler_keys:
            # De-duplicated merge of contract keys + setup-choice keys
            doppler_keys = list(dict.fromkeys([*doppler_keys, *extra_doppler_keys]))
    except Exception as e:
        print()
        print(f"  ERROR: Could not parse {COMPONENTS_FILE}: {e}")
        print()
        return 2

    # Parse .env file for local keys
    env_file_keys = parse_env_file(Path(".env"))

    # Print verification report
    print()
    print("=" * 60)
    print("  Required Secrets Verification")
    print("=" * 60)
    print()

    # Check doppler keys (locally verifiable)
    print("Doppler keys (from .env or environment):")
    print("-" * 60)
    missing_keys = []
    for key in doppler_keys:
        val = os.environ.get(key)
        is_set = bool(val.strip()) if val is not None else (key in env_file_keys)
        status = "OK" if is_set else "MISSING"
        print(f"  {key:<40} [{status}]")
        if not is_set:
            missing_keys.append(key)
    print()

    # Print GitHub secrets (manual checklist)
    if github_secrets:
        print("GitHub secrets (check in repository Settings → Secrets):")
        print("-" * 60)
        for secret in github_secrets:
            print(f"  {secret}")
        print()

    # Print Fly secrets (manual checklist)
    if fly_secrets:
        print(f"Fly secrets (check in fly.io dashboard for app; from {doppler_project}):")
        print("-" * 60)
        for secret in fly_secrets:
            print(f"  {secret}")
        print()

    # Print repo vars (manual checklist)
    if repo_vars:
        print("GitHub repository variables (check in repository Settings → Variables):")
        print("-" * 60)
        for var in repo_vars:
            print(f"  {var}")
        print()

    # Failure case: missing doppler keys
    if missing_keys:
        print("=" * 60)
        print("  SETUP INCOMPLETE — Missing Required Secrets")
        print("=" * 60)
        print()
        print("The following Doppler keys are missing:")
        print()
        for key in missing_keys:
            print(f"  {key}")
        print()
        print("Add them to .env or export as environment variables:")
        print()
        print("  .env format:      KEY=value")
        print("  Environment:      export KEY=value")
        print()
        print(f"Doppler project: {doppler_project}")
        print("Retrieve values: https://dashboard.doppler.com (or 'doppler run')")
        print()
        return 2

    # Success case
    print("=" * 60)
    print("  All Required Secrets Verified ✓")
    print("=" * 60)
    print()
    return 0


def run_setup() -> dict:
    """Run the interactive setup questionnaire. Returns dict of choices."""
    print()
    print("=" * 60)
    print("  AI Starter Pack — First Session Setup")
    print("=" * 60)
    print()
    print("This takes ~60 seconds. You can re-run anytime by deleting")
    print(f"  {SETUP_COMPLETE_FILE}")
    print()

    choices = {}

    # --- OSS routing ---
    print_separator()
    print("1/4  OSS MODEL ROUTING")
    print()
    print("  Routes haiku/sonnet-class tasks to open-source models via a")
    print("  LiteLLM proxy (Fireworks / Together / Groq). Saves 60-80% on")
    print("  API costs for non-sensitive tasks. Requires deploying a proxy")
    print("  (scripts/setup-litellm.sh) and adding LITELLM_PROXY_URL to .env.")
    print()
    choices["oss_routing"] = ask_yn(
        "  Enable OSS model routing?", default=False
    )
    print()

    # --- Langfuse telemetry ---
    print_separator()
    print("2/4  LANGFUSE OTEL TELEMETRY")
    print()
    print("  Sends session traces to Langfuse for cost/latency dashboards.")
    print("  Requires LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY in your env.")
    print("  The otelHeadersHelper in settings.json auto-generates auth headers.")
    print()
    choices["langfuse_telemetry"] = ask_yn(
        "  Enable Langfuse OTEL telemetry?", default=False
    )
    print()

    # --- Browserbase ---
    print_separator()
    print("3/4  BROWSERBASE CLOUD BROWSER AUTOMATION")
    print()
    print("  Adds cloud browser automation for external-site scraping,")
    print("  form filling, and bot-protected pages (residential proxies).")
    print("  Requires BROWSERBASE_API_KEY + BROWSERBASE_PROJECT_ID in .env")
    print("  and the browserbase MCP entry in .mcp.json (see .mcp.example.json).")
    print()
    choices["browserbase"] = ask_yn(
        "  Enable Browserbase cloud browser automation?", default=False
    )
    print()

    # --- Remote memory backend ---
    print_separator()
    print("4/4  REMOTE MEMORY BACKEND")
    print()
    print("  Replaces local JSONL memory with a KL-compatible MCP server")
    print("  (Supabase-backed) for semantic search, cross-session persistence,")
    print("  and multi-device access. Requires MCP_API_KEY and a KL server URL")
    print("  added to .mcp.json.")
    print()
    choices["remote_memory"] = ask_yn(
        "  Enable remote memory backend?", default=False
    )
    print()

    return choices


def write_setup_complete(choices: dict):
    """Write .ai/setup-complete with YAML-like config block."""
    SETUP_COMPLETE_FILE.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    enabled = [k for k, v in choices.items() if v]
    disabled = [k for k, v in choices.items() if not v]

    lines = [
        "# AI Starter Pack — Setup Complete",
        f"# Generated: {now}",
        "#",
        "# Re-run setup: delete this file, then run python3 scripts/first-run-setup.py",
        "",
        "setup_version: 1",
        f"completed_at: {now}",
        "",
        "features:",
        f"  oss_routing: {'true' if choices['oss_routing'] else 'false'}",
        f"  langfuse_telemetry: {'true' if choices['langfuse_telemetry'] else 'false'}",
        f"  browserbase: {'true' if choices['browserbase'] else 'false'}",
        f"  remote_memory: {'true' if choices['remote_memory'] else 'false'}",
        "",
    ]

    if enabled:
        lines.append(f"enabled: [{', '.join(enabled)}]")
    else:
        lines.append("enabled: []")

    if disabled:
        lines.append(f"disabled: [{', '.join(disabled)}]")
    else:
        lines.append("disabled: []")

    SETUP_COMPLETE_FILE.write_text("\n".join(lines) + "\n")


def print_summary(choices: dict, marker_written: bool = True):
    """Print a summary of what was enabled and next steps."""
    print("=" * 60)
    print("  Setup Summary")
    print("=" * 60)
    print()

    labels = {
        "oss_routing": "OSS model routing",
        "langfuse_telemetry": "Langfuse OTEL telemetry",
        "browserbase": "Browserbase cloud browser",
        "remote_memory": "Remote memory backend",
    }

    for key, label in labels.items():
        status = "ENABLED " if choices[key] else "disabled"
        print(f"  {label:<35} {status}")

    print()
    print_separator()
    print("  Next Steps")
    print_separator()
    print()

    has_next_steps = False

    if choices["oss_routing"]:
        has_next_steps = True
        print("  OSS routing:")
        print("    1. Run: bash scripts/setup-litellm.sh")
        print("       (deploys LiteLLM proxy to Fly.io, configures all 8 tiers)")
        print("    2. Add to .env:  LITELLM_PROXY_URL=https://your-proxy.fly.dev")
        print("    3. Add to .env:  USE_LITELLM_PROXY=true")
        print()

    if choices["langfuse_telemetry"]:
        has_next_steps = True
        print("  Langfuse telemetry:")
        print("    1. Sign up at https://langfuse.com (free tier available)")
        print("    2. For production use, store API keys in a secrets manager")
        print("       (Doppler, AWS SSM, etc.) rather than hardcoding.")
        print("    3. Add to .env:  LANGFUSE_PUBLIC_KEY=pk-lf-...")
        print("    4. Add to .env:  LANGFUSE_SECRET_KEY=sk-lf-...")
        print("       (The otelHeadersHelper in settings.json handles the rest)")
        print()

    if choices["browserbase"]:
        has_next_steps = True
        print("  Browserbase:")
        print("    1. Sign up at https://browserbase.com")
        print("    2. Add to .env:  BROWSERBASE_API_KEY=bb_live_...")
        print("    3. Add to .env:  BROWSERBASE_PROJECT_ID=...")
        print("    4. Copy the browserbase entry from .mcp.example.json to .mcp.json")
        print()

    if choices["remote_memory"]:
        has_next_steps = True
        print("  Remote memory backend:")
        print("    1. Deploy a compatible MCP memory server")
        print("    2. Add to .env:  MCP_API_KEY=your-key")
        print("    3. Add the remote-memory MCP entry to .mcp.json:")
        print('       "remote-memory": { "type": "http",')
        print('         "url": "https://your-memory-server.vercel.app/api/mcp",')
        print('         "headers": { "Authorization": "Bearer ${MCP_API_KEY}" } }')
        print()

    if not has_next_steps:
        print("  Nothing to configure — all optional features are disabled.")
        print("  Your starter pack is ready to use!")
        print()
        print("  You can enable features later by re-running this script:")
        print("    rm .ai/setup-complete && python3 scripts/first-run-setup.py")
        print()

    print_separator()
    if marker_written:
        print(f"  Setup written to: {SETUP_COMPLETE_FILE}")
        print("  Re-read CLAUDE.md before starting your first task.")
    else:
        print("  Setup NOT marked complete — add the missing secrets reported")
        print("  above, then re-run: python3 scripts/first-run-setup.py")
    print("=" * 60)
    print()


def main():
    # Allow --verify-secrets flag for re-running verification anytime
    if "--verify-secrets" in sys.argv:
        sys.exit(verify_required_secrets())

    if SETUP_COMPLETE_FILE.exists():
        print("Setup already complete.")
        print(f"  (Delete {SETUP_COMPLETE_FILE} to re-run setup)")
        sys.exit(0)

    try:
        choices = run_setup()
    except KeyboardInterrupt:
        print()
        print()
        print("Setup cancelled. Run again when ready:")
        print("  python3 scripts/first-run-setup.py")
        sys.exit(1)

    # Verify BEFORE writing the completion marker (Codex review, PR #2185):
    # keys for interactively-enabled features are merged into the check, and a
    # failed verification leaves setup re-runnable.
    extra_keys = [
        key
        for feature, keys in CHOICE_REQUIRED_KEYS.items()
        if choices.get(feature)
        for key in keys
    ]
    rc = verify_required_secrets(extra_keys)

    if rc == 0:
        write_setup_complete(choices)
    print_summary(choices, marker_written=(rc == 0))
    sys.exit(rc)


if __name__ == "__main__":
    main()
