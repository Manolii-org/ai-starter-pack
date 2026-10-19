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

SCRIPTS = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.PIPE)

    def save_config(self):
        (self.root / "config/agent-checks.json").write_text(json.dumps(self.config))

    def run_check(self, *args, expected=0, env=None):
        result = subprocess.run([sys.executable, str(self.root / "scripts/agent-check.py"),
                                 "--repo-root", str(self.root), "--provider", "codex", *args],
                                capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def receipt(self):
        return json.loads((self.root / ".ai/agent-checks/receipt.json").read_text())

    def change_source(self):
        (self.root / "src/value.py").write_text("value = 1\n# changed\n")

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
    def test_each_managed_input_selects_the_native_contract(self):
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
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            for rel in paths:
                with self.subTest(path=rel):
                    path = root / rel
                    path.write_text("changed\n")
                    plan = runner.plan_for(root, config, "HEAD")
                    self.assertEqual(plan["paths"], [rel])
                    self.assertIn("agent-validation-contract", [c["id"] for c in plan["checks"]])
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
