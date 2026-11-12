from __future__ import annotations
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_owner_recipe_tests", ROOT / "scripts/ci/native_owner_recipe.py")
N = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(N)
A, B, T = "a" * 40, "b" * 40, "c" * 40


def environment():
    return {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": N.OWNER,
            "GITHUB_EVENT_NAME": "pull_request", "GITHUB_RUN_ID": "101",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_WORKFLOW_SHA": A,
            "GITHUB_JOB": "owner_recipe"}


def event(head=B, base=A):
    return {"repository": {"full_name": N.OWNER}, "pull_request": {
        "base": {"repo": {"full_name": N.OWNER}, "sha": base},
        "head": {"sha": head}}}


class SubjectContracts(unittest.TestCase):
    def test_all_native_events_bind_exact_source_and_base(self):
        self.assertEqual(N.subject(event(), environment())["head_sha"], B)
        env = {**environment(), "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": B}
        self.assertEqual(N.subject({"repository": {"full_name": N.OWNER}, "ref": "refs/heads/main",
                                   "before": A, "after": B}, env)["base_sha"], A)
        env["GITHUB_EVENT_NAME"] = "merge_group"
        self.assertEqual(N.subject({"repository": {"full_name": N.OWNER},
            "merge_group": {"base_ref": "refs/heads/main", "base_sha": A, "head_sha": B}}, env)["head_sha"], B)

    def test_foreign_scope_or_incomplete_native_identity_refuses_before_git(self):
        cases = [{"GITHUB_REPOSITORY": "example-org/other"}, {"GITHUB_ACTIONS": "false"},
                 {"GITHUB_RUN_ID": "0"}, {"GITHUB_RUN_ATTEMPT": "1.0"},
                 {"GITHUB_WORKFLOW_SHA": "0" * 40}]
        for change in cases:
            with self.subTest(change=change), patch.object(N, "git", side_effect=AssertionError("source read")):
                with self.assertRaises(ValueError):
                    N.execute(Path("/synthetic-never-opened"), event(), {**environment(), **change}, plan_only=True)

    def test_missing_unknown_or_ambiguous_receipt_work_is_not_pass(self):
        current = {"head_sha": B, "tree_sha": T}
        original = {"schema": "agent-check/v1", "outcome": "passed", "cached": False,
                    "merge_authority": False, "base": A, "snapshot": {"head": B, "tree": T},
                    "results": [{"id": "fixture", "status": "passed"}]}
        self.assertTrue(N.passing_receipt(original, current, ["fixture"], A))
        for key, value in [("cached", True), ("outcome", "failed"), ("merge_authority", True),
                           ("base", B), ("results", []), ("results", [{"id": "fixture", "status": "skipped"}]),
                           ("snapshot", {"head": A, "tree": T}),
                           ("results", original["results"] * 2)]:
            changed = copy.deepcopy(original)
            changed[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                N.passing_receipt(changed, current, ["fixture"], A)
        with self.assertRaises(ValueError):
            N.passing_receipt(original, current, [], A)
        with self.assertRaises(ValueError):
            N.passing_receipt(original, current, ["missing"], A)


class RealGitExecutionContracts(unittest.TestCase):
    def fixture(self, directory, *, mode="pass", selected=True):
        root = Path(directory)
        (root / "scripts").mkdir()
        code = """import json,subprocess,sys
from pathlib import Path
def config_for(root):
    return {"checks":[{"id":"fixture"}]}
def plan_for(root,config,base):
    return {"base":base,"checks":config["checks"] if SELECTED else []}
def main(args):
    assert "--provision" in args and "--no-cache" in args
    assert args[args.index("--provider")+1]=="codex"
    if MODE=="fail": return 1
    root=Path(args[args.index("--repo-root")+1])
    if MODE=="missing": return 0
    def git(*argv):
        return subprocess.check_output(["git",*argv],cwd=root,text=True).strip()
    receipt={"schema":"agent-check/v1","outcome":"passed","cached":False,
        "base":args[args.index("--base")+1],"merge_authority":False,
        "snapshot":{"head":git("rev-parse","HEAD"),"tree":git("rev-parse","HEAD^{tree}")},
        "results":[{"id":"fixture","status":"passed"}],"private_key":"synthetic-private-sentinel"}
    if MODE=="cached": receipt["cached"]=True
    p=root/".ai/agent-checks/receipt.json";p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(receipt))
    if MODE=="drift": (root/"tracked").write_text("changed during qualification")
    return 0
""".replace("SELECTED", repr(selected)).replace("MODE", repr(mode))
        code += """
if __name__ == "__main__":
    args=sys.argv[1:]
    root=Path(args[args.index("--repo-root")+1])
    base=args[args.index("--base")+1]
    if "--plan" in args:
        assert "--select-only" in args
        print(json.dumps(plan_for(root,config_for(root),base)))
    else:
        raise SystemExit(main(args))
"""
        (root / "config").mkdir()
        (root / "config/agent-checks.json").write_text(json.dumps({"checks": [{"id":"fixture"}]}))
        (root / "scripts/agent-check.py").write_text(code)
        (root / "tracked").write_text("fixed input")
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=15).strip()
        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "baseline")
        base = git("rev-parse", "HEAD")
        (root / "tracked").write_text("selected source")
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "candidate")
        return root, event(git("rev-parse", "HEAD"), base)

    def test_real_git_subject_and_actual_callback_are_bound_without_private_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root, native = self.fixture(directory)
            original_path = list(sys.path)
            summary, code = N.execute(root, native, environment())
            self.assertEqual(code, 0)
            self.assertTrue(summary["recipe_passed"])
            self.assertTrue(summary["whole_configured_recipe_passed"])
            self.assertFalse(summary["merge_authority"])
            self.assertFalse(summary["admission_authorized"])
            self.assertFalse(summary["production_authority"])
            self.assertEqual(sys.path, original_path)
            self.assertNotIn("synthetic-private-sentinel", json.dumps(summary))
            self.assertNotIn("snapshot", summary)
            self.assertEqual(len(summary["receipt_sha256"]), 64)

    def test_plan_or_unselected_work_does_not_execute_or_claim_pass(self):
        for selected in (True, False):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as directory:
                root, native = self.fixture(directory, mode="fail", selected=selected)
                summary, code = N.execute(root, native, environment(), plan_only=True)
                self.assertEqual(code, 0)
                self.assertFalse(summary["recipe_passed"])
                self.assertFalse(summary["whole_configured_recipe_passed"])
                self.assertFalse((root / ".ai/agent-checks/receipt.json").exists())

    def test_failed_missing_cached_and_changed_source_cannot_publish_success(self):
        for mode in ("fail", "missing", "cached", "drift"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root, native = self.fixture(directory, mode=mode)
                if mode == "fail":
                    summary, code = N.execute(root, native, environment())
                    self.assertEqual(code, 1)
                    self.assertFalse(summary["recipe_passed"])
                else:
                    with self.assertRaises((OSError, ValueError)):
                        N.execute(root, native, environment())

    def test_checkout_or_helper_change_and_wrong_job_refuse(self):
        for kind in ("checkout", "helper", "job"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root, native = self.fixture(directory)
                env = environment()
                if kind == "checkout": native["pull_request"]["head"]["sha"] = A
                elif kind == "helper": (root / "scripts/agent-check.py").write_text("unreviewed bytes")
                else: env["GITHUB_JOB"] = "different"
                with self.assertRaises(ValueError):
                    N.execute(root, native, env)

    def test_foreign_cli_cannot_delete_a_prior_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "native-owner-recipe.json"
            output.write_text("retained")
            with patch.dict(os.environ, {"GITHUB_REPOSITORY": "example-org/other", "GITHUB_ACTIONS": "true"},
                            clear=True), patch.object(sys, "argv", ["qualification", "--output", str(output)]):
                self.assertEqual(N.main(), 2)
            self.assertEqual(output.read_text(), "retained")


class WorkflowContracts(unittest.TestCase):
    def test_one_readonly_native_job_classifies_before_expensive_work(self):
        import yaml
        workflow = yaml.load((ROOT / ".github/workflows/native-owner-recipe.yml").read_text(),
                             Loader=yaml.BaseLoader)
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(workflow["jobs"]), {"owner_recipe"})
        self.assertEqual(workflow["concurrency"]["group"],
                         "native-owner-recipe-${{ github.event.pull_request.number || github.sha }}")
        self.assertNotIn("workflow_dispatch", workflow["on"])
        self.assertNotIn("ready_for_review", workflow["on"]["pull_request"]["types"])
        job = workflow["jobs"]["owner_recipe"]
        self.assertNotIn("environment", job)
        self.assertNotIn("secrets", job)
        steps = job["steps"]
        classify = next(i for i, value in enumerate(steps) if value.get("id") == "classify")
        for step in steps[classify + 1:]:
            if step.get("if") == "always()":
                self.assertIn("upload-artifact@", step["uses"])
                self.assertEqual(step["with"]["path"], "${{ runner.temp }}/native-owner-recipe.json")
            else:
                self.assertIn("steps.classify.outcome == 'success'", step["if"])
                self.assertIn("steps.classify.outputs.selected == 'true'", step["if"])
        checkout = steps[0]
        self.assertEqual(checkout["with"]["fetch-depth"], "0")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")

    def test_pack_only_profile_and_consumer_boundaries_are_declared(self):
        config = json.loads((ROOT / "config/agent-checks.json").read_text())
        check = next(value for value in config["checks"] if value["id"] == "native-owner-recipe-contract")
        self.assertIn("scripts/ci/native_owner_recipe.py", check["paths"])
        self.assertIn("tests/test_native_owner_recipe.py", check["paths"])
        copier = (ROOT / "copier.yml").read_text()
        for path in ("scripts/ci/native_owner_recipe.py", ".github/workflows/native-owner-recipe.yml",
                     "docs/native-owner-recipe.md", "tests/test_native_owner_recipe.py"):
            self.assertIn("  - " + path, copier)



class SelectionOnlyContracts(unittest.TestCase):
    def engine(self):
        name = "native_owner_selection_only_engine"
        path = ROOT / "scripts/agent-check.py"
        spec = importlib.util.spec_from_file_location(name, path)
        engine = importlib.util.module_from_spec(spec)
        sys.modules[name] = engine
        original_path = list(sys.path)
        try:
            sys.path.insert(0, str(path.parent))
            exec(compile(path.read_bytes(), str(path), "exec"), engine.__dict__)
        finally:
            sys.path[:] = original_path
        self.addCleanup(sys.modules.pop, name, None)
        return engine

    def test_selection_only_uses_existing_plan_without_tool_or_cache_evidence(self):
        import contextlib
        import io
        engine = self.engine()
        output = io.StringIO()
        with (
            patch.object(engine, "versions", side_effect=AssertionError("tools must not run")),
            patch.object(engine, "private_cache_key", side_effect=AssertionError("cache must not run")),
            patch.object(engine, "write_json", side_effect=AssertionError("receipt must not write")),
            contextlib.redirect_stdout(output),
        ):
            code = engine.main(["--repo-root", str(ROOT), "--base", "HEAD", "--plan", "--select-only"])
        self.assertEqual(code, 0)
        value = json.loads(output.getvalue())
        self.assertIn("checks", value)
        self.assertNotIn("tool_versions", value)
        self.assertNotIn("outcome", value)

    def test_selection_only_cannot_replace_actual_execution(self):
        engine = self.engine()
        with self.assertRaises(SystemExit) as failure:
            engine.main(["--repo-root", str(ROOT), "--select-only"])
        self.assertEqual(failure.exception.code, 2)

    def test_legacy_plan_still_reports_tool_versions(self):
        import contextlib
        import io
        engine = self.engine()
        output = io.StringIO()
        with (
            patch.object(engine, "versions", return_value={"synthetic": "version"}) as versions,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(engine.main(["--repo-root", str(ROOT), "--base", "HEAD", "--plan"]), 0)
        self.assertEqual(json.loads(output.getvalue())["tool_versions"], {"synthetic": "version"})
        versions.assert_called_once()


if __name__ == "__main__":
    unittest.main()
