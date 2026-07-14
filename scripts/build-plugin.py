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

Phase A: agents/, commands/, skills/, .claude-plugin/plugin.json.
Phase B (this script): hooks/hooks.json + bundled scripts/ + hooks/. The hook
  entrypoints self-resolve their roots via ${CLAUDE_PROJECT_DIR} (consumer state)
  and ${CLAUDE_PLUGIN_ROOT} (bundled code) env-fallbacks, so they are copied
  verbatim — no per-file path rewriting in the artifact.

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

# manolii-om plugin — separate marketplace entry that ships OM skills, the eval
# pack, and a reference to the KL contract validators. Sourced from
# .claude/skills/om-*/SKILL.md (rendered with kl_integration=true) plus
# plugin-sources/manolii-om/ (evals + plugin README, excluded from copier).
OM_PLUGIN_NAME = "manolii-om"
OM_SKILLS = ("om-fact-capture", "om-readiness", "om-staff-answer", "om-handover")
OM_SOURCE_DIR = REPO / "plugin-sources" / OM_PLUGIN_NAME

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
    """Assemble agents/commands/skills at the plugin root + write plugin.json.

    OM_SKILLS are ship-via-manolii-om-only: they exist in the rendered
    .claude/skills (kl_integration=true) but must not be duplicated into the
    framework plugin's skills/ directory."""
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
            # Drop OM skills — they ship via the manolii-om plugin (dual-run
            # retirement policy §6 / plan B5).
            for om in OM_SKILLS:
                d = out / comp / om
                if d.is_dir():
                    shutil.rmtree(d)
            # Plugin skill discovery requires skills/<name>/SKILL.md; some canonical
            # skill dirs ship CLAUDE.md — promote those so the skill loads on install.
            for d in (out / comp).iterdir():
                if d.is_dir() and not (d / "SKILL.md").exists() and (d / "CLAUDE.md").exists():
                    shutil.move(str(d / "CLAUDE.md"), str(d / "SKILL.md"))
            counts[comp] = sum(1 for p in (out / comp).iterdir()
                               if p.is_dir() and (p / "SKILL.md").exists())
        elif comp == "commands":
            # Plugin commands run from the consumer cwd; rewrite bundled-script
            # invocations to the plugin root so /commands work in a pure plugin
            # install. Source keeps bare scripts/ for in-tree template use.
            for md in (out / comp).rglob("*.md"):
                t = md.read_text(encoding="utf-8")
                nt = re.sub(r'\b(python3|bash) (scripts/[^\s`"]+)',
                            r'\1 "${CLAUDE_PLUGIN_ROOT}/\2"', t)
                if nt != t:
                    md.write_text(nt, encoding="utf-8")
            counts[comp] = sum(1 for p in (out / comp).rglob("*.md"))
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
        "author": {"name": "Manolii", "url": "https://github.com/manolii-org"},
    }
    (out / ".claude-plugin").mkdir()
    (out / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    return counts


def build_hooks_config() -> dict:
    """Generate the plugin hooks/hooks.json mirroring the canonical settings.json
    hooks block, with plugin path conventions:
      - each command cds to ${CLAUDE_PROJECT_DIR} (consumer project) so relative
        state paths (.ai/*, .git/*) resolve there, not in the plugin install dir;
      - bundled code is invoked from ${CLAUDE_PLUGIN_ROOT}/{scripts,hooks}/.
    Kept honest by the event-parity check in verify() (fails the build if the
    canonical settings.json gains/loses a hook event)."""
    P = "${CLAUDE_PLUGIN_ROOT}"

    def cmd(c: str, timeout: int) -> dict:
        return {
            "type": "command",
            "command": f'cd "${{CLAUDE_PROJECT_DIR}}" && {c}',
            "timeout": timeout,
        }

    # Stop self-check: mirrors settings.json's compound command verbatim (only the
    # script paths are plugin-rooted; the >> .ai/memory/... sink stays cwd-relative).
    stop_selfcheck = (
        f'python3 "{P}/scripts/system-self-check.py" > /dev/null 2>&1 '
        f'&& python3 "{P}/scripts/build-skill-graph.py" > /dev/null 2>&1; '
        '_rc=$?; mkdir -p .ai/memory; '
        'echo "{\\"timestamp\\":\\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\\",'
        '\\"exit_code\\":$_rc,\\"event\\":\\"stop-hook-self-check\\"}" '
        '>> .ai/memory/eval-failures.jsonl'
    )

    return {
        "hooks": {
            "PreToolUse":   [{"hooks": [cmd(f'python3 "{P}/scripts/pre-tool-use.py"', 5)]}],
            "SessionStart": [{"hooks": [cmd(f'bash "{P}/hooks/session-start.sh"', 30)]}],
            "UserPromptSubmit": [
                {"hooks": [cmd(f'python3 "{P}/scripts/classify-message.py"', 15)]},
                {"hooks": [cmd(f'python3 "{P}/hooks/user-prompt.py"', 15)]},
            ],
            "PostToolUse":  [{"hooks": [cmd(f'python3 "{P}/hooks/post-tool.py"', 30)]}],
            "PreCompact":   [{"hooks": [cmd(f'bash "{P}/hooks/pre-compact.sh"', 30)]}],
            "PostCompact":  [{"hooks": [cmd(f'bash "{P}/hooks/post-compact.sh"', 15)]}],
            "Stop": [{"hooks": [
                cmd(f'bash "{P}/scripts/session-stop-checklist.sh"', 5),
                cmd(stop_selfcheck, 30),
            ]}],
        }
    }


def assemble_hooks_and_scripts(rendered: Path, out: Path) -> dict:
    """Bundle scripts/ + .claude/hooks/ into the plugin and emit hooks/hooks.json.

    The hook entrypoints resolve their roots via ${CLAUDE_PROJECT_DIR} /
    ${CLAUDE_PLUGIN_ROOT} env-fallbacks (see the canonical hook sources), so they
    are copied verbatim — no per-file path rewriting in the artifact."""
    counts: dict[str, int] = {}

    rsrc = rendered / "scripts"
    if rsrc.is_dir():
        shutil.copytree(rsrc, out / "scripts")
        counts["scripts"] = sum(1 for p in (out / "scripts").rglob("*") if p.is_file())

    rhooks = rendered / ".claude" / "hooks"
    if rhooks.is_dir():
        shutil.copytree(rhooks, out / "hooks",
                        ignore=shutil.ignore_patterns("pre-tool.py", "stop.sh"))
    (out / "hooks").mkdir(parents=True, exist_ok=True)
    counts["hooks"] = sum(1 for p in (out / "hooks").iterdir() if p.is_file())

    (out / "hooks" / "hooks.json").write_text(
        json.dumps(build_hooks_config(), indent=2) + "\n"
    )
    return counts


def assemble_data(rendered: Path, out: Path) -> dict:
    """Bundle framework data (routing policy + secret-shape patterns) so a pure
    plugin install (no Copier render in the consumer) still has routing
    governance and secret redaction. Hooks resolve the consumer override first,
    then these bundled defaults (data/), then the in-tree fallback."""
    src_root = Path(__file__).resolve().parent.parent
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    spec = {
        "model-routing.json": [rendered / ".claude" / "model-routing.json",
                               src_root / ".claude" / "model-routing.json"],
        "token-shapes.json": [rendered / ".ai" / "security" / "token-shapes.json",
                              src_root / ".ai" / "security" / "token-shapes.json"],
    }
    bundled = 0
    for name, cands in spec.items():
        srcf = next((c for c in cands if c.is_file()), None)
        if srcf is None:
            sys.stderr.write(f"FAIL: framework data source missing: {name}\n")
            sys.exit(1)
        shutil.copy2(srcf, data / name)
        bundled += 1
    return {"data": bundled}


def read_om_plugin_version() -> str:
    """Read the manolii-om plugin's own version from the committed plugin.json
    if present, else default to 0.1.0. The om plugin bumps independently of
    pack.manifest.yml (it releases on its own cadence via v1.x pack tags but
    tracks its skill/eval-pack sha)."""
    manifest = REPO / "plugin" / OM_PLUGIN_NAME / ".claude-plugin" / "plugin.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text())
            if isinstance(data, dict):
                v = data.get("version")
                if v:
                    return v
        except (json.JSONDecodeError, OSError):
            pass
    return "0.1.0"


def assemble_om_plugin(rendered: Path, out: Path) -> dict:
    """Assemble the manolii-om plugin.

    Layout:
      out/.claude-plugin/plugin.json
      out/README.md                — copied verbatim from plugin-sources
      out/skills/om-*/SKILL.md     — 4 OM skills sourced from the render
      out/evals/operational-memory/ — sourced from plugin-sources
    """
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    counts: dict[str, int] = {}
    skills_out = out / "skills"
    skills_out.mkdir()
    for name in OM_SKILLS:
        src = rendered / ".claude" / "skills" / name / "SKILL.md"
        if not src.is_file():
            sys.stderr.write(
                f"FAIL: OM skill source missing in render: .claude/skills/{name}/SKILL.md — "
                "did copier render with kl_integration=true?\n")
            sys.exit(1)
        (skills_out / name).mkdir()
        shutil.copy2(src, skills_out / name / "SKILL.md")
    counts["skills"] = len(OM_SKILLS)

    if not OM_SOURCE_DIR.is_dir():
        sys.stderr.write(f"FAIL: OM plugin source dir missing: {OM_SOURCE_DIR}\n")
        sys.exit(1)

    evals_src = OM_SOURCE_DIR / "evals" / "operational-memory"
    if not evals_src.is_dir():
        sys.stderr.write(f"FAIL: OM eval pack missing: {evals_src}\n")
        sys.exit(1)
    shutil.copytree(evals_src, out / "evals" / "operational-memory")
    counts["evals"] = sum(1 for _ in (out / "evals").rglob("*.json"))

    readme_src = OM_SOURCE_DIR / "README.md"
    if readme_src.is_file():
        shutil.copy2(readme_src, out / "README.md")

    # Optional vendored contracts mirror — see plugin-sources/manolii-om/README.md.
    # Currently unpopulated; when a future build ships offline schemas, drop them
    # under plugin-sources/manolii-om/contracts-mirror/ with a SOURCE.md pinning
    # the manolii-knowledge-layer commit. verify_om_plugin enforces the SOURCE.md
    # invariant on the resulting bundle.
    mirror_src = OM_SOURCE_DIR / "contracts-mirror"
    if mirror_src.is_dir():
        shutil.copytree(mirror_src, out / "contracts-mirror")
        counts["contracts_mirror"] = sum(
            1 for p in (out / "contracts-mirror").rglob("*") if p.is_file()
        )

    manifest = {
        "name": OM_PLUGIN_NAME,
        "description": (
            "Operational Memory skills for Claude Code — propose-only fact "
            "capture, read-only readiness / staff-answer, and client "
            "handover. Requires a Knowledge Layer MCP backend (contract "
            "schemas live in manolii-knowledge-layer/contracts/operational-memory/)."
        ),
        "version": read_om_plugin_version(),
        "author": {"name": "Manolii", "url": "https://github.com/manolii-org"},
    }
    (out / ".claude-plugin").mkdir()
    (out / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    return counts


def verify_om_plugin(out: Path, counts: dict) -> None:
    """Structural checks on the manolii-om plugin artifact."""
    manifest_path = out / ".claude-plugin" / "plugin.json"
    if not manifest_path.is_file():
        sys.stderr.write("FAIL: manolii-om missing .claude-plugin/plugin.json\n")
        sys.exit(1)
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict):
        sys.stderr.write("FAIL: manolii-om plugin.json is not a JSON object\n")
        sys.exit(1)
    for key in ("name", "description", "version"):
        if not manifest.get(key):
            sys.stderr.write(f"FAIL: manolii-om plugin.json missing '{key}'\n")
            sys.exit(1)
    for name in OM_SKILLS:
        if not (out / "skills" / name / "SKILL.md").is_file():
            sys.stderr.write(f"FAIL: manolii-om missing skill: {name}\n")
            sys.exit(1)
    if not (out / "evals" / "operational-memory").is_dir():
        sys.stderr.write("FAIL: manolii-om missing eval pack\n")
        sys.exit(1)
    # OM plugin must NOT vendor forked schemas at the top level. contracts-mirror
    # is optional; when present it must carry a SOURCE.md pin.
    mirror = out / "contracts-mirror"
    if mirror.is_dir() and not (mirror / "SOURCE.md").is_file():
        sys.stderr.write(
            "FAIL: manolii-om contracts-mirror present without SOURCE.md — "
            "vendored schemas must record a pinned source commit\n")
        sys.exit(1)
    leaks = [str(p.relative_to(out)) for p in out.rglob("*")
             if "{%" in p.name or "{{" in p.name or p.name.endswith(".jinja")]
    if leaks:
        sys.stderr.write("FAIL: unresolved Jinja artifacts in manolii-om:\n  "
                         + "\n  ".join(leaks) + "\n")
        sys.exit(1)
    print(f"OK  plugin '{manifest['name']}' v{manifest['version']} assembled at {out}")
    print(f"    components: {json.dumps(counts)}")


def verify(out: Path, counts: dict, rendered: Path) -> None:
    """Fail loudly if any Jinja survived, or required manifest is missing."""
    for _d in ("model-routing.json", "token-shapes.json"):
        if not (out / "data" / _d).is_file():
            sys.stderr.write(f"FAIL: framework data not bundled: data/{_d}\n")
            sys.exit(1)
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

    # ── hooks.json integrity (Phase B) ───────────────────────────────────────
    hooks_json = out / "hooks" / "hooks.json"
    if hooks_json.is_file():
        cfg = json.loads(hooks_json.read_text())
        events = cfg.get("hooks", {})
        # (a) every ${CLAUDE_PLUGIN_ROOT}/... file referenced by a command exists
        refs: set[str] = set()
        for groups in events.values():
            for group in groups:
                for h in group.get("hooks", []):
                    refs.update(re.findall(
                        r'\$\{CLAUDE_PLUGIN_ROOT\}/([^"\s]+)', h.get("command", "")))
        missing = sorted(r for r in refs if not (out / r).is_file())
        if missing:
            sys.stderr.write("FAIL: hooks.json references missing bundled files:\n  "
                             + "\n  ".join(missing) + "\n")
            sys.exit(1)
        # Import-time deps invisible to the command-string regex (a hook does
        # `import injection_scan` from sys.path). If dropped from the bundle the
        # security scan silently no-ops, so assert the known ones are present.
        for dep in ("scripts/injection_scan.py",):
            if not (out / dep).is_file():
                sys.stderr.write(f"FAIL: runtime-imported dep missing from bundle: {dep}\n")
                sys.exit(1)
        # (b) drift guard: plugin hook events must match the canonical settings.json
        settings = rendered / ".claude" / "settings.json"
        if settings.is_file():
            src_events = set(json.loads(settings.read_text()).get("hooks", {}))
            if set(events) != src_events:
                sys.stderr.write(
                    f"FAIL: hooks.json events {sorted(events)} != settings.json "
                    f"events {sorted(src_events)} — update build_hooks_config()\n")
                sys.exit(1)
        print(f"OK  hooks.json: {len(events)} events, {len(refs)} bundled refs present")

    print(f"OK  plugin '{manifest['name']}' v{manifest['version']} assembled at {out}")
    print(f"    components: {json.dumps(counts)}")


def smoke_test_hooks(out: Path) -> None:
    """Prove every hook in hooks.json actually FIRES under a simulated plugin
    layout — evidence, not assumption. For each command: run it with
    CLAUDE_PLUGIN_ROOT=<plugin> + CLAUDE_PROJECT_DIR=<throwaway consumer>, feed a
    minimal stdin payload, assert exit 0, and assert the hook wrote NO state into
    the (read-only) plugin tree — consumer state must land in the consumer dir."""
    import os
    import subprocess
    import tempfile

    cfg = json.loads((out / "hooks" / "hooks.json").read_text())

    def snapshot() -> dict:
        return {p: p.stat().st_mtime_ns for p in out.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}

    before = snapshot()
    consumer = Path(tempfile.mkdtemp(prefix="manolii-smoke-consumer-"))
    (consumer / ".git").mkdir(parents=True, exist_ok=True)
    env = {**os.environ,
           "CLAUDE_PLUGIN_ROOT": str(out),
           "CLAUDE_PROJECT_DIR": str(consumer),
           "PYTHONDONTWRITEBYTECODE": "1",
           "DOPPLER_TOKEN_PRD": "", "DOPPLER_TOKEN": "", "DOPPLER_PERSONAL": ""}
    stdin_for = {
        "PreToolUse": '{"tool_name":"Read","tool_input":{}}',
        "PostToolUse": '{"tool_name":"Read","tool_input":{},"tool_response":"ok"}',
        "UserPromptSubmit": '{"prompt":"smoke continuation"}',
        "SessionStart": "{}",
        "PreCompact": '{"trigger":"manual","transcript_path":""}',
        "PostCompact": '{"compact_summary":"smoke"}',
        "Stop": "{}",
    }

    fired: list = []
    failures: list = []
    for event, groups in cfg.get("hooks", {}).items():
        payload = stdin_for.get(event, "{}")
        for group in groups:
            for h in group.get("hooks", []):
                command = h["command"]
                try:
                    proc = subprocess.run(["bash", "-c", command], input=payload,
                                          capture_output=True, text=True,
                                          env=env, timeout=60)
                    fired.append({"event": event, "exit": proc.returncode})
                    if proc.returncode != 0:
                        failures.append(
                            f"{event}: exit {proc.returncode} :: {proc.stderr.strip()[:160]}")
                except subprocess.TimeoutExpired:
                    fired.append({"event": event, "exit": "timeout"})
                    failures.append(f"{event}: TIMEOUT")

    after = snapshot()
    leaked = sorted({str(p.relative_to(out)) for p in after
                     if p not in before or after[p] != before[p]})
    if leaked:
        failures.append("hook wrote into plugin tree (must write to consumer): "
                        + ", ".join(leaked))
    shutil.rmtree(consumer, ignore_errors=True)

    if failures:
        sys.stderr.write("FAIL: hook smoke test:\n  " + "\n  ".join(failures) + "\n")
        sys.exit(1)
    print(f"OK  smoke: {len(fired)} hook command(s) fired, all exit 0, no plugin-tree writes")
    for f in fired:
        print(f"    fired {f['event']:<16} exit={f['exit']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--install-mode", default="branded", choices=["branded", "unbranded"])
    ap.add_argument("--out",
                    help="Output dir. Defaults to plugin/<plugin-name>. "
                         "Only used when --plugin selects a single plugin.")
    ap.add_argument("--plugin", default="all",
                    choices=["all", PLUGIN_NAME, OM_PLUGIN_NAME],
                    help="Which plugin to build (default: all).")
    ap.add_argument("--keep-rendered", action="store_true",
                    help="keep the temp copier render dir (debug)")
    ap.add_argument("--no-smoke", action="store_true",
                    help="skip the hook-firing smoke test (manolii-framework only)")
    ap.add_argument("--smoke-only", action="store_true",
                    help="run the smoke test against the existing --out plugin (no render)")
    args = ap.parse_args()

    if args.smoke_only:
        smoke_test_hooks(Path(args.out or (REPO / "plugin" / PLUGIN_NAME)))
        return

    if args.out and args.plugin == "all":
        sys.stderr.write("FAIL: --out requires --plugin <name> (cannot redirect two builds)\n")
        sys.exit(2)

    build_framework = args.plugin in ("all", PLUGIN_NAME)
    build_om = args.plugin in ("all", OM_PLUGIN_NAME)

    rendered = render_template(args.install_mode)
    try:
        if build_framework:
            out = Path(args.out) if (args.out and args.plugin == PLUGIN_NAME) \
                else REPO / "plugin" / PLUGIN_NAME
            counts = assemble_plugin(rendered, out)
            counts.update(assemble_hooks_and_scripts(rendered, out))
            counts.update(assemble_data(rendered, out))
            verify(out, counts, rendered)
            if not args.no_smoke:
                smoke_test_hooks(out)

        if build_om:
            om_out = Path(args.out) if (args.out and args.plugin == OM_PLUGIN_NAME) \
                else REPO / "plugin" / OM_PLUGIN_NAME
            om_counts = assemble_om_plugin(rendered, om_out)
            verify_om_plugin(om_out, om_counts)
    finally:
        if not args.keep_rendered:
            shutil.rmtree(rendered, ignore_errors=True)
        else:
            print(f"    (kept render dir: {rendered})")


if __name__ == "__main__":
    main()
