"""Mocked wire-level and adversarial tests for the deployment gate."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "actions/require-successful-deployment"
SPEC = importlib.util.spec_from_file_location("deployment_verifier", ACTION / "verify.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)

REPOSITORY = "example/service"
SHA = "a" * 40
TOKEN = "test-only-placeholder"
RUN_URL = "https://api.github.com/repos/example/service/actions/runs/42"
HTML_URL = "https://github.com/example/service/actions/runs/42"


def run():
    return {
        "id": 42, "run_attempt": 2, "workflow_id": 7, "name": "Deployment",
        "path": ".github/workflows/deploy.yml", "head_sha": SHA,
        "head_branch": "staging", "event": "push", "status": "completed",
        "conclusion": "success", "url": RUN_URL, "html_url": HTML_URL,
        "repository": {"id": 8, "full_name": REPOSITORY},
        "head_repository": {"id": 8, "full_name": REPOSITORY},
    }


def job(name="Deploy", job_id=100):
    return {
        "id": job_id, "run_id": 42, "run_attempt": 2, "run_url": RUN_URL,
        "head_sha": SHA, "head_branch": "staging", "workflow_name": "Deployment",
        "name": name, "status": "completed", "conclusion": "success",
    }


def run_page(value=None):
    return {"total_count": 5, "workflow_runs": [run() if value is None else value]}


def job_page(values=None):
    jobs = [job(), job("Verify deployment", 101)] if values is None else values
    return {"total_count": len(jobs), "jobs": jobs}


@pytest.fixture
def wire(monkeypatch):
    connections = []

    def install(payloads, status=200):
        factory = MagicMock()
        for payload in payloads:
            response = MagicMock(status=status)
            response.read.return_value = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            connection = MagicMock()
            connection.getresponse.return_value = response
            connections.append(connection)
        factory.side_effect = connections
        monkeypatch.setattr(verifier.http.client, "HTTPSConnection", factory)
        return factory, connections

    return install


def verify(**overrides):
    inputs = {"repository": REPOSITORY, "workflow": "deploy.yml", "branch": "staging",
              "sha": SHA, "required_jobs": '["Deploy", "Verify deployment"]', "token": TOKEN}
    inputs.update(overrides)
    return verifier.verify(**inputs)


def test_success_current_attempt_and_only_fixed_unfiltered_endpoints(wire):
    factory, connections = wire([run_page(), job_page(), run_page()])
    assert verify() == {"run_id": "42", "html_url": HTML_URL}
    assert factory.call_count == 3
    for call in factory.call_args_list:
        assert call.args == ("api.github.com",)
        assert call.kwargs == {"timeout": 15}
    paths = [connection.request.call_args.args[1] for connection in connections]
    assert paths == [
        "/repos/example/service/actions/workflows/deploy.yml/runs?branch=staging&per_page=1",
        "/repos/example/service/actions/runs/42/attempts/2/jobs?per_page=100",
        "/repos/example/service/actions/workflows/deploy.yml/runs?branch=staging&per_page=1",
    ]
    for connection in connections:
        request = connection.request.call_args
        assert request.args[0] == "GET"
        assert request.kwargs["headers"] == {
            "Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "require-successful-deployment",
        }
        connection.getresponse.return_value.read.assert_called_once_with(verifier.MAX_RESPONSE_BYTES + 1)
        connection.close.assert_called_once()


@pytest.mark.parametrize("branch", ["staging", "main", "release/1.2"])
@pytest.mark.parametrize("qualified", [False, True])
def test_documented_workflow_paths_match_the_exact_branch(wire, branch, qualified):
    value = run()
    value["head_branch"] = branch
    if qualified:
        value["path"] += f"@{branch}"
    jobs = job_page()
    for item in jobs["jobs"]:
        item["head_branch"] = branch
    wire([run_page(value), jobs, run_page(value)])
    assert verify(branch=branch) == {"run_id": "42", "html_url": HTML_URL}


def test_branch_query_cannot_inject_filters(wire):
    value = run()
    value["head_branch"] = "release/ready&event=push#fragment"
    jobs = job_page()
    for item in jobs["jobs"]:
        item["head_branch"] = value["head_branch"]
    _, connections = wire([run_page(value), jobs, run_page(value)])
    verify(branch=value["head_branch"])
    url = urlsplit(connections[0].request.call_args.args[1])
    assert parse_qs(url.query) == {"branch": [value["head_branch"]], "per_page": ["1"]}
    assert not url.fragment


@pytest.mark.parametrize("field,value", [
    ("repository", "../service"), ("repository", "owner/service/extra"),
    ("repository", "owner/.."), ("repository", "owner/repo?x=1"),
    ("repository", "owner/repo\n"), ("repository", "https://evil.test/repo"),
    ("repository", "-owner/repo"), ("repository", "owner/"),
    ("repository", "owner--name/repo"), ("repository", "a" * 40 + "/repo"),
    ("repository", "owner/" + "a" * 101),
    ("workflow", "../deploy.yml"), ("workflow", ".github/workflows/deploy.yml"),
    ("workflow", "123"), ("workflow", "deploy.yml?event=push"),
    ("workflow", "deploy.yml\n"), ("workflow", "deploy.json"),
    ("workflow", "a" * 256 + ".yml"),
    ("sha", "a" * 39), ("sha", "a" * 41), ("sha", "A" * 40),
    ("sha", "g" * 40), ("sha", SHA + "\n"),
    ("branch", ""), ("branch", "staging\n"), ("branch", " staging"),
    ("branch", "refs/heads/../staging"), ("branch", "feature//x"),
    ("branch", "feature/x.lock"), ("branch", "feature/.hidden"),
    ("branch", "feature/"), ("branch", "/feature"), ("branch", "staging."),
    ("branch", "feature@{1}"), ("branch", "feature\\x"), ("branch", "@"),
    ("branch", "feature?x"), ("branch", "feature x"), ("branch", "feature\x7fx"),
    ("branch", "feature*x"), ("branch", "feature[x"), ("branch", "feature^x"),
    ("branch", "a" * 256),
    ("required_jobs", "not json"), ("required_jobs", '"Deploy"'),
    ("required_jobs", "{}"), ("required_jobs", "[]"),
    ("required_jobs", '["Deploy", "Deploy"]'), ("required_jobs", '["Deploy", 1]'),
    ("required_jobs", '[null]'), ("required_jobs", '[true]'),
    ("required_jobs", '[""]'), ("required_jobs", '[" Deploy"]'),
    ("required_jobs", json.dumps(["x" * 513])),
    ("required_jobs", '["Deploy\\noutput=bad"]'),
    ("required_jobs", json.dumps([str(i) for i in range(101)])),
    ("required_jobs", " " * 65537), ("required_jobs", "[" * 2000),
    ("token", ""), ("token", "test\r\nInjected: bad"),
])
def test_invalid_inputs_never_request_network(monkeypatch, field, value):
    connection = MagicMock()
    monkeypatch.setattr(verifier.http.client, "HTTPSConnection", connection)
    with pytest.raises(verifier.VerificationError):
        verify(**{field: value})
    connection.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("id", True), ("id", "42"), ("id", 0),
    ("run_attempt", None), ("run_attempt", True), ("run_attempt", 0),
    ("workflow_id", "7"), ("workflow_id", 0),
    ("path", ".github/workflows/other.yml"), ("path", None), ("path", []),
    ("path", ".github/workflows/deploy.yml@main"),
    ("path", ".github/workflows/other.yml@staging"),
    ("path", ".github/workflows/deploy.yml@refs/heads/staging"),
    ("path", ".github/workflows/deploy.yml@" + SHA),
    ("path", ".github/workflows/deploy.yml@staging@main"),
    ("path", ".github/workflows/deploy.yml@staging\n"),
    ("head_branch", "main"), ("head_sha", "b" * 40),
    ("event", "workflow_dispatch"), ("event", "pull_request"),
    ("event", "schedule"), ("status", "in_progress"), ("status", "queued"),
    ("conclusion", "failure"), ("conclusion", "skipped"),
    ("conclusion", "cancelled"), ("conclusion", None),
    ("repository", {"id": 8, "full_name": "other/service"}),
    ("head_repository", {"id": 9, "full_name": REPOSITORY}),
    ("head_repository", {"id": 8, "full_name": "other/service"}),
    ("repository", []), ("head_repository", None),
    ("url", "https://evil.test/runs/42"), ("url", RUN_URL + "?extra=1"),
    ("html_url", HTML_URL + "\nrun_id=99"), ("html_url", "http://github.com/example/service/actions/runs/42"),
    ("name", []),
])
def test_latest_run_rejections_never_search_for_older_success(wire, field, value):
    latest = run()
    latest[field] = value
    _, connections = wire([run_page(latest), run_page()])
    with pytest.raises(verifier.VerificationError):
        verify()
    connections[0].request.assert_called_once()
    connections[1].request.assert_not_called()


@pytest.mark.parametrize("payload", [
    {}, {"total_count": 0, "workflow_runs": []},
    {"total_count": True, "workflow_runs": [run()]},
    {"total_count": "1", "workflow_runs": [run()]},
    {"total_count": 2, "workflow_runs": [run(), run()]},
    {"total_count": 1, "workflow_runs": [None]},
    {"total_count": 1, "workflow_runs": {}},
])
def test_malformed_or_empty_runs(wire, payload):
    wire([payload])
    with pytest.raises(verifier.VerificationError):
        verify()


@pytest.mark.parametrize("field,value", [
    ("id", True), ("id", 0), ("run_id", 41), ("run_id", "42"),
    ("run_attempt", 1), ("run_attempt", "2"), ("run_attempt", None),
    ("run_attempt", True), ("run_attempt", 0), ("run_attempt", 2.0),
    ("head_sha", "b" * 40),
    ("head_branch", "main"), ("run_url", "https://evil.test/run"),
    ("workflow_name", "Another workflow"), ("name", "deploy"),
    ("name", "Deploy (region)"), ("name", "Deploy\n"),
    ("status", "in_progress"), ("status", "queued"),
    ("conclusion", "skipped"), ("conclusion", "failure"),
    ("conclusion", "neutral"), ("conclusion", "cancelled"),
    ("conclusion", "timed_out"), ("conclusion", "action_required"),
    ("conclusion", None),
])
def test_required_job_adversaries(wire, field, value):
    jobs = job_page()
    jobs["jobs"][0][field] = value
    wire([run_page(), jobs])
    with pytest.raises(verifier.VerificationError):
        verify()


@pytest.mark.parametrize("field", [
    "id", "run_id", "head_sha", "head_branch", "run_url", "workflow_name",
    "name", "status", "conclusion",
])
def test_missing_job_metadata(wire, field):
    jobs = job_page()
    del jobs["jobs"][0][field]
    wire([run_page(), jobs])
    with pytest.raises(verifier.VerificationError):
        verify()


@pytest.mark.parametrize("jobs", [
    job_page([]), job_page([job()]), job_page([job(), job("Deploy", 102), job("Verify deployment", 101)]),
    job_page([job(), job("Verify deployment", 100)]),
    {"total_count": 101, "jobs": [job()]},
    {"total_count": 2, "jobs": [job()]}, {"total_count": 1, "jobs": [job(), job()]},
    {"total_count": True, "jobs": [job()]}, {"total_count": "1", "jobs": [job()]},
    {"total_count": 1, "jobs": [None]}, {"total_count": 1, "jobs": {}}, {},
])
def test_incomplete_duplicate_or_oversized_jobs(wire, jobs):
    wire([run_page(), jobs])
    with pytest.raises(verifier.VerificationError):
        verify()


@pytest.mark.parametrize("omit_from", ["Deploy", "Verify deployment", None])
def test_attempt_scoped_jobs_allow_absent_optional_attempt_metadata(wire, omit_from):
    jobs = job_page()
    for item in jobs["jobs"]:
        if omit_from is None or item["name"] == omit_from:
            del item["run_attempt"]
    wire([run_page(), jobs, run_page()])
    assert verify() == {"run_id": "42", "html_url": HTML_URL}


def test_metadata_of_unrequired_jobs_is_also_checked(wire):
    jobs = job_page()
    other = job("Optional", 102)
    other["run_attempt"] = 1
    jobs["jobs"].append(other)
    jobs["total_count"] = 3
    wire([run_page(), jobs])
    with pytest.raises(verifier.VerificationError):
        verify()


def test_exactly_100_jobs_matrix_names_and_optional_skip_are_allowed(wire):
    values = [job(f"Deploy ({i})", 100 + i) for i in range(99)] + [job("Optional", 199)]
    values[-1]["conclusion"] = "skipped"
    wire([run_page(), job_page(values), run_page()])
    assert verify(required_jobs='["Deploy (0)", "Deploy (98)"]')["run_id"] == "42"


def test_all_100_jobs_can_be_required(wire):
    values = [job(f"Deploy ({i})", 100 + i) for i in range(100)]
    wire([run_page(), job_page(values), run_page()])
    assert verify(required_jobs=json.dumps([item["name"] for item in values]))["run_id"] == "42"


def test_yaml_filename_and_first_attempt_are_supported(wire):
    value, jobs = run(), job_page()
    value["path"] = ".github/workflows/deploy.yaml"
    value["run_attempt"] = 1
    for item in jobs["jobs"]:
        item["run_attempt"] = 1
    _, connections = wire([run_page(value), jobs, run_page(value)])
    verify(workflow="deploy.yaml")
    assert "/attempts/1/jobs?" in connections[1].request.call_args.args[1]


@pytest.mark.parametrize("field,value", [
    ("id", 43), ("run_attempt", 3), ("workflow_id", 9),
    ("repository", {"id": 99, "full_name": REPOSITORY}),
    ("head_sha", "b" * 40), ("status", "in_progress"),
    ("conclusion", "failure"), ("name", "Renamed deployment"),
])
def test_superseding_run_rerun_or_metadata_change_blocks_outputs(wire, field, value):
    final = run()
    final[field] = value
    if field == "id":
        final["url"] = RUN_URL.replace("42", "43")
        final["html_url"] = HTML_URL.replace("42", "43")
    if field == "repository":
        final["head_repository"] = deepcopy(value)
    wire([run_page(), job_page(), run_page(final)])
    with pytest.raises(verifier.VerificationError):
        verify()


@pytest.mark.parametrize("status", [301, 302, 401, 403, 404, 429, 500])
def test_http_errors_and_redirects_fail_closed_without_following(wire, status):
    factory, connections = wire([b"private response body"], status=status)
    with pytest.raises(verifier.VerificationError) as error:
        verify()
    assert "private response" not in str(error.value)
    assert factory.call_count == 1
    connections[0].getresponse.return_value.read.assert_not_called()
    connections[0].close.assert_called_once()


@pytest.mark.parametrize("body", [
    b"not JSON", b"null", b"[]", b"\xff", b'{"total_count": 0, "total_count": 1}',
    b'{"repository": {"id": 1, "id": 2}}', b"[" * 2000,
    b"x" * (verifier.MAX_RESPONSE_BYTES + 1),
])
def test_invalid_or_oversized_json(wire, body):
    wire([body])
    with pytest.raises(verifier.VerificationError):
        verify()


@pytest.mark.parametrize("error", [TimeoutError("private detail"), OSError("private detail"),
                                      verifier.http.client.HTTPException("private detail")])
def test_network_errors_are_sanitized_and_connection_closed(wire, error):
    _, connections = wire([run_page()])
    connections[0].getresponse.side_effect = error
    with pytest.raises(verifier.VerificationError) as caught:
        verify()
    assert "private detail" not in str(caught.value)
    connections[0].close.assert_called_once()


def configure_environment(monkeypatch, output):
    for key, value in {
        "DEPLOYMENT_REPOSITORY": REPOSITORY, "INPUT_WORKFLOW": "deploy.yml",
        "INPUT_BRANCH": "staging", "INPUT_SHA": SHA,
        "INPUT_REQUIRED_JOBS": '["Deploy", "Verify deployment"]',
        "DEPLOYMENT_TOKEN": TOKEN, "GITHUB_OUTPUT": str(output),
        "GITHUB_API_URL": "https://evil.test", "GH_TOKEN": "unused-placeholder",
    }.items():
        monkeypatch.setenv(key, value)


def test_main_emits_only_validated_outputs(wire, monkeypatch, tmp_path, capsys):
    output = tmp_path / "outputs"
    configure_environment(monkeypatch, output)
    wire([run_page(), job_page(), run_page()])
    assert verifier.main() == 0
    assert output.read_text() == f"run_id=42\nhtml_url={HTML_URL}\n"
    assert capsys.readouterr() == ("", "")


def test_main_failure_emits_no_outputs_or_sensitive_values(wire, monkeypatch, tmp_path, capsys):
    output = tmp_path / "outputs"
    output.write_text("existing=value\n")
    configure_environment(monkeypatch, output)
    final = run()
    final["run_attempt"] = 3
    wire([run_page(), job_page(), run_page(final)])
    assert verifier.main() == 1
    assert output.read_text() == "existing=value\n"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert TOKEN not in captured.err and REPOSITORY not in captured.err
    assert "changed during verification" in captured.err


def test_no_ambient_token_fallback(wire, monkeypatch, tmp_path, capsys):
    configure_environment(monkeypatch, tmp_path / "outputs")
    monkeypatch.delenv("DEPLOYMENT_TOKEN")
    factory, _ = wire([])
    assert verifier.main() == 1
    factory.assert_not_called()
    assert "github.token" in capsys.readouterr().err


def test_missing_output_file_rejected_before_network(wire, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    factory, _ = wire([])
    assert verifier.main() == 1
    factory.assert_not_called()
    assert "Missing GitHub output file" in capsys.readouterr().err


def test_output_write_error_is_sanitized(wire, monkeypatch, tmp_path, capsys):
    configure_environment(monkeypatch, tmp_path)
    wire([run_page(), job_page(), run_page()])
    assert verifier.main() == 1
    assert str(tmp_path) not in capsys.readouterr().err


def test_action_wiring_and_least_privilege_documentation():
    action = yaml.safe_load((ACTION / "action.yml").read_text())
    assert set(action["inputs"]) == {"workflow", "branch", "sha", "required_jobs"}
    assert all(item["required"] is True for item in action["inputs"].values())
    assert set(action["outputs"]) == {"run_id", "html_url"}
    assert action["runs"]["using"] == "composite"
    step, = action["runs"]["steps"]
    assert step["env"] == {
        "DEPLOYMENT_REPOSITORY": "${{ github.repository }}",
        "DEPLOYMENT_TOKEN": "${{ github.token }}",
        "INPUT_WORKFLOW": "${{ inputs.workflow }}", "INPUT_BRANCH": "${{ inputs.branch }}",
        "INPUT_SHA": "${{ inputs.sha }}", "INPUT_REQUIRED_JOBS": "${{ inputs.required_jobs }}",
    }
    assert step["run"] == 'timeout --kill-after=5s 90s python3 "$GITHUB_ACTION_PATH/verify.py"'
    assert "${{" not in step["run"]
    for name in action["outputs"]:
        assert action["outputs"][name]["value"] == "${{ steps.verify.outputs." + name + " }}"
    example = (ACTION / "README.md").read_text().split("```yaml\n", 1)[1].split("```", 1)[0]
    assert yaml.safe_load(example)["jobs"]["gate"]["permissions"] == {"actions": "read"}
