"""Expression-context contract for the reusable secret-scan workflow."""
from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github/workflows/secret-scan-reusable.yml"
)


def test_optional_license_is_promoted_before_step_conditions():
    """Step-level conditions must use env because GitHub forbids secrets there."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "GITLEAKS_LICENSE: ${{ secrets.GITLEAKS_LICENSE }}" in text
    assert "if: ${{ secrets.GITLEAKS_LICENSE" not in text
    assert text.count("if: ${{ env.GITLEAKS_LICENSE") == 3


def test_oss_install_runs_on_ubuntu_slim_without_sudo():
    """ubuntu-slim is root and has no sudo binary. A bare sudo install fails the required Detect Secrets job."""
    text = WORKFLOW.read_text(encoding="utf-8")
    install = text.split("Install gitleaks (OSS binary fallback)", 1)[1].split("Run Gitleaks (OSS binary)", 1)[0]
    assert 'if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi' in install
    assert "$SUDO install -m 0755 gitleaks /usr/local/bin/gitleaks" in install
    assert "sudo install gitleaks /usr/local/bin/gitleaks" not in install
