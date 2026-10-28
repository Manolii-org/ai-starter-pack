"""Contract tests for the shared QA framework (docs/qa-framework.md).

Covers the finalise gate (command classification, every host adapter, block /
warn / bypass / incomplete outcomes, git shim) and the deterministic parts of
ui-smoke (route inference and selection). A real-browser ui-smoke case runs
only where Playwright and Chromium are installed.
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "qa" / "finalise-gate.py"
LAUNCHER = ROOT / "scripts" / "qa" / "finalise-gate.sh"
SMOKE = ROOT / "scripts" / "qa" / "ui-smoke.mjs"


def load_gate():
    spec = importlib.util.spec_from_file_location("finalise_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and k != "AGENT_FINALISE_GATE"}
    env.update(extra)
    return env


class ClassifyCommandTests(unittest.TestCase):
    def setUp(self):
        self.classify = load_gate().classify_command

    def trigger(self, command):
        return self.classify(command)["trigger"]

    def test_hand_offs_trigger(self):
        for command in ("git push", "git push -u origin feat/x", "git add . && git commit -m x && git push",
                        "cd repo; git -C sub push origin HEAD", "git -c push.default=current push",
                        "/usr/bin/git push origin main", "FOO=1 git push"):
            self.assertEqual(self.trigger(command), "push", command)
        self.assertEqual(self.trigger("gh pr create --draft --fill"), "pr")

    def test_non_hand_offs_do_not_trigger(self):
        for command in ("git status", "git push --dry-run", "git push -n origin x",
                        "git push origin --delete old", "git push origin :old :older",
                        "echo git push", "git log --grep push", "grep -r 'git push' docs"):
            self.assertIsNone(self.trigger(command), command)

    def test_repo_dir_and_inline_bypass(self):
        info = self.classify("AGENT_FINALISE_GATE=off git -C ../other push")
        self.assertEqual(info["trigger"], "push")
        self.assertEqual(info["repo_dir"], "../other")
        self.assertTrue(info["bypass"])

    def test_unparseable_shell_falls_back_to_triggering(self):
        self.assertEqual(self.trigger("git push 'unterminated"), "push")

    def test_quoted_and_heredoc_text_is_data_not_a_command(self):
        commit = ('git commit -m "$(cat <<\'EOF\'\nfeat: gate\n\n'
                  'git push now runs the gate; gh pr create too\nEOF\n)"')
        for command in (commit, 'grep -n "a|git push" docs/x.md', "cat <<EOF > f\ngit push\nEOF",
                        "echo 'git push; gh pr create'", "git commit -m 'x; git push'"):
            self.assertIsNone(self.trigger(command), command)

    def test_wrappers_subshells_and_nested_shells_are_followed(self):
        for command in ("(cd sub && git push)", 'bash -c "git push"', "env X=1 git push",
                        "timeout 60 git push", "sudo -u me git push", "git --git-dir .git push",
                        "nohup git push origin x &", "git push 2>&1 | tail -3"):
            self.assertEqual(self.trigger(command), "push", command)

    def test_cd_selects_the_repository_that_is_pushed(self):
        self.assertEqual(self.classify("cd repoB && git push")["repo_dir"], "repoB")
        self.assertEqual(self.classify("cd a; cd b && git -C c push")["repo_dir"], os.path.join("a", "b", "c"))


class GateRepo(unittest.TestCase):
    """A throwaway adopted repository with one controllable check."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "repo"
        (self.root / "scripts" / "qa").mkdir(parents=True)
        (self.root / "config").mkdir()
        (self.root / "src").mkdir()
        shutil.copyfile(ROOT / "scripts" / "agent-check.py", self.root / "scripts" / "agent-check.py")
        for name in ("finalise-gate.py", "finalise-gate.sh"):
            shutil.copyfile(ROOT / "scripts" / "qa" / name, self.root / "scripts" / "qa" / name)
        (self.root / ".gitignore").write_text(".ai/agent-checks/\n.ai/*.jsonl\n")
        (self.root / "src" / "value.py").write_text("value = 1\n")
        self.write_checks([sys.executable, "-c",
                           "import pathlib,sys; sys.exit('value = 1' not in pathlib.Path('src/value.py').read_text())"])
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True,
                              text=True, env=clean_env()).stdout

    def write_checks(self, command):
        (self.root / "config" / "agent-checks.json").write_text(json.dumps({
            "schema_version": 1, "base": "main", "environments": [],
            "checks": [{"id": "value-check", "paths": ["src/*"], "tools": ["python"],
                        "commands": [command]}],
        }))

    def write_qa(self, **gate):
        (self.root / "config" / "qa.json").write_text(json.dumps(
            {"schema_version": 1, "finalise_gate": gate}))

    def break_source(self):
        (self.root / "src" / "value.py").write_text("value = 2\n")

    def run_hook(self, adapter, payload=None, stdin=None, **env):
        if stdin is None:
            stdin = json.dumps(payload if payload is not None else {
                "tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": str(self.root)})
        return subprocess.run(["sh", str(self.root / "scripts/qa/finalise-gate.sh"), adapter],
                              input=stdin, capture_output=True, text=True, cwd=self.root,
                              env=clean_env(**env), timeout=120)


class FinaliseGateTests(GateRepo):
    def test_passing_candidate_is_allowed_silently(self):
        proc = self.run_hook("claude")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_failing_candidate_blocks_every_host_in_block_mode(self):
        self.write_qa(mode="block")
        self.break_source()
        claude = self.run_hook("claude")
        self.assertEqual(claude.returncode, 2)
        self.assertIn("FINALISE-GATE FAIL", claude.stderr)
        self.assertIn("value-check", claude.stderr)
        self.assertEqual(self.run_hook("codex").returncode, 2)
        cursor = self.run_hook("cursor", {"command": "git push", "cwd": str(self.root)})
        self.assertEqual(cursor.returncode, 0)
        response = json.loads(cursor.stdout)
        self.assertEqual(response["permission"], "deny")
        self.assertIn("value-check", response["agent_message"])
        refs = "refs/heads/x 1111111111111111111111111111111111111111 refs/heads/x 0000000000000000000000000000000000000000\n"
        self.assertEqual(self.run_hook("git-pre-push", stdin=refs).returncode, 1)

    def test_pull_request_tool_is_gated_under_any_mcp_server_name(self):
        self.write_qa(mode="block")
        self.break_source()
        for tool in ("mcp__github__create_pull_request", "mcp__plugin_github_github__create_pull_request"):
            proc = self.run_hook("claude", {"tool_name": tool, "tool_input": {}, "cwd": str(self.root)})
            self.assertEqual(proc.returncode, 2, tool)
        other = self.run_hook("claude", {"tool_name": "mcp__github__list_pull_requests",
                                         "tool_input": {}, "cwd": str(self.root)})
        self.assertEqual(other.returncode, 0)

    def test_prefilter_skips_the_interpreter_for_ordinary_commands(self):
        (self.root / "scripts/qa/finalise-gate.py").write_text("raise SystemExit(9)\n")
        proc = self.run_hook("claude", {"tool_name": "Bash", "tool_input": {"command": "ls -la"},
                                        "cwd": str(self.root)})
        self.assertEqual(proc.returncode, 0)  # the broken interpreter path never ran

    def test_non_hand_off_never_runs_checks(self):
        self.write_qa(mode="block")
        self.break_source()
        proc = self.run_hook("claude", {"tool_name": "Bash", "tool_input": {"command": "git status"},
                                        "cwd": str(self.root)})
        self.assertEqual(proc.returncode, 0)
        self.assertFalse((self.root / ".ai/agent-checks/finalise-gate.log").exists())

    def test_missing_config_defaults_to_warn_with_model_visible_context(self):
        self.break_source()
        for adapter in ("claude", "codex"):
            proc = self.run_hook(adapter)
            self.assertEqual(proc.returncode, 0, adapter)
            context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("FINALISE-GATE FAIL", context)
            self.assertIn("mode=warn", context)

    def test_shipped_config_is_warn(self):
        shipped = json.loads((ROOT / "config" / "qa.json").read_text())
        self.assertEqual(shipped["finalise_gate"]["mode"], "warn")

    def test_environment_setup_failure_is_incomplete(self):
        self.write_qa(mode="block")
        config = json.loads((self.root / "config/agent-checks.json").read_text())
        config["environments"] = [{"id": "deps", "commands": [[sys.executable, "-c", "raise SystemExit(1)"]]}]
        config["checks"][0]["environment"] = "deps"
        (self.root / "config/agent-checks.json").write_text(json.dumps(config))
        self.break_source()
        proc = self.run_hook("claude")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("INCOMPLETE", proc.stdout)

    def test_check_timeout_and_exit_3_are_incomplete(self):
        self.write_qa(mode="block")
        for command in ([sys.executable, "-c", "raise SystemExit(3)"],
                        [sys.executable, "-c", "import time; time.sleep(30)"]):
            config = {"schema_version": 1, "base": "main", "environments": [],
                      "checks": [{"id": "slow", "paths": ["src/*"], "tools": ["python"],
                                  "timeout_seconds": 1, "commands": [command]}]}
            (self.root / "config/agent-checks.json").write_text(json.dumps(config))
            self.break_source()
            proc = self.run_hook("claude")
            self.assertEqual(proc.returncode, 0, command)
            self.assertIn("INCOMPLETE", proc.stdout)

    def test_repository_outside_the_workspace_is_not_run(self):
        self.write_qa(mode="block")
        self.break_source()
        elsewhere = Path(self.temp.name) / "workspace"
        elsewhere.mkdir()
        proc = self.run_hook("claude", CLAUDE_PROJECT_DIR=str(elsewhere))
        self.assertEqual(proc.returncode, 0)
        self.assertFalse((self.root / ".ai/agent-checks/finalise-gate.log").exists())
        inside = self.run_hook("claude", CLAUDE_PROJECT_DIR=str(self.root.parent))
        self.assertEqual(inside.returncode, 2)

    def test_cursor_root_cwd_falls_back_to_workspace_root(self):
        self.write_qa(mode="block")
        self.break_source()
        cursor = self.run_hook("cursor", {"command": "git push", "cwd": "/",
                                          "workspace_roots": [str(self.root)]})
        self.assertEqual(json.loads(cursor.stdout)["permission"], "deny")

    def test_bypass_is_allowed_and_audited(self):
        self.write_qa(mode="block")
        self.break_source()
        proc = self.run_hook("claude", {"tool_name": "Bash", "cwd": str(self.root),
                                        "tool_input": {"command": "AGENT_FINALISE_GATE=off git push"}})
        self.assertEqual(proc.returncode, 0)
        log = (self.root / ".ai/bypass-log.jsonl").read_text()
        self.assertIn('"scope": "finalise-gate"', log)
        self.assertEqual(self.run_hook("codex", AGENT_FINALISE_GATE="off").returncode, 0)

    def test_unavailable_toolchain_is_incomplete_not_blocking(self):
        self.write_qa(mode="block")
        self.write_checks(["definitely-not-an-installed-tool-xyz"])
        self.break_source()
        proc = self.run_hook("codex")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("FINALISE-GATE INCOMPLETE", proc.stdout)

    def test_blocked_runner_is_incomplete_not_blocking(self):
        self.write_qa(mode="block")
        (self.root / "config" / "agent-checks.json").write_text(json.dumps(
            {"schema_version": 1, "base": "origin/does-not-exist", "checks": []}))
        proc = self.run_hook("claude")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("INCOMPLETE", proc.stdout)

    def test_unadopted_repository_is_a_no_op(self):
        (self.root / "config" / "agent-checks.json").unlink()
        cursor = self.run_hook("cursor", {"command": "git push", "cwd": str(self.root)})
        self.assertEqual(json.loads(cursor.stdout), {"permission": "allow"})
        self.assertEqual(self.run_hook("claude").returncode, 0)

    def test_ledger_records_cost_and_outcome(self):
        self.run_hook("claude")
        rows = [json.loads(line) for line in
                (self.root / ".ai/agent-checks/finalise-gate.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["outcome"], "pass")
        self.assertIn("duration_ms", rows[-1])

    def test_launcher_fails_open_for_cursor_without_gate(self):
        (self.root / "scripts/qa/finalise-gate.py").unlink()
        cursor = self.run_hook("cursor", {"command": "git push", "cwd": str(self.root)})
        self.assertEqual(json.loads(cursor.stdout), {"permission": "allow"})

    def test_query_replace_legacy_pre_push(self):
        query = [sys.executable, str(self.root / "scripts/qa/finalise-gate.py"),
                 "--query", "replace-legacy-pre-push"]
        self.assertEqual(subprocess.run(query, cwd=self.root, env=clean_env()).returncode, 1)
        self.write_qa(mode="block", replace_legacy_pre_push=True)
        self.assertEqual(subprocess.run(query, cwd=self.root, env=clean_env()).returncode, 0)


class GitShimTests(GateRepo):
    def install(self):
        return subprocess.run([sys.executable, str(self.root / "scripts/qa/finalise-gate.py"),
                               "--install-git-hook"], cwd=self.root, capture_output=True,
                              text=True, env=clean_env())

    def test_installs_marked_shim_and_refreshes_it(self):
        self.assertEqual(self.install().returncode, 0)
        shim = self.root / ".git/hooks/pre-push"
        self.assertIn("managed-by: ai-starter-pack finalise-gate", shim.read_text())
        self.assertTrue(os.access(shim, os.X_OK))
        self.assertEqual(self.install().returncode, 0)

    def test_never_displaces_a_foreign_hook_or_hook_manager(self):
        foreign = self.root / ".git/hooks/pre-push"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text("#!/bin/sh\necho mine\n")
        self.install()
        self.assertEqual(foreign.read_text(), "#!/bin/sh\necho mine\n")
        foreign.unlink()
        self.git("config", "core.hooksPath", ".husky/_")
        self.install()
        self.assertFalse(foreign.exists())

    def test_shim_blocks_a_failing_push(self):
        self.write_qa(mode="block")
        self.install()
        bare = Path(self.temp.name) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=clean_env())
        self.git("remote", "add", "origin", str(bare))
        self.git("checkout", "-qb", "feat")  # the candidate differs from base only on a branch
        self.break_source()
        self.git("commit", "-qam", "break")
        proc = subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/x"], cwd=self.root,
                              capture_output=True, text=True, env=clean_env())
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FINALISE-GATE FAIL", proc.stderr)


@unittest.skipUnless(shutil.which("node"), "node is required for ui-smoke tests")
class UiSmokeSelectionTests(unittest.TestCase):
    def node(self, expression):
        script = (f"import * as m from {json.dumps(SMOKE.as_uri())};\n"
                  f"process.stdout.write(JSON.stringify({expression}));")
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_next_route_inference(self):
        cases = {
            "app/page.tsx": "/", "src/app/settings/page.tsx": "/settings",
            "app/(marketing)/pricing/page.mdx": "/pricing", "app/dashboard/layout.tsx": "/dashboard",
            "pages/index.tsx": "/", "pages/about/team.tsx": "/about/team",
            "pages/api/health.ts": None, "pages/_app.tsx": None, "app/blog/[slug]/page.tsx": None,
            "src/components/Button.tsx": None, "app/global-error.tsx": None,
            "app/ui/landing-page.tsx": None, "app/x/_components/page.tsx": None,
            "app/(.)photo/page.tsx": None, "app/@modal/settings/page.tsx": "/settings",
            "e2e/pages/login-page.ts": None, "src/components/pages/Foo.tsx": None,
            "pages/about.test.tsx": None, "apps/web/src/app/b/page.tsx": "/b",
            "packages/site/pages/y.tsx": "/y", "foo/app/page.tsx": None,
        }
        got = self.node("Object.fromEntries(" + json.dumps(list(cases)) +
                        ".map((f) => [f, m.inferNextRoute(f)]))")
        self.assertEqual(got, cases)
        self.assertEqual(self.node('m.inferNextRoute("app/blog/[slug]/page.tsx", {"/blog/[slug]": "/blog/hello"})'),
                         "/blog/hello")

    def test_selection_maps_globs_falls_back_and_caps(self):
        config = {"routes": [{"path": "/checkout", "sources": ["src/features/checkout/**"]}],
                  "always_routes": ["/"], "infer_next_routes": True, "dynamic_samples": {}, "max_routes": 2}
        def select(changed):
            return self.node(f"m.selectRoutes({json.dumps(config)}, "
                             f"{{changed: {json.dumps(changed)}, all: false, routes: null}})")
        self.assertEqual(select(["src/features/checkout/Cart.tsx"])["routes"], ["/checkout"])
        self.assertEqual(select(["src/lib/util.ts"])["routes"], ["/"])
        self.assertEqual(select([])["routes"], ["/"])
        capped = select(["app/a/page.tsx", "app/b/page.tsx", "app/c/page.tsx"])
        self.assertEqual(capped["routes"], ["/a", "/b"])
        self.assertTrue(any("max_routes" in note for note in capped["notes"]))
        dynamic = select(["app/blog/[slug]/page.tsx"])
        self.assertEqual(dynamic["routes"], ["/"])
        self.assertTrue(any("dynamic_samples" in note for note in dynamic["notes"]))

    def test_glob_semantics(self):
        got = self.node('[m.globToRegExp("src/**/*.tsx").test("src/a/b/c.tsx"), '
                        'm.globToRegExp("src/**/*.tsx").test("src/c.tsx"), '
                        'm.globToRegExp("src/*.tsx").test("src/a/c.tsx")]')
        self.assertEqual(got, [True, True, False])


def _playwright_ready() -> bool:
    if not shutil.which("node"):
        return False
    probe = ("const {createRequire}=require('module');const path=require('path');"
             "const r=createRequire(process.cwd()+'/x.js');"
             "const paths=[process.cwd(),...(process.env.NODE_PATH||'').split(path.delimiter).filter(Boolean),"
             "path.join(path.dirname(process.execPath),'..','lib','node_modules')];"
             "const pw=require(r.resolve('playwright',{paths}));"
             "require('fs').accessSync(pw.chromium.executablePath());")
    return subprocess.run(["node", "-e", probe], capture_output=True, cwd=ROOT, timeout=60).returncode == 0


PAGES = {
    "/": "<!doctype html><html lang=en><head><title>ok</title><meta name=viewport content='width=device-width'></head><body><main><h1>Home</h1></main></body></html>",
    "/broken": ("<!doctype html><html lang=en><head><title>broken</title><meta name=viewport content='width=device-width'></head><body><main><h1>B</h1>"
                "<div style='width:900px'>wide</div><script>throw new Error('boom')</script></main></body></html>"),
}


SEEN_HEADERS: list[tuple[int, str, str | None]] = []
REDIRECTS: dict[str, str] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        SEEN_HEADERS.append((self.server.server_address[1], self.path,
                             self.headers.get("x-vercel-protection-bypass")))
        if self.path in REDIRECTS:
            self.send_response(302)
            self.send_header("Location", REDIRECTS[self.path])
            self.end_headers()
            return
        body = PAGES.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/javascript" if self.path.endswith(".js") else "text/html")
        self.end_headers()
        self.wfile.write((body or "missing").encode())

    def log_message(self, *args):
        pass


@unittest.skipUnless(_playwright_ready(), "playwright + chromium not installed")
class UiSmokeBrowserTests(unittest.TestCase):
    def setUp(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{port}"
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        (self.work / "config").mkdir()
        (self.work / "config/qa.json").write_text(json.dumps({"schema_version": 1, "ui_smoke": {
            "axe": {"enabled": False}, "viewports": [{"name": "mobile", "width": 375, "height": 700}]}}))

    def smoke(self, *args):
        return subprocess.run(["node", str(SMOKE), "--base-url", self.base, *args], cwd=self.work,
                              capture_output=True, text=True, timeout=180)

    def test_clean_route_passes_and_writes_artifacts(self):
        proc = self.smoke("--routes", "/")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("UI-SMOKE-PASSED", proc.stdout)
        report = json.loads((self.work / ".ai/agent-checks/ui-smoke/report.json").read_text())
        self.assertEqual(report["status"], "passed")
        self.assertTrue((self.work / ".ai/agent-checks/ui-smoke/root.mobile.png").is_file())

    def test_runtime_error_overflow_and_missing_page_fail(self):
        proc = self.smoke("--routes", "/broken,/missing")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("runtime error: boom", proc.stdout)
        self.assertIn("horizontal overflow", proc.stdout)
        self.assertIn("document HTTP 404", proc.stdout)

    def test_missing_viewport_meta_is_a_mobile_failure(self):
        PAGES["/no-meta"] = "<!doctype html><html lang=en><head><title>x</title></head><body><main>x</main></body></html>"
        self.addCleanup(PAGES.pop, "/no-meta")
        proc = self.smoke("--routes", "/no-meta")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("no responsive viewport", proc.stdout)

    def test_bypass_header_reaches_only_the_app_origin(self):
        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.shutdown)
        other_port = other.server_address[1]
        PAGES["/hdr"] = ("<!doctype html><html lang=en><head><title>h</title>"
                         "<meta name=viewport content='width=device-width'></head><body><main>"
                         f"<img alt='' src='http://127.0.0.1:{other_port}/'><img alt='' src='/'></main></body></html>")
        self.addCleanup(PAGES.pop, "/hdr")
        SEEN_HEADERS.clear()
        proc = subprocess.run(["node", str(SMOKE), "--base-url", self.base, "--routes", "/hdr"],
                              cwd=self.work, capture_output=True, text=True, timeout=120,
                              env={**os.environ, "VERCEL_AUTOMATION_BYPASS_SECRET": "s3cret"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        app_port = self.server.server_address[1]
        # The first app request is the header-less reachability probe.
        app = [h for port, _, h in SEEN_HEADERS if port == app_port][1:]
        third_party = [h for port, _, h in SEEN_HEADERS if port == other_port]
        self.assertTrue(app and all(h == "s3cret" for h in app), SEEN_HEADERS)
        self.assertTrue(third_party and all(h is None for h in third_party), SEEN_HEADERS)
        self.assertNotIn("s3cret", proc.stdout + proc.stderr)

    def test_third_party_console_errors_are_not_app_failures(self):
        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.shutdown)
        PAGES["/widget.js"] = "console.error('third-party widget misconfigured')"
        self.addCleanup(PAGES.pop, "/widget.js")
        port = other.server_address[1]
        PAGES["/3p"] = ("<!doctype html><html lang=en><head><title>t</title>"
                        "<meta name=viewport content='width=device-width'>"
                        f"<script src='http://localhost:{port}/widget.js'></script></head>"
                        "<body><main>x</main><script>console.error('own app error')</script></body></html>")
        self.addCleanup(PAGES.pop, "/3p")
        proc = self.smoke("--routes", "/3p")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("own app error", proc.stdout)
        self.assertNotIn("third-party widget", proc.stdout)

    def test_unknown_launch_network_is_a_config_error(self):
        (self.work / "config/qa.json").write_text(json.dumps(
            {"schema_version": 1, "ui_smoke": {"launch": {"network": "slow-4g"}}}))
        proc = self.smoke("--routes", "/")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("launch.network", proc.stderr)

    def test_unreachable_server_is_reported_unavailable(self):
        proc = subprocess.run(["node", str(SMOKE), "--base-url", "http://127.0.0.1:9", "--routes", "/"],
                              cwd=self.work, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("UI-SMOKE-UNAVAILABLE", proc.stdout)

    def test_bypass_header_never_follows_a_cross_origin_redirect(self):
        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.shutdown)
        other_port = other.server_address[1]
        REDIRECTS["/sso"] = f"http://127.0.0.1:{other_port}/"
        self.addCleanup(REDIRECTS.pop, "/sso")
        SEEN_HEADERS.clear()
        subprocess.run(["node", str(SMOKE), "--base-url", self.base, "--routes", "/sso"],
                       cwd=self.work, capture_output=True, text=True, timeout=120,
                       env={**os.environ, "VERCEL_AUTOMATION_BYPASS_SECRET": "s3cret"})
        hops = [h for port, _, h in SEEN_HEADERS if port == other_port]
        self.assertTrue(hops, SEEN_HEADERS)
        self.assertTrue(all(h is None for h in hops), SEEN_HEADERS)

    def test_started_server_is_stopped_when_smoke_is_killed(self):
        marker = self.work / "server.pid"
        server = self.work / "srv.py"
        server.write_text("import http.server, os, pathlib, sys\n"
                          f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
                          "class H(http.server.BaseHTTPRequestHandler):\n"
                          "    def do_GET(self):\n"
                          "        import time; time.sleep(60)\n"
                          "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()\n")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        (self.work / "config/qa.json").write_text(json.dumps({"schema_version": 1, "ui_smoke": {
            "axe": {"enabled": False}, "start_command": [sys.executable, str(server), str(port)],
            "viewports": [{"name": "desktop", "width": 1280, "height": 800}]}}))
        proc = subprocess.Popen(["node", str(SMOKE), "--base-url", f"http://127.0.0.1:{port}", "--routes", "/"],
                                cwd=self.work, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.2)
        self.assertTrue(marker.exists(), "start_command never ran")
        pid = int(marker.read_text())
        time.sleep(1)
        proc.kill()  # SIGKILL: no handler runs, only the watchdog can clean up
        proc.wait()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            os.kill(pid, 9)
            self.fail("dev server outlived a SIGKILLed ui-smoke")


if __name__ == "__main__":
    unittest.main()
