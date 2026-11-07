import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("agent_merge", ROOT / "scripts/agent-merge.py")
am = importlib.util.module_from_spec(spec)
spec.loader.exec_module(am)

HEAD = "0f8658bd699ce4cbc995716564960689ad634121"
BOT = {"id": 41898282, "login": "github-actions[bot]", "type": "Bot"}


def codex(body, head=HEAD, user=BOT, cid=1, updated="2026-11-07T00:00:00Z"):
    return {"id": cid, "user": user, "updated_at": updated,
            "body": f"{am.CODEX_MARKER}\n### Codex PR review\n{body}\n<sub>head `{head[:10]}`</sub>"}


def test_codex_review_must_bind_this_head_and_come_from_the_actions_bot():
    old = codex("- [P1] stale", head="1111111111" + HEAD[10:])
    spoof = codex("- [P1] spoof", user={"id": 1, "login": "someone", "type": "User"})
    assert am.codex_review_at([old, spoof], HEAD) is None
    current = codex("clean", cid=2)
    assert am.codex_review_at([old, spoof, current], HEAD) is current


def test_newest_review_at_the_head_wins():
    first = codex("- [P1] old finding", cid=1, updated="2026-11-07T01:00:00Z")
    second = codex("No actionable defects.", cid=2, updated="2026-11-07T02:00:00Z")
    assert am.codex_review_at([first, second], HEAD) is second


def test_findings_are_extracted_once_each():
    body = ("Summary\n- [P2] Check the staff marker first\n  details\n"
            "- [P2] Check the staff marker first\n- [P1] Avoid cancelling a release")
    assert am.codex_findings(body) == ["[P2] Check the staff marker first", "[P1] Avoid cancelling a release"]
    assert am.codex_findings("No actionable regressions were identified.") == []


def test_latest_run_per_name_uses_the_rerun():
    runs = [{"id": 1, "name": "coverage / coverage", "status": "completed", "conclusion": "failure"},
            {"id": 9, "name": "coverage / coverage", "status": "completed", "conclusion": "success"}]
    assert am.latest_runs(runs)["coverage / coverage"]["conclusion"] == "success"


def test_required_problems_cover_missing_pending_red_and_statuses():
    runs = am.latest_runs([
        {"id": 1, "name": "lint", "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "test", "status": "in_progress", "conclusion": None},
        {"id": 3, "name": "audit", "status": "completed", "conclusion": "failure"},
        {"id": 4, "name": "build", "status": "completed", "conclusion": "skipped"},
    ])
    problems = am.required_problems({"lint", "test", "audit", "build", "Vercel", "ioc-scan"},
                                    runs, {"Vercel": "pending"})
    assert problems == ["required status 'Vercel' is pending",
                        "required check 'audit' concluded failure",
                        "required check 'ioc-scan' has not reported on this head",
                        "required check 'test' is in_progress"]


@pytest.mark.parametrize("path,pattern,hit", [
    (".github/workflows/deploy-prod.yml", ".github/workflows/**", True),
    ("alembic/versions/20261103_x.py", "alembic/versions/*.py", True),
    # fnmatch, as the pack's check-guarded-paths uses: * crosses directories.
    ("alembic/versions/sub/x.py", "alembic/versions/*.py", True),
    ("app/core/secrets.py", "app/core/secrets.py", True),
    ("docs/x.md", ".github/workflows/**", False),
])
def test_guard_globs(path, pattern, hit):
    assert am.glob_match(path, pattern) is hit


def test_tripped_guards_ignore_warn_mode_and_report_files():
    policy = {"guards": [
        {"id": "deploy-workflows", "paths": [".github/workflows/**"], "mode": "block"},
        {"id": "deps", "paths": ["package.json"], "mode": "warn"},
    ]}
    files = [".github/workflows/codex-pr-review.yml", "package.json", "app/page.tsx"]
    assert am.tripped_guards(policy, files) == {"deploy-workflows": [".github/workflows/codex-pr-review.yml"]}


def test_acknowledgement_by_label_or_trailer_block_only():
    ack = am.acknowledged_guards(
        ["guard-ok:deploy-workflows", "other"],
        ["fix\n\nGuarded-Path: migrations\nClaude-Session: x",
         "prose\n\nGuarded-Path: db-secrets-core is mentioned here\nbut not as a trailer",
         "Guarded-Path: deps"])
    assert ack == {"deploy-workflows", "migrations"}


class FakeGitHub:
    """Serves canned API responses by path prefix."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, path):
        self.calls.append(("GET", path))
        for prefix, value in self.routes.items():
            if path.startswith(prefix):
                return value
        return None

    def paged(self, path, key=None):
        data = self.get(path)
        return (data or {}).get(key, []) if key else (data or [])

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        return {"merged": True, "sha": "merge"}


def pr(state="clean", labels=(), base="main"):
    return {"number": 7, "state": "open", "merged": False, "draft": False, "mergeable_state": state,
            "head": {"sha": HEAD, "ref": "staging"}, "base": {"ref": base},
            "labels": [{"name": n} for n in labels]}


def routes(pr_obj, comments=(), checks=None, files=(), commits=(), threads=()):
    import base64
    guards = {"guards": [{"id": "deploy-workflows", "paths": [".github/workflows/**"], "mode": "block"}]}
    checks = checks if checks is not None else [
        {"id": 1, "name": "lint", "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "test", "status": "completed", "conclusion": "success"}]
    return {
        "/repos/o/r/pulls/7/ccr/review_threads": list(threads),
        "/repos/o/r/pulls/7/files": [{"filename": f} for f in files],
        "/repos/o/r/pulls/7/commits": [{"commit": {"message": m}} for m in commits],
        "/repos/o/r/pulls/7": pr_obj,
        f"/repos/o/r/commits/{HEAD}/check-runs": {"check_runs": checks},
        f"/repos/o/r/commits/{HEAD}/status": {"statuses": []},
        "/repos/o/r/rules/branches/": [{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "lint"}, {"context": "test"}]}}],
        "/repos/o/r/branches/": {"contexts": []},
        "/repos/o/r/issues/7/comments": list(comments),
        "/repos/o/r/contents/.ai/guards.json": {"content": base64.b64encode(json.dumps(guards).encode()).decode()},
    }


def test_clean_pr_with_clean_review_passes():
    gh = FakeGitHub(routes(pr(), comments=[codex("No actionable regressions were identified.")]))
    _, refusals, _ = am.evaluate(gh, "o/r", 7, HEAD[:10], None, False)
    assert refusals == []


def test_blocked_state_and_p2_without_acknowledgement_are_refused():
    gh = FakeGitHub(routes(pr(state="blocked"), comments=[codex("- [P2] Check the staff marker first")]))
    _, refusals, notes = am.evaluate(gh, "o/r", 7, None, None, False)
    assert "mergeable_state is 'blocked'" in refusals
    assert any("--accept-findings" in r for r in refusals)
    assert any("Check the staff marker first" in n for n in notes)


def test_p1_is_refused_even_when_findings_are_accepted():
    gh = FakeGitHub(routes(pr(), comments=[codex("- [P1] Avoid cancelling a release")]))
    _, refusals, _ = am.evaluate(gh, "o/r", 7, None, "looked at it", False)
    assert "Codex advisory review has P0/P1 findings at this head" in refusals


def test_unacknowledged_guard_into_main_is_refused_and_label_clears_it():
    files = [".github/workflows/codex-pr-review.yml"]
    gh = FakeGitHub(routes(pr(), files=files))
    _, refusals, _ = am.evaluate(gh, "o/r", 7, None, None, False)
    assert any("guard-ok:deploy-workflows" in r for r in refusals)
    gh = FakeGitHub(routes(pr(labels=["guard-ok:deploy-workflows"]), files=files))
    _, refusals, _ = am.evaluate(gh, "o/r", 7, None, None, False)
    assert refusals == []


def test_guard_on_staging_is_reported_but_not_refused():
    gh = FakeGitHub(routes(pr(base="staging"), files=[".github/workflows/x.yml"]))
    _, refusals, notes = am.evaluate(gh, "o/r", 7, None, None, False)
    assert refusals == [] and any("NOT acknowledged" in n for n in notes)


def test_moved_head_unresolved_threads_and_running_checks():
    with pytest.raises(am.Refused):
        am.evaluate(FakeGitHub(routes(pr())), "o/r", 7, "deadbeef", None, False)
    checks = [{"id": 1, "name": "lint", "status": "completed", "conclusion": "success"},
              {"id": 2, "name": "test", "status": "completed", "conclusion": "success"},
              {"id": 3, "name": "Codex review (subscription)", "status": "in_progress", "conclusion": None}]
    gh = FakeGitHub(routes(pr(), checks=checks, threads=[{"resolved": False}, {"resolved": True}]))
    _, refusals, _ = am.evaluate(gh, "o/r", 7, None, None, False)
    assert "1 unresolved review thread(s)" in refusals
    assert "Codex advisory review for this head is still running" in refusals
    assert any(r.startswith("checks still running") for r in refusals)


def test_thread_resolution_reads_the_rest_and_graphql_shapes():
    # Shape served by the REST review-thread route used in Claude Code web sessions.
    assert am.thread_resolved({"resolved": True, "outdated": False, "path": "x", "comment_ids": [1]})
    assert not am.thread_resolved({"resolved": False})
    assert am.thread_resolved({"isResolved": True})
    assert not am.thread_resolved({"path": "unknown shape"})


def worker_review(body, head=HEAD, state="completed", cid=5):
    return {"id": cid, "user": BOT, "updated_at": "2026-11-07T03:00:00Z",
            "body": f"{am.CODEX_MARKER}\n### Codex code review\n\nRevision `{head}`\n\n"
                    f"Execution: **{state}** (reason).\n\n```text\n{body}\n```\n"}


def test_worker_revision_format_is_recognised_and_enforced():
    review = worker_review("- [P1] Avoid cancelling a release")
    assert am.codex_review_at([review], HEAD) is review
    gh = FakeGitHub(routes(pr(), comments=[review]))
    _, refusals, _ = am.evaluate(gh, "o/r", 7, None, None, False)
    assert "Codex advisory review has P0/P1 findings at this head" in refusals


def test_incomplete_worker_review_is_reported_not_enforced():
    gh = FakeGitHub(routes(pr(), comments=[worker_review("", state="failed")]))
    _, refusals, notes = am.evaluate(gh, "o/r", 7, None, None, False)
    assert refusals == [] and any("did not complete (failed)" in n for n in notes)


def test_trailers_parse_like_check_guarded_paths():
    ack = am.acknowledged_guards([], ["fix\n\nguarded-path: deploy-workflows, migrations\nX: y"])
    assert ack == {"deploy-workflows", "migrations"}


def test_head_is_required_to_merge_but_not_to_check(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert am.main(["o/r", "7"]) == 2          # merge without --head: refused before any call
    assert am.main(["o/r", "7", "--check"]) == 2  # passes the --head rule, stops at the missing token


class GraphQLOnly:
    """REST thread route absent; GraphQL answers in two pages, or with a null PR."""

    def __init__(self, pages):
        self.pages = list(pages)

    def get(self, path):
        return None

    def request(self, method, path, body=None):
        return self.pages.pop(0)


def test_graphql_threads_are_paginated_and_null_safe():
    page1 = {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [{"isResolved": True}]}}}}}
    page2 = {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False}, "nodes": [{"isResolved": False}, {"isResolved": True}]}}}}}
    assert am.unresolved_threads(GraphQLOnly([page1, page2]), "o/r", 7) == 1
    with pytest.raises(RuntimeError):
        am.unresolved_threads(GraphQLOnly([{"data": {"repository": {"pullRequest": None}}}]), "o/r", 7)
