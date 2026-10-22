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
COMMENT = {"id": 42, "node_id": "IC_42", "created_at": "2026-10-17T16:00:00Z", "updated_at": "2026-10-17T16:13:40Z",
           "issue_url": f"https://api.github.com/repos/{REPO}/issues/201",
           "user": {"login": "coderabbitai[bot]", "type": "Bot"}, "body": BODY}


def with_provenance(comment):
    c = copy.deepcopy(comment)
    actor = {"login": (c.get("user") or {}).get("login", "").removesuffix("[bot]"),
             "__typename": (c.get("user") or {}).get("type")}
    c["review_provenance"] = {"id": c.get("node_id"), "databaseId": c.get("id"),
        "author": actor, "editor": actor, "body": c.get("body"),
        "createdAt": c.get("created_at"), "updatedAt": c.get("updated_at"),
        "lastEditedAt": c.get("updated_at"), "includesCreatedEdit": True,
        "userContentEdits": {"totalCount": 2, "pageInfo": {"hasNextPage": False, "endCursor": "last"},
            "nodes": [{"id": "created", "editedAt": c.get("created_at"), "editor": actor, "deletedAt": None},
                      {"id": "edited", "editedAt": c.get("updated_at"), "editor": actor, "deletedAt": None}]}}
    return c


COMMENT = with_provenance(COMMENT)


def verdict(comments):
    # Semantic parser cases model fresh authenticated snapshots of each body.
    # Provenance attack cases below deliberately bypass this fixture helper.
    return module.coderabbit_clean_summary([with_provenance(c) if isinstance(c, dict) and isinstance(c.get("user"), dict) else c
                                            for c in comments] if isinstance(comments, list) else comments,
                                           SHA, REPO, 201)


def test_actual_clean_summary_requires_no_synthetic_approval():
    assert verdict([COMMENT])
    assert module.bot_review_verdict([], SHA) == "none"
    assert module.bot_review_verdict([{"commit_id": SHA, "user": COMMENT["user"],
                                       "state": "COMMENTED", "body": ""}], SHA) == "none"


def test_generated_header_preserves_raw_body_provenance():
    wrapped = with_provenance(dict(COMMENT, body=module.GENERATED_HEADER + BODY))
    assert module.coderabbit_clean_summary([wrapped], SHA, REPO, 201)
    wrapped["review_provenance"]["body"] = BODY
    assert not module.coderabbit_clean_summary([wrapped], SHA, REPO, 201)


@pytest.mark.parametrize("body", [
    module.GENERATED_HEADER + BODY.replace(SHA, "b" * 40),
    module.GENERATED_HEADER + BODY.replace("No actionable", "Review in progress. No actionable"),
    module.GENERATED_HEADER + BODY.replace("No actionable", "Rate limit exceeded. No actionable"),
    module.GENERATED_HEADER + module.GENERATED_HEADER + BODY,
    module.GENERATED_HEADER.replace(":v1", ":v2") + BODY,
    module.GENERATED_HEADER + "\n" + BODY,
    "\n" + module.GENERATED_HEADER + BODY.replace("No actionable", "Review in progress. No actionable"),
    " " + module.GENERATED_HEADER + BODY.replace("No actionable", "Review in progress. No actionable"),
    "\n" + BODY.replace("No actionable", "Review in progress. No actionable"),
])
def test_new_generated_attempt_never_reuses_an_older_clean_summary(body):
    newer = with_provenance(dict(COMMENT, id=43, node_id="IC_43",
        updated_at="2026-10-17T16:14:00Z", body=body))
    assert not module.coderabbit_clean_summary([COMMENT, newer], SHA, REPO, 201)


def test_generated_reply_remains_an_authenticated_acknowledgement():
    reply = with_provenance(dict(COMMENT, id=43, node_id="IC_43",
        updated_at="2026-10-17T16:14:00Z",
        body=module.GENERATED_HEADER + module.REPLY_PREFIX + "\nReview finished."))
    assert module.coderabbit_clean_summary([COMMENT, reply], SHA, REPO, 201)
    reply["review_provenance"]["userContentEdits"]["nodes"][0]["editor"] = {
        "login": "writer", "__typename": "User"}
    assert not module.coderabbit_clean_summary([COMMENT, reply], SHA, REPO, 201)


@pytest.mark.parametrize("leading", ["\n", " ", "\t"])
def test_fetcher_does_not_fall_back_past_a_malformed_newer_generated_attempt(monkeypatch, leading):
    import json
    newer = with_provenance(dict(COMMENT, id=43, node_id="IC_43",
        updated_at="2026-10-17T16:14:00Z",
        body=leading + module.GENERATED_HEADER + BODY.replace(
            "No actionable", "Review in progress. No actionable")))
    requested = []
    def gh(arguments, payload=None):
        requested.append(arguments[0])
        assert arguments[0] != "graphql", "the newer incomplete attempt must stop fetch planning"
        return json.dumps([COMMENT, newer])
    monkeypatch.setattr(module, "gh_read", gh)
    result = module.fetch_comments(REPO, 201, SHA)
    assert len(requested) == 1
    assert not module.coderabbit_clean_summary(result, SHA, REPO, 201)


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


@pytest.mark.parametrize("change", [
    lambda c: c.pop("review_provenance"),
    lambda c: c["review_provenance"].update(id="IC_other"),
    lambda c: c["review_provenance"].update(databaseId=43),
    lambda c: c["review_provenance"].update(body="edited body"),
    lambda c: c["review_provenance"].update(updatedAt="2026-10-18T16:13:40Z"),
    lambda c: c["review_provenance"].update(editor={"login": "writer", "__typename": "User"}),
    lambda c: c["review_provenance"].update(includesCreatedEdit=False),
    lambda c: c["review_provenance"]["userContentEdits"].update(totalCount=3),
    lambda c: c["review_provenance"]["userContentEdits"].update(totalCount=True),
    lambda c: c["review_provenance"]["userContentEdits"]["pageInfo"].update(hasNextPage=True),
    lambda c: c["review_provenance"]["userContentEdits"]["nodes"][0].update(editor=None),
    lambda c: c["review_provenance"]["userContentEdits"]["nodes"][0].update(deletedAt="2026-10-17T16:02:00Z"),
    lambda c: c["review_provenance"]["userContentEdits"]["nodes"][1].update(id="created"),
    lambda c: c["review_provenance"]["userContentEdits"]["nodes"][0].update(editedAt="2026-10-17T16:05:00Z"),
    # The latest editor is still the bot; an earlier human edit must veto.
    lambda c: c["review_provenance"]["userContentEdits"]["nodes"][0].update(editor={"login": "writer", "__typename": "User"}),
])
def test_mutable_author_identity_is_not_edit_provenance(change):
    c = copy.deepcopy(COMMENT)
    change(c)
    assert not module.coderabbit_clean_summary([c], SHA, REPO, 201)


def test_unedited_bot_body_has_no_missing_edit_history():
    c = copy.deepcopy(COMMENT)
    c["updated_at"] = c["created_at"]
    c["review_provenance"].update(updatedAt=c["created_at"], lastEditedAt=None,
        editor=None, includesCreatedEdit=False,
        userContentEdits={"totalCount": 0, "nodes": [], "pageInfo": {"hasNextPage": False}})
    assert module.trusted_comment(c)
    assert module.coderabbit_clean_summary([c], SHA, REPO, 201)


def test_later_untrusted_snapshot_cannot_reuse_an_older_clean_result():
    later = dict(COMMENT, id=43, updated_at="2026-10-17T16:14:00Z")
    assert not module.coderabbit_clean_summary([COMMENT, later], SHA, REPO, 201)


@pytest.mark.parametrize("body", ["Thanks, addressed.", "", "unrelated acknowledgement"])
def test_removed_prefix_cannot_hide_a_later_tainted_review(body):
    later = with_provenance(dict(COMMENT, id=43, node_id="IC_43",
        updated_at="2026-10-17T16:14:00Z", body=body))
    later["review_provenance"]["userContentEdits"]["nodes"][0]["editor"] = {
        "login": "writer", "__typename": "User"}
    assert not module.trusted_comment(later)
    assert not module.coderabbit_clean_summary([COMMENT, later], SHA, REPO, 201)
    assert not module.coderabbit_clean_summary([later, COMMENT], SHA, REPO, 201)


def test_older_unused_history_does_not_block_a_current_authenticated_review():
    older = dict(COMMENT, id=41, node_id="IC_41", updated_at="2026-10-17T16:10:00Z")
    older.pop("review_provenance")
    assert module.coderabbit_clean_summary([older, COMMENT], SHA, REPO, 201)


@pytest.mark.parametrize("header", ["", module.GENERATED_HEADER])
def test_fetcher_authenticates_newer_acknowledgements_but_not_unused_old_history(monkeypatch, header):
    import json
    current = with_provenance(dict(COMMENT, body=header + BODY))
    newer = with_provenance(dict(COMMENT, id=43, node_id="IC_43",
        updated_at="2026-10-17T16:14:00Z", body=header + module.REPLY_PREFIX + "\nThanks, addressed."))
    older = with_provenance(dict(COMMENT, id=41, node_id="IC_41",
        updated_at="2026-10-17T16:10:00Z"))
    snapshots = {c["node_id"]: copy.deepcopy(c["review_provenance"]) for c in (current, newer)}
    requested = []
    def gh(arguments, payload=None):
        if arguments[0] != "graphql":
            return json.dumps([older, current, newer])
        ident = json.loads(payload)["variables"]["id"]
        requested.append(ident)
        assert ident != "IC_41", "unused history must not add calls or block this review"
        return json.dumps({"data": {"node": snapshots[ident]}})
    monkeypatch.setattr(module, "gh_read", gh)
    result = module.fetch_comments(REPO, 201, SHA)
    assert requested == ["IC_43", "IC_42"]
    assert result[1]["body"] == header + BODY
    assert module.coderabbit_clean_summary(result, SHA, REPO, 201)


def test_fetcher_collects_all_comment_and_edit_pages(monkeypatch):
    import json
    calls = []
    def gh(arguments, payload=None):
        calls.append((arguments, payload))
        if arguments[0] != "graphql":
            assert "--paginate" in arguments
            return json.dumps([COMMENT]) + "\n[]"
        variables = json.loads(payload)["variables"]
        n = copy.deepcopy(COMMENT["review_provenance"])
        h = n["userContentEdits"]
        if variables["cursor"] is None:
            h["nodes"] = h["nodes"][:1]
            h["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
        else:
            assert variables["cursor"] == "next"
            h["nodes"] = h["nodes"][1:]
        return json.dumps({"data": {"node": n}})
    monkeypatch.setattr(module, "gh_read", gh)
    result = module.fetch_comments(REPO, 201, SHA)
    assert len(calls) == 3
    assert module.coderabbit_clean_summary(result, SHA, REPO, 201)


@pytest.mark.parametrize("failure", ["changed", "errors", "repeated_cursor"])
def test_fetcher_never_combines_incomplete_or_racing_history(monkeypatch, failure):
    import json
    count = 0
    def gh(arguments, payload=None):
        nonlocal count
        if arguments[0] != "graphql":
            return json.dumps([COMMENT])
        count += 1
        if failure == "errors":
            return json.dumps({"errors": [{"message": "unavailable"}]})
        n = copy.deepcopy(COMMENT["review_provenance"])
        n["userContentEdits"]["pageInfo"] = {"hasNextPage": True, "endCursor": "repeat"}
        if count > 1 and failure == "changed":
            n["body"] = "changed during pagination"
        return json.dumps({"data": {"node": n}})
    monkeypatch.setattr(module, "gh_read", gh)
    with pytest.raises(ValueError):
        module.fetch_comments(REPO, 201, SHA)
    assert count <= 2
