#!/usr/bin/env python3
"""Exercise local execution, invalidation and native-surface delivery."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ascii_environment():
    return load("agent-check").git_environment({
        **os.environ, "LC_ALL": "C", "LANG": "C",
        "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0",
    })


class AgentCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for directory in ("scripts", "config", "src"):
            (self.root / directory).mkdir()
        shutil.copyfile(SCRIPTS / "agent-check.py", self.root / "scripts/agent-check.py")
        (self.root / ".gitignore").write_text(".ai/agent-checks/\nnode_modules/\n")
        (self.root / "src/value.py").write_text("value = 1\n")
        (self.root / "lock.json").write_text("{}\n")
        self.config = {
            "schema_version": 1, "base": "origin/main", "environments": [],
            "checks": [{"id": "test", "paths": ["src/*", "lock.json", "config/agent-checks.json"],
                        "commands": [[sys.executable, "-c", "from pathlib import Path; assert 'value = 1' in Path('src/value.py').read_text()"]]}],
        }
        self.save_config()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.PIPE,
                                       env=load("agent-check").git_environment())

    def save_config(self):
        (self.root / "config/agent-checks.json").write_text(json.dumps(self.config))

    def run_check(self, *args, expected=0, env=None):
        result = subprocess.run([sys.executable, str(self.root / "scripts/agent-check.py"),
                                 "--repo-root", str(self.root), "--provider", "codex", *args],
                                capture_output=True, text=True,
                                env=load("agent-check").git_environment() if env is None else env, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def receipt(self):
        return json.loads((self.root / ".ai/agent-checks/receipt.json").read_text())

    def change_source(self):
        (self.root / "src/value.py").write_text("value = 1\n# changed\n")

    def test_utf8_repository_and_receipts_ignore_default_locale(self):
        self.config["note"] = "Café validation"
        (self.root / "workflow.yml").write_text("# Café validation\n", encoding="utf-8")
        self.config["checks"][0]["ci_sources"] = [{"path": "workflow.yml", "contains": "Café validation"}]
        setup = ("from pathlib import Path; p=Path('node_modules'); p.mkdir(exist_ok=True); "
                 "c=p/'installs'; c.write_text(c.read_text()+'1' if c.exists() else '1')")
        self.config["environments"] = [{"id": "dependencies", "ready_paths": ["node_modules"],
                                        "commands": [[sys.executable, "-c", setup]]}]
        self.config["checks"][0]["environment"] = "dependencies"
        (self.root / "config/agent-checks.json").write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")
        env = ascii_environment()
        self.run_check("--provision", env=env)
        state = self.root / ".ai/agent-checks"
        for path in [*state.glob("environments/*.json"), *state.glob("cache/*.json")]:
            value = json.loads(path.read_text(encoding="utf-8"))
            value["note"] = "Café metadata"
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        self.run_check("--provision", env=env)
        self.assertTrue(self.receipt()["cached"])
        self.assertEqual((self.root / "node_modules/installs").read_text(), "1")

    def test_invalid_utf8_environment_receipt_requires_fresh_setup(self):
        setup = ("from pathlib import Path; p=Path('node_modules'); p.mkdir(exist_ok=True); "
                 "c=p/'installs'; c.write_text(c.read_text()+'1' if c.exists() else '1')")
        self.config["environments"] = [{"id": "dependencies", "ready_paths": ["node_modules"],
                                        "commands": [[sys.executable, "-c", setup]]}]
        self.config["checks"][0]["environment"] = "dependencies"
        self.save_config()
        env = ascii_environment()
        self.run_check("--provision", env=env)
        receipt = next((self.root / ".ai/agent-checks/environments").glob("*.json"))
        receipt.write_bytes(b"\xff")
        self.run_check(expected=1, env=env)
        self.assertEqual(self.receipt()["outcome"], "failed")
        self.assertEqual((self.root / "node_modules/installs").read_text(), "1")
        self.run_check("--provision", env=env)
        self.assertEqual((self.root / "node_modules/installs").read_text(), "11")
        self.assertEqual(json.loads(receipt.read_text())["result"]["status"], "passed")

    def test_python_check_does_not_create_untracked_bytecode(self):
        self.config["checks"][0]["commands"] = [[sys.executable, "-c", "import src.value"]]
        self.save_config()
        self.run_check()
        self.assertEqual(self.receipt()["outcome"], "passed")
        self.assertEqual(list((self.root / "src").rglob("*.pyc")), [])

    def test_execute_and_reuse_only_identical_candidate(self):
        self.change_source()
        self.run_check()
        first = self.receipt()
        self.assertFalse(first["cached"])
        self.assertFalse(first["merge_authority"])
        self.run_check()
        self.assertTrue(self.receipt()["cached"])
        (self.root / "src/value.py").write_text("value = 2\n")
        self.run_check(expected=1)
        self.assertNotEqual(first["digest"], self.receipt()["digest"])

    def test_staged_untracked_and_deleted_paths_count(self):
        self.change_source()
        self.git("add", "src/value.py")
        (self.root / "src/new.py").write_text("new = 1\n")
        (self.root / "lock.json").unlink()
        plan = json.loads(self.run_check("--plan").stdout)
        self.assertEqual(plan["paths"], ["lock.json", "src/new.py", "src/value.py"])
        self.assertEqual(len(plan["checks"]), 1)

    def test_rename_out_of_scope_still_checks_removed_source(self):
        self.git("mv", "src/value.py", "archived.txt")
        plan = json.loads(self.run_check("--plan").stdout)
        self.assertIn("src/value.py", plan["paths"])
        self.assertEqual([c["id"] for c in plan["checks"]], ["test"])

    def test_docs_do_not_select_application_suite(self):
        (self.root / "README.md").write_text("docs\n")
        self.assertEqual(json.loads(self.run_check("--plan").stdout)["checks"], [])

    def test_workflow_lint_only_receives_affected_workflows(self):
        (self.root / ".github/workflows").mkdir(parents=True)
        (self.root / ".github/workflows/changed.yml").write_text("name: changed\n")
        self.config["checks"] = [{"id": "workflow", "paths": [".github/workflows/*"],
                                  "append_changed_paths": True, "commands": [["actionlint"]]}]
        self.save_config()
        plan = json.loads(self.run_check("--plan").stdout)
        self.assertEqual(plan["checks"][0]["commands"], [["actionlint", ".github/workflows/changed.yml"]])

    def test_framework_change_does_not_implicitly_run_application_suite(self):
        self.config["checks"][0]["paths"] = ["src/*"]
        self.save_config()
        self.assertEqual(json.loads(self.run_check("--plan").stdout)["checks"], [])

    def test_deleted_workflow_alongside_edit_validates_remaining_callers(self):
        directory = self.root / ".github/workflows"
        directory.mkdir(parents=True)
        (directory / "reusable.yml").write_text("name: reusable\n")
        self.git("add", ".github")
        self.git("commit", "-qm", "workflow fixture")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        (directory / "reusable.yml").unlink()
        (directory / "unrelated.yml").write_text("name: unrelated\n")
        self.config["checks"] = [{"id": "workflow", "paths": [".github/workflows/*"],
                                  "append_changed_paths": True, "commands": [["actionlint"]]}]
        self.save_config()
        plan = json.loads(self.run_check("--plan").stdout)
        self.assertEqual(plan["checks"][0]["commands"], [["actionlint"]])

    def test_manifest_lock_and_environment_inputs_revalidate(self):
        self.config["environments"] = [{
            "id": "dependencies", "inputs": ["lock.json"], "ready_paths": ["node_modules"],
            "commands": [[sys.executable, "-c", "from pathlib import Path; p=Path('node_modules'); p.mkdir(exist_ok=True); count=p/'installs'; count.write_text(count.read_text()+'1' if count.exists() else '1')"]],
        }]
        self.config["checks"][0]["environment"] = "dependencies"
        self.save_config()
        self.run_check(expected=1)
        self.run_check("--provision")
        self.run_check("--provision")
        counter = self.root / "node_modules/installs"
        self.assertEqual(counter.read_text(), "1")
        (self.root / "lock.json").write_text('{"changed":true}')
        self.run_check("--provision")
        self.assertEqual(counter.read_text(), "11")
        shutil.rmtree(self.root / "node_modules")
        self.run_check("--provision")
        self.assertEqual(counter.read_text(), "1")

    def test_failed_install_never_runs_application_check(self):
        self.config["environments"] = [{"id": "dependencies", "commands": [[sys.executable, "-c", "raise SystemExit(4)"]]}]
        self.config["checks"][0]["environment"] = "dependencies"
        self.save_config()
        self.run_check("--provision", expected=1)
        self.assertEqual([r["id"] for r in self.receipt()["results"]], ["diff-check", "dependencies"])
        self.assertFalse((self.root / ".ai/agent-checks/environments").exists())

    def test_all_environment_receipts_use_final_provisioned_tool_versions(self):
        state = self.root / ".ai/agent-checks"
        binaries = state / "bin"
        binaries.mkdir(parents=True)
        binary = binaries / "node"
        binary.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
                          "ready=Path('.ai/agent-checks/tool-ready').exists()\n"
                          "print('vfixture-1' if ready else 'not installed')\n"
                          "sys.exit(0 if ready else 1)\n")
        binary.chmod(0o755)
        environments = []
        checks = []
        for name in ("first", "second"):
            setup = ("from pathlib import Path; "
                     f"p=Path('node_modules/{name}'); p.mkdir(parents=True,exist_ok=True); "
                     "counter=p/'installs'; counter.write_text(counter.read_text()+'1' if counter.exists() else '1')")
            if name == "second":
                setup += "; Path('.ai/agent-checks/tool-ready').touch()"
            environments.append({"id": name, "inputs": ["lock.json"], "tools": ["node"],
                                 "ready_paths": [f"node_modules/{name}"],
                                 "commands": [[sys.executable, "-c", setup]]})
            checks.append({**self.config["checks"][0], "id": f"check-{name}", "environment": name, "tools": ["node"]})
        self.config.update({"environments": environments, "checks": checks,
                            "tool_requirements": {"node": "vfixture-1"}})
        self.save_config()
        env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}
        self.run_check("--provision", env=env)
        self.assertFalse(self.receipt()["cached"])
        self.run_check("--provision", env=env)
        self.assertTrue(self.receipt()["cached"])
        self.run_check(env=env)
        self.assertTrue(self.receipt()["cached"])
        for name in ("first", "second"):
            self.assertEqual((self.root / f"node_modules/{name}/installs").read_text(), "1")

    def test_failed_reinstall_cannot_revive_receipt_for_restored_tool_version(self):
        state = self.root / ".ai/agent-checks"
        binaries = state / "bin"
        binaries.mkdir(parents=True)
        version = state / "tool-version"
        version.write_text("vfixture-1")
        binary = binaries / "node"
        binary.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
                          "print(Path('.ai/agent-checks/tool-version').read_text())\n")
        binary.chmod(0o755)
        setup = ("from pathlib import Path; "
                 "p=Path('node_modules'); p.mkdir(exist_ok=True); "
                 "Path('.ai/agent-checks/tool-version').write_text('vfixture-1'); "
                 "raise SystemExit(4 if Path('.ai/agent-checks/fail-install').exists() else 0)")
        self.config["environments"] = [{"id": "dependencies", "tools": ["node"],
                                        "ready_paths": ["node_modules"],
                                        "commands": [[sys.executable, "-c", setup]]}]
        self.config["checks"][0].update({"environment": "dependencies", "tools": ["node"]})
        self.save_config()
        env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}
        self.run_check("--provision", env=env)
        version.write_text("vfixture-2")
        (state / "fail-install").touch()
        self.run_check("--provision", expected=1, env=env)
        self.assertEqual(version.read_text(), "vfixture-1")
        self.assertTrue((self.root / "node_modules").is_dir())
        self.run_check(expected=1, env=env)
        self.assertFalse(self.receipt()["cached"])

    def test_reprovisioning_cannot_reuse_pass_after_mutating_checked_source(self):
        setup = (
            "from pathlib import Path; "
            "marker=Path('.ai/agent-checks/setup-ran'); "
            "Path('src/value.py').write_text('value = 2') if marker.exists() else None; "
            "marker.parent.mkdir(parents=True,exist_ok=True); marker.touch(); "
            "Path('node_modules').mkdir(exist_ok=True)"
        )
        self.config["environments"] = [{"id": "dependencies", "inputs": ["lock.json"],
                                        "ready_paths": ["node_modules"],
                                        "commands": [[sys.executable, "-c", setup]]}]
        self.config["checks"][0]["environment"] = "dependencies"
        self.save_config()
        self.run_check("--provision")
        self.run_check("--provision")
        self.assertTrue(self.receipt()["cached"])
        shutil.rmtree(self.root / "node_modules")
        self.run_check("--provision", expected=1)
        self.assertFalse(self.receipt()["cached"])
        self.assertIn("input-stability", [r["id"] for r in self.receipt()["results"]])

    def test_failed_reinstall_revokes_old_environment_pass(self):
        setup = (
            "from pathlib import Path; "
            "marker=Path('.ai/agent-checks/setup-ran'); "
            "Path('node_modules').mkdir(exist_ok=True); "
            "failed=marker.exists(); marker.parent.mkdir(parents=True,exist_ok=True); marker.touch(); "
            "raise SystemExit(4 if failed else 0)"
        )
        self.config["environments"] = [{"id": "dependencies", "inputs": ["lock.json"],
                                        "ready_paths": ["node_modules"],
                                        "commands": [[sys.executable, "-c", setup]]}]
        self.config["checks"][0]["environment"] = "dependencies"
        self.save_config()
        self.run_check("--provision")
        shutil.rmtree(self.root / "node_modules")
        self.run_check("--provision", expected=1)
        # A partial install directory cannot resurrect the former success.
        self.assertTrue((self.root / "node_modules").is_dir())
        self.run_check(expected=1)
        self.run_check("--provision", expected=1)
        self.assertFalse(self.receipt()["cached"])

    def test_tool_version_changes_invalidate_cached_pass(self):
        directory = self.root / "node_modules/bin"
        directory.mkdir(parents=True)
        for tool, executable, old, new in (("node", "node", "v22.1.0", "v22.2.0"),
                                           ("swift", "swiftc", "Apple Swift version 6.2.0", "Apple Swift version 6.2.1")):
            with self.subTest(tool=tool):
                binary = directory / executable
                binary.write_text(f"#!{sys.executable}\nimport os\nprint(os.environ['FIXTURE_TOOL_VERSION'])\n")
                binary.chmod(0o755)
                self.config["checks"][0]["tools"] = [tool]
                self.save_config()
                env = {**os.environ, "PATH": str(directory) + os.pathsep + os.environ["PATH"], "FIXTURE_TOOL_VERSION": old}
                self.run_check(env=env)
                first = self.receipt()["digest"]
                self.run_check(env=env)
                self.assertTrue(self.receipt()["cached"])
                env["FIXTURE_TOOL_VERSION"] = new
                self.run_check(env=env)
                self.assertNotEqual(first, self.receipt()["digest"])
                self.assertFalse(self.receipt()["cached"])

        # A nested package's Corepack pin must be probed in that package,
        # with the same declared environment as the validation command.
        (self.root / "app").mkdir()
        pnpm = directory / "pnpm"
        pnpm.write_text(f"#!{sys.executable}\nimport os\nfrom pathlib import Path\n"
                        "print(os.environ['PINNED_VERSION'] if Path.cwd().name == 'app' else '11.17.0')\n")
        pnpm.chmod(0o755)
        self.config["checks"][0].update({"tools": ["pnpm"], "cwd": "app",
                                          "env": {"PINNED_VERSION": "11.5.2"},
                                          "commands": [[sys.executable, "-c",
                                                        "from pathlib import Path; assert 'value = 1' in Path('../src/value.py').read_text()"]]})
        self.config["tool_requirements"] = {"pnpm": "11.5.2"}
        self.save_config()
        self.run_check(env={**os.environ, "PATH": str(directory) + os.pathsep + os.environ["PATH"]})
        versions = self.receipt()["tool_versions"]
        self.assertEqual([v for k, v in versions.items() if k.startswith("pnpm@app#")], ["11.5.2"])

    def test_changed_inputs_during_execution_cannot_pass(self):
        self.config["checks"][0]["commands"] = [[sys.executable, "-c", "from pathlib import Path; Path('src/value.py').write_text('mutated')"]]
        self.save_config()
        self.run_check(expected=1)
        self.assertIn("input-stability", [r["id"] for r in self.receipt()["results"]])

    def test_paths_cannot_escape_repo(self):
        self.config["checks"][0]["cwd"] = "../"
        self.save_config()
        self.run_check(expected=2)

    def test_missing_command_reports_unavailable_and_never_passes(self):
        self.config["checks"][0]["commands"] = [["agent-check-fixture-nonexistent"]]
        self.save_config()
        self.run_check(expected=1)
        self.assertIn("unavailable", [r["status"] for r in self.receipt()["results"]])

    def test_receipts_must_be_ignored(self):
        (self.root / ".gitignore").write_text("")
        self.run_check(expected=2)

    def test_inherited_git_locations_do_not_redirect_validation(self):
        self.change_source()
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory).resolve()
            (other / "other.txt").write_text("another repository\n")
            for args in (("init", "-q"), ("config", "user.email", "fixture@example.invalid"),
                         ("config", "user.name", "Fixture"), ("add", "."),
                         ("commit", "-qm", "foreign fixture"),
                         ("update-ref", "refs/remotes/origin/main", "HEAD")):
                subprocess.run(["git", *args], cwd=other, check=True, capture_output=True,
                               env=load("agent-check").git_environment())
            index = (other / ".git/index").read_bytes()
            inherited = {**os.environ, "GIT_DIR": str(other / ".git"),
                         "GIT_COMMON_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                         "GIT_INDEX_FILE": str(other / ".git/index"),
                         "GIT_OBJECT_DIRECTORY": str(other / ".git/objects"),
                         "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other / ".git/objects")}
            plan = json.loads(self.run_check("--plan", env=inherited).stdout)
            self.assertEqual(plan["paths"], ["src/value.py"])
            self.assertEqual(plan["base"], self.git("rev-parse", "HEAD").decode().strip())
            self.run_check(env=inherited)
            self.assertEqual(self.receipt()["snapshot"]["head"], plan["base"])
            self.assertEqual((other / ".git/index").read_bytes(), index)

    def test_blocked_execution_revokes_old_receipt_but_plan_does_not(self):
        self.change_source()
        runner = load("agent-check")
        receipt = self.root / ".ai/agent-checks/receipt.json"
        for failure in ("config", "base", "ignore", "snapshot", "cache-write"):
            with self.subTest(failure=failure):
                self.save_config()
                (self.root / ".gitignore").write_text(".ai/agent-checks/\nnode_modules/\n")
                self.run_check()
                original = receipt.read_bytes()
                self.run_check("--plan")
                self.assertEqual(receipt.read_bytes(), original)
                if failure == "config":
                    (self.root / "config/agent-checks.json").write_text("{")
                    self.run_check("--plan", expected=2)
                    self.assertEqual(receipt.read_bytes(), original)
                    self.run_check(expected=2)
                elif failure == "base":
                    self.run_check("--base", "refs/heads/fixture-missing-base", expected=2)
                elif failure == "ignore":
                    (self.root / ".gitignore").write_text("")
                    self.run_check(expected=2)
                elif failure == "snapshot":
                    with mock.patch.object(runner, "snapshot", side_effect=OSError("fixture snapshot failure")):
                        self.assertEqual(runner.main(["--repo-root", str(self.root)]), 2)
                else:
                    real_write = runner.write_json

                    def fail_cache_write(path, value):
                        if path.parent.name == "cache":
                            raise OSError("fixture cache write failure")
                        real_write(path, value)

                    with mock.patch.object(runner, "write_json", side_effect=fail_cache_write):
                        self.assertEqual(runner.main(["--repo-root", str(self.root), "--no-cache"]), 2)
                self.assertFalse(receipt.exists(), "blocked execution retained an older PASS receipt")

    def test_ci_toolchain_mismatch_is_incomplete_validation(self):
        self.config["tool_requirements"] = {"python": "Python 0."}
        self.save_config()
        self.run_check(expected=1)
        self.assertIn("unavailable", [r["status"] for r in self.receipt()["results"]])

    def test_changed_ci_command_requires_recipe_update(self):
        (self.root / "workflow.yml").write_text("run: new-command\n")
        self.config["checks"][0]["ci_sources"] = [{"path": "workflow.yml", "contains": "old-command"}]
        self.save_config()
        self.run_check(expected=2)


class SurfaceTests(unittest.TestCase):
    def test_installer_uses_target_branch_under_foreign_git_context(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root, foreign = base / "target", base / "foreign"
            (root / "config").mkdir(parents=True)
            foreign.mkdir()
            recipe = json.dumps({"schema_version": 1, "base": "origin/main", "checks": []})
            (root / "config/agent-checks.json").write_text(recipe)
            (root / "AGENTS.md").write_text("Local owner rules\n")
            clean_env = load("agent-check").git_environment()
            for repo, branch in ((root, "main"), (foreign, "codex/foreign-task")):
                for args in (("init", "-q"), ("checkout", "-qb", branch)):
                    subprocess.run(["git", *args], cwd=repo, env=clean_env, check=True, capture_output=True)
            env = {**clean_env, "GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
                   "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "fixture.transport", "GIT_CONFIG_VALUE_0": "preserved"}
            command = [sys.executable, str(SCRIPTS / "install-agent-validation.py"), "--repo-root", str(root)]
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertFalse((root / "scripts").exists())
            self.assertFalse((root / ".ai/agent-validation-source.json").exists())
            self.assertEqual((root / "AGENTS.md").read_text(), "Local owner rules\n")
            self.assertEqual((root / "config/agent-checks.json").read_text(), recipe)
            for repo, branch in ((root, "codex/target-task"), (foreign, "main")):
                subprocess.run(["git", "checkout", "-qb", branch], cwd=repo, env=clean_env,
                               check=True, capture_output=True)
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((root / "scripts/agent-check.py").is_file())
            self.assertIn("Local owner rules", (root / "AGENTS.md").read_text())

    def test_utf8_adoption_and_surfaces_preserve_owner_prose_in_ascii_locale(self):
        installer = load("install-agent-validation")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            pack, root = base / "pack", base / "repo"
            for rel in installer.FILES:
                original = SCRIPTS.parent / rel
                if not original.is_file():
                    original = SCRIPTS.parent / rel.removeprefix(".claude/")
                destination = pack / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, destination)
            policy = "# Café validation\n\nExecute the local check.\n"
            (pack / "docs/agent-validation.md").write_text(policy, encoding="utf-8")
            (root / "config").mkdir(parents=True)
            (root / "config/agent-checks.json").write_text(json.dumps({"schema_version": 1, "base": "origin/main", "checks": []}))
            owner = "---\ntitle: Café owner\n---\n\nLocal résumé rules.\n"
            (root / "AGENTS.md").write_text(owner, encoding="utf-8")
            (root / ".gitignore").write_text("# Café local ignores\n", encoding="utf-8")
            env = ascii_environment()
            for args in (("init", "-q"), ("checkout", "-qb", "codex/utf8-fixture")):
                subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)
            def run(script, *args):
                result = subprocess.run([sys.executable, str(script), "--repo-root", str(root), *args],
                                        env=env, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run(pack / "scripts/install-agent-validation.py")
            run(pack / "scripts/install-agent-validation.py", "--check")
            self.assertIn("Local résumé rules.", (root / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertTrue((root / "AGENTS.md").read_text(encoding="utf-8").startswith("---\ntitle: Café owner\n---\n"))
            self.assertTrue((root / ".gitignore").read_text(encoding="utf-8").startswith("# Café local ignores\n"))
            new_policy = "# Updated café validation\n"
            (root / "docs/agent-validation.md").write_text(new_policy, encoding="utf-8")
            run(root / "scripts/sync-agent-validation.py")
            run(root / "scripts/sync-agent-validation.py", "--check")
            for rel in ("AGENTS.md", ".claude/CLAUDE.md", ".cursor/rules/agent-validation.mdc"):
                self.assertIn(new_policy.strip(), (root / rel).read_text(encoding="utf-8"))
            self.assertIn("Local résumé rules.", (root / "AGENTS.md").read_text(encoding="utf-8"))

    def test_each_managed_input_selects_an_owning_check(self):
        installed = SCRIPTS.parent
        config_path = installed / "config/agent-checks.json"
        if not config_path.is_file():
            self.skipTest("plugin-only bundle has no repository-owned recipe")
        config = json.loads(config_path.read_text())
        runner = load("agent-check")
        paths = (*load("install-agent-validation").FILES, "config/agent-checks.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for rel in paths:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("original\n")
            for args in (("init", "-q"), ("config", "user.email", "fixture@example.invalid"),
                         ("config", "user.name", "Fixture"), ("add", "."),
                         ("commit", "-qm", "managed input fixture")):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                               env=runner.git_environment())
            for rel in paths:
                with self.subTest(path=rel):
                    path = root / rel
                    path.write_text("changed\n")
                    plan = runner.plan_for(root, config, "HEAD")
                    self.assertEqual(plan["paths"], [rel])
                    self.assertTrue(plan["checks"], f"no owning check covers {rel}")
                    renamed = {**config, "checks": [{**c, "id": f"owner-check-{i}"}
                                                     for i, c in enumerate(config["checks"])]}
                    owner_plan = runner.plan_for(root, renamed, "HEAD")
                    self.assertTrue(owner_plan["checks"], f"custom owner check ids lost coverage for {rel}")
                    self.assertEqual([c["commands"] for c in owner_plan["checks"]],
                                     [c["commands"] for c in plan["checks"]])
                    path.write_text("original\n")

    def test_installed_repository_surfaces_are_current(self):
        root = SCRIPTS.parent
        if not (root / "config/agent-checks.json").is_file():
            self.skipTest("plugin-only bundle has no repository-owned recipe")
        for path, expected in load("sync-agent-validation").outputs(root).items():
            self.assertTrue(path.is_file(), str(path))
            self.assertEqual(path.read_text(), expected, str(path))

    def test_same_policy_preserves_local_instructions_and_detects_drift(self):
        module = load("sync-agent-validation")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "docs").mkdir()
            (root / "docs/agent-validation.md").write_text("Shared command and policy\n")
            (root / "AGENTS.md").write_text("Local ownership rules\n")
            self.assertEqual(module.main(["--repo-root", str(root), "--dry-run"]), 0)
            self.assertEqual((root / "AGENTS.md").read_text(), "Local ownership rules\n")
            self.assertEqual(module.main(["--repo-root", str(root)]), 0)
            for rel in ("AGENTS.md", ".claude/CLAUDE.md", ".cursor/rules/agent-validation.mdc"):
                self.assertIn("Shared command and policy", (root / rel).read_text())
                self.assertFalse((root / rel).read_text().endswith("\n\n"))
            self.assertTrue((root / "AGENTS.md").read_text().endswith("Local ownership rules\n"))
            self.assertEqual(module.main(["--repo-root", str(root), "--check"]), 0)
            (root / "docs/agent-validation.md").write_text("New policy\n")
            self.assertEqual(module.main(["--repo-root", str(root), "--check"]), 1)

    def test_symlink_destination_refused_before_any_write(self):
        module = load("sync-agent-validation")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "docs").mkdir()
            (root / "docs/agent-validation.md").write_text("policy")
            (root / "real.md").write_text("local")
            (root / "AGENTS.md").symlink_to(root / "real.md")
            self.assertEqual(module.main(["--repo-root", str(root)]), 2)
            self.assertFalse((root / ".claude/CLAUDE.md").exists())


if __name__ == "__main__":
    unittest.main()
