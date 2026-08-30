"""Regression contract for shellcheck selection in static-review workflows."""
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github/workflows/static-review.yml",
    ROOT / ".github/workflows/static-review-reusable.yml",
)


def test_shellcheck_excludes_deleted_paths_in_both_workflows():
    """A deleted ``.sh`` path cannot be passed to shellcheck in the checkout."""
    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        selection = next(
            line for line in text.splitlines()
            if "CHANGED_SH=$(git diff" in line
        )
        assert "--diff-filter=ACMR" in selection, workflow
        assert "-- '*.sh'" in selection, workflow
        assert "grep" not in selection, workflow


def test_delete_only_shell_diff_is_successful_and_skips_shellcheck(tmp_path):
    """The workflow selection remains successful when only a shell file is deleted."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    script = tmp_path / "deleted.sh"
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    subprocess.run(["git", "add", "deleted.sh"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    script.unlink()
    subprocess.run(["git", "add", "-u"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "delete"], cwd=tmp_path, check=True)

    marker = tmp_path / "shellcheck-called"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "shellcheck"
    fake.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
    fake.chmod(0o755)
    command = (
        f"CHANGED_SH=$(git diff --name-only --diff-filter=ACMR {base}...HEAD -- '*.sh'); "
        'if [ -n "$CHANGED_SH" ]; then shellcheck $CHANGED_SH; fi'
    )
    result = subprocess.run(
        ["bash", "-e", "-c", command],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        check=False,
    )
    assert result.returncode == 0
    assert not marker.exists()
