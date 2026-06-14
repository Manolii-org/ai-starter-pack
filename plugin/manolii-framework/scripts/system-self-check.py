#!/usr/bin/env python3
"""System self-check — runs at session Stop.

Validates that critical harness files are present and well-formed.
Reports a short summary; exits 0 always (never blocks shutdown).

Checks:
  - .claude/settings.json   — valid JSON, hooks keys present
  - .mcp.json               — valid JSON, mcpServers key present
  - .claude/hooks/*.py/.sh  — all files referenced in settings.json exist
  - .ai/memory/             — directory present
  - CLAUDE.md               — present (not just placeholder)
"""
import json
import os
import shlex
import sys
from pathlib import Path

# As a plugin Stop hook (run after `cd $CLAUDE_PROJECT_DIR`), validate the consumer
# project, not the plugin install dir.
REPO_ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).parent.parent)
SETTINGS  = REPO_ROOT / ".claude" / "settings.json"
MCP_FILE  = REPO_ROOT / ".mcp.json"

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"

issues: list[str] = []
warnings: list[str] = []
passed = 0


def ok(label: str) -> None:
    global passed
    passed += 1
    print(f"  {PASS} {label}")


def fail(label: str, detail: str = "") -> None:
    msg = f"{label}: {detail}" if detail else label
    issues.append(msg)
    print(f"  {FAIL} {label}" + (f": {detail}" if detail else ""))


def warn(label: str, detail: str = "") -> None:
    msg = f"{label}: {detail}" if detail else label
    warnings.append(msg)
    print(f"  {WARN} {label}" + (f": {detail}" if detail else ""))


def _safe_read(path: Path) -> str | None:
    """Read a file as text; return None and call fail() on any read error."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError, OSError) as e:
        fail(f"unreadable: {path.name}", str(e))
        return None


def main() -> None:
    print("\n── System self-check ──────────────────────────────────")

    # 1. settings.json
    print("\n.claude/settings.json")
    if not SETTINGS.exists():
        fail("missing", str(SETTINGS))
    else:
        content = _safe_read(SETTINGS)
        if content is not None:
            try:
                settings = json.loads(content)
                ok("valid JSON")
                if not isinstance(settings, dict):
                    fail("invalid settings.json shape", "root must be an object")
                else:
                    if "hooks" in settings:
                        hooks_obj = settings.get("hooks")
                        if not isinstance(hooks_obj, dict):
                            fail("invalid hooks shape", "hooks must be an object")
                        else:
                            ok("hooks key present")
                            # Check referenced hook scripts exist
                            for event, hook_list in hooks_obj.items():
                                if not isinstance(hook_list, list):
                                    fail(f"invalid hooks list in {event}", "expected array")
                                    continue
                                for entry in hook_list:
                                    if not isinstance(entry, dict):
                                        fail(f"invalid hook entry in {event}", "expected object")
                                        continue
                                    for hook in entry.get("hooks", []):
                                        if not isinstance(hook, dict):
                                            fail(f"invalid hook entry in {event}", "expected object")
                                            continue
                                        if hook.get("type") == "command":
                                            cmd = hook.get("command")
                                            if not isinstance(cmd, str) or not cmd.strip():
                                                fail(f"invalid command hook in {event}", "missing/invalid command")
                                                continue
                                            try:
                                                parts = shlex.split(cmd)
                                            except ValueError:
                                                parts = cmd.split()
                                            for part in parts[1:]:  # skip interpreter (bash/python3)
                                                if part.endswith((".py", ".sh")):
                                                    resolved_repo = REPO_ROOT.resolve()
                                                    resolved_hook = (REPO_ROOT / part).resolve()
                                                    try:
                                                        resolved_hook.relative_to(resolved_repo)
                                                    except ValueError:
                                                        fail(f"hook outside repo: {part}")
                                                        break
                                                    if resolved_hook.exists():
                                                        ok(f"hook exists: {part}")
                                                    else:
                                                        fail(f"hook missing: {part}")
                                                    break
                    else:
                        warn("no hooks key — hooks disabled")
                    if "permissions" not in settings:
                        warn("no permissions block")
            except json.JSONDecodeError as e:
                fail("invalid JSON", str(e))

    # 2. .mcp.json
    print("\n.mcp.json")
    if not MCP_FILE.exists():
        warn("missing — no MCP servers configured")
    else:
        content = _safe_read(MCP_FILE)
        if content is not None:
            try:
                mcp = json.loads(content)
                ok("valid JSON")
                if not isinstance(mcp, dict):
                    fail("invalid .mcp.json shape", "root must be an object")
                else:
                    servers = mcp.get("mcpServers", {})
                    if not isinstance(servers, dict):
                        fail("invalid mcpServers shape", "mcpServers must be an object")
                    elif servers:
                        ok(f"{len(servers)} server(s) configured: {', '.join(servers)}")
                    else:
                        warn("mcpServers is empty")
            except json.JSONDecodeError as e:
                fail("invalid JSON", str(e))

    # 3. CLAUDE.md
    print("\nCLAUDE.md")
    claude_md = REPO_ROOT / "CLAUDE.md"
    if not claude_md.exists():
        warn("missing — project instructions not set up")
    else:
        content = _safe_read(claude_md)
        if content is not None:
            placeholder_count = content.count("{")
            ok("present")
            if placeholder_count > 5:
                warn(f"{placeholder_count} unfilled placeholders ({{...}}) — remember to customize")

    # 4. .ai/memory directory
    print("\n.ai/memory")
    memory_dir = REPO_ROOT / ".ai" / "memory"
    if memory_dir.exists():
        files = list(memory_dir.glob("*.jsonl"))
        ok(f"present ({len(files)} JSONL files)")
    else:
        warn("missing — run /remember to initialise")

    # 5. .ai/knowledge-index.md
    print("\n.ai/knowledge-index.md")
    ki = REPO_ROOT / ".ai" / "knowledge-index.md"
    if ki.exists():
        ok("present")
    else:
        warn("missing — run scripts/generate-knowledge-index.sh to create")

    # ── Summary ──────────────────────────────────────────────────────────────────
    print("\n────────────────────────────────────────────────────────")
    total = passed + len(issues) + len(warnings)
    print(f"  {passed}/{total} checks passed  |  {len(issues)} error(s)  |  {len(warnings)} warning(s)")

    if issues:
        print("\n  Errors to fix:")
        for i in issues:
            print(f"    • {i}")

    if warnings:
        print("\n  Warnings:")
        for w in warnings:
            print(f"    • {w}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        fail("unexpected error", str(e))
        print(f"\n  {FAIL} unexpected error: {e}")
    finally:
        print()
        sys.exit(0)
