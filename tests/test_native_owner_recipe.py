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
if MODE=="untracked-import":
    import unreviewed_recipe_dependency
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
    if MODE=="untracked-output": (root/"untracked-output.txt").write_text("synthetic unreviewed output")
    if MODE=="ignored-output":
        (root/"dist").mkdir(exist_ok=True)
        (root/"dist/output.txt").write_text("synthetic ignored build output")
    if MODE=="recipe-filter":
        (root/".git/info/attributes").write_text("tracked filter=synthetic\\n")
        (root/"tracked").touch()
    if MODE.startswith("recipe-index-"):
        subprocess.check_call(["git","update-index","--"+MODE[len("recipe-index-"):],"tracked"],cwd=root)
        (root/"tracked").write_text("synthetic hidden recipe drift")
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
        if MODE=="classification-untracked":
            (root/"classification-output.py").write_text("synthetic unreviewed classification output")
        if MODE=="classification-filter":
            (root/".git/info/attributes").write_text("tracked filter=synthetic\\n")
            (root/"tracked").touch()
        if MODE.startswith("classification-index-"):
            subprocess.check_call(["git","update-index","--"+MODE[len("classification-index-"):],"tracked"],cwd=root)
            (root/"tracked").write_text("synthetic hidden classification drift")
    else:
        raise SystemExit(main(args))
""".replace("MODE", repr(mode))
        (root / "config").mkdir()
        (root / "config/agent-checks.json").write_text(json.dumps({"checks": [{"id":"fixture"}]}))
        (root / "scripts/agent-check.py").write_text(code)
        # Match actual owner output policy; generated receipts, bytecode and
        # build artifacts are runtime state, not newly admitted source.
        (root / ".gitignore").write_text(".ai/agent-checks/\n__pycache__/\ndist/\n")
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

    def test_stable_untracked_module_is_refused_before_any_owner_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root, native = self.fixture(directory, mode="untracked-import")
            module = root / "scripts/unreviewed_recipe_dependency.py"
            module.write_text("from pathlib import Path\n"
                              "(Path(__file__).parents[1]/'untracked-module-ran').write_text('ran')\n")
            absent = subprocess.run(["git", "cat-file", "-e", "HEAD:scripts/unreviewed_recipe_dependency.py"],
                                    cwd=root, capture_output=True, timeout=15)
            self.assertNotEqual(absent.returncode, 0)
            with self.assertRaisesRegex(ValueError, "untracked"):
                N.execute(root, native, environment())
            self.assertFalse((root / "untracked-module-ran").exists())
            self.assertFalse((root / ".ai/agent-checks/receipt.json").exists())

    def test_classification_created_untracked_source_is_refused_before_recipe(self):
        for plan_only in (True, False):
            for selected in (True, False):
                with self.subTest(plan_only=plan_only, selected=selected), tempfile.TemporaryDirectory() as directory:
                    root, native = self.fixture(directory, mode="classification-untracked", selected=selected)
                    with self.assertRaisesRegex(ValueError, "source changed"):
                        N.execute(root, native, environment(), plan_only=plan_only)
                    self.assertTrue((root / "classification-output.py").is_file())
                    self.assertFalse((root / ".ai/agent-checks/receipt.json").exists())

    def test_untracked_path_bytes_and_symlink_do_not_hide_inputs(self):
        for kind in ("newline", "nonutf8", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root, native = self.fixture(directory)
                if kind == "symlink":
                    (root / "untracked-link").symlink_to("tracked")
                else:
                    name = b"untracked\ninput" if kind == "newline" else b"untracked-\xff"
                    fd = os.open(os.fsencode(root) + b"/" + name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(fd)
                with self.assertRaisesRegex(ValueError, "untracked"):
                    N.execute(root, native, environment())
                self.assertFalse((root / ".ai/agent-checks/receipt.json").exists())

    def test_untracked_inventory_failure_never_becomes_success(self):
        for failed_probe in (1, 2, 3):
            with self.subTest(failed_probe=failed_probe), tempfile.TemporaryDirectory() as directory:
                root, native = self.fixture(directory)
                original_git = N.git
                seen = 0

                def failing_git(owner, *args, **kwargs):
                    nonlocal seen
                    if args == ("ls-files", "--others", "--exclude-standard", "-z"):
                        seen += 1
                        if seen == failed_probe:
                            raise subprocess.CalledProcessError(128, ["git", "ls-files"])
                    return original_git(owner, *args, **kwargs)

                with patch.object(N, "git", side_effect=failing_git):
                    with self.assertRaises(subprocess.CalledProcessError):
                        N.execute(root, native, environment())
                self.assertEqual(seen, failed_probe)
                self.assertEqual((root / ".ai/agent-checks/receipt.json").exists(), failed_probe == 3)

    def test_metadata_queries_cannot_execute_local_fsmonitor_or_write_index(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as controls:
            root, native = self.fixture(directory)
            marker = Path(controls) / "fsmonitor-ran"
            hook = Path(controls) / "fsmonitor"
            hook.write_text("#!" + sys.executable + "\nfrom pathlib import Path\n"
                            "Path(" + repr(str(marker)) + ").write_text('executed')\n")
            hook.chmod(0o700)
            subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=root, check=True)
            subprocess.run(["git", "config", "core.untrackedCache", "true"], cwd=root, check=True)
            before = (root / ".git/index").read_bytes()
            summary, code = N.execute(root, native, environment())
            self.assertEqual(code, 0)
            self.assertTrue(summary["recipe_passed"])
            self.assertFalse(marker.exists())
            self.assertEqual((root / ".git/index").read_bytes(), before)

    def test_every_metadata_query_ignores_foreign_git_selectors_and_trace(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as controls:
            root, native = self.fixture(directory)
            trace = Path(controls) / "foreign-trace"
            foreign = Path(controls) / "not-this-repository"
            overrides = {"GIT_DIR": str(foreign), "GIT_WORK_TREE": str(foreign),
                         "GIT_OBJECT_DIRECTORY": str(foreign), "GIT_COMMON_DIR": str(foreign),
                         "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(foreign),
                         "GIT_TRACE": str(trace), "GIT_TRACE2_EVENT": str(trace),
                         "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor",
                         "GIT_CONFIG_VALUE_0": str(foreign)}
            # Selection executes no owner Git command: this isolates all wrapper
            # metadata queries, including merge-base, from recipe behavior.
            with patch.dict(os.environ, overrides):
                summary, code = N.execute(root, native, environment(), plan_only=True)
            self.assertEqual(code, 0)
            self.assertEqual(summary["status"], "selected-pending-execution")
            self.assertFalse(summary["recipe_passed"])
            self.assertFalse(trace.exists())
            self.assertFalse((root / ".ai/agent-checks/receipt.json").exists())

    def test_integrated_base_negative_and_transport_error_remain_distinct(self):
        for failed_code in (1, 128):
            with self.subTest(failed_code=failed_code), tempfile.TemporaryDirectory() as directory:
                root, native = self.fixture(directory)
                original_git = N.git

                def failing_git(owner, *args, **kwargs):
                    if args[0] == "merge-base":
                        raise subprocess.CalledProcessError(failed_code, ["git", "merge-base"])
                    return original_git(owner, *args, **kwargs)

                expected = ValueError if failed_code == 1 else subprocess.CalledProcessError
                with patch.object(N, "git", side_effect=failing_git), self.assertRaises(expected):
                    N.execute(root, native, environment())
                self.assertFalse((root / ".ai/agent-checks/receipt.json").exists())

    def test_local_graft_and_replace_cannot_forge_integrated_base(self):
        for kind in ("graft", "replace"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root, native = self.fixture(directory)

                def git(*args):
                    return subprocess.check_output(["git", "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", *args], cwd=root, text=True,
                        stderr=subprocess.DEVNULL, timeout=15).strip()

                head = native["pull_request"]["head"]["sha"]
                tree = git("rev-parse", "HEAD^{tree}")
                unrelated = git("commit-tree", tree, "-m", "synthetic unrelated root")
                native["pull_request"]["base"]["sha"] = unrelated
                self.assertEqual(subprocess.run(["git", "merge-base", "--is-ancestor", unrelated, head],
                    cwd=root, capture_output=True, timeout=15).returncode, 1)
                if kind == "graft":
                    (root / ".git/info/grafts").write_text(head + " " + unrelated + "\n")
                else:
                    replacement = git("commit-tree", tree, "-p", unrelated, "-m", "synthetic replacement")
                    git("replace", head, replacement)
                # Prove the fixture really forges the ordinary effective graph.
                self.assertEqual(subprocess.run(["git", "merge-base", "--is-ancestor", unrelated, head],
                    cwd=root, capture_output=True, timeout=15).returncode, 0)
                with self.assertRaisesRegex(ValueError, "current base must be integrated"):
                    N.execute(root, native, environment(), plan_only=True)
                self.assertFalse((root / ".ai/agent-checks/receipt.json").exists())

    def test_shallow_overlay_cannot_replace_available_raw_parent_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            root, native = self.fixture(directory)
            head = native["pull_request"]["head"]["sha"]
            base = native["pull_request"]["base"]["sha"]
            (root / ".git/shallow").write_text(head + "\n")
            self.assertEqual(subprocess.run(["git", "merge-base", "--is-ancestor", base, head],
                cwd=root, capture_output=True, timeout=15).returncode, 1)
            summary, code = N.execute(root, native, environment(), plan_only=True)
            self.assertEqual(code, 0)
            self.assertEqual(summary["status"], "selected-pending-execution")
            self.assertFalse(summary["recipe_passed"])
            self.assertEqual((root / ".git/shallow").read_text(), head + "\n")

    def test_missing_raw_parent_object_cannot_qualify_a_shallow_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root, native = self.fixture(directory)
            head = native["pull_request"]["head"]["sha"]
            base = native["pull_request"]["base"]["sha"]
            (root / ".git/shallow").write_text(head + "\n")
            (root / ".git/objects" / base[:2] / base[2:]).unlink()
            with self.assertRaises(subprocess.CalledProcessError):
                N.execute(root, native, environment(), plan_only=True)
            self.assertFalse((root / ".ai/agent-checks/receipt.json").exists())

    def test_effective_clean_filters_hold_before_diff_at_every_stage(self):
        for stage in ("tracked-attributes", "local-attributes", "classification-filter", "recipe-filter"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as controls:
                root, native = self.fixture(directory, mode=stage)
                marker = Path(controls) / "clean-filter-ran"
                hook = Path(controls) / "clean-filter"
                hook.write_text("#!" + sys.executable + "\nimport sys\nfrom pathlib import Path\n"
                    "Path(" + repr(str(marker)) + ").write_text('executed')\n"
                    "sys.stdout.buffer.write(sys.stdin.buffer.read())\n")
                hook.chmod(0o700)
                if stage == "tracked-attributes":
                    (root / ".gitattributes").write_text("tracked filter=synthetic\n")
                    subprocess.run(["git", "add", ".gitattributes"], cwd=root, check=True)
                    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-qm", "synthetic filter attribute"], cwd=root, check=True)
                    native["pull_request"]["head"]["sha"] = subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
                elif stage == "local-attributes":
                    (root / ".git/info/attributes").write_text("tracked filter=synthetic\n")
                subprocess.run(["git", "config", "filter.synthetic.clean", str(hook)], cwd=root, check=True)
                (root / "tracked").touch()
                with self.assertRaisesRegex(ValueError, "filtered or ambiguous source"):
                    N.execute(root, native, environment())
                self.assertFalse(marker.exists())
                self.assertEqual((root / ".ai/agent-checks/receipt.json").exists(), stage == "recipe-filter")

    def test_nonlocal_attribute_file_is_not_loaded_or_used(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as controls:
            root, native = self.fixture(directory)
            attributes = Path(controls) / "foreign-attributes"
            # A directory cannot be read as an attributes file. Overriding the
            # nonlocal route keeps it outside both guard and diff observations.
            attributes.mkdir()
            subprocess.run(["git", "config", "core.attributesFile", str(attributes)], cwd=root, check=True)
            summary, code = N.execute(root, native, environment())
            self.assertEqual(code, 0)
            self.assertTrue(summary["recipe_passed"])

    def test_filter_metadata_failure_or_ambiguity_cannot_become_pass(self):
        for failure in ("transport", "truncated", "wrong-path", "empty-attribute", "filter-literal", "duplicate"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root, native = self.fixture(directory)
                original_git = N.git

                def failing_git(owner, *args, **kwargs):
                    if args[0] == "check-attr":
                        if failure == "transport":
                            raise subprocess.CalledProcessError(128, ["git", "check-attr"])
                        if failure == "truncated": return b"tracked"
                        if failure == "wrong-path": return b"foreign\0text\0set\0"
                        if failure == "empty-attribute": return b"tracked\0\0set\0"
                        if failure == "duplicate": return b"tracked\0text\0set\0" * 2
                        return b"tracked\0filter\0unspecified\0"
                    return original_git(owner, *args, **kwargs)

                expected = subprocess.CalledProcessError if failure == "transport" else ValueError
                with patch.object(N, "git", side_effect=failing_git), self.assertRaises(expected):
                    N.execute(root, native, environment())
                self.assertFalse((root / ".ai/agent-checks/receipt.json").exists())

    def test_filter_sentinel_driver_names_and_disabled_declarations_hold(self):
        for driver in ("unset", "unspecified", "set", "synthetic", "-filter"):
            with self.subTest(driver=driver), tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as controls:
                root, native = self.fixture(directory)
                marker = Path(controls) / "reserved-filter-ran"
                hook = Path(controls) / "reserved-filter"
                hook.write_text("#!" + sys.executable + "\nimport sys\nfrom pathlib import Path\n"
                    "Path(" + repr(str(marker)) + ").write_text('executed')\n"
                    "sys.stdout.buffer.write(sys.stdin.buffer.read())\n")
                hook.chmod(0o700)
                attribute = "-filter" if driver == "-filter" else "filter=" + driver
                (root / ".git/info/attributes").write_text("tracked " + attribute + "\n")
                if driver != "-filter":
                    subprocess.run(["git", "config", "filter." + driver + ".clean", str(hook)], cwd=root, check=True)
                (root / "tracked").touch()
                with self.assertRaisesRegex(ValueError, "filtered or ambiguous source"):
                    N.execute(root, native, environment())
                self.assertFalse(marker.exists())
                self.assertFalse((root / ".ai/agent-checks/receipt.json").exists())

    def test_benign_attributes_and_actual_filter_reset_remain_supported(self):
        for attribute in ("text eol=lf", "!filter", "text eol=lf !filter"):
            with self.subTest(attribute=attribute), tempfile.TemporaryDirectory() as directory:
                root, native = self.fixture(directory)
                (root / ".git/info/attributes").write_text("tracked " + attribute + "\n")
                summary, code = N.execute(root, native, environment())
                self.assertEqual(code, 0)
                self.assertTrue(summary["recipe_passed"])

    def test_special_index_flags_cannot_hide_tracked_drift_at_any_stage(self):
        for flag in ("assume-unchanged", "skip-worktree"):
            for stage in ("before", "classification", "recipe"):
                with self.subTest(flag=flag, stage=stage), tempfile.TemporaryDirectory() as directory:
                    mode = stage + "-index-" + flag if stage != "before" else "pass"
                    root, native = self.fixture(directory, mode=mode)
                    if stage == "before":
                        subprocess.run(["git", "update-index", "--" + flag, "tracked"], cwd=root, check=True)
                        (root / "tracked").write_text("synthetic hidden initial drift")
                    with self.assertRaisesRegex(ValueError, "source index flags"):
                        N.execute(root, native, environment())
                    self.assertEqual(subprocess.check_output(["git", "diff", "--name-only", "HEAD"], cwd=root), b"")
                    raw = subprocess.check_output(["git", "hash-object", "--no-filters", "tracked"], cwd=root)
                    committed = subprocess.check_output(["git", "rev-parse", "HEAD:tracked"], cwd=root)
                    self.assertNotEqual(raw, committed)
                    entry = subprocess.check_output(["git", "ls-files", "-v", "tracked"], cwd=root)
                    self.assertTrue(entry.startswith(b"h " if flag == "assume-unchanged" else b"S "))
                    self.assertEqual((root / ".ai/agent-checks/receipt.json").exists(), stage == "recipe")

    def test_recipe_created_untracked_output_cannot_publish_a_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root, native = self.fixture(directory, mode="untracked-output")
            with self.assertRaisesRegex(ValueError, "source changed"):
                N.execute(root, native, environment())
            self.assertTrue((root / "untracked-output.txt").is_file())
            self.assertTrue((root / ".ai/agent-checks/receipt.json").is_file())

    def test_ignored_artifacts_and_caches_remain_permitted(self):
        with tempfile.TemporaryDirectory() as directory:
            root, native = self.fixture(directory, mode="ignored-output")
            (root / "dist").mkdir()
            (root / "dist/existing.txt").write_text("synthetic ignored input artifact")
            (root / "scripts/__pycache__").mkdir()
            (root / "scripts/__pycache__/fixture.pyc").write_bytes(b"synthetic ignored cache")
            summary, code = N.execute(root, native, environment())
            self.assertEqual(code, 0)
            self.assertTrue(summary["recipe_passed"])
            self.assertTrue(summary["whole_configured_recipe_passed"])
            self.assertTrue((root / "dist/output.txt").is_file())


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
