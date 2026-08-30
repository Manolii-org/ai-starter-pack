"""Regression contract for shellcheck selection in static-review workflows."""
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
