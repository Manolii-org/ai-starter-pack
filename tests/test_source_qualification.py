"""Synthetic source qualification contracts; no native/provider enforcement is claimed."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "scripts/ci/source_qualification.py"
SPEC = importlib.util.spec_from_file_location("source_qualification_contract", PATH)
Q = importlib.util.module_from_spec(SPEC)
exec(compile(PATH.read_bytes(), str(PATH), "exec"), Q.__dict__)
REVIEW = Q.review_module()
HEAD, BASE, MERGE, TREE = (c * 40 for c in "abcd")
REPO = "Example/framework"
WORKFLOW = b"name: Synthetic qualification\non: pull_request\n"


def configuration():
    checks = [{"name": role, "app_id": 10101, "workflow_path": ".github/workflows/synthetic.yml",
               "workflow_sha256": hashlib.sha256(WORKFLOW).hexdigest(), "job_key": role,
               "role": role, "artifact_key": role, "steps": ["Execute " + role]}
              for role in ("classify", "deterministic", "security")]
    optional = dict(checks[1], name="optional-model", job_key="optional_model", artifact_key="optional_model", role="model")
    return {"schema_version": 1, "model_assessment": "optional",
            "security_scope": {'semgrep': {'include': ['*.py'], 'exclude': []}, 'bandit': {'include': ['*.py'], 'exclude': []}},
            "required_checks": checks, "optional_model_checks": [optional],
            "review_publishers": [{"login": "coderabbitai[bot]", "user_id": 101},
                                  {"login": "chatgpt-codex-connector[bot]", "user_id": 202}]}


def reports():
    return ({"version": "1.185.0", "results": [], "errors": [],
             "paths": {"scanned": ["fixture.py"]}},
            {"results": [], "errors": [], "metrics": {"_totals": {"loc": 1}, "fixture.py": {"loc": 1}}})


def context():
    return {"security_scope_sha256": Q.digest({"scope": configuration()["security_scope"],
                "inventory": {"semgrep": ["fixture.py"], "bandit": ["fixture.py"]}}),
            "repository": REPO, "pr": 201, "author": "implementer", "author_id": 303, "head_sha": HEAD,
            "base_sha": BASE, "tested_sha": MERGE, "tested_tree": TREE}


def checks():
    result = []
    for i, required in enumerate(configuration()["required_checks"]):
        security = None
        if required["role"] == "security":
            security = {name: Q.scanner_report(report, 0, name)
                        for name, report in zip(("semgrep", "bandit"), reports())}
        result.append({"id": 200 + i, "name": required["name"], "app": {"id": required["app_id"]},
                       "head_sha": HEAD, "status": "completed", "conclusion": "success",
                       "details_url": f"https://github.com/{REPO}/actions/runs/{100+i}/job/{300+i}",
                       "workflow_run": {"id": 100 + i, "run_attempt": 1, "path": required["workflow_path"],
                                        "observed_workflow_sha256": required["workflow_sha256"],
                                        "head_sha": HEAD, "event": "pull_request", "source_base_sha": BASE},
                       "native_job": {"id": 300 + i, "run_id": 100 + i, "run_attempt": 1, "status": "completed", "conclusion": "success",
                                      "steps": [{"name": required["steps"][0], "status": "completed",
                                                 "conclusion": "success"}],
                                      "check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/{200+i}"},
                       "qualification_receipt": {"schema": Q.SCHEMA, **{k: v for k, v in context().items()
                            if k not in ("author", "author_id", "pr", "security_scope_sha256")}, "run_id": 100 + i, "run_attempt": 1,
                            "workflow_sha": MERGE, "job_key": required["job_key"], "artifact_key": required["artifact_key"], "role": required["role"],
                            "native_job_id": 300 + i, "native_check_id": 200 + i,
                            "security_scope_sha256": context()["security_scope_sha256"] if security else None,
                            "result": "executed_pass", "security": security}})
    return result


def reviews():
    return [{"id": 12, "commit_id": HEAD, "state": "APPROVED", "submitted_at": "2026-11-11T10:00:00Z",
             "user": {"login": "chatgpt-codex-connector[bot]", "type": "Bot", "id": 202}}]


def qualify(**changes):
    kwargs = dict(compiled=configuration(), context=context(), checks=checks(), reviews=reviews(),
                  comments=[], unresolved_threads=0, reviewer=REVIEW)
    kwargs.update(changes)
    return Q.qualify(**kwargs)


def test_failed_or_missing_optional_model_is_audit_and_cannot_clear_native_authority():
    native = checks()
    native.append({"id": 1000, "name": "optional-model", "app": {"id": 10101},
                   "status": "completed", "conclusion": "failure"})
    result = qualify(checks=native)
    assert result["source_qualified"]
    assert result["optional_model_audit"] == [{"name": "optional-model", "observed": True,
                                               "status": "completed", "conclusion": "failure"}]
    assert result["admission_authorized"] is False
    assert result["installed_required_checks_changed"] is False
    assert result["cutover"] == "held_pending_native_owner_acceptance"
    missing = qualify()["optional_model_audit"][0]
    assert missing == {"name": "optional-model", "observed": False, "status": None, "conclusion": None}


@pytest.mark.parametrize("variant", ["missing", "skipped", "neutral", "failed", "running", "wrong-app",
                                     "stale-head", "stale-base", "wrong-workflow", "workflow-drift",
                                     "step-skipped", "step-missing", "step-duplicate", "no-receipt"])
def test_required_native_checks_never_accept_missing_skipped_stale_or_foreign_evidence(variant):
    values = checks()
    target = values[-1]
    if variant == "missing":
        values.pop()
    elif variant in ("skipped", "neutral", "failed"):
        target["conclusion"] = "failure" if variant == "failed" else variant
    elif variant == "running":
        target["status"] = "in_progress"
    elif variant == "wrong-app":
        target["app"]["id"] += 1
    elif variant == "stale-head":
        target["head_sha"] = "e" * 40
    elif variant == "stale-base":
        target["workflow_run"]["source_base_sha"] = "e" * 40
    elif variant == "wrong-workflow":
        target["workflow_run"]["path"] = ".github/workflows/foreign.yml"
    elif variant == "workflow-drift":
        target["workflow_run"]["observed_workflow_sha256"] = "e" * 64
    elif variant == "step-skipped":
        target["native_job"]["steps"][0]["conclusion"] = "skipped"
    elif variant == "step-missing":
        target["native_job"]["steps"] = []
    elif variant == "step-duplicate":
        target["native_job"]["steps"] *= 2
    else:
        target.pop("qualification_receipt")
    with pytest.raises(Q.QualificationError):
        qualify(checks=values)


@pytest.mark.parametrize("field", ["head_sha", "base_sha", "tested_sha", "tested_tree", "workflow_sha",
                                   "job_key", "artifact_key", "role", "run_id", "run_attempt", "result", "repository"])
def test_changed_receipt_subject_or_producer_cannot_be_reused(field):
    native = checks()
    old = native[0]["qualification_receipt"][field]
    native[0]["qualification_receipt"][field] = old + 1 if type(old) is int else "changed"
    with pytest.raises(Q.QualificationError):
        qualify(checks=native)


@pytest.mark.parametrize("variant", ["absent", "stale", "acknowledgement", "author", "foreign",
                                     "changes-requested", "unresolved", "local-peer"])
def test_independent_completed_exact_head_review_is_required(variant):
    values = reviews()
    extra = {}
    if variant == "absent":
        values = []
    elif variant == "stale":
        values[0]["commit_id"] = "e" * 40
    elif variant == "acknowledgement":
        values[0]["state"] = "COMMENTED"
        values[0]["body"] = ""
    elif variant == "author":
        extra["context"] = {**context(), "author": values[0]["user"]["login"]}
    elif variant == "foreign":
        values[0]["user"] = {"login": "untrusted-reviewer", "type": "Bot"}
    elif variant == "changes-requested":
        values[0]["state"] = "CHANGES_REQUESTED"
    elif variant == "unresolved":
        extra["unresolved_threads"] = 1
    else:
        values = [{"id": 12, "commit_id": HEAD, "state": "APPROVED", "submitted_at": "2026-11-11T10:00:00Z",
                   "user": {"login": "local-peer", "type": "User"}}]
    with pytest.raises(Q.QualificationError):
        qualify(reviews=values, **extra)


@pytest.mark.parametrize("scanner", ["semgrep", "bandit"])
@pytest.mark.parametrize("bad", ["missing", "errors", "missing-results", "bad-row", "incomplete-scope",
                                 "bad-exit", "false-exit", "nonzero-empty"])
def test_scanner_report_failures_never_become_clean_evidence(scanner, bad):
    report = dict(zip(("semgrep", "bandit"), reports()))[scanner]
    code = 0
    if bad == "missing":
        report = None
    elif bad == "errors":
        report["errors"] = [{"level": "warn", "type": "PartialParsing"}]
    elif bad == "missing-results":
        del report["results"]
    elif bad == "bad-row":
        report["results"] = [{}]
    elif bad == "incomplete-scope":
        report.pop("paths" if scanner == "semgrep" else "metrics")
    elif bad == "bad-exit":
        code = 2
    elif bad == "false-exit":
        code = False
    else:
        code = 1
    with pytest.raises(Q.QualificationError):
        Q.scanner_report(report, code, scanner)


@pytest.mark.parametrize("bad", ["version", "skipped-rules", "empty-scope", "invalid-scanned-file"])
def test_semgrep_partial_coverage_is_not_full_security(bad):
    report, _ = reports()
    if bad == "version":
        report["version"] = "unqualified"
    elif bad == "skipped-rules":
        report["skipped_rules"] = [{"id": "synthetic"}]
    else:
        report["paths"]["scanned"] = [] if bad == "empty-scope" else [None]
    with pytest.raises(Q.QualificationError):
        Q.scanner_report(report, 0, "semgrep")


def test_completed_scanner_findings_stay_findings_and_block_source_qualification():
    semgrep, _ = reports()
    semgrep["results"] = [{"check_id": "synthetic", "path": "fixture.py", "start": {"line": 1},
                           "extra": {"message": "Synthetic finding", "severity": "WARNING"}}]
    summary = Q.scanner_report(semgrep, 1, "semgrep")
    assert summary["finding_count"] == 1
    native = checks()
    native[-1]["qualification_receipt"]["security"]["semgrep"] = summary
    with pytest.raises(Q.QualificationError):
        qualify(checks=native)


def test_policy_cannot_make_models_required_or_omit_meaningful_deterministic_security():
    for change in ({"schema_version": True}, {"schema_version": 1.0},
                   {"model_assessment": "required"}, {"extra": "unknown"},
                   {"required_checks": configuration()["required_checks"][:2]}):
        with pytest.raises(Q.QualificationError):
            Q.policy({**configuration(), **change})
    with pytest.raises(Q.QualificationError):
        Q.strict_json('{"schema_version":1,"schema_version":1}')


def test_latest_matching_native_provider_failure_cannot_reuse_old_pass():
    native = checks()
    latest = copy.deepcopy(native[0])
    latest.update(id=1001, conclusion="failure")
    with pytest.raises(Q.QualificationError):
        qualify(checks=native + [latest])
    foreign = copy.deepcopy(latest)
    foreign["app"]["id"] += 1
    assert qualify(checks=native + [foreign])["source_qualified"]


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), "-c", "user.name=Fixture",
        "-c", "user.email=fixture@example.invalid", *args], stderr=subprocess.PIPE, timeout=10).decode().strip()


def test_real_git_producer_binds_current_combined_tree_and_refuses_stale_base(tmp_path):
    git(tmp_path, "init", "--quiet")
    (tmp_path / "common.txt").write_text("synthetic")
    (tmp_path / "fixture.py").write_text("print(1)\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "--quiet", "-m", "Common source")
    oldbase = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "--quiet", "-b", "feature")
    (tmp_path / "feature.txt").write_text("synthetic feature")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "--quiet", "-m", "Feature source")
    head = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "--quiet", "-b", "integration", oldbase)
    (tmp_path / "base.txt").write_text("synthetic current base")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "--quiet", "-m", "Current base")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "merge", "--quiet", "--no-ff", "feature", "-m", "Combined qualification")
    tested = git(tmp_path, "rev-parse", "HEAD")
    env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": REPO,
           "GITHUB_SHA": tested, "GITHUB_WORKFLOW_SHA": tested, "GITHUB_RUN_ID": "123",
           "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "security"}
    event = {"pull_request": {"head": {"sha": head}, "base": {"sha": base, "repo": {"full_name": REPO}}}}
    semgrep, bandit = reports()
    receipt = Q.produce_receipt(env, event, "security", lambda *a: git(tmp_path, *a),
                               semgrep=semgrep, bandit=bandit, semgrep_exit=0, bandit_exit=0,
                               compiled=configuration(), producer={"native_job_id": 300, "native_check_id": 200})
    assert receipt["tested_tree"] == git(tmp_path, "rev-parse", "HEAD^{tree}")
    assert (receipt["head_sha"], receipt["base_sha"], receipt["tested_sha"]) == (head, base, tested)
    event["pull_request"]["base"]["sha"] = oldbase
    with pytest.raises(Q.QualificationError):
        Q.produce_receipt(env, event, "classify", lambda *a: git(tmp_path, *a))


class FakeAPI:
    def __init__(self):
        self.checks = checks()
        self.pr = {"head": {"sha": HEAD}, "base": {"sha": BASE, "repo": {"full_name": REPO}},
                   "merge_commit_sha": MERGE, "user": {"login": "implementer", "id": 303}}
        self.reads = []
        self.pr_reads = 0
        self.drift = False
        self.artifact_missing = False

    def get(self, path):
        self.reads.append(path)
        if path == f"repos/{REPO}/pulls/201":
            self.pr_reads += 1
            result = copy.deepcopy(self.pr)
            if self.drift and self.pr_reads > 1:
                result["base"]["sha"] = "e" * 40
            return result
        if "/contents/" in path:
            raw = (json.dumps(configuration()).encode() if "/config/" in path else WORKFLOW)
            return {"type": "file", "encoding": "base64", "content": base64.b64encode(raw).decode(),
                    "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()}
        if "/git/trees/" in path:
            return {"truncated": False, "tree": [{"path": "fixture.py", "mode": "100644", "type": "blob", "sha": "f" * 40}]}
        if "/git/commits/" in path:
            return {"tree": {"sha": TREE}}
        for row in self.checks:
            if path.endswith("/actions/runs/" + str(row["workflow_run"]["id"])):
                return copy.deepcopy(row["workflow_run"])
            if path.endswith("/actions/jobs/" + str(row["native_job"]["id"])):
                return copy.deepcopy(row["native_job"])
        raise AssertionError("unexpected native route " + path)

    def pages(self, path, key=None):
        self.reads.append(path)
        if "/check-runs" in path:
            return copy.deepcopy(self.checks)
        if path.endswith("/reviews"):
            return reviews()
        raise AssertionError("unexpected native collection")

    def artifact(self, repo, run, name, *, expected_head):
        assert repo == REPO and expected_head == HEAD
        if self.artifact_missing:
            raise Q.QualificationError("native source-bound artifact missing")
        row = next(c for c in self.checks if c["workflow_run"]["id"] == run)
        assert name == "source-qualification-" + row["qualification_receipt"]["artifact_key"] + "-" + str(row["native_job"]["run_attempt"])
        return copy.deepcopy(row["qualification_receipt"])


def native_reviewer():
    return SimpleNamespace(bot_review_verdict=REVIEW.bot_review_verdict,
                           coderabbit_clean_summary=REVIEW.coderabbit_clean_summary,
                           codex_clean_verdict=REVIEW.codex_clean_verdict,
                           fetch_comments=lambda *a: [],
                           gh_read=lambda *a: json.dumps({"data": {"repository": {"pullRequest": {
                               "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}}))


def test_authenticated_collector_reads_immutable_base_policy_and_revalidates_current_source():
    api = FakeAPI()
    result = Q.collect(api, native_reviewer(), REPO, 201, HEAD, BASE, "config/source-qualification.json")
    assert result["candidate_coherent"] and result["admission_authorized"] is False
    assert result["source_qualified"] is False and result["native_transport_bound"] is False
    assert f"repos/{REPO}/contents/config/source-qualification.json?ref={BASE}" in api.reads
    assert api.pr_reads == 2
    assert all(not p.endswith("/merge") for p in api.reads)


@pytest.mark.parametrize("variant", ["drift", "artifact_missing", "workflow_mutation"])
def test_collector_never_reuses_stale_or_unbound_native_inputs(variant):
    api = FakeAPI()
    if variant == "workflow_mutation":
        for row in api.checks:
            row["workflow_run"]["path"] = ".github/workflows/changed.yml"
    else:
        setattr(api, variant, True)
    with pytest.raises(Q.QualificationError):
        Q.collect(api, native_reviewer(), REPO, 201, HEAD, BASE, "config/source-qualification.json")


def test_new_parallel_workflow_has_no_model_or_deployment_authority():
    workflow = yaml.load((ROOT / ".github/workflows/source-qualification-reusable.yml").read_text(),
                         Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_call"}
    assert workflow["permissions"] == {"contents": "read", "checks": "read", "pull-requests": "read", "actions": "read"}
    text = (ROOT / ".github/workflows/source-qualification-reusable.yml").read_text()
    for forbidden in ("secrets.", "id-token:", "environment:", "workflow_dispatch:", "continue-on-error:",
                      "approve", "skip-ai-review", "CODEX_REVIEW_ENABLED"):
        assert forbidden not in text
    assert "persist-credentials: false" in text
    assert 'source_qualified=true' in text
    assert "set -euo pipefail" in text
    action = yaml.safe_load((ROOT / ".github/actions/source-qualification-receipt/action.yml").read_text())
    assert action["runs"]["steps"][-1]["with"]["if-no-files-found"] == "error"


def test_existing_authenticated_coderabbit_summary_can_satisfy_without_synthetic_approval():
    path = ROOT / "scripts/tests/test_review_evidence.py"
    spec = importlib.util.spec_from_file_location("existing_review_evidence_fixtures", path)
    fixtures = importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(), str(path), "exec"), fixtures.__dict__)
    comment = copy.deepcopy(fixtures.COMMENT)
    comment["user"]["id"] = 101
    assert qualify(reviews=[], comments=[comment])["source_qualified"]
    forged = copy.deepcopy(comment)
    forged["review_provenance"]["editor"] = {"login": "implementer", "__typename": "User"}
    with pytest.raises(Q.QualificationError):
        qualify(reviews=[], comments=[forged])
    stale = copy.deepcopy(comment)
    stale["body"] = stale["body"].replace(HEAD, "e" * 40)
    stale = fixtures.with_provenance(stale)
    with pytest.raises(Q.QualificationError):
        qualify(reviews=[], comments=[stale])


def test_parallel_source_pass_cannot_override_existing_required_native_worker_failure():
    path = ROOT / "scripts/agent-merge.py"
    spec = importlib.util.spec_from_file_location("unchanged_legacy_merge_contract", path)
    legacy = importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(), str(path), "exec"), legacy.__dict__)
    native = checks() + [{"id": 999, "name": "optional-model", "app": {"id": 10101},
                         "status": "completed", "conclusion": "failure"}]
    assert qualify(checks=native)["source_qualified"]
    assert legacy.required_problems({"optional-model"}, legacy.latest_runs(native), {}) == [
        "required check 'optional-model' concluded failure"]


def test_cli_does_not_accept_local_review_or_evidence_files(monkeypatch):
    called = []
    monkeypatch.setattr(Q, "collect", lambda *a: called.append(a))
    with pytest.raises(SystemExit) as error:
        Q.main(["--repository", REPO, "--pr", "201", "--head", HEAD, "--base", BASE,
                "--reviews", "/tmp/local-peer-review.json"])
    assert error.value.code == 2
    assert not called


def test_failed_native_receipt_rerun_revokes_old_output_without_touching_link_target(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "missing-native-event.json"))
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_JOB", "security")
    output = tmp_path / "source-qualification-123-1-security-security/source-qualification.json"
    output.parent.mkdir()
    output.write_text('{"result":"executed_pass"}')
    assert Q.receipt_main(["--role", "security", "--output", str(output)]) == 1
    assert not output.exists()
    target = tmp_path / "owned-synthetic-sentinel"
    target.write_text("synthetic unchanged")
    output.symlink_to(target)
    assert Q.receipt_main(["--role", "security", "--output", str(output)]) == 1
    assert target.read_text() == "synthetic unchanged"
    assert output.is_symlink()


def test_schema_example_and_code_are_consistent():
    import jsonschema
    schema = json.loads((ROOT / "schemas/source-qualification.schema.json").read_bytes())
    example = json.loads((ROOT / "config/source-qualification.example.json").read_bytes())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(example, schema)
    assert Q.policy(example) is example
    assert schema["properties"]["model_assessment"] == {"const": "optional"}


def test_security_receipt_does_not_accept_an_empty_or_skipped_manifest():
    native = checks()
    for summary in (None, {}, {"semgrep": {}}, {"semgrep": {"status": "skipped"}, "bandit": {}}):
        native[-1]["qualification_receipt"]["security"] = summary
        with pytest.raises(Q.QualificationError):
            qualify(checks=native)


@pytest.mark.parametrize("variant", ["missing", "wrong-id", "same-author-id"])
def test_native_publisher_and_author_numeric_identities_are_not_guessed(variant):
    values = reviews()
    changed = {}
    if variant == "missing":
        values[0]["user"].pop("id")
    elif variant == "wrong-id":
        values[0]["user"]["id"] = 999
    else:
        changed["context"] = {**context(), "author_id": values[0]["user"]["id"]}
    with pytest.raises(Q.QualificationError):
        qualify(reviews=values, **changed)


def test_partial_rerun_preserves_a_separately_executed_job_attempt_without_rebilling():
    api = FakeAPI()
    for row in api.checks:
        row["workflow_run"]["run_attempt"] = 2
        assert row["native_job"]["run_attempt"] == 1
    result = Q.collect(api, native_reviewer(), REPO, 201, HEAD, BASE, "config/source-qualification.json")
    assert result["candidate_coherent"] and result["source_qualified"] is False


def test_missing_actual_job_attempt_is_held_instead_of_inferred():
    api = FakeAPI()
    api.checks[0]["native_job"].pop("run_attempt")
    with pytest.raises(Q.QualificationError, match="attempt unavailable"):
        Q.collect(api, native_reviewer(), REPO, 201, HEAD, BASE, "config/source-qualification.json")


def test_receipt_namespace_traversal_and_directory_replacement_cannot_write_elsewhere(tmp_path):
    env = {"RUNNER_TEMP": str(tmp_path), "GITHUB_JOB": "security",
           "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
    outside = tmp_path / "owned-unread-target"
    outside.mkdir()
    output = tmp_path / "source-qualification-123-1-security-security/source-qualification.json"
    with pytest.raises(Q.QualificationError):
        with Q.receipt_output(output, env, "../owned-unread-target"):
            pytest.fail("invalid artifact namespace accepted")
    with Q.receipt_output(output, env, "security") as write:
        output.parent.rename(tmp_path / "retained-owned-directory")
        output.parent.symlink_to(outside, target_is_directory=True)
        with pytest.raises(Q.QualificationError, match="directory changed"):
            write({"schema": "synthetic"})
    assert not list(outside.iterdir())


def test_receipt_artifact_names_preserve_attempts_and_support_distinct_matrix_keys():
    action = yaml.safe_load((ROOT / ".github/actions/source-qualification-receipt/action.yml").read_text())
    name = action["runs"]["steps"][-1]["with"]["name"]
    assert "inputs.artifact_key" in name
    assert "github.run_attempt" in name
    assert "overwrite" not in action["runs"]["steps"][-1]["with"]


@pytest.mark.parametrize("variant", ["tool-failure", "malformed", "false", "wrong-head", "admission", "pass"])
def test_actual_workflow_step_propagates_failure_and_rejects_nonpassing_output(tmp_path, variant):
    import os
    workflow = yaml.load((ROOT / ".github/workflows/source-qualification-reusable.yml").read_text(),
                         Loader=yaml.BaseLoader)
    step = next(s for s in workflow["jobs"]["qualification"]["steps"] if s.get("id") == "qualification")
    helper = tmp_path / ".source-qualification-pack/scripts/ci/source_qualification.py"
    helper.parent.mkdir(parents=True)
    payload = {"schema": Q.SCHEMA, "source_qualified": True, "admission_authorized": False,
               "head_sha": HEAD, "base_sha": BASE}
    if variant == "false":
        payload["source_qualified"] = False
    elif variant == "wrong-head":
        payload["head_sha"] = "e" * 40
    elif variant == "admission":
        payload["admission_authorized"] = True
    text = "not-json" if variant == "malformed" else json.dumps(payload)
    helper.write_text("import sys\nprint(" + repr(text) + ")\nsys.exit(" +
                      ("1" if variant == "tool-failure" else "0") + ")\n")
    temporary = tmp_path / "runner"
    temporary.mkdir()
    output = temporary / "github-output"
    env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "RUNNER_TEMP": str(temporary),
           "GITHUB_OUTPUT": str(output), "REPOSITORY": REPO, "PR": "201", "HEAD_SHA": HEAD,
           "BASE_SHA": BASE, "POLICY_PATH": "config/source-qualification.json"}
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    if variant == "pass":
        assert result.returncode == 0, result.stderr
        assert output.read_text() == "source_qualified=true\n"
    else:
        assert result.returncode != 0
        assert not output.exists()


@pytest.fixture
def complete_native_source(tmp_path):
    git(tmp_path, "init", "-q", "-b", "base")
    (tmp_path / "a.py").write_text("print(1)\n")
    (tmp_path / "b.py").write_text("print(2)\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "Synthetic reviewed base")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-qb", "feature")
    (tmp_path / "a.py").write_text("print(3)\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "Synthetic reviewed source")
    head = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-q", "base")
    git(tmp_path, "merge", "--no-ff", "-qm", "Synthetic combined source", head)
    tested = git(tmp_path, "rev-parse", "HEAD")
    env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": REPO,
           "GITHUB_SHA": tested, "GITHUB_WORKFLOW_SHA": tested, "GITHUB_RUN_ID": "102",
           "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "security"}
    event = {"pull_request": {"head": {"sha": head}, "base": {"sha": base, "repo": {"full_name": REPO}}}}
    semgrep = {"version": "1.185.0", "results": [], "errors": [], "paths": {"scanned": ["a.py", "b.py"]}}
    bandit = {"results": [], "errors": [], "metrics": {"_totals": {"loc": 2}, "a.py": {"loc": 1}, "b.py": {"loc": 1}}}
    return tmp_path, env, event, lambda *a: git(tmp_path, *a), semgrep, bandit


def completed_source_receipt(fixture, **changes):
    root, env, event, read, semgrep, bandit = fixture
    parameters = {"compiled": configuration(), "producer": {"native_job_id": 302, "native_check_id": 202},
                  "semgrep": semgrep, "bandit": bandit, "semgrep_exit": 0, "bandit_exit": 0}
    parameters.update(changes)
    return Q.produce_receipt(env, event, "security", read, **parameters)


def test_actual_complete_reviewed_scope_produces_source_bound_receipt(complete_native_source):
    result = completed_source_receipt(complete_native_source)
    assert result["native_job_id"] == 302 and result["native_check_id"] == 202
    assert result["security_scope_sha256"] == Q.digest({
        "scope": configuration()["security_scope"], "inventory": {"semgrep": ["a.py", "b.py"], "bandit": ["a.py", "b.py"]}})
    assert result["result"] == "executed_pass"


@pytest.mark.parametrize("variant", ["ordinary", "assume-unchanged", "skip-worktree", "mode", "untracked"])
def test_actual_observable_source_change_cannot_claim_reviewed_tree(complete_native_source, variant):
    root, env, event, read, *_ = complete_native_source
    if variant in {"assume-unchanged", "skip-worktree"}:
        git(root, "update-index", "--" + variant, "b.py")
    if variant == "mode":
        (root / "b.py").chmod(0o755)
    elif variant == "untracked":
        (root / "unreviewed.py").write_text("print(5)\n")
    else:
        (root / "b.py").write_text("print(4)  # changed after meaningful checks\n")
    with pytest.raises(Q.QualificationError):
        completed_source_receipt(complete_native_source)


def test_observable_change_during_report_validation_invalidates_receipt(complete_native_source, monkeypatch):
    original = Q.scanner_report
    changed = False
    def report(*arguments):
        nonlocal changed
        if not changed:
            (complete_native_source[0] / "b.py").write_text("print(4)\n")
            changed = True
        return original(*arguments)
    monkeypatch.setattr(Q, "scanner_report", report)
    with pytest.raises(Q.QualificationError, match="source bytes"):
        completed_source_receipt(complete_native_source)


@pytest.mark.parametrize("scanner", ["semgrep", "bandit"])
def test_actual_tracked_python_omission_is_not_full_security(complete_native_source, scanner):
    if scanner == "semgrep":
        complete_native_source[4]["paths"]["scanned"].remove("b.py")
    else:
        del complete_native_source[5]["metrics"]["b.py"]
        complete_native_source[5]["metrics"]["_totals"]["loc"] = 1
    with pytest.raises(Q.QualificationError, match="complete reviewed"):
        completed_source_receipt(complete_native_source)


@pytest.mark.parametrize("reason", ["exceeded_size_limit", "parse_error", "ignored"])
def test_explicit_semgrep_skips_never_become_full_security(reason):
    report, _ = reports()
    report["paths"]["skipped"] = [{"path": "important.py", "reason": reason}]
    with pytest.raises(Q.QualificationError):
        Q.scanner_report(report, 0, "semgrep")


def test_reviewed_exclusions_bind_inventory_and_do_not_silently_waive_files(complete_native_source):
    policy = configuration()
    for scanner in ("semgrep", "bandit"):
        policy["security_scope"][scanner]["exclude"] = [{"path": "b.py", "reason": "Synthetic generated source; covered by separate deterministic recipe"}]
    complete_native_source[4]["paths"]["scanned"] = ["a.py"]
    del complete_native_source[5]["metrics"]["b.py"]
    complete_native_source[5]["metrics"]["_totals"]["loc"] = 1
    result = completed_source_receipt(complete_native_source, compiled=policy)
    assert result["security_scope_sha256"] == Q.digest({
        "scope": policy["security_scope"], "inventory": {"semgrep": ["a.py"], "bandit": ["a.py"]}})
    with pytest.raises(Q.QualificationError):
        completed_source_receipt(complete_native_source)


@pytest.mark.parametrize("bad", ["missing", "unreasoned", "traversal", "unsupported", "empty", "python-omitted"])
def test_security_scope_must_be_explicit_reviewed_and_supported(bad):
    value = configuration()
    if bad == "missing":
        del value["security_scope"]
    elif bad == "unreasoned":
        value["security_scope"]["semgrep"]["exclude"] = [{"path": "a.py", "reason": ""}]
    elif bad == "traversal":
        value["security_scope"]["semgrep"]["include"] = ["../*.py"]
    elif bad == "unsupported":
        value["security_scope"]["semgrep"]["extra"] = "unknown"
    elif bad == "empty":
        value["security_scope"]["semgrep"]["include"] = []
    else:
        value["security_scope"]["bandit"]["include"] = ["a.py"]
    with pytest.raises(Q.QualificationError):
        Q.policy(value)


def test_symlink_source_scope_does_not_open_an_external_target(complete_native_source, tmp_path):
    root, env, event, read, *_ = complete_native_source
    # A link outside the checkout has its Git text observed; its target is never
    # read and cannot qualify as scanned Python source without an owner adapter.
    target = root.parent / ("synthetic-unread-" + root.name)
    target.write_text("outside scope")
    (root / "b.py").unlink()
    (root / "b.py").symlink_to(target)
    git(root, "add", "b.py")
    git(root, "commit", "-qm", "Synthetic link candidate")
    tested = git(root, "rev-parse", "HEAD")
    event["pull_request"]["head"]["sha"] = tested
    event["pull_request"]["base"]["sha"] = git(root, "rev-parse", "HEAD^")
    env.update(GITHUB_SHA=tested, GITHUB_WORKFLOW_SHA=tested)
    target.chmod(0)
    try:
        with pytest.raises(Q.QualificationError, match="symlink"):
            completed_source_receipt(complete_native_source)
    finally:
        target.chmod(0o600)


def test_distinct_native_matrix_checks_require_unique_compiled_artifacts():
    value = configuration()
    first = value["required_checks"][-1]
    first["name"] = "security (3.12)"
    second = {**copy.deepcopy(first), "name": "security (3.14)"}
    value["required_checks"].append(second)
    with pytest.raises(Q.QualificationError, match="unique artifact"):
        Q.policy(value)
    second["artifact_key"] = "security_py314"
    assert Q.policy(value) is value


@pytest.mark.parametrize("field", ["native_job_id", "native_check_id", "security_scope_sha256"])
def test_receipt_cannot_move_between_actual_native_jobs_or_inventory(field):
    values = checks()
    old = values[-1]["qualification_receipt"][field]
    values[-1]["qualification_receipt"][field] = old + 1 if type(old) is int else "f" * 64
    with pytest.raises(Q.QualificationError):
        qualify(checks=values)


def test_actual_readonly_producing_job_identity_is_bound_not_inferred_from_job_key():
    api = FakeAPI()
    actual = api.checks[-1]
    actual.update(status="in_progress", conclusion=None)
    actual["native_job"].update(name="security", status="in_progress", conclusion=None)
    result = Q.producing_job(api, REPO, HEAD, 102, 1, configuration()["required_checks"][-1], tested_sha=MERGE)
    assert result == {"native_job_id": 302, "native_check_id": 202}
    actual["native_job"]["check_run_url"] = f"https://api.github.com/repos/{REPO}/check-runs/999"
    with pytest.raises(Q.QualificationError):
        Q.producing_job(api, REPO, HEAD, 102, 1, configuration()["required_checks"][-1], tested_sha=MERGE)


def test_two_actual_matrix_jobs_cannot_claim_same_compiled_producing_identity():
    api = FakeAPI()
    original = api.checks[-1]
    original.update(status="in_progress", conclusion=None)
    original["native_job"].update(name="security", status="in_progress", conclusion=None)
    duplicate = copy.deepcopy(original)
    duplicate.update(id=203, details_url=f"https://github.com/{REPO}/actions/runs/102/job/303")
    duplicate["native_job"].update(id=303, check_run_url=f"https://api.github.com/repos/{REPO}/check-runs/203")
    api.checks.append(duplicate)
    with pytest.raises(Q.QualificationError):
        Q.producing_job(api, REPO, HEAD, 102, 1, configuration()["required_checks"][-1], tested_sha=MERGE)


def test_actual_missing_native_producing_identity_fails_even_with_clean_checkout(complete_native_source):
    with pytest.raises(Q.QualificationError, match="actual native producing"):
        completed_source_receipt(complete_native_source, producer=None)


def test_old_head_request_is_audit_after_current_completed_independent_review():
    old = {"id": 1, "commit_id": "e" * 40, "state": "CHANGES_REQUESTED",
           "user": {"login": "chatgpt-codex-connector[bot]", "type": "Bot", "id": 202}}
    assert qualify(reviews=[old, *reviews()])["source_qualified"]
    current = {**old, "id": 13, "commit_id": HEAD, "submitted_at": "2026-11-11T10:01:00Z"}
    with pytest.raises(Q.QualificationError):
        qualify(reviews=[old, *reviews(), current])
    replacement = {**reviews()[0], "id": 14, "submitted_at": "2026-11-11T10:02:00Z"}
    assert qualify(reviews=[current, replacement])["source_qualified"]
    with pytest.raises(Q.QualificationError):
        qualify(reviews=[old, *reviews()], unresolved_threads=1)


def test_current_human_request_remains_a_blocker_even_with_bot_approval():
    human = {"id": 13, "commit_id": HEAD, "state": "CHANGES_REQUESTED", "submitted_at": "2026-11-11T10:01:00Z",
             "user": {"login": "owner", "type": "User", "id": 404}}
    with pytest.raises(Q.QualificationError):
        qualify(reviews=[*reviews(), human])


def test_complete_native_tree_inventory_and_compiled_scope_are_required():
    class Incomplete(FakeAPI):
        def get(self, path):
            value = super().get(path)
            if "/git/trees/" in path:
                value["truncated"] = True
            return value
    with pytest.raises(Q.QualificationError, match="complete native source"):
        Q.collect(Incomplete(), native_reviewer(), REPO, 201, HEAD, BASE, "config/source-qualification.json")


def test_distinct_matrix_namespaces_collect_only_their_actual_job_receipts():
    compiled = configuration()
    first = compiled["required_checks"][-1]
    first["name"] = "security (3.12)"
    second = {**copy.deepcopy(first), "name": "security (3.14)", "artifact_key": "security_py314"}
    compiled["required_checks"].append(second)
    class MatrixAPI(FakeAPI):
        def __init__(self):
            super().__init__()
            self.checks[-1]["name"] = first["name"]
            duplicate = copy.deepcopy(self.checks[-1])
            duplicate.update(id=203, name=second["name"],
                             details_url=f"https://github.com/{REPO}/actions/runs/102/job/303")
            duplicate["native_job"].update(id=303, check_run_url=f"https://api.github.com/repos/{REPO}/check-runs/203")
            duplicate["qualification_receipt"].update(
                native_job_id=303, native_check_id=203, artifact_key=second["artifact_key"])
            self.checks.append(duplicate)
        def get(self, path):
            if "/contents/config/" in path:
                raw = json.dumps(compiled).encode()
                return {"type": "file", "encoding": "base64", "content": base64.b64encode(raw).decode(),
                        "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()}
            return super().get(path)
        def artifact(self, repo, run, name, *, expected_head):
            matches = [c["qualification_receipt"] for c in self.checks
                       if c["workflow_run"]["id"] == run
                       and "source-qualification-" + c["qualification_receipt"]["artifact_key"] + "-1" == name]
            assert len(matches) == 1
            return copy.deepcopy(matches[0])
    api = MatrixAPI()
    result = Q.collect(api, native_reviewer(), REPO, 201, HEAD, BASE, "config/source-qualification.json")
    assert result["native_check_ids"] == [200, 201, 202, 203]
    api.checks[-1]["qualification_receipt"]["native_job_id"] = 302
    with pytest.raises(Q.QualificationError, match="execution identity"):
        Q.collect(api, native_reviewer(), REPO, 201, HEAD, BASE, "config/source-qualification.json")


def test_missing_security_inventory_identity_never_passes_a_direct_reader():
    values = checks()
    values[-1]["qualification_receipt"]["security_scope_sha256"] = None
    value = context()
    value.pop("security_scope_sha256")
    with pytest.raises(Q.QualificationError):
        qualify(checks=values, context=value)


def test_exact_native_producer_missing_permissions_cannot_mint_or_keep_receipt(tmp_path, monkeypatch):
    import os
    # Fail on the genuine authenticated route before any producer receipt is
    # emitted; there is no alternative credential route or stale pass fallback.
    environment = {"RUNNER_TEMP": str(tmp_path), "GITHUB_JOB": "security",
                   "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
                   "GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_PATH": str(tmp_path / "event.json"),
                   "GITHUB_SHA": MERGE, "GITHUB_WORKFLOW_SHA": MERGE,
                   "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request"}
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "event.json").write_text(json.dumps({"pull_request": {
        "head": {"sha": HEAD}, "base": {"sha": BASE, "repo": {"full_name": REPO}}}}))
    original = subprocess.check_output
    def git_read(arguments, **kwargs):
        if arguments[:3] == ["git", "--no-replace-objects", "show"]:
            return json.dumps(configuration()).encode()
        return original(arguments, **kwargs)
    monkeypatch.setattr(Q.subprocess, "check_output", git_read)
    monkeypatch.setattr(Q, "strict_json", lambda raw: json.loads(raw))
    calls = []
    class Denied:
        def __init__(self, reviewer):
            pass
        def get(self, *args):
            calls.append(args)
            raise RuntimeError("scoped native read unavailable")
        pages = get
    monkeypatch.setattr(Q, "NativeAPI", Denied)
    output = tmp_path / "source-qualification-123-1-security-security/source-qualification.json"
    output.parent.mkdir()
    output.write_text('{"result":"executed_pass"}')
    # Classify avoids any raw scanner inputs; compiled role still must match.
    monkeypatch.setenv("GITHUB_JOB", "classify")
    classify_output = tmp_path / "source-qualification-123-1-classify-classify/source-qualification.json"
    classify_output.parent.mkdir()
    classify_output.write_text('{"result":"executed_pass"}')
    # The producer catches an authenticated native availability error just like
    # the collector, retains no successful evidence and does not switch routes.
    assert Q.receipt_main(["--role", "classify", "--output", str(classify_output)]) == 1
    assert calls and not classify_output.exists()
    assert output.exists()  # A different job's namespace is not touched.
    assert os.environ["GITHUB_REPOSITORY"] == REPO


@pytest.mark.parametrize("request_later", [True, False])
@pytest.mark.parametrize("reverse_api_order", [True, False])
def test_native_submission_time_not_draft_creation_id_orders_effective_review(request_later, reverse_api_order):
    approved = reviews()[0]
    requested = copy.deepcopy(approved)
    approved.update(id=1000 if request_later else 900,
                    submitted_at="2026-11-11T11:00:00Z" if request_later else "2026-11-11T12:00:00Z")
    requested.update(id=900 if request_later else 1000, state="CHANGES_REQUESTED",
                     submitted_at="2026-11-11T12:00:00Z" if request_later else "2026-11-11T11:00:00Z")
    values = [approved, requested]
    if reverse_api_order:
        values.reverse()
    if request_later:
        with pytest.raises(Q.QualificationError, match="independent completed"):
            qualify(reviews=values)
    else:
        result = qualify(reviews=values)
        assert result["source_qualified"] and result["admission_authorized"] is False


@pytest.mark.parametrize("stamp", [None, "", True, 1000, "2026-11-11T10:00:00",
    "2026-11-11 10:00:00Z", "2026-13-11T10:00:00Z", "2026-11-11T10:60:00Z",
    "2026-11-11T10:00:00-00:00", "2026-11-11T10:00:00+24:00", "2026-11-11T10:00:00.123456789Z"])
def test_missing_malformed_or_timezone_unknown_native_submission_is_never_inferred(stamp):
    values = reviews()
    values[0]["submitted_at"] = stamp
    values[0].update(id=999999, created_at="2026-11-11T10:00:00Z")
    with pytest.raises(Q.QualificationError, match="submission timestamp"):
        qualify(reviews=values)


def test_absent_native_submission_field_never_falls_back_to_creation_id_or_clock():
    values = reviews()
    values[0].pop("submitted_at")
    values[0].update(created_at="2026-11-11T10:00:00Z", id=999999)
    with pytest.raises(Q.QualificationError, match="submission timestamp"):
        qualify(reviews=values)


@pytest.mark.parametrize("same_state", [True, False])
def test_ambiguous_latest_timezone_equivalent_submissions_fail_closed(same_state):
    first = reviews()[0]
    second = copy.deepcopy(first)
    first.update(id=1000, submitted_at="2026-11-11T12:00:00.100000Z")
    second.update(id=900, submitted_at="2026-11-11T14:00:00.1+02:00",
                  state="APPROVED" if same_state else "CHANGES_REQUESTED")
    for values in ([first, second], [second, first]):
        with pytest.raises(Q.QualificationError, match="ambiguous"):
            qualify(reviews=values)


def test_timezone_offsets_are_compared_as_actual_instants_not_lexical_strings():
    approved = reviews()[0]
    requested = copy.deepcopy(approved)
    approved.update(id=900, submitted_at="2026-11-11T13:00:00+01:00")
    requested.update(id=1000, state="CHANGES_REQUESTED", submitted_at="2026-11-11T09:59:59-02:00")
    assert qualify(reviews=[requested, approved])["source_qualified"]
    requested["submitted_at"] = "2026-11-11T13:00:01+01:00"
    with pytest.raises(Q.QualificationError, match="independent completed"):
        qualify(reviews=[approved, requested])


def test_definitively_later_submission_supersedes_older_tie_without_a_dismissal_task():
    old = reviews()[0]
    old.update(id=1000, submitted_at="2026-11-11T10:00:00Z", state="CHANGES_REQUESTED")
    tied = {**old, "id": 1001, "state": "APPROVED"}
    latest = {**old, "id": 900, "state": "APPROVED", "submitted_at": "2026-11-11T10:00:01Z"}
    for values in ([old, tied, latest], [latest, tied, old]):
        assert qualify(reviews=values)["source_qualified"]


def test_duplicate_native_identity_is_not_a_chronology_tie_and_conflicting_records_refuse():
    current = reviews()[0]
    assert qualify(reviews=[current, copy.deepcopy(current)])["source_qualified"]
    conflicting = {**current, "submitted_at": "2026-11-11T11:00:00Z", "state": "CHANGES_REQUESTED"}
    with pytest.raises(Q.QualificationError, match="conflicting duplicate"):
        qualify(reviews=[current, conflicting])


def test_earlier_current_opinion_with_unknown_submission_cannot_be_silently_ignored():
    old = {**reviews()[0], "id": 900, "submitted_at": None, "state": "CHANGES_REQUESTED"}
    current = {**reviews()[0], "id": 1000, "submitted_at": "2026-11-11T12:00:00Z"}
    with pytest.raises(Q.QualificationError, match="submission timestamp"):
        qualify(reviews=[old, current])


def test_old_head_missing_timestamp_is_audit_but_unresolved_threads_still_block():
    # Separate native records have separate IDs; one ID cannot attest two heads.
    old = {**reviews()[0], "id": 11, "commit_id": "e" * 40, "state": "CHANGES_REQUESTED"}
    old.pop("submitted_at")
    assert qualify(reviews=[old, *reviews()])["source_qualified"]
    with pytest.raises(Q.QualificationError):
        qualify(reviews=[old, *reviews()], unresolved_threads=1)


def test_current_human_request_remains_independent_of_newer_bot_approval():
    human = {"id": 1000, "commit_id": HEAD, "state": "CHANGES_REQUESTED", "submitted_at": "2026-11-11T11:00:00Z",
             "user": {"id": 404, "login": "owner", "type": "User"}}
    bot = {**reviews()[0], "id": 900, "submitted_at": "2026-11-11T12:00:00Z"}
    with pytest.raises(Q.QualificationError):
        qualify(reviews=[human, bot])


def test_pending_or_comment_only_native_review_never_becomes_approval_or_a_new_opinion():
    pending = {**reviews()[0], "id": 1000, "state": "PENDING", "submitted_at": None}
    comment = {**reviews()[0], "id": 1001, "state": "COMMENTED", "submitted_at": "2026-11-11T12:00:00Z", "body": ""}
    with pytest.raises(Q.QualificationError, match="independent completed"):
        qualify(reviews=[pending, comment])
    assert qualify(reviews=[*reviews(), pending, comment])["source_qualified"]


@pytest.mark.parametrize("bad_state", ["missing", None, "", "UNKNOWN", "changes_requested", True, [], {}])
@pytest.mark.parametrize("publisher", ["human", "same-bot"])
def test_unknown_current_native_opinion_is_never_silently_discarded(bad_state, publisher):
    current = copy.deepcopy(reviews()[0])
    current.update(id=800, submitted_at="2026-11-11T12:00:00Z", state=bad_state)
    if publisher == "human":
        current["user"] = {"id": 404, "login": "owner", "type": "User"}
    if bad_state == "missing":
        current.pop("state")
    with pytest.raises(Q.QualificationError, match="current-head review state unavailable"):
        qualify(reviews=[*reviews(), current])


@pytest.mark.parametrize("known_state", ["PENDING", "COMMENTED"])
def test_explicit_known_nondispositive_state_neither_supplies_approval_nor_vetoes_it(known_state):
    current = copy.deepcopy(reviews()[0])
    current.update(id=800, submitted_at=None, state=known_state)
    current["user"] = {"id": 404, "login": "owner", "type": "User"}
    with pytest.raises(Q.QualificationError, match="independent completed"):
        qualify(reviews=[current])
    assert qualify(reviews=[*reviews(), current])["source_qualified"]
    with pytest.raises(Q.QualificationError):
        qualify(reviews=[*reviews(), current], unresolved_threads=1)


@pytest.mark.parametrize("old_state", [None, "UNKNOWN"])
def test_unknown_old_head_history_does_not_replace_current_head_native_facts(old_state):
    old = copy.deepcopy(reviews()[0])
    old.update(id=800, commit_id="e" * 40, submitted_at=None, state=old_state)
    assert qualify(reviews=[old, *reviews()])["source_qualified"]
    with pytest.raises(Q.QualificationError):
        qualify(reviews=[old, *reviews()], unresolved_threads=1)


class PagedNative(Q.NativeAPI):
    """Synthetic authenticated-response shapes; no network or native proof."""
    def __init__(self, values):
        self.values = iter(values)
        self.reads = []

    def get(self, path):
        self.reads.append(path)
        return copy.deepcopy(next(self.values))


@pytest.mark.parametrize("key", [None, "check_runs", "artifacts"])
def test_actual_native_page_reader_requires_complete_unique_inventory(key):
    rows = [{"id": n + 1} for n in range(101)]
    values = [rows[:100], rows[100:]]
    if key:
        values = [{"total_count": 101, key: batch} for batch in values]
    api = PagedNative(values)
    assert api.pages("repos/Example/framework/native", key) == rows
    assert api.reads[-1].endswith("per_page=100&page=2")


@pytest.mark.parametrize("values,key", [
    ([{"check_runs": []}], "check_runs"),
    ([{"total_count": True, "check_runs": []}], "check_runs"),
    ([{"total_count": 1.0, "check_runs": []}], "check_runs"),
    ([{"total_count": 2, "check_runs": [{"id": 1}]}], "check_runs"),
    ([{"total_count": 0, "check_runs": [{"id": 1}]}], "check_runs"),
    ([[{"id": True}]], None),
    ([[{"id": 0}]], None),
    ([[{"id": 1}, {"id": 1}]], None),
    ([[{"id": n + 1} for n in range(101)]], None),
    ([{"total_count": 101, "check_runs": [{"id": n + 1} for n in range(100)]},
      {"total_count": 100, "check_runs": []}], "check_runs"),
    ([[{"id": n + 1} for n in range(100)], [{"id": 100}]], None),
])
def test_partial_malformed_or_repeated_native_pages_cannot_supply_qualification(values, key):
    with pytest.raises(Q.QualificationError, match="pagination|page record"):
        PagedNative(values).pages("repos/Example/framework/native", key)


def test_native_pagination_bound_is_not_a_successful_empty_terminal_page():
    api = PagedNative([[{"id": p * 100 + n + 1} for n in range(100)] for p in range(30)])
    with pytest.raises(Q.QualificationError, match="bound exceeded"):
        api.pages("repos/Example/framework/native")
    assert len(api.reads) == 30


def artifact_transport(monkeypatch, *, mode=None, dos_directory=False):
    import io
    import zipfile
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        member = zipfile.ZipInfo("source-qualification.json")
        member.create_system = 3
        member.external_attr = ((0o600 if mode is None else mode) << 16) | (0x10 if dos_directory else 0)
        archive.writestr(member, json.dumps({"synthetic": "transport-only"}))
    body = data.getvalue()

    class ArtifactAPI(Q.NativeAPI):
        def __init__(self):
            self.metadata = {"id": 900, "name": "source-qualification-security-1", "expired": False,
                             "size_in_bytes": len(body), "workflow_run": {"id": 102, "head_sha": HEAD}}
            self.reads, self.downloads = [], []

        def get(self, path):
            self.reads.append(path)
            if path.endswith("artifacts?per_page=100&page=1"):
                return {"total_count": 1, "artifacts": [copy.deepcopy(self.metadata)]}
            if path == f"repos/{REPO}/actions/artifacts/900":
                return copy.deepcopy(self.metadata)
            raise AssertionError("unexpected native metadata route")

    api = ArtifactAPI()
    def downloaded(command, **kwargs):
        assert command == ["gh", "api", f"repos/{REPO}/actions/artifacts/900/zip"]
        api.downloads.append(command)
        return SimpleNamespace(stdout=body)
    monkeypatch.setattr(Q.subprocess, "run", downloaded)
    return api


def test_artifact_native_api_binds_only_actual_run_and_head_not_an_invented_job_attestation(monkeypatch):
    api = artifact_transport(monkeypatch)
    assert "job_id" not in api.metadata and "job_id" not in api.metadata["workflow_run"]
    assert api.artifact(REPO, 102, "source-qualification-security-1", expected_head=HEAD) == {"synthetic": "transport-only"}
    assert len(api.downloads) == 1


@pytest.mark.parametrize("broken", ["run", "bool-run", "head", "missing-run", "missing-head", "id", "expired"])
def test_artifact_run_source_unknown_or_foreign_identity_holds_before_download(monkeypatch, broken):
    api = artifact_transport(monkeypatch)
    if broken == "run":
        api.metadata["workflow_run"].update(id=999, job_id=302)  # Bogus extra cannot attest a job or repair a wrong run.
    elif broken == "bool-run":
        api.metadata["workflow_run"]["id"] = True
    elif broken == "head":
        api.metadata["workflow_run"]["head_sha"] = "e" * 40
    elif broken == "missing-run":
        api.metadata.pop("workflow_run")
    elif broken == "missing-head":
        api.metadata["workflow_run"].pop("head_sha")
    elif broken == "id":
        api.metadata["id"] = True
    else:
        api.metadata["expired"] = True
    with pytest.raises(Q.QualificationError):
        api.artifact(REPO, 102, "source-qualification-security-1", expected_head=HEAD)
    assert not api.downloads


@pytest.mark.parametrize("broken", ["missing", "head", "path", "event", "workflow-bytes"])
def test_prepared_producer_requires_independent_exact_native_workflow_source(broken):
    class WrongWorkflow(FakeAPI):
        def get(self, path):
            value = super().get(path)
            if "/actions/runs/" in path:
                if broken == "head":
                    value["head_sha"] = "e" * 40
                elif broken == "path":
                    value["path"] = ".github/workflows/unrelated.yml"
                elif broken == "event":
                    value["event"] = "workflow_dispatch"
            elif "/contents/" in path and broken == "workflow-bytes":
                raw = b"name: unreviewed producer\n"
                value.update(content=base64.b64encode(raw).decode(),
                             sha=hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest())
            return value
    api = WrongWorkflow()
    with pytest.raises(Q.QualificationError, match="producer (source|workflow)"):
        Q.producing_job(api, REPO, HEAD, 102, 1, configuration()["required_checks"][-1],
                        tested_sha=None if broken == "missing" else MERGE)


@pytest.mark.parametrize("field,value", [("run_attempt", True), ("run_id", True)])
def test_actual_native_job_numeric_fields_never_accept_boolean_aliases(field, value):
    values = checks()
    # Attempt1 equals True in Python; record types must not be inferred from equality.
    values[-1]["native_job"][field] = value
    with pytest.raises(Q.QualificationError):
        qualify(checks=values)


@pytest.mark.parametrize("source", [None, "", True, "not-a-source", "a" * 40 + "\n"])
def test_unknown_review_source_cannot_be_silently_classified_as_old_head_audit(source):
    current = copy.deepcopy(reviews()[0])
    current.update(id=99, commit_id=source, state="CHANGES_REQUESTED")
    with pytest.raises(Q.QualificationError, match="review source unavailable"):
        qualify(reviews=[*reviews(), current])


@pytest.mark.parametrize("raw", ['{"n":NaN}', '{"n":Infinity}', '{"n":-Infinity}', '{"n":1,"n":2}'])
def test_native_json_nonfinite_or_duplicate_values_hold(raw):
    with pytest.raises(Q.QualificationError):
        Q.strict_json(raw)


def test_native_json_size_limit_is_checked_before_parsing(monkeypatch):
    monkeypatch.setattr(Q, "MAX_BYTES", 2)
    with pytest.raises(Q.QualificationError, match="bound"):
        Q.strict_json(b"null")


@pytest.mark.parametrize("conclusion", ["UNAVAILABLE", "failure", "cancelled", "skipped", None])
def test_optional_model_unavailability_is_visible_and_never_relabelled_pass(conclusion):
    values = checks()
    values.append({"id": 901, "name": "optional-model", "app": {"id": 10101},
                   "status": "completed", "conclusion": conclusion})
    result = qualify(checks=values)
    assert result["source_qualified"] is True and result["admission_authorized"] is False
    assert result["optional_model_audit"][0]["conclusion"] == conclusion
    assert result["optional_model_audit"][0]["conclusion"] != "success"


@pytest.mark.parametrize("repository", ["../..", "./framework", "Example/..", "Example/."])
def test_repository_dot_segments_cannot_select_a_different_native_api_scope(repository):
    with pytest.raises(Q.QualificationError):
        qualify(context={**context(), "repository": repository})
    api = PagedNative([])
    with pytest.raises(Q.QualificationError):
        api.artifact(repository, 102, "source-qualification-security-1", expected_head=HEAD)
    assert not api.reads


@pytest.mark.parametrize("job", [None, False, "unknown", []])
def test_unknown_native_job_record_holds(job):
    values = checks()
    values[-1]["native_job"] = job
    with pytest.raises(Q.QualificationError, match="native required job"):
        qualify(checks=values)


def test_git_blob_hashing_uses_nonsecurity_sha1_without_changing_git_identity(monkeypatch):
    api = FakeAPI()
    original = hashlib.sha1
    calls = []
    def permitted(data=b"", *, usedforsecurity=True):
        assert usedforsecurity is False
        calls.append(data)
        return original(data, usedforsecurity=False)
    # Prepare actual synthetic metadata before requiring nonsecurity SHA-1, so
    # only the reader's raw Git identity calculation is measured here.
    metadata = api.get(f"repos/{REPO}/contents/config/source-qualification.json?ref={BASE}")
    monkeypatch.setattr(Q.hashlib, "sha1", permitted)
    assert Q.immutable_file(SimpleNamespace(get=lambda path: metadata), REPO,
                            "config/source-qualification.json", BASE) == base64.b64decode(metadata["content"])
    assert calls


def transport_module():
    path = ROOT / "scripts/ci/source_qualification_transport.py"
    spec = importlib.util.spec_from_file_location("exclusive_transport_contract", path)
    module = importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def exclusive_native_fixture():
    transport = transport_module()
    controller, component = "e" * 40, "f" * 40
    workflow = b"name: Metadata only producer\non: workflow_dispatch\njobs:\n  producer: {}\n"
    compiled = {"schema_version": 1, "publication_status": "accepted_source_only",
                "component_repository": transport.COMPONENT, "component_sha": component,
                "native_app_id": 10101, "workflow_path": transport.WORKFLOW,
                "workflow_sha256": hashlib.sha256(workflow).hexdigest(),
                "job_key": transport.JOB, "job_name": transport.JOB_NAME,
                "artifact_key": transport.ARTIFACT_KEY}

    class API(transport.Q.NativeAPI):
        def __init__(self):
            self.run = {"id": 701, "event": "workflow_dispatch", "path": transport.WORKFLOW,
                        "head_sha": controller, "repository": {"full_name": REPO},
                        "run_attempt": 1, "status": "completed", "conclusion": "success"}
            self.jobs = [{"id": 702, "name": transport.JOB_NAME, "run_id": 701, "run_attempt": 1,
                          "status": "completed", "conclusion": "success",
                          "check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/703"}]
            self.check = {"id": 703, "name": transport.JOB_NAME, "app": {"id": 10101},
                          "head_sha": controller, "status": "completed", "conclusion": "success",
                          "details_url": f"https://github.com/{REPO}/actions/runs/701/job/702"}
            self.compiled = copy.deepcopy(compiled)
            self.record = None
            self.reads = []
            self.artifact_reads = []

        def get(self, path):
            self.reads.append(path)
            if "/contents/" in path:
                raw = json.dumps(self.compiled).encode() if transport.BINDING in path else workflow
                return {"encoding": "base64", "type": "file", "content": base64.b64encode(raw).decode(),
                        "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()}
            if path.endswith("/actions/runs/701/attempts/1"):
                return copy.deepcopy(self.run)
            if path.endswith("/attempts/1/jobs?per_page=100&page=1"):
                return {"total_count": len(self.jobs), "jobs": copy.deepcopy(self.jobs)}
            if path.endswith("/check-runs/703"):
                return copy.deepcopy(self.check)
            raise AssertionError("unexpected synthetic native route")

        def artifact(self, repo, run_id, name, *, expected_head):
            assert (repo, run_id, name, expected_head) == (REPO, 701, "source-qualification-transport_proof-1", controller)
            self.artifact_reads.append(name)
            return copy.deepcopy(self.record)

    api = API()
    api.record = transport.prepared_record(transport.native_context(api, REPO, controller, 701, 1, completed=True))
    return transport, api, controller


def test_exclusive_prepared_native_run_can_bind_transport_without_qualifying_source_or_production():
    transport, api, controller = exclusive_native_fixture()
    result = transport.read_transport(api, REPO, controller, 701, 1)
    assert result["native_transport_bound"] is True
    assert result["source_qualified"] is False and result["admission_authorized"] is False
    assert len(api.artifact_reads) == 1
    assert any("/attempts/1/jobs?" in path for path in api.reads)


@pytest.mark.parametrize("change", ["other-job", "skipped-other-job", "wrong-event", "wrong-controller",
                                    "wrong-App", "wrong-job", "failed", "unknown-attempt", "pending-source"])
def test_run_association_is_not_job_attestation_without_complete_exclusive_producer(change):
    transport, api, controller = exclusive_native_fixture()
    if change in {"other-job", "skipped-other-job"}:
        api.jobs.append({**api.jobs[0], "id": 799, "name": "Untrusted PR test code",
                         "status": "completed", "conclusion": "skipped" if change == "skipped-other-job" else "success"})
    elif change == "wrong-event":
        # A workflow_run head belongs to its triggering source; never infer the controller from it.
        api.run["event"] = "workflow_run"
    elif change == "wrong-controller":
        api.run["head_sha"] = "a" * 40
    elif change == "wrong-App":
        api.check["app"]["id"] = 909
    elif change == "wrong-job":
        api.jobs[0]["name"] = "Different producer"
    elif change == "failed":
        api.jobs[0]["conclusion"] = "failure"
    elif change == "unknown-attempt":
        api.jobs[0]["run_attempt"] = True
    else:
        api.compiled.update(publication_status="pending", component_sha=None)
    with pytest.raises(transport.Q.QualificationError):
        transport.read_transport(api, REPO, controller, 701, 1)
    assert not api.artifact_reads


@pytest.mark.parametrize("change", ["component", "controller", "job", "attempt", "extra", "source", "digest"])
def test_exclusive_prepared_subject_is_exact_not_payload_self_granted(change):
    transport, api, controller = exclusive_native_fixture()
    if change == "component":
        api.record["component_sha"] = "a" * 40
    elif change == "controller":
        api.record["controller_sha"] = "a" * 40
    elif change == "job":
        api.record["native_job_id"] = 799
    elif change == "attempt":
        api.record["run_attempt"] = True
    elif change == "extra":
        api.record["extra_authority"] = True
    elif change == "source":
        api.record["source_qualified"] = True
    else:
        api.record["subject_sha256"] = "0" * 64
    with pytest.raises(transport.Q.QualificationError, match="subject/digest"):
        transport.read_transport(api, REPO, controller, 701, 1)


def test_same_run_forged_component_receipt_never_becomes_native_source_authority():
    api = FakeAPI()
    result = Q.collect(api, native_reviewer(), REPO, 201, HEAD, BASE, "config/source-qualification.json")
    assert result["candidate_coherent"] is True  # IDs and source payloads can all agree.
    assert result["source_qualified"] is False  # RUN-only artifact origin does not identify the JOB.
    assert result["native_transport_bound"] is False
    assert result["admission_authorized"] is False
    assert result["cutover"] == "held_native_component_artifact_origin_unqualified"


def test_metadata_producer_workflow_has_one_job_no_untrusted_code_or_mutation_credentials():
    transport = transport_module()
    path = ROOT / transport.WORKFLOW
    value = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert set(value["on"]) == {"workflow_dispatch"}
    assert not value["on"]["workflow_dispatch"]
    assert set(value["jobs"]) == {"producer"}
    assert value["permissions"] == {"contents": "read", "actions": "read", "checks": "read"}
    steps = value["jobs"]["producer"]["steps"]
    assert steps[0]["with"]["ref"] == "${{ github.workflow_sha }}"
    component = next(step for step in steps if step.get("name") == "Read only the compiled trusted metadata producer")
    assert component["with"]["ref"] == "${{ steps.binding.outputs.component_sha }}"
    tokens = [step for step in steps if "GH_TOKEN" in step.get("env", {})]
    assert len(tokens) == 1 and tokens[0]["id"] == "receipt"
    text = path.read_text()
    for forbidden in ["pull_request", "secrets.", "id-token", "workflow_run:", "pip install", "npm ", "pytest", "environment:"]:
        assert forbidden not in text
    for step in steps:
        assert "continue-on-error" not in step
    assert steps[-1]["with"]["name"] == "source-qualification-transport_proof-${{ github.run_attempt }}"


def test_default_producer_binding_is_pending_source_publication_not_native_authority():
    transport = transport_module()
    value = json.loads((ROOT / transport.BINDING).read_text())
    assert value["publication_status"] == "pending" and value["component_sha"] is None
    assert value["native_app_id"] is None
    assert value["workflow_sha256"] == hashlib.sha256((ROOT / transport.WORKFLOW).read_bytes()).hexdigest()
    with pytest.raises(transport.Q.QualificationError):
        transport.binding(value)


@pytest.mark.parametrize("change", ["running", "run-attempt", "unknown-run-status", "check-status", "missing-controller"])
def test_exclusive_run_requires_finished_exact_native_attempt_before_artifact_read(change):
    transport, api, controller = exclusive_native_fixture()
    if change == "running":
        api.run.update(status="in_progress", conclusion=None)
    elif change == "run-attempt":
        api.run["run_attempt"] = 2
    elif change == "unknown-run-status":
        api.run.pop("status")
    elif change == "check-status":
        api.check["status"] = "in_progress"
    else:
        controller = None
    with pytest.raises(transport.Q.QualificationError):
        transport.read_transport(api, REPO, controller, 701, 1)
    assert not api.artifact_reads


@pytest.mark.parametrize("change", ["native-denial", "changed-binding", "different-component", "wrong-event"])
def test_exclusive_producer_denial_never_keeps_stale_canary(tmp_path, monkeypatch, change):
    transport, api, controller = exclusive_native_fixture()
    for row in [api.run, api.jobs[0], api.check]:
        row.update(status="in_progress", conclusion=None)
    env = {"RUNNER_TEMP": str(tmp_path), "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
           "GITHUB_JOB": "producer", "GITHUB_WORKFLOW_SHA": controller, "GITHUB_REPOSITORY": REPO,
           "GITHUB_RUN_ID": "701", "GITHUB_RUN_ATTEMPT": "1"}
    compiled = tmp_path / "binding.json"
    compiled.write_text(json.dumps(api.compiled))
    output = tmp_path / "source-qualification-701-1-producer-transport_proof/source-qualification.json"
    output.parent.mkdir()
    output.write_text('{"native_transport_bound":true}')
    monkeypatch.setattr(transport.subprocess, "check_output", lambda *a, **kw: (api.compiled["component_sha"] + "\n").encode())
    if change == "native-denial":
        def denied(path):
            raise subprocess.CalledProcessError(1, "gh", stderr="scoped read denied")
        api.get = denied
    elif change == "changed-binding":
        compiled.write_text(json.dumps({**api.compiled, "native_app_id": 909}))
    elif change == "different-component":
        monkeypatch.setattr(transport.subprocess, "check_output", lambda *a, **kw: b"a" * 40 + b"\n")
    else:
        env["GITHUB_EVENT_NAME"] = "workflow_run"
    with pytest.raises((transport.Q.QualificationError, subprocess.CalledProcessError)):
        transport.produce_transport(api, env, compiled, output)
    assert not output.exists()


def test_exclusive_producer_emits_only_rederived_prepared_context(tmp_path, monkeypatch):
    transport, api, controller = exclusive_native_fixture()
    expected = copy.deepcopy(api.record)
    for row in [api.run, api.jobs[0], api.check]:
        row.update(status="in_progress", conclusion=None)
    env = {"RUNNER_TEMP": str(tmp_path), "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
           "GITHUB_JOB": "producer", "GITHUB_WORKFLOW_SHA": controller, "GITHUB_REPOSITORY": REPO,
           "GITHUB_RUN_ID": "701", "GITHUB_RUN_ATTEMPT": "1"}
    compiled = tmp_path / "binding.json"
    compiled.write_text(json.dumps(api.compiled))
    output = tmp_path / "source-qualification-701-1-producer-transport_proof/source-qualification.json"
    observed = []
    def component(args, **kwargs):
        observed.append((args, kwargs))
        return (api.compiled["component_sha"] + "\n").encode()
    monkeypatch.setattr(transport.subprocess, "check_output", component)
    transport.produce_transport(api, env, compiled, output)
    assert json.loads(output.read_text()) == expected
    assert observed[0][0] == ["git", "--no-replace-objects", "rev-parse", "HEAD"]
    assert observed[0][1]["timeout"] == 15
    assert not json.loads(output.read_text())["source_qualified"]


def test_exclusive_cli_malformed_native_archive_is_failed_not_partial_success(monkeypatch, capsys):
    transport, api, controller = exclusive_native_fixture()
    def malformed(*args, **kwargs):
        raise transport.zipfile.BadZipFile("synthetic malformed native archive")
    api.artifact = malformed
    monkeypatch.setattr(transport.Q, "NativeAPI", lambda reviewer: api)
    assert transport.main(["--repository", REPO, "--controller-sha", controller, "--run-id", "701", "--attempt", "1"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {"schema": transport.SCHEMA, "native_transport_bound": False,
                      "source_qualified": False, "admission_authorized": False}


def test_held_live_collector_cli_is_nonzero_even_when_all_normalized_payloads_agree(monkeypatch, capsys):
    monkeypatch.setattr(Q, "NativeAPI", lambda reviewer: FakeAPI())
    monkeypatch.setattr(Q, "review_module", native_reviewer)
    assert Q.main(["--repository", REPO, "--pr", "201", "--head", HEAD, "--base", BASE]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["candidate_coherent"] is True
    assert result["source_qualified"] is False and result["admission_authorized"] is False


def test_native_json_limit_counts_utf8_bytes_not_python_characters(monkeypatch):
    monkeypatch.setattr(Q, "MAX_BYTES", 8)
    assert Q.strict_json('"ééé"') == "ééé"  # Exactly eight UTF-8 bytes.
    with pytest.raises(Q.QualificationError):
        Q.strict_json('"éééé"')


@pytest.mark.parametrize("publisher", ["human", "compiled-bot"])
def test_distinct_dismissed_record_cannot_hide_a_still_active_change_request(publisher):
    request = {**reviews()[0], "id": 501, "state": "CHANGES_REQUESTED",
               "submitted_at": "2026-11-11T10:00:00Z"}
    if publisher == "human":
        request["user"] = {"id": 404, "login": "owner", "type": "User"}
    else:
        request["user"] = {"id": 101, "login": "coderabbitai[bot]", "type": "Bot"}
    dismissed = {**copy.deepcopy(request), "id": 502, "state": "DISMISSED",
                 "submitted_at": "2026-11-11T11:00:00Z"}
    for values in ([request, dismissed, *reviews()], [*reviews(), dismissed, request]):
        with pytest.raises(Q.QualificationError, match="independent completed"):
            qualify(reviews=values)


def test_actual_dismissed_request_record_is_audit_and_never_approval():
    dismissed = {**reviews()[0], "id": 501, "state": "DISMISSED", "submitted_at": None}
    with pytest.raises(Q.QualificationError, match="independent completed"):
        qualify(reviews=[dismissed])
    dismissed["user"] = {"id": 404, "login": "owner", "type": "User"}
    assert qualify(reviews=[dismissed, *reviews()])["source_qualified"]
    assert qualify(reviews=[*reviews(), dismissed])["source_qualified"]


def test_later_explicit_approval_can_supersede_request_without_dismissal_chronology_guess():
    request = {**reviews()[0], "id": 501, "state": "CHANGES_REQUESTED"}
    dismissed = {**request, "id": 502, "state": "DISMISSED", "submitted_at": None}
    approved = {**request, "id": 499, "state": "APPROVED", "submitted_at": "2026-11-11T11:00:00Z"}
    for values in ([request, dismissed, approved], [approved, dismissed, request]):
        assert qualify(reviews=values)["source_qualified"]


@pytest.mark.parametrize("mode,dos_directory", [
    (0o010600, False), (0o020600, False), (0o040600, False),
    (0o060600, False), (0o120600, False), (0o140600, False),
    (0o600, True), (0o100600, True),
])
def test_native_archive_encoded_nonregular_members_refuse_before_json_parser(monkeypatch, mode, dos_directory):
    api = artifact_transport(monkeypatch, mode=mode, dos_directory=dos_directory)
    parsed = []
    monkeypatch.setattr(Q, "strict_json", lambda raw: parsed.append(raw))
    with pytest.raises(Q.QualificationError, match="unsafe native artifact"):
        api.artifact(REPO, 102, "source-qualification-security-1", expected_head=HEAD)
    assert not parsed


@pytest.mark.parametrize("mode", [0o600, 0o100600, 0o100644])
def test_native_archive_regular_or_unspecified_kind_is_only_read_as_json_bytes(monkeypatch, mode):
    api = artifact_transport(monkeypatch, mode=mode)
    assert api.artifact(REPO, 102, "source-qualification-security-1", expected_head=HEAD) == {
        "synthetic": "transport-only"}


NATIVE_REVIEW_STATES = ["APPROVED", "CHANGES_REQUESTED", "DISMISSED", "COMMENTED", "PENDING"]


@pytest.mark.parametrize("first_state", NATIVE_REVIEW_STATES)
@pytest.mark.parametrize("second_state", NATIVE_REVIEW_STATES)
def test_conflicting_same_native_review_id_refuses_before_any_state_filter(first_state, second_state):
    first = {**reviews()[0], "state": first_state, "body": "First native snapshot"}
    second = {**copy.deepcopy(first), "state": second_state, "body": "Conflicting native snapshot"}
    for values in ([first, second], [second, first]):
        with pytest.raises(Q.QualificationError, match="conflicting duplicate"):
            Q.effective_current_reviews(values, HEAD)


@pytest.mark.parametrize("state", NATIVE_REVIEW_STATES)
def test_identical_native_review_duplicates_are_idempotent_for_every_known_state(state):
    first = {**reviews()[0], "state": state}
    reordered = dict(reversed(list(copy.deepcopy(first).items())))
    expected = [] if state in {"DISMISSED", "COMMENTED", "PENDING"} else [first]
    assert Q.effective_current_reviews([first, reordered], HEAD) == expected


@pytest.mark.parametrize("state", NATIVE_REVIEW_STATES)
@pytest.mark.parametrize("identity", [None, True, 0, "12", []])
def test_every_current_native_review_needs_a_typed_id_before_disposition_filter(state, identity):
    value = {**reviews()[0], "state": state, "id": identity}
    with pytest.raises(Q.QualificationError, match="record identity"):
        Q.effective_current_reviews([value], HEAD)


def test_duplicate_source_and_numeric_alias_edits_cannot_be_filtered_as_audit():
    first = reviews()[0]
    old_head = {**copy.deepcopy(first), "commit_id": "e" * 40}
    for values in ([first, old_head], [old_head, first]):
        with pytest.raises(Q.QualificationError, match="conflicting duplicate"):
            Q.effective_current_reviews(values, HEAD)
    alias = copy.deepcopy(first)
    alias["user"]["id"] = float(first["user"]["id"])
    with pytest.raises(Q.QualificationError, match="conflicting duplicate"):
        Q.effective_current_reviews([first, alias], HEAD)
