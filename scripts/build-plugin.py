#!/usr/bin/env python3
"""Build the Manolii framework Claude Code plugin from the canonical Copier template.

The plugin is a GENERATED, Jinja-free artifact rendered from the single canonical
`.claude/` source (ADR-0023 Decision 5: per-surface adapters generated from one
canonical source). There is exactly one hand-edited home (the template); the plugin
is never hand-edited, so the two-home drift ADR-0023 exists to kill cannot recur.

Why render instead of copy: a Claude Code plugin is consumed VERBATIM (no template
pass), so the `.jinja` files, `SKILL.md.jinja`, and `{% if flag %}name{% endif %}`
filenames in the canonical `.claude/` would break a directly-installed plugin. We run
the template through copier (all feature flags ON) to resolve every conditional, then
assemble the Jinja-free result into the plugin layout.

Phase A (this script): agents/, commands/, skills/, .claude-plugin/plugin.json
  -> a valid, installable plugin (hooks are optional, added in Phase B).
Phase B (later): hooks/hooks.json + scripts/ + ${CLAUDE_PLUGIN_ROOT} path rewrites.

Usage:
  python3 scripts/build-plugin.py [--install-mode branded|unbranded]
                                  [--out plugin/manolii-framework] [--keep-rendered]
"""
from __future__ import annotations
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_NAME = "manolii-framework"  # kebab-case (plugin name constraint)
PLUGIN_COMPONENTS = ("agents", "commands", "skills")

# Render with every feature flag ON so the plugin ships the COMPLETE framework
# (28 agents / 47 commands / 23 skills). Whether a given agent/command actually
# functions in a consumer repo depends on that instance's MCP/secrets at runtime;
# shipping all of them keeps the plugin feature-complete and override-friendly.
FEATURE_FLAGS = (
    "oss_routing",
    "kl_integration",
    "browserbase",
    "langfuse_telemetry",
    "codex_adversarial",
    "mesh_telemetry",
)


def read_pack_version() -> str:
    """Read pack version from pack.manifest.yml (repo root), fallback to CHANGELOG."""
    manifest = REPO / "pack.manifest.yml"
    if manifest.is_file():
        m = re.search(r"^\s*version:\s*['\"]?([0-9][^'\"\s]*)", manifest.read_text(), re.M)
        if m:
            return m.group(1)
    changelog = REPO / "CHANGELOG.md"
    if changelog.is_file():
        m = re.search(r"##\s*\[?v?([0-9]+\.[0-9]+\.[0-9]+)", changelog.read_text())
        if m:
            return m.group(1)
    return "0.0.0"


def render_template(install_mode: str) -> Path:
    """copier copy the canonical template into a temp dir with Jinja fully resolved."""
    tmp = Path(tempfile.mkdtemp(prefix="manolii-plugin-render-"))
    cmd = ["copier", "copy", "--defaults", "--vcs-ref", "HEAD",
           "--data", f"install_mode={install_mode}"]
    for flag in FEATURE_FLAGS:
        cmd += ["--data", f"{flag}=true"]
    cmd += [str(REPO), str(tmp)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write("copier render failed:\n" + proc.stdout + proc.stderr + "\n")
        shutil.rmtree(tmp, ignore_errors=True)
        sys.exit(1)
    return tmp


def assemble_plugin(rendered: Path, out: Path) -> dict:
    """Assemble agents/commands/skills at the plugin root + write plugin.json."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    src = rendered / ".claude"
    counts: dict[str, int] = {}
    for comp in PLUGIN_COMPONENTS:
        s = src / comp
        if not s.is_dir():
            continue
        shutil.copytree(s, out / comp)
        if comp == "skills":
            counts[comp] = sum(1 for p in (out / comp).iterdir() if p.is_dir())
        else:
            counts[comp] = sum(1 for p in (out / comp).rglob("*.md"))

    manifest = {
        "name": PLUGIN_NAME,
        "description": (
            "Manolii AI framework for Claude Code — specialist sub-agents, "
            "slash-commands, and skills for PR review, persistent memory, "
            "planning, and model-routing governance."
        ),
        "version": read_pack_version(),
    }
    (out / ".claude-plugin").mkdir()
    (out / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    return counts


def verify(out: Path, counts: dict) -> None:
    """Fail loudly if any Jinja survived, or required manifest is missing."""
    leaks = [
        str(p.relative_to(out))
        for p in out.rglob("*")
        if "{%" in p.name or "{{" in p.name or p.name.endswith(".jinja")
    ]
    if leaks:
        sys.stderr.write("FAIL: unresolved Jinja artifacts in plugin:\n  "
                         + "\n  ".join(leaks) + "\n")
        sys.exit(1)

    manifest_path = out / ".claude-plugin" / "plugin.json"
    if not manifest_path.is_file():
        sys.stderr.write("FAIL: missing .claude-plugin/plugin.json\n")
        sys.exit(1)
    manifest = json.loads(manifest_path.read_text())
    for key in ("name", "description"):
        if not manifest.get(key):
            sys.stderr.write(f"FAIL: plugin.json missing required key '{key}'\n")
            sys.exit(1)

    print(f"OK  plugin '{manifest['name']}' v{manifest['version']} assembled at {out}")
    print(f"    components: {json.dumps(counts)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--install-mode", default="branded", choices=["branded", "unbranded"])
    ap.add_argument("--out", default=str(REPO / "plugin" / PLUGIN_NAME))
    ap.add_argument("--keep-rendered", action="store_true",
                    help="keep the temp copier render dir (debug)")
    args = ap.parse_args()

    rendered = render_template(args.install_mode)
    try:
        out = Path(args.out)
        counts = assemble_plugin(rendered, out)
        verify(out, counts)
    finally:
        if not args.keep_rendered:
            shutil.rmtree(rendered, ignore_errors=True)
        else:
            print(f"    (kept render dir: {rendered})")


if __name__ == "__main__":
    main()
