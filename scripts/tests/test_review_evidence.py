"""Adversarial review-publication contracts, including empty acknowledgement traps."""
import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("review_evidence", ROOT / "scripts/ci/review_evidence.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
SHA = "a" * 40
REPO = "Example/framework"
BODY = (module.PREFIX + "\n<!-- recent_review_start -->\n"
        "No actionable comments were generated in the recent review.\n"
        "<!-- recent_review_end -->\n<!-- walkthrough_start -->\nReviewed implementation.\n"
        "<!-- walkthrough_end -->\n<!-- final_review_risk_coverage:"
        '{"sourceCommitId":"' + SHA + '","coveredCommitId":"' + SHA + '","kind":"reviewed"} -->')
COMMENT = {"id": 42, "updated_at": "2026-10-17T16:13:40Z",
           "issue_url": f"https://api.github.com/repos/{REPO}/issues/201",
           "user": {"login": "coderabbitai[bot]", "type": "Bot"}, "body": BODY}


def verdict(comments):
    return module.coderabbit_clean_summary(comments, SHA, REPO, 201)


def test_actual_clean_summary_requires_no_synthetic_approval():
    assert verdict([COMMENT])
    assert module.bot_review_verdict([], SHA) == "none"
    assert module.bot_review_verdict([{"commit_id": SHA, "user": COMMENT["user"],
                                       "state": "COMMENTED", "body": ""}], SHA) == "none"


@pytest.mark.parametrize("body", [
    "", "Thanks, addressed.", BODY.replace(SHA, "b" * 40), BODY.replace(SHA, SHA[:7]),
    BODY.replace('"kind":"reviewed"', '"kind":"skipped"'),
    BODY.replace('"kind":"reviewed"', '"kind":"skipped","kind":"reviewed"'),
    BODY.replace("No actionable comments", "One actionable comment"),
    BODY.replace("No actionable", "Review skipped. No actionable"),
    BODY.replace("No actionable", "Review in progress. No actionable"),
    BODY.replace("No actionable", "Review limit reached. No actionable"),
    BODY.replace("No actionable", "Rate limit exceeded. No actionable"),
    BODY + BODY, BODY.replace("recent_review_end", "absent"),
    BODY.replace("<!-- walkthrough_end -->", ""),
    BODY.replace("<!-- walkthrough_end -->", "<!-- walkthrough_end -->\n<!-- walkthrough_end -->"),
    BODY.replace("<!-- final_review_risk_coverage:", "> <!-- final_review_risk_coverage:"),
    BODY.replace("<!-- final_review_risk_coverage:", "```json\n<!-- final_review_risk_coverage:") + "\n```",
    BODY.replace("<!-- walkthrough_end -->\n", "") + "\n<!-- walkthrough_end -->",
])
def test_unreviewed_stale_incomplete_or_ambiguous_bodies_fail_closed(body):
    comment = copy.deepcopy(COMMENT)
    comment["body"] = body
    assert not verdict([comment])


@pytest.mark.parametrize("patch", [
    {"user": {"login": "human", "type": "User"}},
    {"user": {"login": "coderabbitai[bot]", "type": "User"}},
    {"user": {"login": "github-actions[bot]", "type": "Bot"}},
    {"issue_url": "https://api.github.com/repos/Other/repo/issues/201"},
    {"issue_url": f"https://api.github.com/repos/{REPO}/issues/202"},
    {"issue_url": None}, {"id": True}, {"id": None}, {"updated_at": "broken"},
    {"updated_at": "2026-10-17T16:13:40"}, {"body": None},
])
def test_identity_binding_and_provenance_are_required(patch):
    comment = dict(COMMENT, **patch)
    assert not verdict([comment])


@pytest.mark.parametrize("new_body", [
    "<!-- This is an auto-generated comment: review in progress by coderabbit.ai -->",
    "<!-- This is an auto-generated comment: rate limit exceeded by coderabbit.ai -->",
    BODY.replace(SHA, "b" * 40), BODY.replace("No actionable", "Some actionable"),
])
def test_later_attempt_does_not_reuse_earlier_clean_review(new_body):
    later = dict(COMMENT, id=43, updated_at="2026-10-17T16:14:00Z", body=new_body)
    assert not verdict([COMMENT, later])
    assert not verdict([later, COMMENT])


def test_unrelated_acknowledgement_does_not_erase_actual_review():
    reply = dict(COMMENT, id=44, updated_at="2026-10-17T16:15:00Z", body="Thanks for fixing it.")
    assert verdict([COMMENT, reply])


def test_conflicting_snapshots_of_same_comment_fail_closed():
    assert not verdict([COMMENT, dict(COMMENT, body=BODY.replace("No actionable", "Some actionable"))])
    assert verdict([COMMENT, COMMENT])


def test_pr_text_inside_walkthrough_cannot_override_generated_result():
    comment = dict(COMMENT, body=BODY.replace("Reviewed implementation.",
                   "```md\n" + BODY + "\n```\n> <!-- final_review_risk_coverage:{} -->"))
    assert verdict([comment])
    assert not verdict([dict(comment, body=comment["body"].replace('"kind":"reviewed"', '"kind":"skipped"'))])


def test_bot_approval_and_blocking_review_are_distinct_from_comments():
    approval = {"commit_id": SHA, "user": COMMENT["user"], "state": "APPROVED"}
    assert module.bot_review_verdict([approval], SHA) == "satisfy"
    assert module.bot_review_verdict([dict(approval, state="COMMENTED", body="Thanks")], SHA) == "none"
    assert module.bot_review_verdict([dict(approval, commit_id="b" * 40)], SHA) == "none"
    assert module.bot_review_verdict([dict(approval, user={"login": "human", "type": "User"})], SHA) == "none"
    assert module.bot_review_verdict([approval, dict(approval, state="CHANGES_REQUESTED")], SHA) == "block"


@pytest.mark.parametrize("data", [None, {}, [None], [{"user": "bad"}], "bad"])
def test_malformed_input_never_approves(data):
    assert not verdict(data)
    assert module.bot_review_verdict(data, SHA) == "none"


@pytest.mark.parametrize("comments,expected", [([COMMENT], "satisfy"), ([], "none"),
    ([dict(COMMENT, issue_url="https://api.github.com/repos/Other/repo/issues/201")], "none"),
    ([dict(COMMENT, body="Thanks, addressed.")], "none")])
def test_real_cli_preserves_exact_context(comments, expected):
    import json
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/ci/review_evidence.py"), "--comments",
         "--repo", REPO, "--pr", "201", "--head-sha", SHA],
        input=json.dumps(comments), capture_output=True, text=True, check=True, timeout=10,
    )
    assert result.stdout.strip() == expected
