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
